"""
应用流程测试

测试 app.run_job 的主流程。
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from scrape_new.models import (
    Config,
    DownloadItem,
    DownloadResult,
    ExtractResult,
    JobRequest,
    JobResult,
)


@pytest.fixture
def config():
    """测试配置"""
    return Config(
        max_retries=1,
        retry_delay=0,
        timeout=5,
        check_cookie=False,
        generate_report=True,
        auto_organize=True,
    )


@pytest.fixture
def request_obj(tmp_path):
    """测试请求"""
    return JobRequest(
        intent_desc="图片",
        url="http://example.com/images",
        output_dir=tmp_path / "output",
        no_dedup=False,
    )


class TestRunJobFlow:
    """测试 run_job 流程"""

    @patch("scrape_new.app.is_downloaded")
    @patch("scrape_new.app.create_session")
    @patch("scrape_new.app.load_config")
    def test_skip_if_already_downloaded(self, mock_load_config, mock_create_session, mock_is_downloaded, request_obj):
        """已下载过的 URL 跳过"""
        from scrape_new.app import run_job

        mock_load_config.return_value = Config()
        mock_create_session.return_value = MagicMock()
        mock_is_downloaded.return_value = True

        result = run_job(request_obj)

        assert result.skipped == 1
        assert result.downloaded == 0

    @patch("scrape_new.app.is_downloaded")
    @patch("scrape_new.app.create_session")
    @patch("scrape_new.app.load_config")
    def test_no_dedup_skips_history_check(self, mock_load_config, mock_create_session, mock_is_downloaded, request_obj):
        """no_dedup=True 跳过历史检查"""
        from scrape_new.app import run_job

        request_obj.no_dedup = True
        mock_load_config.return_value = Config(check_cookie=False, generate_report=False, auto_organize=False)
        mock_create_session.return_value = MagicMock()

        # Mock extractor
        with patch("scrape_new.app.registry") as mock_registry:
            mock_extractor = MagicMock()
            mock_extractor.extract.return_value = ExtractResult(items=[])
            mock_registry.detect_intent.return_value = "image"
            mock_registry.get.return_value = mock_extractor

            with patch("scrape_new.app.check_blockers", return_value=""):
                result = run_job(request_obj)

        mock_is_downloaded.assert_not_called()

    @patch("scrape_new.app.check_cookie")
    @patch("scrape_new.app.create_session")
    @patch("scrape_new.app.load_config")
    def test_check_cookie_failure(self, mock_load_config, mock_create_session, mock_check_cookie, request_obj):
        """Cookie 检查失败"""
        from scrape_new.app import run_job

        config = Config(check_cookie=True)
        mock_load_config.return_value = config
        mock_create_session.return_value = MagicMock()
        mock_check_cookie.return_value = False

        with patch("scrape_new.app.is_downloaded", return_value=False):
            result = run_job(request_obj)

        assert result.failed == 1
        assert "Cookie" in result.error

    @patch("scrape_new.app.check_blockers")
    @patch("scrape_new.app.is_downloaded")
    @patch("scrape_new.app.create_session")
    @patch("scrape_new.app.load_config")
    def test_extractor_exception_returns_failed(self, mock_load_config, mock_create_session,
                                                 mock_is_downloaded, mock_check_blockers, request_obj):
        """提取器异常返回失败"""
        from scrape_new.app import run_job

        mock_load_config.return_value = Config(check_cookie=False, generate_report=False, auto_organize=False)
        mock_create_session.return_value = MagicMock()
        mock_is_downloaded.return_value = False
        mock_check_blockers.return_value = ""

        with patch("scrape_new.app.registry") as mock_registry:
            mock_registry.detect_intent.return_value = "image"
            mock_extractor = MagicMock()
            mock_extractor.extract.side_effect = Exception("提取失败")
            mock_registry.get.return_value = mock_extractor

            result = run_job(request_obj)

        assert result.error != "" or result.failed > 0

    @patch("scrape_new.app.save_manifest")
    @patch("scrape_new.app.save_download_log")
    @patch("scrape_new.app.save_job_report")
    @patch("scrape_new.app.download_many")
    @patch("scrape_new.app.check_blockers")
    @patch("scrape_new.app.is_downloaded")
    @patch("scrape_new.app.create_session")
    @patch("scrape_new.app.load_config")
    def test_reporter_called(self, mock_load_config, mock_create_session, mock_is_downloaded,
                             mock_check_blockers, mock_download, mock_save_report,
                             mock_save_log, mock_save_manifest, request_obj):
        """报告被调用"""
        from scrape_new.app import run_job

        config = Config(check_cookie=False, generate_report=True, auto_organize=False)
        mock_load_config.return_value = config
        mock_create_session.return_value = MagicMock()
        mock_is_downloaded.return_value = False
        mock_check_blockers.return_value = ""
        mock_save_report.return_value = Path("report.json")

        items = [DownloadItem(url="http://example.com/1.jpg", kind="image")]

        with patch("scrape_new.app.registry") as mock_registry:
            mock_registry.detect_intent.return_value = "image"
            mock_extractor = MagicMock()
            mock_extractor.extract.return_value = ExtractResult(items=items)
            mock_registry.get.return_value = mock_extractor

            mock_download.return_value = [
                DownloadResult(item=items[0], status="ok", size_bytes=1000)
            ]
            result = run_job(request_obj)

        mock_save_report.assert_called_once()
        mock_save_log.assert_called_once()
        mock_save_manifest.assert_called_once()

    @patch("scrape_new.app.auto_organize_job")
    @patch("scrape_new.app.check_blockers")
    @patch("scrape_new.app.is_downloaded")
    @patch("scrape_new.app.create_session")
    @patch("scrape_new.app.load_config")
    def test_organizer_called_when_enabled(self, mock_load_config, mock_create_session,
                                           mock_is_downloaded, mock_check_blockers,
                                           mock_organize, request_obj):
        """auto_organize=True 时调用 organizer"""
        from scrape_new.app import run_job

        config = Config(check_cookie=False, generate_report=False, auto_organize=True)
        mock_load_config.return_value = config
        mock_create_session.return_value = MagicMock()
        mock_is_downloaded.return_value = False
        mock_check_blockers.return_value = ""
        mock_organize.return_value = {"warnings": [], "errors": []}

        with patch("scrape_new.app.registry") as mock_registry:
            mock_registry.detect_intent.return_value = "image"
            mock_extractor = MagicMock()
            mock_extractor.extract.return_value = ExtractResult(items=[])
            mock_registry.get.return_value = mock_extractor

            result = run_job(request_obj)

        mock_organize.assert_called_once()