"""
上传模块统一入口

提供 run_upload_cli() 函数，由 cli.py 调用。
不反向依赖 cli.py。
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

logger = logging.getLogger(__name__)


def run_upload_cli(args: list[str]) -> int:
    """
    运行上传 CLI

    Args:
        args: 命令行参数（不含 'upload' 前缀）

    Returns:
        退出码
    """
    if not args:
        logger.error("请指定子命令: build-mapping 或 upload")
        return 1

    subcmd = args[0]
    rest_args = args[1:]

    try:
        if subcmd == "build-mapping":
            return _run_build_mapping(rest_args)
        elif subcmd == "upload":
            return _run_upload(rest_args)
        elif subcmd == "exercise":
            return _run_exercise(rest_args)
        elif subcmd == "retry-failed":
            return _run_retry_failed(rest_args)
        elif subcmd == "preflight":
            return _run_preflight(rest_args)
        else:
            logger.error(f"未知子命令: {subcmd}")
            return 1
    except Exception as e:
        logger.error(f"上传命令执行失败: {e}")
        return 1


def _run_build_mapping(args: list[str]) -> int:
    """运行 build-mapping"""
    from .cli import main as upload_main
    return upload_main(["build-mapping"] + args)


def _run_upload(args: list[str]) -> int:
    """运行 upload"""
    from .cli import main as upload_main
    return upload_main(["upload"] + args)


def _run_exercise(args: list[str]) -> int:
    """运行 exercise (generate / upload)"""
    from .cli import main as upload_main
    return upload_main(["exercise"] + args)


def _run_retry_failed(args: list[str]) -> int:
    """运行 retry-failed"""
    from .cli import main as upload_main
    return upload_main(["retry-failed"] + args)


def _run_preflight(args: list[str]) -> int:
    """运行 preflight 体检报告"""
    from .cli import main as upload_main
    return upload_main(["preflight"] + args)