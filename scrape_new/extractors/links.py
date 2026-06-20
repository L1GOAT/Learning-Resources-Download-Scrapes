"""
链接提取器

从 HTML 中提取所有链接。
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import urljoin, urlparse

from .base import BaseExtractor
from .utils import fetch_html, extract_page_title, unique_keep_order

if TYPE_CHECKING:
    from ..models import ExtractContext, ExtractResult

logger = logging.getLogger(__name__)


class LinksExtractor(BaseExtractor):
    """链接提取器"""

    intent = "links"
    keywords = ["链接", "links", "url", "网址"]

    def supports_url(self, url: str) -> bool:
        """链接提取器不支持直链"""
        return False

    def extract(self, ctx: ExtractContext) -> ExtractResult:
        """提取所有链接"""
        from ..models import ExtractResult

        result = ExtractResult()

        try:
            html = fetch_html(ctx.session, ctx.url, ctx.config.timeout)
            if not html:
                return result

            result.title = extract_page_title(html)

            # 获取过滤参数
            filter_ext = ctx.extra.get("filter_ext", "")
            filter_kw = ctx.extra.get("filter_kw", "")

            # 提取所有链接
            links = self._extract_links(html, ctx.url)

            # 过滤
            if filter_ext:
                exts = [e.strip().lower() for e in filter_ext.split(',')]
                links = [l for l in links if any(l.lower().endswith(e) for e in exts)]

            if filter_kw:
                keywords = [k.strip().lower() for k in filter_kw.split(',')]
                links = [l for l in links if any(kw in l.lower() for kw in keywords)]

            # 去重
            links = unique_keep_order(links)

            # 保存链接
            output_dir = Path(ctx.output_dir)
            output_dir.mkdir(parents=True, exist_ok=True)

            # 保存为 txt
            txt_path = output_dir / "links.txt"
            txt_path.write_text('\n'.join(links), encoding='utf-8')

            # 保存为 json
            json_path = output_dir / "links.json"
            with open(json_path, 'w', encoding='utf-8') as f:
                json.dump({"source": ctx.url, "links": links}, f, indent=2, ensure_ascii=False)

            result.metadata["links_path"] = str(txt_path)
            result.metadata["links_count"] = str(len(links))

            logger.info(f"链接提取完成: {len(links)} 个")

        except Exception as e:
            logger.error(f"链接提取失败: {e}")

        return result

    def _extract_links(self, html: str, base_url: str) -> list[str]:
        """提取所有链接"""
        urls = []

        # 匹配 a[href]
        pattern = r'<a[^>]*\shref=["\']([^"\']+)["\']'
        for match in re.finditer(pattern, html, re.IGNORECASE):
            url = match.group(1)

            # 跳过锚点和 javascript
            if url.startswith('#') or url.startswith('javascript:'):
                continue

            # 转换为绝对 URL
            abs_url = urljoin(base_url, url)

            # 只保留 http/https
            if abs_url.startswith('http://') or abs_url.startswith('https://'):
                urls.append(abs_url)

        return urls