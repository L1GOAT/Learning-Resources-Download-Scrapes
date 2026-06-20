"""
服务层

提供历史记录、报告生成、批量下载、失败重试、自动归档等服务。
"""

from .history import is_downloaded, record_download, list_history
from .reporter import save_job_report, save_download_log, save_manifest
from .batch import run_batch_jobs
from .retry import retry_failed_downloads
from .organizer import auto_organize_job

__all__ = [
    "is_downloaded",
    "record_download",
    "list_history",
    "save_job_report",
    "save_download_log",
    "save_manifest",
    "run_batch_jobs",
    "retry_failed_downloads",
    "auto_organize_job",
]