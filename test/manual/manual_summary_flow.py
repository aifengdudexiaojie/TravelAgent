from utils.redis_storage import RedisMemory
import asyncio
from functions.get_intent import get_user_intent
from controller.chat_routes import _sse
from functions.summary_from_notes import summary_from_notes


async def test_summary():
    task_id, _ = await get_user_intent("规划一份10.3-10.5的香港旅游计划，购物为主，不要太累，预算充足。", RedisMemory())
    async for event in summary_from_notes(task_id, RedisMemory()):
        print(event["type"], event["data"])


asyncio.run(test_summary())