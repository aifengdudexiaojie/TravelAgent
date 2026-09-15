# 当前端用户进行生成时触发的流程
# 在意图识别时redis.add_message(task_id, "intent", 0, tojson) 将task_id传给 前端 如果 前端对意图识别表示确认 则将对应的task_id从前端传回后端
# 后端从redis中去拿task_id相关信息 取出"intent"中的"location" 和 "expand_query" 信息作为分析帖子的输入
import asyncio
import json

from agents.tips_agent import GeneralAgent
from functions.analyzing_notes import analyze_notes
from utils.redis_storage import RedisMemory
from utils.flow_tracer import trace, async_timeout
from utils.toJson import to_json


# 【修改②】改为 async generator：逐步 yield 进度事件，最后 yield 最终总结
async def summary_from_notes(task_id, redis: RedisMemory):
    print(f"进入 总结方法part ---------")
    # 从redis中获取内容
    task_context = redis.get_messages_by_task(task_id)

    # 从context中获取 location 和 expand_query
    # 【修复】task_context 的 key 是 "intent:0"（因为 redis.add_message(task_id, "intent", 0, ...)），
    # 不是 "intent"，需遍历查找以 intent 开头的 key
    intent_data = None
    for k, v in task_context.items():
        if k.startswith("intent"):
            intent_data = v
            break
    if intent_data is None:
        intent_data = task_context

    # 容错处理：兼容 intent 包裹 / 扁平、location / locations 字段
    intent = intent_data.get("intent", intent_data)
    location = intent.get("location") or intent.get("locations") or []
    expand_query = intent_data.get("expand_query") or intent.get("expand_query") or []
    if isinstance(location, str):
        location = [location]
    print(f"得到 intent 内容 ---------")
    yield {"type": "start", "data": {"task_id": task_id}}

    analyze_num = len(location)
    all_summary_list = []
    found_any = False
    print(f"检测到{analyze_num}个地址")
    for i in range(analyze_num):
        loc = location[i]
        # 【修改②】地点开始进度
        yield {"type": "address_start", "data": {"location": loc, "index": i + 1, "total": analyze_num}}

        # 【修改②】原为 f_post_res, _ = analyze_notes(...)
        # 现在消费生成器：透传 post_start 进度，收集 address_result 结果
        # 【修改②】修正原 bug：location[0] → location[i]（原代码所有地点都用了第一个地点）
        f_post_res = ""
        img_notes = []
        query = expand_query[i] if i < len(expand_query) else f"{loc}旅游攻略"
        print(f" 当前查询内容为：{query} ---------")
        # 【修复】单个地点失败不中断整体流程
        try:
            async for event in analyze_notes(query, loc, 15, redis, task_id):
                if event["type"] == "address_result":
                    f_post_res = event["data"]["f_post_res"]
                    img_notes = event["data"]["img_notes"]
                    if event["data"].get("status") == "ok":
                        found_any = True
                    trace(f"【边界】收到 {loc} 汇总结果")
                else:
                    # post_start 等进度事件透传给前端
                    yield event
        except asyncio.TimeoutError:
            print(f"⚠️ 地点分析超时: {loc}", flush=True)
            f_post_res = f"{{'location': '{loc}', 'error': '分析超时'}}"
        except Exception as e:
            print(f"⚠️ 地点分析失败: {loc} - {e}", flush=True)
            f_post_res = f"{{'location': '{loc}', 'error': '{e}'}}"

        if f_post_res:
            all_summary_list.append(f_post_res)

        # 【修改②】地点完成进度
        yield {"type": "address_done", "data": {"location": loc, "index": i + 1, "total": analyze_num}}
        trace(f"【边界】已发送 {loc} address_done")
    if not found_any:
        yield {
            "type": "error",
            "data": {"message": "未搜索到有效的小红书帖子，请检查小红书 MCP 服务或稍后重试"},
        }
        return
    print("完成所有地点分析----- 进行最后总结")
    trace("【边界】进入最终总结 (LLM, 300s 超时)")
    all_summary_list.append(task_context)
    # 根据所有的帖子总结进行最后的内容生成
    final_summary_agent = GeneralAgent("deepseek", "travel-summarizer.md")
    # 【修复】content 必须是字符串，不能直接传 list（OpenAI 会把 list 当数组，要求每项有 type 字段）
    f_summary_msg = [{"role": "user", "content": json.dumps(all_summary_list, ensure_ascii=False)}]

    # 【修复】最终总结必须加超时：openai SDK 默认 timeout=600s ×2 重试，
    # 不加保护会静默挂起最多 30 分钟（且发生在所有地点分析完成后，最容易被误判为"卡住"）
    try:
        final_summary_res = await async_timeout(final_summary_agent.chat(f_summary_msg), 300)
        print(final_summary_res)
        trace("【边界】最终总结完成")
    except asyncio.TimeoutError:
        print(f"⚠️ 最终总结生成超时(300s)", flush=True)
        final_summary_res = json.dumps({"error": "最终总结超时", "locations_summary": all_summary_list}, ensure_ascii=False)
    except Exception as e:
        print(f"⚠️ 最终总结生成失败: {e}", flush=True)
        final_summary_res = json.dumps({"error": str(e), "locations_summary": all_summary_list}, ensure_ascii=False)

    # 【修复】把 LLM 原始文本解析成 JSON 对象再传给前端。
    # 若直接传字符串，前端 FinalSummary 按对象取 summary.daily_plan 等字段全为 undefined，渲染为空。
    # to_json() 会剥离 ```json ``` 代码块标记并修复截断/语法错误
    try:
        final_summary_obj = to_json(final_summary_res)
        if not isinstance(final_summary_obj, dict):
            final_summary_obj = {"raw": final_summary_obj}
    except Exception as e:
        print(f"⚠️ 最终总结 JSON 解析失败，按原文回传: {e}", flush=True)
        final_summary_obj = {"raw": final_summary_res, "error": str(e)}

    # 【修改②】末尾 yield 最终总结（原为 return）
    yield {"type": "done", "data": final_summary_obj}
