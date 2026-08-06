# 当前端用户进行生成时触发的流程
# 在意图识别时redis.add_message(task_id, "intent", 0, tojson) 将task_id传给 前端 如果 前端对意图识别表示确认 则将对应的task_id从前端传回后端
# 后端从redis中去拿task_id相关信息 取出"intent"中的"location" 和 "expand_query" 信息作为分析帖子的输入
import json

from agents.tips_agent import GeneralAgent
from functions.analyzing_notes import analyze_notes
from utils.redis_storage import RedisMemory


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
        async for event in analyze_notes(query, loc, 15, redis, task_id):
            if event["type"] == "address_result":
                f_post_res = event["data"]["f_post_res"]
                img_notes = event["data"]["img_notes"]
            else:
                # post_start 等进度事件透传给前端
                yield event

        all_summary_list.append(f_post_res)

        # 【修改②】地点完成进度
        yield {"type": "address_done", "data": {"location": loc, "index": i + 1, "total": analyze_num}}

    all_summary_list.append(task_context)
    # 根据所有的帖子总结进行最后的内容生成
    final_summary_agent = GeneralAgent("deepseek", "travel-summarizer.md")
    # 【修复】content 必须是字符串，不能直接传 list（OpenAI 会把 list 当数组，要求每项有 type 字段）
    f_summary_msg = [{"role": "user", "content": json.dumps(all_summary_list, ensure_ascii=False)}]

    final_summary_res = await final_summary_agent.chat(f_summary_msg)
    # 【修改②】末尾 yield 最终总结（原为 return）
    yield {"type": "done", "data": final_summary_res}
