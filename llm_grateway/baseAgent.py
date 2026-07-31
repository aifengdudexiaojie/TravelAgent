"""
Agent 基类
定义所有 Agent 的通用结构和接口
"""

from abc import ABC, abstractmethod
from .LLMGateWay import LLMGateway


class BaseAgent(ABC):
    """
    Agent 抽象基类

    所有 Agent 共享：
    - LLM 网关实例
    - 配置访问
    - 统一的运行接口

    子类需实现 run() 方法
    """

    def __init__(self, model: str, system_prompt: str = ""):
        """
        Args:
            provider: LLM 提供商
            model: 模型名称 deepseek or kimi
            system_prompt: 系统提示词
        """
        self.llm = LLMGateway(
            model=model,
            system_prompt=system_prompt,
        )

    @abstractmethod
    async def run(self, *args, **kwargs):
        """
        执行 Agent 的核心逻辑
        每个 Agent 子类必须实现此方法
        """
        pass
