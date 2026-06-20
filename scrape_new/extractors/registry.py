"""
提取器注册表

负责注册、管理和选择提取器。
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .base import BaseExtractor

logger = logging.getLogger(__name__)


class ExtractorRegistry:
    """
    提取器注册表

    管理所有已注册的提取器，根据意图和 URL 选择合适的提取器。
    """

    def __init__(self) -> None:
        self._extractors: dict[str, BaseExtractor] = {}
        self._keyword_map: dict[str, str] = {}  # keyword -> intent

    def register(self, extractor: BaseExtractor) -> None:
        """
        注册提取器

        Args:
            extractor: 提取器实例
        """
        intent = extractor.intent
        if not intent:
            raise ValueError(f"提取器 {extractor.__class__.__name__} 未设置 intent")

        self._extractors[intent] = extractor

        # 注册关键词映射
        for keyword in extractor.keywords:
            self._keyword_map[keyword.lower()] = intent

        logger.debug(f"注册提取器: {intent} (关键词: {extractor.keywords})")

    def get(self, intent: str) -> BaseExtractor | None:
        """
        根据意图获取提取器

        Args:
            intent: 意图标识

        Returns:
            提取器实例，未找到返回 None
        """
        return self._extractors.get(intent)

    def detect_intent(self, intent_desc: str, url: str = "") -> str:
        """
        检测意图

        Args:
            intent_desc: 意图描述（如 "视频", "image", "全部"）
            url: 目标 URL（可选，用于辅助判断）

        Returns:
            意图标识
        """
        if not intent_desc:
            return "video"  # 默认视频

        desc_lower = intent_desc.lower()

        # 1. 精确匹配 intent
        if desc_lower in self._extractors:
            return desc_lower

        # 2. 关键词匹配
        for keyword, intent in self._keyword_map.items():
            if keyword in desc_lower:
                return intent

        # 3. 中文关键词匹配
        chinese_keywords = {
            "视频": "video",
            "video": "video",
            "图片": "image",
            "image": "image",
            "照片": "image",
            "文档": "document",
            "document": "document",
            "pdf": "document",
            "表格": "table",
            "table": "table",
            "excel": "table",
            "文章": "article",
            "article": "article",
            "链接": "links",
            "links": "links",
            "url": "links",
            "api": "api",
            "json": "api",
            "全部": "all",
            "all": "all",
        }

        for keyword, intent in chinese_keywords.items():
            if keyword in desc_lower:
                return intent

        # 4. URL 辅助判断
        if url:
            for extractor in self._extractors.values():
                if extractor.supports_url(url):
                    return extractor.intent

        # 5. 默认视频
        return "video"

    def get_all(self) -> list[BaseExtractor]:
        """获取所有提取器"""
        return list(self._extractors.values())

    def get_by_intent(self, intent: str) -> BaseExtractor | None:
        """根据意图获取提取器"""
        return self._extractors.get(intent)

    def list_intents(self) -> list[str]:
        """列出所有已注册的意图"""
        return list(self._extractors.keys())


# 全局注册表实例
registry = ExtractorRegistry()


def register_extractor(extractor_class: type[BaseExtractor]) -> type[BaseExtractor]:
    """
    提取器注册装饰器

    Args:
        extractor_class: 提取器类

    Returns:
        提取器类（未修改）
    """
    extractor = extractor_class()
    registry.register(extractor)
    return extractor_class