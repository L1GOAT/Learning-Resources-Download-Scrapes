"""
文章提取器

从 HTML 中提取文章正文。
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import TYPE_CHECKING

from .base import BaseExtractor
from .utils import fetch_html, extract_page_title

if TYPE_CHECKING:
    from ..models import ExtractContext, ExtractResult

logger = logging.getLogger(__name__)


class ArticleExtractor(BaseExtractor):
    """文章提取器"""

    intent = "article"
    keywords = ["文章", "article", "text", "正文"]

    def supports_url(self, url: str) -> bool:
        """文章提取器不支持直链"""
        return False

    def extract(self, ctx: ExtractContext) -> ExtractResult:
        """提取文章正文"""
        from ..models import ExtractResult

        result = ExtractResult()

        try:
            html = fetch_html(ctx.session, ctx.url, ctx.config.timeout)
            if not html:
                return result

            result.title = extract_page_title(html)

            # 提取正文
            content = self._extract_content(html)

            if content:
                # 保存为 markdown
                output_dir = Path(ctx.output_dir)
                output_dir.mkdir(parents=True, exist_ok=True)

                title = result.title or "article"
                safe_title = re.sub(r'[^\w\s-]', '', title)[:50].strip()
                if not safe_title:
                    safe_title = "article"

                md_path = output_dir / f"{safe_title}.md"

                # 写入 markdown
                md_content = f"# {result.title}\n\n{content}"
                md_path.write_text(md_content, encoding='utf-8')

                result.metadata["article_path"] = str(md_path)
                result.metadata["content_length"] = str(len(content))

                logger.info(f"文章提取完成: {md_path}")
            else:
                logger.warning("未提取到文章内容")

        except Exception as e:
            logger.error(f"文章提取失败: {e}")

        return result

    def _extract_content(self, html: str) -> str:
        """提取文章正文"""
        # 移除 script、style、nav、footer、header
        html = self._remove_tags(html)

        # 尝试多种选择器
        content = ""

        # 1. 尝试 article 标签
        content = self._extract_by_tag(html, 'article')

        # 2. 尝试 main 标签
        if not content:
            content = self._extract_by_tag(html, 'main')

        # 3. 尝试常见 class/id
        if not content:
            content = self._extract_by_class(html)

        # 4. 兜底：提取 body 内容
        if not content:
            content = self._extract_body(html)

        # 清理内容
        content = self._clean_content(content)

        return content

    def _remove_tags(self, html: str) -> str:
        """移除不需要的标签"""
        patterns = [
            r'<script[^>]*>.*?</script>',
            r'<style[^>]*>.*?</style>',
            r'<nav[^>]*>.*?</nav>',
            r'<footer[^>]*>.*?</footer>',
            r'<header[^>]*>.*?</header>',
            r'<!--.*?-->',
        ]
        for pattern in patterns:
            html = re.sub(pattern, '', html, flags=re.IGNORECASE | re.DOTALL)
        return html

    def _extract_by_tag(self, html: str, tag: str) -> str:
        """按标签提取"""
        pattern = f'<{tag}[^>]*>(.*?)</{tag}>'
        match = re.search(pattern, html, re.IGNORECASE | re.DOTALL)
        if match:
            return match.group(1)
        return ""

    def _extract_by_class(self, html: str) -> str:
        """按 class/id 提取"""
        patterns = [
            r'<div[^>]*class=["\'][^"\']*content[^"\']*["\'][^>]*>(.*?)</div>',
            r'<div[^>]*class=["\'][^"\']*article[^"\']*["\'][^>]*>(.*?)</div>',
            r'<div[^>]*class=["\'][^"\']*post[^"\']*["\'][^>]*>(.*?)</div>',
            r'<div[^>]*class=["\'][^"\']*entry[^"\']*["\'][^>]*>(.*?)</div>',
            r'<div[^>]*id=["\']content["\'][^>]*>(.*?)</div>',
            r'<div[^>]*id=["\']article["\'][^>]*>(.*?)</div>',
            r'<div[^>]*class=["\'][^"\']*正文[^"\']*["\'][^>]*>(.*?)</div>',
        ]
        for pattern in patterns:
            match = re.search(pattern, html, re.IGNORECASE | re.DOTALL)
            if match:
                return match.group(1)
        return ""

    def _extract_body(self, html: str) -> str:
        """提取 body 内容"""
        pattern = r'<body[^>]*>(.*?)</body>'
        match = re.search(pattern, html, re.IGNORECASE | re.DOTALL)
        if match:
            return match.group(1)
        return html

    def _clean_content(self, html: str) -> str:
        """清理 HTML 为纯文本"""
        if not html:
            return ""

        # 移除所有 HTML 标签
        text = re.sub(r'<[^>]+>', '\n', html)

        # 处理 HTML 实体
        text = text.replace('&nbsp;', ' ')
        text = text.replace('&lt;', '<')
        text = text.replace('&gt;', '>')
        text = text.replace('&amp;', '&')
        text = text.replace('&quot;', '"')

        # 清理空白
        lines = []
        for line in text.split('\n'):
            line = line.strip()
            if line:
                lines.append(line)

        return '\n\n'.join(lines)