"""
提取器模块

提供各种资源类型的提取器实现。
"""

from __future__ import annotations

from .base import BaseExtractor
from .registry import registry, register_extractor

# 导入所有提取器
from .video import VideoExtractor
from .image import ImageExtractor
from .document import DocumentExtractor
from .table import TableExtractor
from .article import ArticleExtractor
from .links import LinksExtractor
from .api import ApiExtractor


def register_default_extractors() -> None:
    """注册所有默认提取器"""
    register_extractor(VideoExtractor)
    register_extractor(ImageExtractor)
    register_extractor(DocumentExtractor)
    register_extractor(TableExtractor)
    register_extractor(ArticleExtractor)
    register_extractor(LinksExtractor)
    register_extractor(ApiExtractor)


# 自动注册
register_default_extractors()


__all__ = [
    "BaseExtractor",
    "registry",
    "register_extractor",
    "register_default_extractors",
    "VideoExtractor",
    "ImageExtractor",
    "DocumentExtractor",
    "TableExtractor",
    "ArticleExtractor",
    "LinksExtractor",
    "ApiExtractor",
]