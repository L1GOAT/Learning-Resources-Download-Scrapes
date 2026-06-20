"""
路径操作模块

提供文件名清理、路径安全检查、目录创建等功能。
"""

from __future__ import annotations

import logging
import re
from pathlib import Path, PurePosixPath
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

# Windows 非法字符
_WINDOWS_ILLEGAL_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')

# 哈希文件名模式（纯 hex，长度 32 或 40）
_HASH_FILENAME_PATTERN = re.compile(r'^[0-9a-f]{32}(\.[a-z0-9]+)?$', re.IGNORECASE)
_HASH_FILENAME_PATTERN_40 = re.compile(r'^[0-9a-f]{40}(\.[a-z0-9]+)?$', re.IGNORECASE)

# 保留文件名
_RESERVED_NAMES = {
    'CON', 'PRN', 'AUX', 'NUL',
    'COM1', 'COM2', 'COM3', 'COM4', 'COM5', 'COM6', 'COM7', 'COM8', 'COM9',
    'LPT1', 'LPT2', 'LPT3', 'LPT4', 'LPT5', 'LPT6', 'LPT7', 'LPT8', 'LPT9',
}


def sanitize_filename(name: str, max_length: int = 200) -> str:
    """
    清理文件名，移除非法字符

    Args:
        name: 原始文件名
        max_length: 最大长度

    Returns:
        清理后的文件名
    """
    if not name:
        return "unnamed"

    # 替换 Windows 非法字符
    cleaned = _WINDOWS_ILLEGAL_CHARS.sub('_', name)

    # 替换控制字符
    cleaned = re.sub(r'[\x00-\x1f\x7f]', '_', cleaned)

    # 移除首尾空格和点
    cleaned = cleaned.strip(' .')

    # 处理保留文件名
    stem = cleaned.split('.')[0].upper()
    if stem in _RESERVED_NAMES:
        cleaned = f"_{cleaned}"

    # 截断长度
    if len(cleaned) > max_length:
        # 保留扩展名
        parts = cleaned.rsplit('.', 1)
        if len(parts) == 2:
            stem, ext = parts
            cleaned = stem[:max_length - len(ext) - 1] + '.' + ext
        else:
            cleaned = cleaned[:max_length]

    # 空文件名兜底
    if not cleaned:
        return "unnamed"

    return cleaned


def ensure_output_dir(path: Path) -> Path:
    """
    确保输出目录存在

    Args:
        path: 目录路径

    Returns:
        目录路径
    """
    path.mkdir(parents=True, exist_ok=True)
    return path


def check_path_traversal(filepath: Path, output_dir: Path) -> None:
    """
    检查路径穿越

    Args:
        filepath: 目标文件路径
        output_dir: 输出目录

    Raises:
        ValueError: 路径穿越
    """
    try:
        # 解析为绝对路径
        resolved_file = filepath.resolve()
        resolved_dir = output_dir.resolve()

        # 检查是否在输出目录内
        if not str(resolved_file).startswith(str(resolved_dir)):
            raise ValueError(
                f"路径穿越: {filepath} 不在 {output_dir} 内"
            )
    except ValueError:
        raise
    except Exception as e:
        raise ValueError(f"路径检查失败: {e}") from e


def guess_ext(url: str, fallback: str = "") -> str:
    """
    从 URL 猜测文件扩展名

    Args:
        url: URL
        fallback: 默认扩展名

    Returns:
        扩展名（包含点号）
    """
    try:
        parsed = urlparse(url)
        path = PurePosixPath(parsed.path)
        suffix = path.suffix.lower()

        # 常见扩展名映射
        ext_map = {
            '.jpeg': '.jpg',
            '.htm': '.html',
            '.m4a': '.mp4',
        }

        if suffix:
            return ext_map.get(suffix, suffix)
    except Exception:
        pass

    return fallback


def make_indexed_name(index: int, name: str, ext: str, pad: int = 3) -> str:
    """
    生成带序号的文件名

    Args:
        index: 序号
        name: 文件名（不含扩展名）
        ext: 扩展名（包含点号）
        pad: 序号填充位数

    Returns:
        格式化的文件名
    """
    sanitized = sanitize_filename(name)
    return f"{index:0{pad}d}_{sanitized}{ext}"


def is_hash_filename(filename: str) -> bool:
    """
    判断是否为哈希文件名

    Args:
        filename: 文件名

    Returns:
        是否为哈希文件名
    """
    return bool(_HASH_FILENAME_PATTERN.match(filename) or _HASH_FILENAME_PATTERN_40.match(filename))


def extract_index_prefix(filename: str) -> tuple[int, str]:
    """
    提取文件名前缀序号

    Args:
        filename: 文件名

    Returns:
        (序号, 剩余部分)，无序号返回 (0, filename)
    """
    match = re.match(r'^(\d{3})_(.+)$', filename)
    if match:
        return int(match.group(1)), match.group(2)
    return 0, filename


def safe_filepath(output_dir: Path, filename: str) -> Path:
    """
    生成安全的文件路径

    Args:
        output_dir: 输出目录
        filename: 文件名

    Returns:
        安全的文件路径
    """
    sanitized = sanitize_filename(filename)
    filepath = output_dir / sanitized

    # 检查路径穿越
    check_path_traversal(filepath, output_dir)

    return filepath