# 当前端用户进行生成时触发的流程
# 在意图识别时redis.add_message(task_id, "intent", 0, tojson) 将task_id传给 前端 如果 前端对意图识别表示确认 则将对应的task_id从前端传回后端
# 后端从redis中去拿task_id相关信息 取出"intent"中的"location" 和 "expand_query" 信息作为分析帖子的输入
from functions.analyzing_notes import analyze_notes
from utils.redis_storage import RedisMemory


def summary_from_notes(task_id, redis: RedisMemory):
    # 从redis中获取内容
    task_context = redis.get_messages_by_task(task_id)
    # 从context中获取 location 和 expand_query
    location = task_context["intent"]["location"]
    expand_query = task_context["expand_query"]

    analyze_num = len(location)
    for i in range(analyze_num):
        # 先不处理图像相关内容 f_post_res 为一个地点下的总结攻略
        f_post_res, _ = analyze_notes(expand_query[i], location[0], 15, redis)
        # 要去 f_post_res 中找所有 spots下的name字段 从而获取所有的旅游地址 => 查询所有的经纬度
        # 尝试将这部分 作为 function_calling 进行agent处理



    return