"""
应用核心

业务流程编排，唯一业务入口。
"""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import TYPE_CHECKING

from .config import load_config
from .core.session import create_session
from .core.cookies import check_cookie
from .core.blockers import check_blockers
from .core.downloader import download_many
from .exceptions import ScrapeError
from .extractors import registry
from .models import (
    BatchResult,
    ExtractContext,
    JobRequest,
    JobResult,
)
from .services.history import is_downloaded, record_download
from .services.organizer import auto_organize_job
from .services.reporter import save_download_log, save_job_report, save_manifest

if TYPE_CHECKING:
    from .models import Config

logger = logging.getLogger(__name__)


def run_job(request: JobRequest) -> JobResult:
    """
    执行单个下载任务

    Args:
        request: 作业请求

    Returns:
        作业结果
    """
    start_time = time.time()
    result = JobResult(
        intent=request.intent_desc,
        url=request.url,
        output_dir=request.output_dir or Path.cwd() / "output",
    )

    try:
        # 1. 加载配置
        config = load_config(request.config_path)
        logger.info(f"配置加载完成: {request.config_path or '默认'}")

        # 2. 创建 Session
        session = create_session(config)
        logger.info("Session 创建完成")

        # 3. 检测意图
        intent = registry.detect_intent(request.intent_desc, request.url)
        result.intent = intent
        logger.info(f"检测到意图: {intent}")

        # 4. 去重检查
        if not request.no_dedup:
            if is_downloaded(request.url, config):
                result.skipped = 1
                result.elapsed = time.time() - start_time
                logger.info(f"URL 已下载过，跳过: {request.url}")
                return result

        # 5. Cookie 检查（可选）
        if config.check_cookie:
            if not check_cookie(session, request.url):
                result.failed = 1
                result.error = "Cookie 无效或已过期"
                result.elapsed = time.time() - start_time
                logger.error(f"Cookie 检查失败: {request.url}")
                return result

        # 6. 阻断条件检查
        blocker_result = check_blockers(session, request.url, config)
        if blocker_result:
            result.failed = 1
            result.error = blocker_result
            result.elapsed = time.time() - start_time
            logger.error(f"阻断条件: {blocker_result}")
            return result

        # 7. 选择提取器并提取
        ctx = ExtractContext(
            url=request.url,
            session=session,
            config=config,
            output_dir=result.output_dir,
            course_name=request.course_name,
            chapter_list=request.chapter_list,
            extra=request.extra,
        )

        all_items = []

        if intent == "all":
            # all 模式：依次调用多个提取器
            all_extractors = ["video", "image", "document", "table", "article", "links"]
            for sub_intent in all_extractors:
                extractor = registry.get(sub_intent)
                if not extractor:
                    continue
                try:
                    logger.info(f"提取 [{sub_intent}]: {request.url}")
                    extract_result = extractor.extract(ctx)
                    all_items.extend(extract_result.items)
                    logger.info(f"[{sub_intent}] 提取到 {len(extract_result.items)} 个资源")
                except Exception as e:
                    logger.warning(f"[{sub_intent}] 提取失败: {e}")
                    continue
        else:
            extractor = registry.get(intent)
            if not extractor:
                result.failed = 1
                result.error = f"未找到提取器: {intent}"
                result.elapsed = time.time() - start_time
                logger.error(f"未找到提取器: {intent}")
                return result

            logger.info(f"开始提取: {intent}")
            extract_result = extractor.extract(ctx)
            all_items = extract_result.items

        result.found = len(all_items)
        logger.info(f"提取完成: 发现 {result.found} 个资源")

        # 8. 下载文件
        if all_items:
            download_results = download_many(
                items=all_items,
                session=session,
                output_dir=result.output_dir,
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

        # 9. 生成报告
        if config.generate_report:
            result.report_path = save_job_report(result, result.output_dir)
            save_download_log(result.results, result.output_dir)
            save_manifest(request.url, result.results, result.output_dir)
            logger.info(f"报告已保存: {result.report_path}")

        # 10. 记录历史
        if result.downloaded > 0:
            result.history_path = record_download(
                url=request.url,
                intent=result.intent,
                output_dir=result.output_dir,
                file_count=result.downloaded,
                config=config,
            )
            logger.info(f"历史已记录: {result.history_path}")

        # 11. 自动归档
        if request.auto_organize and config.auto_organize:
            organize_result = auto_organize_job(
                result=result,
                course_name=request.course_name,
                chapter_list=request.chapter_list,
            )
            if organize_result.get("errors"):
                logger.warning(f"归档有错误: {organize_result['errors']}")
            logger.info("归档完成")

        result.elapsed = time.time() - start_time
        logger.info(
            f"任务完成: 发现 {result.found}, "
            f"下载 {result.downloaded}, "
            f"失败 {result.failed}, "
            f"跳过 {result.skipped}, "
            f"可疑 {result.suspicious}, "
            f"不完整 {result.incomplete}, "
            f"耗时 {result.elapsed:.1f}s"
        )

    except ScrapeError as e:
        result.error = str(e)
        result.elapsed = time.time() - start_time
        logger.error(f"任务失败: {e}")
    except Exception as e:
        result.error = f"未知错误: {e}"
        result.elapsed = time.time() - start_time
        logger.exception(f"任务异常: {e}")

    return result


def run_batch(
    intent_desc: str,
    urls_file: Path,
    output_dir: Path | None = None,
    config_path: Path | None = None,
    no_dedup: bool = False,
) -> BatchResult:
    """
    执行批量下载任务

    Args:
        intent_desc: 意图描述
        urls_file: URL 列表文件路径
        output_dir: 输出目录
        config_path: 配置文件路径
        no_dedup: 是否跳过去重检查

    Returns:
        批量结果
    """
    start_time = time.time()
    batch_result = BatchResult()

    try:
        # 读取 URL 列表
        urls = _read_urls_file(urls_file)
        batch_result.total_urls = len(urls)
        logger.info(f"从 {urls_file} 读取到 {len(urls)} 个 URL")

        # 加载配置
        config = load_config(config_path)

        # 逐个执行
        for i, url in enumerate(urls, 1):
            logger.info(f"处理第 {i}/{len(urls)} 个 URL: {url}")

            # 创建子目录
            sub_dir = output_dir / f"{i:03d}" if output_dir else Path.cwd() / "output" / f"{i:03d}"

            request = JobRequest(
                intent_desc=intent_desc,
                url=url,
                output_dir=sub_dir,
                config_path=config_path,
                no_dedup=no_dedup,
            )

            job_result = run_job(request)
            batch_result.job_results.append(job_result)

            if job_result.error:
                batch_result.failed_urls += 1
            elif job_result.skipped > 0:
                batch_result.skipped_urls += 1
            else:
                batch_result.success_urls += 1

        batch_result.elapsed = time.time() - start_time
        logger.info(
            f"批量完成: 总计 {batch_result.total_urls}, "
            f"成功 {batch_result.success_urls}, "
            f"失败 {batch_result.failed_urls}, "
            f"跳过 {batch_result.skipped_urls}, "
            f"耗时 {batch_result.elapsed:.1f}s"
        )

    except Exception as e:
        logger.exception(f"批量任务异常: {e}")

    return batch_result


def retry_job(output_dir: Path, config_path: Path | None = None) -> JobResult:
    """
    重试失败的下载

    Args:
        output_dir: 输出目录
        config_path: 配置文件路径

    Returns:
        作业结果
    """
    from .services.retry import retry_failed_downloads
    config = load_config(config_path)
    return retry_failed_downloads(output_dir, config)


def show_history(config_path: Path | None = None) -> None:
    """显示下载历史"""
    from .services.history import list_history
    config = load_config(config_path)
    records = list_history(config)
    for r in records:
        print(f"[{r.timestamp}] {r.intent} - {r.url} ({r.file_count} files) -> {r.output_dir}")


def _read_urls_file(path: Path) -> list[str]:
    """
    读取 URL 列表文件

    Args:
        path: 文件路径

    Returns:
        URL 列表
    """
    urls = []
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                # 跳过空行和注释
                if not line or line.startswith("#"):
                    continue
                urls.append(line)
    except Exception as e:
        logger.error(f"读取 URL 文件失败: {path}: {e}")
    return urls