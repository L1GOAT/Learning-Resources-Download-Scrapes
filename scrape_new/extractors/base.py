"""
提取器基类

定义所有提取器必须实现的接口。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..models import ExtractContext, ExtractResult


class BaseExtractor(ABC):
    """
    提取器基类

    所有资源类型提取器都必须继承此类并实现 extract 方法。

    Attributes:
        intent: 意图标识（如 "video", "image" 等）
        keywords: 关键词列表，用于意图匹配
    """

    intent: str = ""
    keywords: list[str] = []

    def supports_url(self, url: str) -> bool:
        """
        判断是否支持该 URL

        Args:
            url: 目标 URL

        Returns:
            是否支持
        """
        return False

    @abstractmethod
    def extract(self, ctx: ExtractContext) -> ExtractResult:
        """
        提取下载项

        Args:
            ctx: 提取上下文

        Returns:
            提取结果
        """
        pass