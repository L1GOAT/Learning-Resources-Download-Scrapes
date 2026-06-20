"""
服务层测试

测试 history、reporter、organizer、batch、retry 等服务功能。
"""

from __future__ import annotations

import csv
import json
from pathlib import Path

import pytest

from scrape_new.config import Config
from scrape_new.core.paths import sanitize_filename
from scrape_new.models import DownloadItem, DownloadResult, HistoryRecord, JobResult
from scrape_new.services.history import (
    url_hash,
    is_downloaded,
    record_download,
    load_history,
    save_history,
)
from scrape_new.services.reporter import (
    save_download_log,
    load_failed_items,
    summarize_results,
)
from scrape_new.services.organizer import (
    is_hash_name,
    get_file_index,
)
from scrape_new.services.batch import load_urls
from scrape_new.services.retry import load_retry_items


class TestHistory:
    """测试历史记录"""

    def test_url_hash(self):
        """URL 哈希"""
        hash1 = url_hash("http://example.com/video1.mp4")
        hash2 = url_hash("http://example.com/video1.mp4")
        hash3 = url_hash("http://example.com/video2.mp4")

        assert hash1 == hash2
        assert hash1 != hash3
        assert len(hash1) == 16

    def test_record_and_dedup(self, tmp_path):
        """记录和去重"""
        config = Config(history_file=str(tmp_path / "history.json"))

        # 第一次应该未下载
        assert is_downloaded("http://example.com/test.mp4", config) is False

        # 记录下载
        record_download(
            url="http://example.com/test.mp4",
            intent="video",
            output_dir=tmp_path / "output",
            file_count=1,
            config=config,
        )

        # 第二次应该已下载
        assert is_downloaded("http://example.com/test.mp4", config) is True

    def test_history_corrupt_backup(self, tmp_path):
        """损坏历史文件备份"""
        config = Config(history_file=str(tmp_path / "history.json"))

        # 创建损坏的文件
        history_path = tmp_path / "history.json"
        history_path.write_text("invalid json{{{", encoding="utf-8")

        # 加载应该返回空记录
        data = load_history(config)
        assert data == {"records": []}

        # 应该有备份文件
        backup_path = tmp_path / "history.corrupt"
        assert backup_path.exists()

    def test_history_max_records(self, tmp_path):
        """历史记录数量限制"""
        config = Config(
            history_file=str(tmp_path / "history.json"),
            history_max_records=5,
        )

        # 添加 10 条记录
        for i in range(10):
            record_download(
                url=f"http://example.com/video{i}.mp4",
                intent="video",
                output_dir=tmp_path / "output",
                file_count=1,
                config=config,
            )

        # 应该只保留最后 5 条
        data = load_history(config)
        assert len(data["records"]) == 5


class TestReporter:
    """测试报告生成"""

    def test_save_download_log(self, tmp_path):
        """保存下载日志"""
        results = [
            DownloadResult(
                item=DownloadItem(url="http://example.com/1.mp4", filename="1.mp4"),
                path=tmp_path / "1.mp4",
                status="ok",
                size_bytes=1024,
            ),
            DownloadResult(
                item=DownloadItem(url="http://example.com/2.mp4", filename="2.mp4"),
                status="failed",
                error="timeout",
            ),
        ]

        log_path = save_download_log(results, tmp_path)
        assert log_path.exists()

        # 验证 CSV 内容
        with open(log_path, "r", encoding="utf-8-sig") as f:
            reader = csv.DictReader(f)
            rows = list(reader)
            assert len(rows) == 2
            assert rows[0]["status"] == "ok"
            assert rows[1]["status"] == "failed"

    def test_load_failed_items(self, tmp_path):
        """加载失败项"""
        # 创建日志文件
        log_path = tmp_path / "_download_log.csv"
        with open(log_path, "w", encoding="utf-8-sig", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(["time", "index", "filename", "url", "source_url", "kind", "path", "size_bytes", "size_mb", "status", "error"])
            writer.writerow(["2024-01-01", "1", "1.mp4", "http://example.com/1.mp4", "", "video", "", "0", "0", "ok", ""])
            writer.writerow(["2024-01-01", "2", "2.mp4", "http://example.com/2.mp4", "", "video", "", "0", "0", "failed", "timeout"])
            writer.writerow(["2024-01-01", "3", "3.mp4", "http://example.com/3.mp4", "", "video", "", "0", "0", "suspicious", ""])

        items = load_failed_items(tmp_path)
        assert len(items) == 2
        assert items[0].url == "http://example.com/2.mp4"
        assert items[1].url == "http://example.com/3.mp4"

    def test_summarize_results(self):
        """汇总结果"""
        results = [
            DownloadResult(item=DownloadItem(url="1"), status="ok", size_bytes=1000),
            DownloadResult(item=DownloadItem(url="2"), status="ok", size_bytes=2000),
            DownloadResult(item=DownloadItem(url="3"), status="failed"),
            DownloadResult(item=DownloadItem(url="4"), status="skipped"),
        ]

        summary = summarize_results(results)
        assert summary["total"] == 4
        assert summary["ok"] == 2
        assert summary["failed"] == 1
        assert summary["skipped"] == 1
        assert summary["total_bytes"] == 3000


class TestOrganizer:
    """测试归档功能"""

    def test_is_hash_name(self):
        """哈希文件名判断"""
        assert is_hash_name("abc123def456.txt") is False  # 不是 32 位
        assert is_hash_name("a" * 32 + ".txt") is True
        assert is_hash_name("a" * 40 + ".txt") is True
        assert is_hash_name("normal_file.txt") is False

    def test_get_file_index(self):
        """提取文件序号"""
        assert get_file_index("001_video.mp4") == 1
        assert get_file_index("042_document.pdf") == 42
        assert get_file_index("video.mp4") is None
        assert get_file_index("abc_video.mp4") is None

    def test_sanitize_filename(self):
        """文件名清理"""
        assert sanitize_filename("test<>file.txt") == "test__file.txt"
        assert sanitize_filename("CON") == "_CON"
        assert sanitize_filename("") == "unnamed"


class TestBatch:
    """测试批量下载"""

    def test_load_urls_skip_comments(self, tmp_path):
        """跳过注释行"""
        url_file = tmp_path / "urls.txt"
        url_file.write_text(
            "# 这是注释\n"
            "http://example.com/1.mp4\n"
            "\n"
            "http://example.com/2.mp4\n"
            "# 另一个注释\n",
            encoding="utf-8",
        )

        urls = load_urls(url_file)
        assert len(urls) == 2
        assert urls[0] == "http://example.com/1.mp4"
        assert urls[1] == "http://example.com/2.mp4"

    def test_load_urls_only_http_https(self, tmp_path):
        """只接受 http/https"""
        url_file = tmp_path / "urls.txt"
        url_file.write_text(
            "http://example.com/1.mp4\n"
            "https://example.com/2.mp4\n"
            "ftp://example.com/3.mp4\n"
            "file:///local/file.txt\n",
            encoding="utf-8",
        )

        urls = load_urls(url_file)
        assert len(urls) == 2
        assert all(u.startswith("http") for u in urls)


class TestRetry:
    """测试重试功能"""

    def test_load_retry_items(self, tmp_path):
        """加载重试项"""
        # 创建日志文件
        log_path = tmp_path / "_download_log.csv"
        with open(log_path, "w", encoding="utf-8-sig", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(["time", "index", "filename", "url", "source_url", "kind", "path", "size_bytes", "size_mb", "status", "error"])
            writer.writerow(["2024-01-01", "1", "1.mp4", "http://example.com/1.mp4", "", "video", "", "1000", "0.0", "ok", ""])
            writer.writerow(["2024-01-01", "2", "2.mp4", "http://example.com/2.mp4", "", "video", "", "0", "0", "failed", "timeout"])
            writer.writerow(["2024-01-01", "3", "3.mp4", "http://example.com/3.mp4", "", "video", "", "0", "0", "incomplete", "size"])

        items = load_retry_items(tmp_path)
        assert len(items) == 2
        assert items[0].url == "http://example.com/2.mp4"
        assert items[1].url == "http://example.com/3.mp4"