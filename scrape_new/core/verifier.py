"""
文件校验模块

提供文件大小、完整性校验功能。
"""

from __future__ import annotations

import logging
from pathlib import Path

from ..config import Config

logger = logging.getLogger(__name__)


def verify_file(
    filepath: Path,
    min_size: int = 0,
    size_hint: int = 0,
    suspicious_ratio: float = 0.5,
) -> str:
    """
    校验文件

    Args:
        filepath: 文件路径
        min_size: 最小文件大小（字节）
        size_hint: 预期文件大小（字节）
        suspicious_ratio: 可疑比例（实际大小 < size_hint * ratio 视为不完整）

    Returns:
        校验结果: "ok", "missing", "suspicious", "incomplete"
    """
    if not filepath.exists():
        return "missing"

    file_size = filepath.stat().st_size

    # 空文件
    if file_size == 0:
        logger.warning(f"空文件: {filepath}")
        return "suspicious"

    # 小于最小大小
    if min_size > 0 and file_size < min_size:
        logger.warning(f"文件过小 ({file_size} < {min_size}): {filepath}")
        return "suspicious"

    # 与预期大小比较
    if size_hint > 0:
        expected_min = int(size_hint * suspicious_ratio)
        if file_size < expected_min:
            logger.warning(
                f"文件不完整 ({file_size} < {expected_min}, "
                f"预期 {size_hint}): {filepath}"
            )
            return "incomplete"

    return "ok"


def verify_download(
    filepath: Path,
    config: Config,
    content_type: str = "",
    size_hint: int = 0,
) -> str:
    """
    校验下载文件

    Args:
        filepath: 文件路径
        config: 配置
        content_type: Content-Type
        size_hint: 预期大小

    Returns:
        校验结果
    """
    # 根据 content-type 确定最小大小
    min_size = 0
    if content_type:
        ct_lower = content_type.lower()
        if 'video' in ct_lower:
            min_size = config.min_video_size
        elif 'image' in ct_lower:
            min_size = config.min_image_size
        elif 'pdf' in ct_lower or 'document' in ct_lower:
            min_size = config.min_document_size

    return verify_file(
        filepath,
        min_size=min_size,
        size_hint=size_hint,
        suspicious_ratio=config.suspicious_ratio,
    )