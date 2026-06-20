"""
文档提取器

从 HTML 中提取文档资源。
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
    DOCUMENT_EXTENSIONS,
)

if TYPE_CHECKING:
    from ..models import ExtractContext, ExtractResult

logger = logging.getLogger(__name__)


class DocumentExtractor(BaseExtractor):
    """文档提取器"""

    intent = "document"
    keywords = ["文档", "document", "pdf", "doc", "ppt", "excel", "zip"]

    def supports_url(self, url: str) -> bool:
        """判断是否为文档直链"""
        url_lower = url.lower()
        return any(url_lower.endswith(ext) for ext in DOCUMENT_EXTENSIONS)

    def extract(self, ctx: ExtractContext) -> ExtractResult:
        """提取文档资源"""
        from ..models import ExtractResult

        result = ExtractResult()

        try:
            html = fetch_html(ctx.session, ctx.url, ctx.config.timeout)
            if not html:
                return result

            result.title = extract_page_title(html)

            # 收集所有文档 URL
            doc_urls = []

            # 1. 从 <a href=""> 提取
            doc_urls.extend(self._extract_from_links(html, ctx.url))

            # 2. 从 <iframe src=""> 提取
            doc_urls.extend(self._extract_from_iframe(html, ctx.url))

            # 3. 从 <embed src=""> 提取
            doc_urls.extend(self._extract_from_embed(html, ctx.url))

            # 去重
            doc_urls = unique_keep_order(doc_urls)

            # 创建 DownloadItem
            for i, url in enumerate(doc_urls, 1):
                item = make_download_item(
                    index=i,
                    url=url,
                    source_url=ctx.url,
                    kind="document",
                    default_ext=".pdf",
                )
                result.items.append(item)

            logger.info(f"文档提取完成: {len(result.items)} 个")

        except Exception as e:
            logger.error(f"文档提取失败: {e}")

        return result

    def _extract_from_links(self, html: str, base_url: str) -> list[str]:
        """从 a 标签提取文档链接"""
        urls = []
        pattern = r'<a[^>]*\shref=["\']([^"\']+)["\'][^>]*>(.*?)</a>'
        for match in re.finditer(pattern, html, re.IGNORECASE | re.DOTALL):
            url = match.group(1)
            text = match.group(2).strip()

            abs_url = urljoin(base_url, url)

            # 检查是否为文档文件
            url_lower = abs_url.lower()
            if any(url_lower.endswith(ext) for ext in DOCUMENT_EXTENSIONS):
                urls.append(abs_url)
                continue

            # 检查链接文本是否包含文档关键词
            text_lower = text.lower()
            doc_keywords = ['pdf', '下载', 'download', '文档', 'document', '课件', '资料']
            if any(kw in text_lower for kw in doc_keywords):
                urls.append(abs_url)

        return urls

    def _extract_from_iframe(self, html: str, base_url: str) -> list[str]:
        """从 iframe 提取"""
        urls = []
        pattern = r'<iframe[^>]*\ssrc=["\']([^"\']+)["\']'
        for match in re.finditer(pattern, html, re.IGNORECASE):
            url = match.group(1)
            abs_url = urljoin(base_url, url)

            # 检查是否为文档
            url_lower = abs_url.lower()
            if any(url_lower.endswith(ext) for ext in DOCUMENT_EXTENSIONS):
                urls.append(abs_url)

        return urls

    def _extract_from_embed(self, html: str, base_url: str) -> list[str]:
        """从 embed 提取"""
        urls = []
        pattern = r'<embed[^>]*\ssrc=["\']([^"\']+)["\']'
        for match in re.finditer(pattern, html, re.IGNORECASE):
            url = match.group(1)
            abs_url = urljoin(base_url, url)

            # 检查是否为文档
            url_lower = abs_url.lower()
            if any(url_lower.endswith(ext) for ext in DOCUMENT_EXTENSIONS):
                urls.append(abs_url)

        return urls