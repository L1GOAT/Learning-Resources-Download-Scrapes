"""
提取器辅助函数

提供 URL 处理、HTML 解析等通用功能。
"""

from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING
from urllib.parse import urljoin, urlparse, unquote

if TYPE_CHECKING:
    import requests

logger = logging.getLogger(__name__)

# 常见视频扩展名
VIDEO_EXTENSIONS = {'.mp4', '.webm', '.flv', '.mov', '.avi', '.mkv', '.m3u8', '.ts'}

# 常见图片扩展名
IMAGE_EXTENSIONS = {'.jpg', '.jpeg', '.png', '.gif', '.webp', '.bmp', '.svg', '.ico'}

# 常见文档扩展名
DOCUMENT_EXTENSIONS = {'.pdf', '.doc', '.docx', '.ppt', '.pptx', '.xls', '.xlsx', '.zip', '.rar', '.7z'}


def fetch_html(session: requests.Session, url: str, timeout: int = 30) -> str:
    """
    获取页面 HTML

    Args:
        session: requests.Session
        url: URL
        timeout: 超时秒数

    Returns:
        HTML 字符串
    """
    try:
        resp = session.get(url, timeout=timeout)
        resp.raise_for_status()
        return resp.text
    except Exception as e:
        logger.error(f"获取页面失败: {url}: {e}")
        return ""


def unique_keep_order(items: list) -> list:
    """
    去重并保持顺序

    Args:
        items: 列表

    Returns:
        去重后的列表
    """
    seen = set()
    result = []
    for item in items:
        if item not in seen:
            seen.add(item)
            result.append(item)
    return result


def extract_page_title(html: str) -> str:
    """
    从 HTML 提取页面标题

    Args:
        html: HTML 字符串

    Returns:
        页面标题
    """
    match = re.search(r'<title[^>]*>(.*?)</title>', html, re.IGNORECASE | re.DOTALL)
    if match:
        title = match.group(1).strip()
        # 清理多余空白
        title = re.sub(r'\s+', ' ', title)
        return title[:200]  # 限制长度
    return ""


def url_basename(url: str) -> str:
    """
    从 URL 提取文件名（不含扩展名）

    Args:
        url: URL

    Returns:
        文件名
    """
    try:
        parsed = urlparse(url)
        path = unquote(parsed.path)
        filename = path.split('/')[-1]

        # 移除查询参数
        if '?' in filename:
            filename = filename.split('?')[0]

        # 移除扩展名
        if '.' in filename:
            filename = '.'.join(filename.split('.')[:-1])

        return filename or "file"
    except Exception:
        return "file"


def make_download_item(
    index: int,
    url: str,
    source_url: str,
    kind: str,
    default_ext: str = "",
    filename: str | None = None,
    size_hint: int = 0,
    min_size: int = 0,
) -> DownloadItem:
    """
    创建 DownloadItem

    Args:
        index: 序号
        url: 下载 URL
        source_url: 来源 URL
        kind: 资源类型
        default_ext: 默认扩展名
        filename: 自定义文件名
        size_hint: 预期大小
        min_size: 最小大小

    Returns:
        DownloadItem
    """
    from ..models import DownloadItem
    from ..core.paths import sanitize_filename, guess_ext

    # 生成文件名
    if not filename:
        basename = url_basename(url)
        ext = guess_ext(url, default_ext)
        filename = f"{index:03d}_{sanitize_filename(basename)}{ext}"

    return DownloadItem(
        url=url,
        filename=filename,
        source_url=source_url,
        kind=kind,
        size_hint=size_hint,
        min_size=min_size,
    )


def extract_urls_by_extension(html: str, base_url: str, extensions: set[str]) -> list[str]:
    """
    从 HTML 中提取指定扩展名的 URL

    Args:
        html: HTML 字符串
        base_url: 基础 URL
        extensions: 扩展名集合

    Returns:
        URL 列表
    """
    urls = []

    # 匹配 href 和 src 属性
    pattern = r'(?:href|src)=["\']([^"\']+)["\']'
    for match in re.finditer(pattern, html, re.IGNORECASE):
        url = match.group(1)
        abs_url = urljoin(base_url, url)

        # 检查扩展名
        parsed = urlparse(abs_url)
        path_lower = parsed.path.lower()
        if any(path_lower.endswith(ext) for ext in extensions):
            urls.append(abs_url)

    return unique_keep_order(urls)