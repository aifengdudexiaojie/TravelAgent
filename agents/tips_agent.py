"""
循环分析帖子时的 Agent
加载不同 skills 从而实现不同的功能

"""

from typing import AsyncIterator

from llm_grateway.baseAgent import BaseAgent
from utils.skill_loader import load_skill


class GeneralAgent(BaseAgent):

    def __init__(self, model_name: str, role: str):
        """
        Args:
            model_name: 使用的api接口厂商 dee/kimi
            role: 需要加载的skill文件名（如 "Intent"、"travel-post-filter"）
                  会自动在 skills/ 目录下查找对应的 .md 文件
        """
        self.model_name = model_name
        self.role = role
        super().__init__(
            model=self.model_name,
            system_prompt=load_skill(role),
        )

    async def chat(self, messages: list[dict]) -> str:
        """单轮对话"""
        return await self.llm.chat(messages)

    async def chat_stream(self, messages: list[dict]) -> AsyncIterator[str]:
        """流式对话"""
        async for chunk in self.llm.chat_stream(messages):
            yield chunk

    async def run(self, *args, **kwargs):
        """实现基类抽象方法"""
        return await self.chat(kwargs.get("messages", []))

