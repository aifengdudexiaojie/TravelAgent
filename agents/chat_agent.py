"""聊天 Agent - 基于 BaseAgent 封装流式/非流式聊天"""

from typing import AsyncIterator, List

from llm_grateway.baseAgent import BaseAgent


class ChatAgent(BaseAgent):

    def __init__(self, system_prompt: str = ""):
        super().__init__(model="deepseek-free", system_prompt=system_prompt)

    async def chat(self, messages: List[dict]) -> str:
        return await self.llm.chat(messages)

    async def chat_stream(self, messages: List[dict]) -> AsyncIterator[str]:
        async for chunk in self.llm.chat_stream(messages):
            yield chunk

    async def run(self, *args, **kwargs):
        return await self.chat(kwargs.get("messages", []))
