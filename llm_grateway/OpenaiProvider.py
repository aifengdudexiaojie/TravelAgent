"""
OpenAI API 提供商实现
封装 OpenAI Python SDK 的调用
"""

from typing import AsyncIterator
import httpx
from openai import AsyncOpenAI

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


    async def chat(self, system_prompt: str, messages: list[dict]) -> str:
        """非流式对话"""
        formatted = self._build_messages(system_prompt, messages)
        response = await self.client.chat.completions.create(
            model=self.model,
            messages=formatted,
            # max_tokens=4096,
        )
        return response.choices[0].message.content or ""

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