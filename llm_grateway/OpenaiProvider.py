"""
OpenAI API 提供商实现
封装 OpenAI Python SDK 的调用
"""
import asyncio
import json
from typing import AsyncIterator
import httpx
from openai import AsyncOpenAI

from utils.function_calling import TOOL_REGISTRY


class OpenaiProvider:
    """OpenAI API 调用封装"""

    def __init__(self, api_key: str, model: str, base_url: str | None = None):
        """
        Args:
            api_key: API 密钥
            base_url: 自定义 API 地址（用于 DeepSeek / Kimi 等兼容接口）
        """
        self.model = model
        kwargs = {"api_key": api_key}
        if base_url:
            kwargs["base_url"] = base_url
        self.client = AsyncOpenAI(
            http_client=httpx.AsyncClient(
                headers={"Accept-Encoding": "gzip, deflate"}  # 禁用 brotli
            ),
            **kwargs,
        )


    # async def chat(self, system_prompt: str, messages: list[dict]) -> str:
    #     """非流式对话"""
    #     formatted = self._build_messages(system_prompt, messages)
    #     response = await self.client.chat.completions.create(
    #         model=self.model,
    #         messages=formatted,
    #         # max_tokens=4096,
    #     )
    #     return response.choices[0].message.content or ""

    async def chat(self, system_prompt, messages, tools=None):
        formatted = self._build_messages(system_prompt, messages)
        # 【修改】工具调用预算：一次 agent 分析帖子不再固定只有 3~5 次调用。
        # 按"每个景点/美食/项目/住宿约 1~2 次 get_location（含失败重试）"设计：
        #   - max_rounds：LLM 请求轮数上限（一次响应内可并行多个 tool_calls）
        #   - max_tool_calls：单次对话工具调用总次数上限（防死循环兜底）
        max_rounds = 25
        max_tool_calls = 40

        total_tool_calls = 0
        for _ in range(max_rounds):
            resp = await self.client.chat.completions.create(
                model=self.model, messages=formatted, tools=tools, tool_choice="auto")
            msg = resp.choices[0].message

            if not msg.tool_calls:
                return msg.content or ""  # 模型不再调工具 → 返回

            formatted.append(msg)  # 记录 tool_calls 消息
            for tc in msg.tool_calls:
                total_tool_calls += 1
                print(f"调用函数名：{tc.function.name}  调用函数参数：{tc.function.arguments}")

                # 【修改】参数解析容错：模型偶发输出非法 JSON 参数时按空参数执行，而不是抛错
                try:
                    args = json.loads(tc.function.arguments)
                except json.JSONDecodeError:
                    args = {}

                # 【修改】工具执行放入线程：get_location 是同步 httpx 调用，
                # 直接跑在事件循环上会阻塞 SSE 流。调用次数放大后更要避免。
                result = await asyncio.to_thread(self._execute_tool, tc.function.name, args)
                print(result)
                formatted.append({"role": "tool", "tool_call_id": tc.id,
                                  "content": json.dumps(result, ensure_ascii=False)})

            if total_tool_calls >= max_tool_calls:
                # 达到工具调用次数上限，提前终止。返回可解析的 JSON 而非硬编码中文哨兵
                return json.dumps({
                    "error": "工具调用次数超限",
                    "message": f"本次对话工具调用达到上限 {max_tool_calls} 次",
                }, ensure_ascii=False)

        # 达到最大轮数：返回可解析的 JSON，避免下游 to_json() 抛错导致整帖被丢弃
        return json.dumps({
            "error": "达到最大工具调用轮次",
            "message": f"LLM 请求轮数达到上限 {max_rounds}",
        }, ensure_ascii=False)

    def _execute_tool(self, name, args):
        func = TOOL_REGISTRY.get(name)
        try:
            return func(**args)
        except Exception as e:
            return {"error": str(e)}  # 错误作为 tool 结果回传，不中断




    async def chat_stream(self, system_prompt: str, messages: list[dict]) -> AsyncIterator[str]:
        """流式对话

        使用 `__REASONING_END__` 标记分隔思考和正式回答阶段。
        """
        formatted = self._build_messages(system_prompt, messages)
        stream = await self.client.chat.completions.create(
            model=self.model,
            messages=formatted,
            max_tokens=4096,
            stream=True,
        )

        async for chunk in stream:
            if chunk.choices[0].delta.content:
                yield chunk.choices[0].delta.content

    def _build_messages(self, system_prompt: str, messages: list[dict]) -> list[dict]:
        """组装消息列表"""
        result = []
        if system_prompt:
            result.append({"role": "system", "content": system_prompt})
        result.extend(messages)
        return result