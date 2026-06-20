"""
Cookie 管理模块

提供 Cookie 加载、检查、保活等功能。
"""

from __future__ import annotations

import json
import logging
import threading
import time
from http.cookiejar import MozillaCookieJar
from pathlib import Path
from typing import Callable

import requests

from ..exceptions import CookieError

logger = logging.getLogger(__name__)


def load_cookies(session: requests.Session, filepath: Path) -> None:
    """
    从文件加载 Cookie

    Args:
        session: requests.Session
        filepath: Cookie 文件路径（支持 .txt 和 .json）

    Raises:
        CookieError: 加载失败
    """
    if not filepath.exists():
        raise CookieError(f"Cookie 文件不存在: {filepath}")

    suffix = filepath.suffix.lower()

    try:
        if suffix == '.json':
            _load_cookies_json(session, filepath)
        else:
            _load_cookies_txt(session, filepath)
        logger.info(f"Cookie 加载成功: {filepath}")
    except CookieError:
        raise
    except Exception as e:
        raise CookieError(f"Cookie 加载失败: {e}") from e


def _load_cookies_json(session: requests.Session, filepath: Path) -> None:
    """从 JSON 文件加载 Cookie"""
    with open(filepath, 'r', encoding='utf-8') as f:
        data = json.load(f)

    if isinstance(data, list):
        # 标准格式: [{"name": "...", "value": "...", "domain": "...", ...}]
        for item in data:
            session.cookies.set(
                name=item.get('name', ''),
                value=item.get('value', ''),
                domain=item.get('domain', ''),
                path=item.get('path', '/'),
            )
    elif isinstance(data, dict):
        # 简单格式: {"name": "value", ...}
        for name, value in data.items():
            session.cookies.set(name, str(value))
    else:
        raise CookieError(f"不支持的 Cookie JSON 格式: {type(data)}")


def _load_cookies_txt(session: requests.Session, filepath: Path) -> None:
    """从 Netscape Cookie.txt 格式加载"""
    try:
        jar = MozillaCookieJar(str(filepath))
        jar.load(ignore_discard=True, ignore_expires=True)
        session.cookies.update(jar)
    except Exception as e:
        raise CookieError(f"Cookie.txt 解析失败: {e}") from e


def check_cookie(session: requests.Session, url: str) -> bool:
    """
    检查 Cookie 是否有效

    Args:
        session: requests.Session
        url: 测试 URL

    Returns:
        Cookie 是否有效
    """
    try:
        resp = session.get(url, timeout=10, allow_redirects=False)

        # 401/403 表示未授权
        if resp.status_code in (401, 403):
            logger.warning(f"Cookie 无效 (HTTP {resp.status_code}): {url}")
            return False

        # 重定向到登录页
        if resp.status_code in (301, 302):
            location = resp.headers.get('Location', '')
            if any(keyword in location.lower() for keyword in ['login', 'signin', 'auth', 'passport']):
                logger.warning(f"Cookie 无效 (重定向到登录页): {location}")
                return False

        # 检查页面内容
        if resp.status_code == 200:
            text = resp.text[:5000].lower()
            if any(keyword in text for keyword in ['请登录', '请先登录', 'login required', 'sign in']):
                logger.warning(f"Cookie 无效 (页面要求登录): {url}")
                return False

        return True

    except Exception as e:
        logger.error(f"Cookie 检查失败: {e}")
        return False


def test_auth(
    session: requests.Session,
    test_url: str,
    check_fn: Callable[[requests.Response], bool] | None = None,
) -> bool:
    """
    测试认证状态

    Args:
        session: requests.Session
        test_url: 测试 URL
        check_fn: 自定义检查函数，返回 True 表示有效

    Returns:
        认证是否有效
    """
    try:
        resp = session.get(test_url, timeout=10)

        if check_fn:
            return check_fn(resp)

        # 默认检查
        return resp.status_code == 200

    except Exception as e:
        logger.error(f"认证测试失败: {e}")
        return False


class CookieKeepalive:
    """
    Cookie 保活

    定期发送请求保持 Cookie 有效。
    """

    def __init__(
        self,
        session: requests.Session,
        url: str,
        interval: int = 300,
    ) -> None:
        """
        初始化

        Args:
            session: requests.Session
            url: 保活 URL
            interval: 间隔秒数
        """
        self._session = session
        self._url = url
        self._interval = interval
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._last_check: float = 0
        self._is_valid: bool = True

    @property
    def is_valid(self) -> bool:
        """Cookie 是否有效"""
        return self._is_valid

    def start(self) -> None:
        """启动保活线程"""
        if self._thread and self._thread.is_alive():
            return

        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._keepalive_loop,
            daemon=True,
            name="cookie-keepalive",
        )
        self._thread.start()
        logger.info(f"Cookie 保活已启动: 每 {self._interval} 秒")

    def stop(self) -> None:
        """停止保活线程"""
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=5)
        logger.info("Cookie 保活已停止")

    def _keepalive_loop(self) -> None:
        """保活循环"""
        while not self._stop_event.is_set():
            try:
                self._do_keepalive()
            except Exception as e:
                logger.error(f"Cookie 保活失败: {e}")

            # 等待下次检查
            self._stop_event.wait(self._interval)

    def _do_keepalive(self) -> None:
        """执行保活请求"""
        try:
            resp = self._session.get(self._url, timeout=10)
            self._last_check = time.time()

            if resp.status_code in (401, 403):
                self._is_valid = False
                logger.warning("Cookie 保活: Cookie 已失效")
            else:
                self._is_valid = True
                logger.debug("Cookie 保活: 正常")

        except Exception as e:
            logger.error(f"Cookie 保活请求失败: {e}")

    def check_now(self) -> bool:
        """立即检查 Cookie 状态"""
        self._do_keepalive()
        return self._is_valid