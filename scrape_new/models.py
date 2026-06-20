"""
数据模型定义

使用 dataclasses 定义所有核心数据结构，确保类型安全和不可变性。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal


@dataclass(frozen=True)
class DownloadItem:
    """单个下载项"""
    url: str
    filename: str | None = None
    source_url: str = ""
    kind: str = "unknown"  # video, image, document, table, article, links, api
    size_hint: int = 0
    min_size: int = 0
    headers: dict[str, str] = field(default_factory=dict)
    metadata: dict[str, str] = field(default_factory=dict)


@dataclass
class DownloadResult:
    """单个下载结果"""
    item: DownloadItem
    path: Path | None = None
    status: Literal["ok", "failed", "skipped", "suspicious", "incomplete"] = "failed"
    size_bytes: int = 0
    error: str = ""


@dataclass
class ExtractResult:
    """Extractor 提取结果"""
    items: list[DownloadItem] = field(default_factory=list)
    title: str = ""
    metadata: dict[str, str] = field(default_factory=dict)


@dataclass
class JobRequest:
    """作业请求"""
    intent_desc: str
    url: str
    output_dir: Path | None = None
    config_path: Path | None = None
    auto_organize: bool = True
    no_dedup: bool = False
    course_name: str = ""
    chapter_list: list[dict[str, str]] | None = None
    extra: dict[str, str] = field(default_factory=dict)


@dataclass
class JobResult:
    """作业结果"""
    intent: str = ""
    url: str = ""
    output_dir: Path | None = None
    found: int = 0
    downloaded: int = 0
    failed: int = 0
    skipped: int = 0
    suspicious: int = 0
    incomplete: int = 0
    results: list[DownloadResult] = field(default_factory=list)
    report_path: Path | None = None
    history_path: Path | None = None
    elapsed: float = 0.0
    error: str = ""


@dataclass
class BatchResult:
    """批量下载结果"""
    total_urls: int = 0
    success_urls: int = 0
    failed_urls: int = 0
    skipped_urls: int = 0
    job_results: list[JobResult] = field(default_factory=list)
    elapsed: float = 0.0


@dataclass
class HistoryRecord:
    """历史记录"""
    url: str
    intent: str
    output_dir: str
    file_count: int
    timestamp: str
    url_hash: str = ""


@dataclass
class Config:
    """配置"""
    # Cookie 相关
    cookies_file: str | None = None
    cookies_string: str | None = None
    check_cookie: bool = False
    cookie_keepalive_interval: int = 300  # 秒

    # 下载设置
    max_retries: int = 3
    retry_delay: float = 1.0
    timeout: int = 30
    chunk_size: int = 8192
    max_concurrent: int = 4

    # 文件校验
    min_video_size: int = 100 * 1024  # 100KB
    min_image_size: int = 1024  # 1KB
    min_document_size: int = 512  # 500B
    suspicious_ratio: float = 0.5  # 实际大小 < size_hint 的 50% = 不完整

    # 历史记录
    history_max_records: int = 500
    history_file: str = "history.json"

    # 输出设置
    auto_organize: bool = True
    generate_report: bool = True
    generate_manifest: bool = True
    play_sound: bool = True

    # 代理
    proxy: str | None = None

    # 自定义 headers
    headers: dict[str, str] = field(default_factory=dict)

    # 阻断条件
    block_on_captcha: bool = True
    block_on_login: bool = True
    block_on_payment: bool = True

    @classmethod
    def from_dict(cls, data: dict[str, str]) -> Config:
        """从字典创建配置"""
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})


@dataclass
class ExtractContext:
    """Extractor 上下文"""
    url: str
    session: object  # requests.Session
    config: Config
    output_dir: Path
    course_name: str = ""
    chapter_list: list[dict[str, str]] | None = None
    extra: dict[str, str] = field(default_factory=dict)