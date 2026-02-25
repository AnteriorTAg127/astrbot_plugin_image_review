"""
内容审核基类模块
定义审核服务的通用接口和异常类
"""

from abc import ABC, abstractmethod
from enum import Enum
from typing import Any, Optional, Set, Tuple


class RiskLevel(Enum):
    """风险等级枚举"""
    Pass = 0
    Review = 1
    Block = 2


class CensorError(Exception):
    """内容审核异常"""

    def __init__(self, message: str):
        self.message = message
        super().__init__(self.message)


class CensorBase(ABC):
    """内容审核基类"""

    def __init__(self, config: dict[str, Any]):
        """
        初始化审核器

        Args:
            config: 配置字典
        """
        self._config = config

    @abstractmethod
    async def detect_text(self, text: str) -> Tuple[RiskLevel, Set[str]]:
        """
        检测文本内容

        Args:
            text: 待检测文本

        Returns:
            (风险等级, 风险词集合)
        """
        pass

    @abstractmethod
    async def detect_image(self, image: str) -> Tuple[RiskLevel, Set[str]]:
        """
        检测图片内容

        Args:
            image: 图片URL或base64字符串

        Returns:
            (风险等级, 风险描述集合)
        """
        pass

    def _split_text(self, text: str, max_length: int = 600) -> list[str]:
        """
        将长文本分割成多个小段

        Args:
            text: 原始文本
            max_length: 每段最大长度

        Returns:
            文本段列表
        """
        if len(text) <= max_length:
            return [text]

        chunks = []
        for i in range(0, len(text), max_length):
            chunks.append(text[i:i + max_length])
        return chunks

    async def __aenter__(self):
        """异步上下文管理器入口"""
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """异步上下文管理器出口"""
        await self.close()

    @abstractmethod
    async def close(self):
        """关闭资源"""
        pass
