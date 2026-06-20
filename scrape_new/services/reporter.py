"""
报告生成模块

生成下载报告、日志、溯源 manifest。
"""

from __future__ import annotations

import csv
import json
import logging
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

from ..models import DownloadItem, DownloadResult, JobResult

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)


def save_job_report(result: JobResult, output_dir: Path) -> Path:
    """
    保存任务报告

    Args:
        result: 任务结果
        output_dir: 输出目录

    Returns:
        报告文件路径
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    report_path = output_dir / "_report.json"

    try:
        report = {
            "intent": result.intent,
            "url": result.url,
            "output_dir": str(result.output_dir),
            "found": result.found,
            "downloaded": result.downloaded,
            "failed": result.failed,
            "skipped": result.skipped,
            "suspicious": result.suspicious,
            "incomplete": result.incomplete,
            "elapsed": round(result.elapsed, 2),
            "timestamp": datetime.now().isoformat(),
            "error": result.error,
        }

        with open(report_path, "w", encoding="utf-8") as f:
            json.dump(report, f, indent=2, ensure_ascii=False)

        logger.debug(f"任务报告已保存: {report_path}")
        return report_path

    except Exception as e:
        logger.error(f"保存任务报告失败: {e}")
        return report_path


def save_download_log(results: list[DownloadResult], output_dir: Path) -> Path:
    """
    保存下载日志 CSV

    Args:
        results: 下载结果列表
        output_dir: 输出目录

    Returns:
        日志文件路径
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    log_path = output_dir / "_download_log.csv"

    try:
        with open(log_path, "w", encoding="utf-8-sig", newline="") as f:
            writer = csv.writer(f)

            # 写入表头
            writer.writerow([
                "time", "index", "filename", "url", "source_url",
                "kind", "path", "size_bytes", "size_mb", "status", "error",
            ])

            # 写入数据
            for i, r in enumerate(results, 1):
                try:
                    filename = r.item.filename or ""
                    size_mb = round(r.size_bytes / (1024 * 1024), 2) if r.size_bytes else 0

                    writer.writerow([
                        datetime.now().isoformat(),
                        i,
                        filename,
                        r.item.url,
                        r.item.source_url,
                        r.item.kind,
                        str(r.path) if r.path else "",
                        r.size_bytes,
                        size_mb,
                        r.status,
                        r.error,
                    ])
                except Exception as e:
                    logger.warning(f"写入日志行失败 [{i}]: {e}")

        logger.debug(f"下载日志已保存: {log_path}")
        return log_path

    except Exception as e:
        logger.error(f"保存下载日志失败: {e}")
        return log_path


def save_manifest(
    source_url: str,
    results: list[DownloadResult],
    output_dir: Path,
) -> Path:
    """
    保存溯源 manifest

    Args:
        source_url: 源 URL
        results: 下载结果列表
        output_dir: 输出目录

    Returns:
        manifest 文件路径
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = output_dir / "_source_manifest.json"

    try:
        manifest = {
            "source_url": source_url,
            "timestamp": datetime.now().isoformat(),
            "files": [],
        }

        for r in results:
            if r.status == "ok" and r.path:
                manifest["files"].append({
                    "filename": r.path.name,
                    "url": r.item.url,
                    "size_bytes": r.size_bytes,
                    "kind": r.item.kind,
                })

        with open(manifest_path, "w", encoding="utf-8") as f:
            json.dump(manifest, f, indent=2, ensure_ascii=False)

        logger.debug(f"溯源 manifest 已保存: {manifest_path}")
        return manifest_path

    except Exception as e:
        logger.error(f"保存 manifest 失败: {e}")
        return manifest_path


def load_failed_items(output_dir: Path) -> list[DownloadItem]:
    """
    从日志中加载失败项

    Args:
        output_dir: 输出目录

    Returns:
        失败的下载项列表
    """
    log_path = output_dir / "_download_log.csv"

    if not log_path.exists():
        logger.warning(f"下载日志不存在: {log_path}")
        return []

    items: list[DownloadItem] = []

    try:
        with open(log_path, "r", encoding="utf-8-sig") as f:
            reader = csv.DictReader(f)

            for row in reader:
                status = row.get("status", "")
                if status in ("failed", "suspicious", "incomplete"):
                    item = DownloadItem(
                        url=row.get("url", ""),
                        filename=row.get("filename"),
                        source_url=row.get("source_url", ""),
                        kind=row.get("kind", ""),
                    )
                    items.append(item)

        logger.info(f"从日志加载 {len(items)} 个失败项")
        return items

    except Exception as e:
        logger.error(f"加载失败项出错: {e}")
        return []


def summarize_results(results: list[DownloadResult]) -> dict:
    """
    汇总下载结果

    Args:
        results: 下载结果列表

    Returns:
        汇总字典
    """
    summary = {
        "total": len(results),
        "ok": 0,
        "failed": 0,
        "skipped": 0,
        "suspicious": 0,
        "incomplete": 0,
        "total_bytes": 0,
    }

    for r in results:
        summary[r.status] = summary.get(r.status, 0) + 1
        summary["total_bytes"] += r.size_bytes

    summary["total_mb"] = round(summary["total_bytes"] / (1024 * 1024), 2)

    return summary