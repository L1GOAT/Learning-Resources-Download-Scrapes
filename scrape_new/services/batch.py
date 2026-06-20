"""
批量下载模块

从文件读取 URL 列表，逐个执行下载任务。
"""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Callable

from ..models import BatchResult, JobRequest, JobResult

logger = logging.getLogger(__name__)


def load_urls(file_path: Path) -> list[str]:
    """
    从文件加载 URL 列表

    Args:
        file_path: 文件路径

    Returns:
        URL 列表
    """
    if not file_path.exists():
        logger.error(f"URL 文件不存在: {file_path}")
        return []

    urls: list[str] = []

    try:
        with open(file_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()

                # 跳过空行和注释
                if not line or line.startswith("#"):
                    continue

                # 只接受 http/https
                if line.startswith("http://") or line.startswith("https://"):
                    urls.append(line)
                else:
                    logger.warning(f"跳过非 http/https URL: {line}")

    except Exception as e:
        logger.error(f"读取 URL 文件失败: {e}")

    logger.info(f"从 {file_path} 加载 {len(urls)} 个 URL")
    return urls


def run_batch_jobs(
    intent: str,
    url_file: Path,
    output_dir: Path | None = None,
    config_path: Path | None = None,
    run_job_func: Callable[[JobRequest], JobResult] | None = None,
) -> BatchResult:
    """
    执行批量下载任务

    Args:
        intent: 意图描述
        url_file: URL 文件路径
        output_dir: 输出目录
        config_path: 配置文件路径
        run_job_func: 任务执行函数（避免循环导入）

    Returns:
        批量结果
    """
    start_time = time.time()
    batch_result = BatchResult()

    # 加载 URL
    urls = load_urls(url_file)
    batch_result.total_urls = len(urls)

    if not urls:
        logger.warning("无有效 URL")
        return batch_result

    # 懒加载 run_job 避免循环导入
    if run_job_func is None:
        from ..app import run_job as _run_job
        run_job_func = _run_job

    # 逐个执行
    for i, url in enumerate(urls, 1):
        logger.info(f"处理 [{i}/{len(urls)}]: {url}")

        # 创建子目录
        if output_dir:
            sub_dir = output_dir / f"{i:03d}"
        else:
            sub_dir = Path.cwd() / "output" / f"{i:03d}"

        request = JobRequest(
            intent_desc=intent,
            url=url,
            output_dir=sub_dir,
            config_path=config_path,
        )

        try:
            job_result = run_job_func(request)
            batch_result.job_results.append(job_result)

            if job_result.error:
                batch_result.failed_urls += 1
            elif job_result.skipped > 0:
                batch_result.skipped_urls += 1
            else:
                batch_result.success_urls += 1

        except Exception as e:
            logger.error(f"任务执行失败: {url}: {e}")
            batch_result.failed_urls += 1

    batch_result.elapsed = time.time() - start_time

    logger.info(
        f"批量完成: 总计 {batch_result.total_urls}, "
        f"成功 {batch_result.success_urls}, "
        f"失败 {batch_result.failed_urls}, "
        f"跳过 {batch_result.skipped_urls}, "
        f"耗时 {batch_result.elapsed:.1f}s"
    )

    return batch_result