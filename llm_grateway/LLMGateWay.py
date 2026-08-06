"""
LLM 统一网关
根据配置选择不同提供商（OpenAI / Anthropic），
屏蔽 API 差异，提供统一的 chat 接口
"""
from typing import AsyncIterator

from config.llmConfig import get_settings

class LLMGateway:
    """
    LLM 调用网关
    根据 provider 自动选择对应的 API 实现

    用法:
        gateway = LLMGateway(provider="openai", model="gpt-4o-mini")
        response = await gateway.chat([{"role": "user", "content": "你好"}])
    """

    def __init__(self, model: str, system_prompt: str = ""):
        """
        初始化网关

        Args:
            model: 模型名称 仅仅填写 deepseek or kimi
            system_prompt: 系统提示词
        """
        self.model = model
        self.system_prompt = system_prompt
        self.settings = get_settings()

    def _get_provider(self):
        curr_provider = ""
        curr_model = ""
        curr_api_key = ""
        curr_base_url = ""

        if self.model == "deepseek":
            curr_provider = "openai"
            curr_model = getattr(self.settings, "DEEPSEEK_MODEL", "")
            curr_api_key = getattr(self.settings, "DEEPSEEK_API_KEY", "")
            curr_base_url = getattr(self.settings, "DEEPSEEK_BASE_URL", "")

        if self.model == "kimi":
            curr_provider = "openai"
            curr_model = getattr(self.settings, "KIMI_MODEL", "")
            curr_api_key = getattr(self.settings, "KIMI_API_KEY", "")
            curr_base_url = getattr(self.settings, "KIMI_BASE_URL", "")

        if curr_provider == "openai":
            from .OpenaiProvider import OpenaiProvider
            return OpenaiProvider(curr_api_key, curr_model, curr_base_url)
        return "当前LLM模型供应商非openai"

    # async def chat(self, messages: list[dict]) -> str:
    #     """普通对话（非流式）"""
    #     provider = self._get_provider()
    #     return await provider.chat(self.system_prompt, messages)

    async def chat(self, messages, tools=None):
        provider = self._get_provider()
        return await provider.chat(self.system_prompt, messages, tools)

    async def chat_stream(self, messages: list[dict]) -> AsyncIterator[str]:
        """流式对话"""
        provider = self._get_provider()
        async for chunk in provider.chat_stream(self.system_prompt, messages):
            yield chunk
