"""
Session 管理模块

负责创建和配置 requests.Session。
"""

from __future__ import annotations

import logging
from pathlib import Path

import requests

from ..config import Config
from .cookies import load_cookies

logger = logging.getLogger(__name__)

# 默认 User-Agent（供 workflows 导入使用）
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)

# 兼容旧代码导出名
DEFAULT_UA = DEFAULT_USER_AGENT


def create_session(config: Config) -> requests.Session:
    """
    创建 Session

    Args:
        config: 配置

    Returns:
        配置好的 requests.Session
    """
    session = requests.Session()

    # 设置 headers
    headers = {
        "User-Agent": DEFAULT_USER_AGENT,
        "Accept": "*/*",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    }
    headers.update(config.headers)
    session.headers.update(headers)

    # 设置代理
    if config.proxy:
        session.proxies = {
            "http": config.proxy,
            "https": config.proxy,
        }
        logger.info(f"使用代理: {config.proxy}")

    # 设置超时
    session.timeout = config.timeout

    # 加载 Cookie
    if config.cookies_string:
        _load_cookies_from_string(session, config.cookies_string)
    elif config.cookies_file:
        cookie_path = Path(config.cookies_file)
        if cookie_path.exists():
            try:
                load_cookies(session, cookie_path)
            except Exception as e:
                logger.warning(f"Cookie 加载失败: {e}")
        else:
            logger.debug(f"Cookie 文件不存在: {cookie_path}")

    return session


def _load_cookies_from_string(session: requests.Session, cookies_string: str) -> None:
    """
    从字符串加载 Cookie

    Args:
        session: requests.Session
        cookies_string: Cookie 字符串（name1=value1; name2=value2; ...）
    """
    try:
        for item in cookies_string.split(';'):
            item = item.strip()
            if '=' in item:
                name, value = item.split('=', 1)
                session.cookies.set(name.strip(), value.strip())
        logger.info("Cookie 从字符串加载成功")
    except Exception as e:
        logger.warning(f"Cookie 字符串解析失败: {e}")