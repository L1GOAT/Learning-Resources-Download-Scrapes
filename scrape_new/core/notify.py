"""
通知模块

下载完成提示音。
"""

from __future__ import annotations

import logging
import platform
import subprocess
import sys

logger = logging.getLogger(__name__)


def notify_complete() -> None:
    """
    下载完成提示音

    失败不影响主流程。
    """
    try:
        system = platform.system()

        if system == "Windows":
            _notify_windows()
        elif system == "Darwin":
            _notify_macos()
        elif system == "Linux":
            _notify_linux()
        else:
            logger.debug(f"不支持的通知系统: {system}")

    except Exception as e:
        logger.debug(f"通知失败: {e}")


def _notify_windows() -> None:
    """Windows 通知"""
    try:
        import winsound
        winsound.MessageBeep(winsound.MB_ICONASTERISK)
    except ImportError:
        logger.debug("winsound 不可用")
    except Exception as e:
        logger.debug(f"Windows 通知失败: {e}")


def _notify_macos() -> None:
    """macOS 通知"""
    try:
        subprocess.run(
            ["afplay", "/System/Library/Sounds/Glass.aiff"],
            timeout=5,
            check=False,
            capture_output=True,
        )
    except Exception as e:
        logger.debug(f"macOS 通知失败: {e}")


def _notify_linux() -> None:
    """Linux 通知"""
    try:
        # 终端蜂鸣
        sys.stdout.write('\a')
        sys.stdout.flush()
    except Exception as e:
        logger.debug(f"Linux 通知失败: {e}")