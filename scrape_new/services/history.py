"""
下载历史记录模块

提供 URL 去重、历史记录、查看历史等功能。
"""

from __future__ import annotations

import json
import logging
import shutil
from datetime import datetime
from hashlib import sha256
from pathlib import Path
from typing import TYPE_CHECKING

from ..exceptions import HistoryError
from ..models import HistoryRecord

if TYPE_CHECKING:
    from ..config import Config

logger = logging.getLogger(__name__)


def get_history_path(config: Config) -> Path:
    """
    获取历史文件路径

    Args:
        config: 配置

    Returns:
        历史文件路径
    """
    return Path(config.history_file)


def load_history(config: Config) -> dict:
    """
    加载历史数据

    Args:
        config: 配置

    Returns:
        历史数据字典
    """
    path = get_history_path(config)

    if not path.exists():
        return {"records": []}

    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)

        # 兼容旧格式
        if isinstance(data, list):
            return {"records": data}

        return data

    except json.JSONDecodeError as e:
        logger.warning(f"历史文件损坏，备份并重建: {path}")
        _backup_corrupt_file(path)
        return {"records": []}

    except Exception as e:
        logger.error(f"加载历史文件失败: {e}")
        return {"records": []}


def save_history(data: dict, config: Config) -> None:
    """
    保存历史数据

    Args:
        data: 历史数据
        config: 配置
    """
    path = get_history_path(config)

    try:
        # 确保目录存在
        path.parent.mkdir(parents=True, exist_ok=True)

        # 限制记录数量
        records = data.get("records", [])
        if len(records) > config.history_max_records:
            data["records"] = records[-config.history_max_records:]

        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)

    except Exception as e:
        logger.error(f"保存历史文件失败: {e}")


def url_hash(url: str) -> str:
    """
    计算 URL 的 SHA256 短哈希

    Args:
        url: URL

    Returns:
        16 位哈希字符串
    """
    return sha256(url.encode("utf-8")).hexdigest()[:16]


def is_downloaded(url: str, config: Config) -> bool:
    """
    检查 URL 是否已下载过

    Args:
        url: URL
        config: 配置

    Returns:
        是否已下载
    """
    data = load_history(config)
    hash_val = url_hash(url)

    for record in data.get("records", []):
        if record.get("url_hash") == hash_val:
            logger.debug(f"URL 已下载过: {url} -> {hash_val}")
            return True

    return False


def record_download(
    url: str,
    intent: str,
    output_dir: Path,
    file_count: int,
    config: Config,
) -> Path:
    """
    记录下载历史

    Args:
        url: URL
        intent: 意图
        output_dir: 输出目录
        file_count: 文件数量
        config: 配置

    Returns:
        历史文件路径
    """
    data = load_history(config)

    record = {
        "url": url,
        "url_hash": url_hash(url),
        "intent": intent,
        "output_dir": str(output_dir),
        "file_count": file_count,
        "timestamp": datetime.now().isoformat(),
    }

    data.setdefault("records", []).append(record)
    save_history(data, config)

    path = get_history_path(config)
    logger.debug(f"历史已记录: {url}")
    return path


def list_history(config: Config, limit: int = 20) -> list[HistoryRecord]:
    """
    列出历史记录

    Args:
        config: 配置
        limit: 返回数量限制

    Returns:
        历史记录列表
    """
    data = load_history(config)
    records = data.get("records", [])

    # 转换为 HistoryRecord
    result = []
    for r in records[-limit:]:
        try:
            record = HistoryRecord(
                url=r.get("url", ""),
                intent=r.get("intent", ""),
                output_dir=r.get("output_dir", ""),
                file_count=r.get("file_count", 0),
                timestamp=r.get("timestamp", ""),
                url_hash=r.get("url_hash", ""),
            )
            result.append(record)
        except Exception as e:
            logger.warning(f"历史记录解析失败: {e}")

    return result


def clear_history(config: Config) -> None:
    """
    清空历史记录

    Args:
        config: 配置
    """
    path = get_history_path(config)
    if path.exists():
        path.unlink()
        logger.info("历史记录已清空")


def _backup_corrupt_file(path: Path) -> None:
    """
    备份损坏的文件

    Args:
        path: 原文件路径
    """
    try:
        backup_path = path.with_suffix(".corrupt")
        shutil.copy2(path, backup_path)
        logger.info(f"损坏文件已备份: {backup_path}")
    except Exception as e:
        logger.error(f"备份损坏文件失败: {e}")