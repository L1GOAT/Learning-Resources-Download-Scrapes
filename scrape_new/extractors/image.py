"""
图片提取器

从 HTML 中提取图片资源。
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
    IMAGE_EXTENSIONS,
)

if TYPE_CHECKING:
    from ..models import ExtractContext, ExtractResult

logger = logging.getLogger(__name__)


class ImageExtractor(BaseExtractor):
    """图片提取器"""

    intent = "image"
    keywords = ["图片", "image", "photo", "照片", "img"]

    def supports_url(self, url: str) -> bool:
        """判断是否为图片直链"""
        url_lower = url.lower()
        return any(url_lower.endswith(ext) for ext in IMAGE_EXTENSIONS)

    def extract(self, ctx: ExtractContext) -> ExtractResult:
        """提取图片资源"""
        from ..models import ExtractResult

        result = ExtractResult()

        try:
            html = fetch_html(ctx.session, ctx.url, ctx.config.timeout)
            if not html:
                return result

            result.title = extract_page_title(html)

            # 获取 min_size
            min_size = int(ctx.extra.get("min_size", ctx.config.min_image_size))

            # 收集所有图片 URL
            image_urls = []

            # 1. 从 <img src=""> 提取
            image_urls.extend(self._extract_from_img_src(html, ctx.url))

            # 2. 从 <img data-src=""> 提取（懒加载）
            image_urls.extend(self._extract_from_data_src(html, ctx.url))

            # 3. 从 srcset 提取
            image_urls.extend(self._extract_from_srcset(html, ctx.url))

            # 4. 从 <a href=""> 提取图片直链
            image_urls.extend(self._extract_from_links(html, ctx.url))

            # 去重
            image_urls = unique_keep_order(image_urls)

            # 过滤 base64
            image_urls = [u for u in image_urls if not u.startswith('data:')]

            # 创建 DownloadItem
            for i, url in enumerate(image_urls, 1):
                item = make_download_item(
                    index=i,
                    url=url,
                    source_url=ctx.url,
                    kind="image",
                    default_ext=".jpg",
                    min_size=min_size,
                )
                result.items.append(item)

            logger.info(f"图片提取完成: {len(result.items)} 个")

        except Exception as e:
            logger.error(f"图片提取失败: {e}")

        return result

    def _extract_from_img_src(self, html: str, base_url: str) -> list[str]:
        """从 img src 提取"""
        urls = []
        pattern = r'<img[^>]*\ssrc=["\']([^"\']+)["\']'
        for match in re.finditer(pattern, html, re.IGNORECASE):
            url = match.group(1)
            if not url.startswith('data:'):
                abs_url = urljoin(base_url, url)
                urls.append(abs_url)
        return urls

    def _extract_from_data_src(self, html: str, base_url: str) -> list[str]:
        """从 data-src 等懒加载属性提取"""
        urls = []
        patterns = [
            r'<img[^>]*\sdata-src=["\']([^"\']+)["\']',
            r'<img[^>]*\sdata-original=["\']([^"\']+)["\']',
            r'<img[^>]*\sdata-lazy-src=["\']([^"\']+)["\']',
        ]
        for pattern in patterns:
            for match in re.finditer(pattern, html, re.IGNORECASE):
                url = match.group(1)
                if not url.startswith('data:'):
                    abs_url = urljoin(base_url, url)
                    urls.append(abs_url)
        return urls

    def _extract_from_srcset(self, html: str, base_url: str) -> list[str]:
        """从 srcset 提取"""
        urls = []
        # 匹配 img[srcset] 和 source[srcset]
        pattern = r'<(?:img|source)[^>]*\ssrcset=["\']([^"\']+)["\']'
        for match in re.finditer(pattern, html, re.IGNORECASE):
            srcset = match.group(1)
            # 解析 srcset: "url1 1x, url2 2x" 或 "url1 100w, url2 200w"
            for item in srcset.split(','):
                parts = item.strip().split()
                if parts:
                    url = parts[0]
                    if not url.startswith('data:'):
                        abs_url = urljoin(base_url, url)
                        urls.append(abs_url)
        return urls

    def _extract_from_links(self, html: str, base_url: str) -> list[str]:
        """从 a 标签提取图片直链"""
        urls = []
        pattern = r'<a[^>]*\shref=["\']([^"\']+)["\']'
        for match in re.finditer(pattern, html, re.IGNORECASE):
            url = match.group(1)
            abs_url = urljoin(base_url, url)

            # 检查是否为图片文件
            url_lower = abs_url.lower()
            if any(url_lower.endswith(ext) for ext in IMAGE_EXTENSIONS):
                urls.append(abs_url)

        return urls