import asyncio
from agents.tips_agent import GeneralAgent
from utils.function_calling import make_tool

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

async def main():
    agent = GeneralAgent("deepseek", "travel-planner-test", tools=[get_location_tool])
    res = await agent.chat([{"role": "user", "content": "帮我总结一下宜兴有什么好玩的"}])
    print(res)

asyncio.run(main())
