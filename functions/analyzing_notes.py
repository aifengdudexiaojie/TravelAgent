import asyncio

from agents.tips_agent import GeneralAgent
from functions.xhs_funcs import show_status, search_notes, get_note_detail
from utils.redis_storage import RedisMemory
from utils.toJson import to_json
import random


async def analyze_notes(query: str, limit: int, redis: RedisMemory):
    # 首先检查登录状态
    ifLogin = show_status()
    if not ifLogin:
        return "未登录账号"
    # 生成随机数用于当前任务测试
    task_id = random.randint(0, 10000)

    # 根据query进行mcp查询所有帖子
    feeds =  search_notes(query, limit)

    travel_res_list = []
    img_notes = []
    # 根据feeds中的内容做循环处理
    for i, f in enumerate(feeds):
        detail = get_note_detail(
            note_id=f["id"],
            xsec_token=f["xsecToken"],
            load_comments=False,  # 不加载评论 容易超时
        )
        # 判断是否为图片攻略
        if isinstance(detail, dict):
            note = detail.get("data", {}).get("note", detail)
            desc = note.get("desc", "") or detail.get("desc", "") or ""
            title = note.get("title", "") or detail.get("title", "")
            # 首先判断是否为旅游相关帖子
            filterAgent = GeneralAgent("kimi","travel-post-filter")
            note_context = f" 'task_id':{task_id}, 'title':{title}, 'post_content':{desc}"
            note_msg = [{"role": "user", "content": note_context}]
            judge = await filterAgent.chat(note_msg)
            if not judge:
                # 认为该帖子非旅游相关
                print(f"当前帖子与旅游无关 title:{title}")
                continue
            if len(desc)<150:
                # 如果内容字数小于150则认为是图片攻略 将其存入list中 后续有图像识别较好模型再做修改
                img_notes.append({
                    "note_id": f["id"],
                    "xsec_token": f["xsecToken"],
                })
                continue

            # ================================================================
            # 多并发：4 个 Agent 同时跑（总结、花费、注意事项、时长、评论）
            # ================================================================
            summaryAgent = GeneralAgent("kimi", "tipsAnalysis/travel-post-summary.md")
            costAgent = GeneralAgent("kimi", "tipsAnalysis/travel-cost-estimator.md")
            precautionsAgent = GeneralAgent("kimi", "tipsAnalysis/travel-precautions-extractor.md")
            durationAgent = GeneralAgent("kimi", "tipsAnalysis/travel-duration-estimator.md")
            # commentAgent = GeneralAgent("kimi", "tipsAnalysis/travel-comment-extractor.md")

            # 评论上下文单独构造
            # comments_raw = detail.get("comments", {})
            # comments = comments_raw.get("list", []) if isinstance(comments_raw, dict) else []
            # c_context = f" 'task_id':{task_id}, 'title':{title}, 'post_content':{desc}, 'comments':{comments}"
            # c_msg = [{"role": "user", "content": c_context}]

            # 并发执行 5 个 Agent
            sem = asyncio.Semaphore(3)

            async def run_agent(agent, msg):
                async with sem:
                    return to_json(await agent.chat(msg))

            summary_res, cost_res, precautions_res, duration_res = await asyncio.gather(
                run_agent(summaryAgent, note_msg),
                run_agent(costAgent, note_msg),
                run_agent(precautionsAgent, note_msg),
                run_agent(durationAgent, note_msg),
                # run_agent(commentAgent, c_msg),
            )

            # ================================================================
            # 去重合并（等所有结果到齐后才执行）
            # ================================================================
            raw_inputs = {
                "cost": cost_res,
                "summary": summary_res,
                "precautions": precautions_res,
                "duration": duration_res,
            }
            # clean_context = f" 'task_id':{task_id}, 'title':{title}, 'raw_inputs':{raw_inputs}"
            # clean_msg = [{"role": "user", "content": clean_context}]
            # cleanAgent = GeneralAgent("deepseek", "tipsAnalysis/clean.md")
            # clean_res = await cleanAgent.chat(clean_msg)
            # 将每一轮的内容放到redis中
            travel_summarizer_agent = GeneralAgent("deepseek", "travel-summarizer.md")
            f_travel_msg = [{"role": "user", "content": raw_inputs}]
            f_travel_res = await travel_summarizer_agent.chat(f_travel_msg)
            travel_res_list.append(f_travel_res)

        else:
            return "返回内容格式错误"

    return None