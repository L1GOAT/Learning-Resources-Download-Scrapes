"""
视频提取器

从 HTML 中提取视频资源。
"""

from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING
from urllib.parse import urljoin

from .base import BaseExtractor
from .utils import (
    fetch_html,
    extract_page_title,
    make_download_item,
    unique_keep_order,
    VIDEO_EXTENSIONS,
)

if TYPE_CHECKING:
    from ..models import ExtractContext, ExtractResult

logger = logging.getLogger(__name__)


class VideoExtractor(BaseExtractor):
    """视频提取器"""

    intent = "video"
    keywords = ["视频", "video", "mp4", "m3u8", "webm"]

    def supports_url(self, url: str) -> bool:
        """判断是否为视频直链"""
        url_lower = url.lower()
        return any(url_lower.endswith(ext) for ext in VIDEO_EXTENSIONS)

    def extract(self, ctx: ExtractContext) -> ExtractResult:
        """提取视频资源"""
        from ..models import ExtractResult

        result = ExtractResult()

        try:
            html = fetch_html(ctx.session, ctx.url, ctx.config.timeout)
            if not html:
                return result

            result.title = extract_page_title(html)

            # 收集所有视频 URL
            video_urls = []

            # 1. 从 <video src=""> 提取
            video_urls.extend(self._extract_from_video_tag(html, ctx.url))

            # 2. 从 <source src=""> 提取
            video_urls.extend(self._extract_from_source_tag(html, ctx.url))

            # 3. 从 <a href=""> 提取视频直链
            video_urls.extend(self._extract_from_links(html, ctx.url))

            # 4. 从 script 中提取
            video_urls.extend(self._extract_from_scripts(html, ctx.url))

            # 去重
            video_urls = unique_keep_order(video_urls)

            # 创建 DownloadItem
            for i, url in enumerate(video_urls, 1):
                item = make_download_item(
                    index=i,
                    url=url,
                    source_url=ctx.url,
                    kind="video",
                    default_ext=".mp4",
                )
                result.items.append(item)

            logger.info(f"视频提取完成: {len(result.items)} 个")

        except Exception as e:
            logger.error(f"视频提取失败: {e}")

        return result

    def _extract_from_video_tag(self, html: str, base_url: str) -> list[str]:
        """从 video 标签提取"""
        urls = []
        pattern = r'<video[^>]*\ssrc=["\']([^"\']+)["\']'
        for match in re.finditer(pattern, html, re.IGNORECASE):
            url = urljoin(base_url, match.group(1))
            urls.append(url)
        return urls

    def _extract_from_source_tag(self, html: str, base_url: str) -> list[str]:
        """从 source 标签提取"""
        urls = []
        pattern = r'<source[^>]*\ssrc=["\']([^"\']+)["\']'
        for match in re.finditer(pattern, html, re.IGNORECASE):
            url = urljoin(base_url, match.group(1))
            urls.append(url)
        return urls

    def _extract_from_links(self, html: str, base_url: str) -> list[str]:
        """从 a 标签提取视频直链"""
        urls = []
        pattern = r'<a[^>]*\shref=["\']([^"\']+)["\']'
        for match in re.finditer(pattern, html, re.IGNORECASE):
            url = match.group(1)
            abs_url = urljoin(base_url, url)

            # 检查是否为视频文件
            url_lower = abs_url.lower()
            if any(url_lower.endswith(ext) for ext in VIDEO_EXTENSIONS):
                urls.append(abs_url)

        return urls

    def _extract_from_scripts(self, html: str, base_url: str) -> list[str]:
        """从 script 标签提取视频 URL"""
        urls = []

        # 匹配常见的视频 URL 模式
        patterns = [
            r'["\']([^"\']*\.m3u8[^"\']*)["\']',
            r'["\']([^"\']*\.mp4[^"\']*)["\']',
            r'["\']([^"\']*\.webm[^"\']*)["\']',
            r'url\s*:\s*["\']([^"\']+)["\']',
            r'src\s*:\s*["\']([^"\']+)["\']',
            r'file\s*:\s*["\']([^"\']+)["\']',
            r'videoUrl\s*[=:]\s*["\']([^"\']+)["\']',
        ]

        for pattern in patterns:
            for match in re.finditer(pattern, html, re.IGNORECASE):
                url = match.group(1)
                abs_url = urljoin(base_url, url)

                # 检查是否为视频文件
                url_lower = abs_url.lower()
                if any(url_lower.endswith(ext) for ext in VIDEO_EXTENSIONS):
                    urls.append(abs_url)

        return urls