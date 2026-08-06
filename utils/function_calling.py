from utils.location import get_location

TOOL_REGISTRY = {
    "get_location": get_location,     # 以后可加 get_weather、get_distance...
}

def make_tool(name: str, description: str, parameters: dict):
    """将函数包装为 OpenAI 格式的 tool 定义"""
    return {
        "type": "function",
        "function": {"name": name, "description": description, "parameters": parameters},
    }