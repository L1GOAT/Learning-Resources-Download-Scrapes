"""
失败重试模块

从下载日志中读取失败项，重新下载。
"""

from __future__ import annotations

import logging
import time
from pathlib import Path

import requests

from ..config import Config
from ..core.downloader import download_many
from ..core.session import create_session
from ..models import DownloadItem, DownloadResult, JobResult
from .reporter import load_failed_items, save_download_log, save_job_report

logger = logging.getLogger(__name__)


def find_download_log(output_dir: Path) -> Path | None:
    """
    查找下载日志文件

    Args:
        output_dir: 输出目录

    Returns:
        日志文件路径，未找到返回 None
    """
    # 优先查找当前目录
    log_path = output_dir / "_download_log.csv"
    if log_path.exists():
        return log_path

    # 在一级子目录中查找
    for sub_dir in output_dir.iterdir():
        if sub_dir.is_dir():
            log_path = sub_dir / "_download_log.csv"
            if log_path.exists():
                return log_path

    return None


def load_retry_items(output_dir: Path) -> list[DownloadItem]:
    """
    加载需要重试的下载项

    Args:
        output_dir: 输出目录

    Returns:
        下载项列表
    """
    log_path = find_download_log(output_dir)

    if not log_path:
        logger.warning(f"未找到下载日志: {output_dir}")
        return []

    return load_failed_items(log_path.parent)


def retry_failed_downloads(
    output_dir: Path,
    config: Config,
    session: requests.Session | None = None,
) -> JobResult:
    """
    重试失败的下载

    Args:
        output_dir: 输出目录
        config: 配置
        session: requests.Session（可选）

    Returns:
        任务结果
    """
    start_time = time.time()
    result = JobResult(
        intent="retry",
        url="",
        output_dir=output_dir,
    )

    try:
        # 加载失败项
        items = load_retry_items(output_dir)
        result.found = len(items)

        if not items:
            logger.info("无失败项需要重试")
            result.elapsed = time.time() - start_time
            return result

        logger.info(f"开始重试 {len(items)} 个失败项")

        # 创建 Session
        if session is None:
            session = create_session(config)

        # 重新下载
        download_results = download_many(
            session=session,
            items=items,
            output_dir=output_dir,
            config=config,
        )

        result.results = download_results

        # 统计结果
        for dr in download_results:
            if dr.status == "ok":
                result.downloaded += 1
            elif dr.status == "failed":
                result.failed += 1
            elif dr.status == "skipped":
                result.skipped += 1
            elif dr.status == "suspicious":
                result.suspicious += 1
            elif dr.status == "incomplete":
                result.incomplete += 1

        # 保存报告
        if config.generate_report:
            result.report_path = save_job_report(result, output_dir)
            save_download_log(download_results, output_dir)

        result.elapsed = time.time() - start_time

        logger.info(
            f"重试完成: 下载 {result.downloaded}, "
            f"失败 {result.failed}, "
            f"跳过 {result.skipped}, "
            f"耗时 {result.elapsed:.1f}s"
        )

    except Exception as e:
        result.error = f"重试异常: {e}"
        result.elapsed = time.time() - start_time
        logger.exception(f"重试失败: {e}")

    return result