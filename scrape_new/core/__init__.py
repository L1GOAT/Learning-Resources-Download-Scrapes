"""
核心模块

提供下载、校验、路径操作等核心能力。
"""

from .session import create_session, DEFAULT_UA, DEFAULT_USER_AGENT
from .cookies import load_cookies, check_cookie
from .downloader import download_file, download_many
from .hls import download_m3u8
from .verifier import verify_file
from .paths import sanitize_filename, ensure_output_dir, check_path_traversal
from .blockers import detect_blockers, check_blockers
from .notify import notify_complete

__all__ = [
    "create_session",
    "DEFAULT_UA",
    "DEFAULT_USER_AGENT",
    "load_cookies",
    "check_cookie",
    "download_file",
    "download_many",
    "download_m3u8",
    "verify_file",
    "sanitize_filename",
    "ensure_output_dir",
    "check_path_traversal",
    "detect_blockers",
    "check_blockers",
    "notify_complete",
]