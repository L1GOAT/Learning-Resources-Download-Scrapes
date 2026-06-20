"""
配置管理

负责加载、验证和提供配置。
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from .exceptions import ConfigError
from .models import Config

logger = logging.getLogger(__name__)

# 默认配置文件路径
DEFAULT_CONFIG_PATHS = [
    Path("config.json"),
    Path("~/.config/scrape/config.json"),
    Path("~/.scrape/config.json"),
]


def load_config(config_path: Path | None = None) -> Config:
    """
    加载配置

    Args:
        config_path: 配置文件路径，None 则使用默认路径

    Returns:
        Config 实例
    """
    if config_path:
        if config_path.exists():
            return _load_from_file(config_path)
        else:
            logger.warning(f"配置文件不存在，使用默认配置: {config_path}")
            return Config()

    # 尝试默认路径
    for path in DEFAULT_CONFIG_PATHS:
        expanded = path.expanduser()
        if expanded.exists():
            logger.info(f"使用配置文件: {expanded}")
            return _load_from_file(expanded)

    # 使用默认配置
    logger.info("未找到配置文件，使用默认配置")
    return Config()


def _load_from_file(path: Path) -> Config:
    """从文件加载配置"""
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return Config.from_dict(data)
    except json.JSONDecodeError as e:
        raise ConfigError(f"配置文件格式错误: {path}") from e
    except Exception as e:
        raise ConfigError(f"加载配置文件失败: {path}") from e


def save_config(config: Config, path: Path) -> None:
    """
    保存配置到文件

    Args:
        config: 配置实例
        path: 保存路径
    """
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(config.__dict__, f, indent=2, ensure_ascii=False)
        logger.info(f"配置已保存到: {path}")
    except Exception as e:
        raise ConfigError(f"保存配置文件失败: {path}") from e


def create_example_config(path: Path) -> None:
    """创建示例配置文件"""
    example = {
        "cookies_file": "cookies.txt",
        "cookies_string": None,
        "check_cookie": False,
        "cookie_keepalive_interval": 300,
        "max_retries": 3,
        "retry_delay": 1.0,
        "timeout": 30,
        "chunk_size": 8192,
        "max_concurrent": 4,
        "min_video_size": 102400,
        "min_image_size": 1024,
        "min_document_size": 512,
        "suspicious_ratio": 0.5,
        "history_max_records": 500,
        "history_file": "history.json",
        "auto_organize": True,
        "generate_report": True,
        "generate_manifest": True,
        "play_sound": True,
        "proxy": None,
        "headers": {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
        },
        "block_on_captcha": True,
        "block_on_login": True,
        "block_on_payment": True,
    }
    save_config(Config.from_dict(example), path)