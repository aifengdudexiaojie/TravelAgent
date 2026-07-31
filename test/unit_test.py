import asyncio
import json

from agents.tips_agent import GeneralAgent
from functions.xhs_funcs import show_status, search_notes, get_note_detail
from utils.redis_storage import RedisMemory

redis = RedisMemory()
from utils.toJson import to_json
import random


async def main():
    intentAgent = GeneralAgent("deepseek", "Intent")
    message = "帮我规划从明天开始的3天左右去大理的旅游计划，尽可能玩得项目多点，最好省点钱"

    messages = [{"role": "user", "content": message}]

    print("=== 正常输出 ===")
    res = await intentAgent.chat(messages)
    print(res)
    print("\n=== 完成 ===")
    parsed = to_json(res)
    query_list = parsed["expand_query"]
    print(query_list)
    # 测试中间过程
    task_id = random.randint(0, 10000)
    ifLogin = show_status()

    feeds = search_notes(query_list[0], 15)
    img_notes = []
    travel_res_list = []
    print("\n=== 搜索结束 开始批量分析 ===")
    for i, f in enumerate(feeds):
        detail = get_note_detail(
            note_id=f["id"],
            xsec_token=f["xsecToken"],
            load_comments=False,
        )
        if isinstance(detail, dict):
            note = detail.get("data", {}).get("note", detail)
            desc = note.get("desc", "") or detail.get("desc", "") or ""
            title = note.get("title", "") or detail.get("title", "")

            filterAgent = GeneralAgent("kimi", "travel-post-filter")
            note_context = f" 'task_id':{task_id}, 'title':{title}, 'post_content':{desc}"
            note_msg = [{"role": "user", "content": note_context}]
            judge = await filterAgent.chat(note_msg)
            if not judge:
                print(f"当前帖子与旅游无关 title:{title}")
                continue
            if len(desc) < 150:
                img_notes.append({
                    "note_id": f["id"],
                    "xsec_token": f["xsecToken"],
                })
                continue

            print("\n=== 开始分析当前帖子（5 个 Agent 并发）===")

            summaryAgent = GeneralAgent("deepseek", "tipsAnalysis/travel-post-summary.md")
            costAgent = GeneralAgent("deepseek", "tipsAnalysis/travel-cost-estimator.md")
            precautionsAgent = GeneralAgent("deepseek", "tipsAnalysis/travel-precautions-extractor.md")
            durationAgent = GeneralAgent("deepseek", "tipsAnalysis/travel-duration-estimator.md")
            # commentAgent = GeneralAgent("deepseek", "tipsAnalysis/travel-comment-estimator.md")

            # comments_raw = detail.get("comments", {})
            # comments = comments_raw.get("list", []) if isinstance(comments_raw, dict) else []
            # c_context = f" 'task_id':{task_id}, 'title':{title}, 'post_content':{desc}, 'comments':{comments}"
            # c_msg = [{"role": "user", "content": c_context}]

            sem = asyncio.Semaphore(3)

            async def run_agent(agent, msg):
                async with sem:
                    return to_json(await agent.chat(msg))

            summary_res, cost_res, precautions_res, duration_res = await asyncio.gather(
                run_agent(summaryAgent, note_msg),
                run_agent(costAgent, note_msg),
                run_agent(precautionsAgent, note_msg),
                run_agent(durationAgent, note_msg),
            )

            print(f"\n=== 帖子总结 ==={summary_res}")
            print(f"\n=== 花费总结 ==={cost_res}")
            print(f"\n=== 注意事项 ==={precautions_res}")
            print(f"\n=== 时长判断 ==={duration_res}")
            # print(f"\n=== 评论总结 ==={comments_res}")

            raw_inputs = {
                "cost": cost_res,
                "summary": summary_res,
                "precautions": precautions_res,
                "duration": duration_res,
                # "comments": comments_res
            }
            clean_context = f" 'task_id':{task_id}, 'title':{title}, 'raw_inputs':{raw_inputs}"
            clean_msg = [{"role": "user", "content": clean_context}]
            cleanAgent = GeneralAgent("deepseek", "tipsAnalysis/clean.md")
            clean_res = await cleanAgent.chat(clean_msg)
            redis.add_message(task_id=task_id, note_id=f["id"], content=clean_res)

            travel_summarizer_agent = GeneralAgent("deepseek", "travel-summarizer.md")
            f_travel_msg = [{"role": "user", "content": clean_res}]
            f_travel_res = await travel_summarizer_agent.chat(f_travel_msg)
            print(f_travel_res)
            travel_res_list.append(f_travel_res)


if __name__ == "__main__":
    asyncio.run(main())
