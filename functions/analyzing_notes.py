import asyncio

from agents.tips_agent import GeneralAgent
from functions.xhs_funcs import show_status, search_notes, get_note_detail
from utils.function_calling import make_tool
from utils.flow_tracer import trace, StepTimer, async_timeout
from utils.redis_storage import RedisMemory
from utils.toJson import to_json


# 用于分析单个地点的旅游攻略
# 【修改①】新增 task_id 参数：由 summary_from_notes 传入，保证与意图识别同任务号
# 【修改①】改为 async generator：帖子循环内 yield 进度事件，末尾 yield 结果
async def analyze_notes(query: str, location: str, limit: int, redis: RedisMemory, task_id: int,
                       mcp_url: str | None = None):
    """分析某个地点的笔记；mcp_url 指定该用户自己的小红书 MCP 实例。"""
    timer = StepTimer(f"📍 开始分析地点: {location}")
    # 【修复】同步 MCP 调用用 to_thread 包裹，避免阻塞事件循环（否则 SSE 流卡住）
    # 首先检查登录状态
    try:
        ifLogin = await async_timeout(
            asyncio.to_thread(show_status, mcp_url), 30)
    except asyncio.TimeoutError:
        trace("⚠️ 登录检测超时(30s)")
        ifLogin = True

    if not ifLogin:
        yield {"type": "address_result", "data": {"f_post_res": "", "img_notes": [], "status": "not_logged_in"}}
        return
    timer.mark("登录检测")

    # 根据query进行mcp查询所有帖子
    try:
        feeds = await async_timeout(
                asyncio.to_thread(search_notes, query, 5, mcp_url), 100)
    except asyncio.TimeoutError:
        trace("⚠️ 搜索帖子超时(100s)，跳过该地点")
        yield {"type": "address_result", "data": {"f_post_res": "", "img_notes": [], "status": "timeout"}}
        return
    except Exception as e:
        trace(f"⚠️ 搜索帖子失败: {e}")
        yield {"type": "address_result", "data": {"f_post_res": "", "img_notes": [], "status": "search_error"}}
        return
    timer.mark("搜索帖子")

    all_notes_list = []
    img_notes = []
    total = len(feeds)
    trace(f" 一共搜索到：{total} 条帖子")
    if total == 0:
        trace("⚠️ 未搜索到帖子，跳过该地点")
        yield {"type": "address_result", "data": {"f_post_res": "", "img_notes": [], "status": "no_posts"}}
        return
    # 根据feeds中的内容做循环处理
    for i, f in enumerate(feeds):
        # 【修改①】每篇帖子开始时推送进度事件
        trace(f" 当前正在分析第 {i+1}/{total} 条帖子")
        title_hint = f.get("noteCard", {}).get("displayTitle", "") or f.get("id", "")
        yield {"type": "post_start", "data": {"title": title_hint, "index": i + 1, "total": total}}

        try:
            detail = await async_timeout(
                asyncio.to_thread(get_note_detail, f["id"], f["xsecToken"], False, mcp_url),
                45,
            )
        except asyncio.TimeoutError:
            trace(f"⚠️ 帖子详情超时(45s)，跳过: {f['id']}")
            continue
        timer.mark(f"获取帖子详情 [{i+1}]")

        # 判断是否为图片攻略
        if isinstance(detail, dict):
            note = detail.get("data", {}).get("note", detail)
            desc = note.get("desc", "") or detail.get("desc", "") or ""
            title = note.get("title", "") or detail.get("title", "")
            # 首先判断是否为旅游相关帖子
            filterAgent = GeneralAgent("kimi","travel-post-filter")
            note_context = f" 'task_id':{task_id}, 'title':{title}, 'post_content':{desc}"
            note_msg = [{"role": "user", "content": note_context}]
            # 【修复】过滤 Agent 之前无超时，Kimi 思考慢时会无限挂起整条流水线
            judge = await async_timeout(filterAgent.chat(note_msg), 60)
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
            print(f" 准备加载工具 ---------")
            # 定义 get_location 工具（OpenAI 格式）
            get_location_tool = make_tool(
                name="get_location",
                description="获取景点/地点的经纬度坐标",
                parameters={
                    "type": "object",
                    "properties": {
                        "address": {"type": "string", "description": "景点名称或地址"},
                        "city": {"type": "string", "description": "所在城市"}
                    },
                    "required": ["address", "city"]
                },
            )

            summaryAgent = GeneralAgent("deepseek-free", "tipsAnalysis/travel-summary-extractor.md",tools=[get_location_tool])
            costAgent = GeneralAgent("deepseek-free", "tipsAnalysis/travel-cost-estimator.md")
            precautionsAgent = GeneralAgent("deepseek-free", "tipsAnalysis/travel-precautions-extractor.md")
            durationAgent = GeneralAgent("deepseek-free", "tipsAnalysis/travel-duration-estimator.md")

            sem = asyncio.Semaphore(3)
            trace("  开始调用 4 个 Agent API")
            async def run_agent(agent, msg):
                async with sem:
                    return to_json(await agent.chat(msg))

            # 【修复】单帖分析整体容错：失败/超时跳过该帖，不中断整个地点流程
            try:
                # 并发 4 个 Agent，整体 300s 超时（Kimi 思考久，120s 不够）
                summary_res, cost_res, precautions_res, duration_res = await async_timeout(
                    asyncio.gather(
                        run_agent(summaryAgent, note_msg),
                        run_agent(costAgent, note_msg),
                        run_agent(precautionsAgent, note_msg),
                        run_agent(durationAgent, note_msg),
                    ),
                    300,
                )
                timer.mark("4 Agent 并发分析")

                # ================================================================
                # 去重合并（等所有结果到齐后才执行）
                # ================================================================
                raw_inputs = {
                    "cost": cost_res,
                    "summary": summary_res,
                    "precautions": precautions_res,
                    "duration": duration_res,
                }
                raw_inputs = f" 'task_id':{task_id}, 'title':{title}, 'raw_inputs':{raw_inputs}"
                travel_summarizer_agent = GeneralAgent("deepseek", "tipsAnalysis/single-travel-summary.md")
                f_travel_msg = [{"role": "user", "content": raw_inputs}]
                single_travel_res = await async_timeout(travel_summarizer_agent.chat(f_travel_msg), 180)
                timer.mark("单帖总结")
                # 将总结内容存入到redis中
                redis.add_message(task_id=task_id, location=location, note_id=f["id"], content=single_travel_res)
                all_notes_list.append(single_travel_res)
            except asyncio.TimeoutError:
                trace(f"⚠️ 单帖分析超时，跳过: {title}")
                continue
            except Exception as e:
                trace(f"⚠️ 单帖分析失败，跳过: {title} - {e}")
                continue
        else:
            # 【修改①】原为 return "返回内容格式错误"（会中断整个生成器），改为跳过
            print(f"帖子详情格式错误，跳过: {f.get('id', '')}")
            continue

    # 对所有帖子进行总结
    # 【修复】地点汇总失败也 yield 兜底结果，不中断流程
    f_post_res = ""
    try:
        trace(f"  开始对 {location} 所有帖子做汇总")
        all_notes_input = f"'task_id':{task_id},'location':{location},'posts_summaries':{all_notes_list} "
        all_notes_agent = GeneralAgent("deepseek", "tipsAnalysis/travel-post-summary.md")
        f_notes_msg = [{"role": "user", "content": all_notes_input}]
        f_post_res = await async_timeout(all_notes_agent.chat(f_notes_msg), 180)
        timer.mark(f"地点汇总完成: {location}")
        timer.summary()
    except asyncio.TimeoutError:
        trace(f"⚠️ 地点汇总超时，返回空结果: {location}")
        f_post_res = f"{{'location': '{location}', 'posts_summaries': {all_notes_list}, 'summary': '汇总超时'}}"
    except Exception as e:
        trace(f"⚠️ 地点汇总失败: {location} - {e}")
        f_post_res = f"{{'location': '{location}', 'error': '{e}'}}"

    # 【修改①】末尾 yield 结果（f_post_res + img_notes），供 summary_from_notes 收集
    yield {"type": "address_result", "data": {"f_post_res": f_post_res, "img_notes": img_notes, "status": "ok"}}
