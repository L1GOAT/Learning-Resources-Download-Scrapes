"""
阻断条件检测模块

检测登录墙、验证码、付费墙等阻断条件。
"""

from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING

import requests

if TYPE_CHECKING:
    from ..config import Config

logger = logging.getLogger(__name__)


def check_blockers(
    session: requests.Session,
    url: str,
    config: Config,
) -> str:
    """
    检查阻断条件

    Args:
        session: requests.Session
        url: 页面 URL
        config: 配置

    Returns:
        阻断原因，无阻断返回空字符串
    """
    try:
        resp = session.get(url, timeout=config.timeout)
        resp.raise_for_status()
        html = resp.text

        result = detect_blockers(html, url)

        if result.get("blocked"):
            reason = result.get("reason", "未知阻断条件")
            logger.warning(f"阻断检测: {reason}")
            return str(reason)

    except Exception as e:
        logger.error(f"阻断检测失败: {e}")

    return ""


def detect_blockers(html: str, url: str = "") -> dict[str, str | bool]:
    """
    检测阻断条件

    Args:
        html: 页面 HTML
        url: 页面 URL（可选，用于日志）

    Returns:
        {"blocked": bool, "reason": str}
    """
    if not html:
        return {"blocked": False, "reason": ""}

    html_lower = html.lower()

    # 检测登录墙
    login_result = _detect_login_wall(html_lower, url)
    if login_result["blocked"]:
        return login_result

    # 检测验证码
    captcha_result = _detect_captcha(html_lower, url)
    if captcha_result["blocked"]:
        return captcha_result

    # 检测付费墙
    payment_result = _detect_payment_wall(html_lower, url)
    if payment_result["blocked"]:
        return payment_result

    return {"blocked": False, "reason": ""}


def _detect_login_wall(html: str, url: str) -> dict[str, str | bool]:
    """
    检测登录墙

    Args:
        html: 页面 HTML（小写）
        url: 页面 URL

    Returns:
        检测结果
    """
    # 登录关键词
    login_keywords = [
        '请登录',
        '请先登录',
        '请先登入',
        '请登入',
        '需要登录',
        '登录后查看',
        'login required',
        'sign in required',
        'please login',
        'please sign in',
        'you need to login',
        'you need to sign in',
    ]

    for keyword in login_keywords:
        if keyword in html:
            logger.info(f"检测到登录墙 ({keyword}): {url}")
            return {"blocked": True, "reason": f"需要登录: {keyword}"}

    # 登录表单检测
    login_form_patterns = [
        r'<form[^>]*action=["\'][^"\']*login[^"\']*["\']',
        r'<form[^>]*action=["\'][^"\']*signin[^"\']*["\']',
        r'<input[^>]*name=["\']username["\']',
        r'<input[^>]*name=["\']password["\']',
        r'<input[^>]*type=["\']password["\']',
    ]

    for pattern in login_form_patterns:
        if re.search(pattern, html):
            logger.info(f"检测到登录表单: {url}")
            return {"blocked": True, "reason": "检测到登录表单"}

    return {"blocked": False, "reason": ""}


def _detect_captcha(html: str, url: str) -> dict[str, str | bool]:
    """
    检测验证码

    Args:
        html: 页面 HTML（小写）
        url: 页面 URL

    Returns:
        检测结果
    """
    # 验证码关键词
    captcha_keywords = [
        '验证码',
        '请输入验证码',
        'captcha',
        'recaptcha',
        'hcaptcha',
        'verify you are human',
        'prove you are not a robot',
        'i\'m not a robot',
    ]

    for keyword in captcha_keywords:
        if keyword in html:
            logger.info(f"检测到验证码 ({keyword}): {url}")
            return {"blocked": True, "reason": f"需要验证码: {keyword}"}

    # 验证码组件检测
    captcha_patterns = [
        r'<div[^>]*class=["\'][^"\']*captcha[^"\']*["\']',
        r'<div[^>]*id=["\'][^"\']*captcha[^"\']*["\']',
        r'<iframe[^>]*src=["\'][^"\']*recaptcha[^"\']*["\']',
        r'<script[^>]*src=["\'][^"\']*recaptcha[^"\']*["\']',
    ]

    for pattern in captcha_patterns:
        if re.search(pattern, html):
            logger.info(f"检测到验证码组件: {url}")
            return {"blocked": True, "reason": "检测到验证码组件"}

    return {"blocked": False, "reason": ""}


def _detect_payment_wall(html: str, url: str) -> dict[str, str | bool]:
    """
    检测付费墙

    Args:
        html: 页面 HTML（小写）
        url: 页面 URL

    Returns:
        检测结果
    """
    # 付费关键词
    payment_keywords = [
        '付费',
        '会员',
        'vip',
        '订阅',
        '购买',
        '解锁',
        'premium',
        'subscription',
        'pay to view',
        'paywall',
        'members only',
        'subscribe to unlock',
        'purchase required',
    ]

    for keyword in payment_keywords:
        if keyword in html:
            logger.info(f"检测到付费墙 ({keyword}): {url}")
            return {"blocked": True, "reason": f"需要付费: {keyword}"}

    # 付费按钮检测
    payment_patterns = [
        r'<button[^>]*>.*?(?:购买|开通|订阅|立即开通|立即购买).*?</button>',
        r'<a[^>]*>.*?(?:购买|开通|订阅|立即开通|立即购买).*?</a>',
        r'<div[^>]*class=["\'][^"\']*price[^"\']*["\']',
    ]

    for pattern in payment_patterns:
        if re.search(pattern, html):
            logger.info(f"检测到付费组件: {url}")
            return {"blocked": True, "reason": "检测到付费组件"}

    return {"blocked": False, "reason": ""}