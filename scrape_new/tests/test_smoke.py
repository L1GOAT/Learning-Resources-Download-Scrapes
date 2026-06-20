"""
冒烟测试

验证项目基本功能，不访问公网。
"""

from __future__ import annotations

from pathlib import Path

import pytest


class TestSmokeImports:
    """测试模块导入"""

    def test_import_scrape_new(self):
        """导入主模块"""
        import scrape_new
        assert hasattr(scrape_new, "__version__")

    def test_import_core(self):
        """导入核心模块"""
        from scrape_new.core import session, cookies, downloader, hls, verifier, paths, blockers, notify
        assert session is not None
        assert cookies is not None
        assert downloader is not None

    def test_import_core_default_ua(self):
        """导入 DEFAULT_UA"""
        from scrape_new.core import DEFAULT_UA
        assert isinstance(DEFAULT_UA, str)
        assert len(DEFAULT_UA) > 0

    def test_import_extractors(self):
        """导入提取器模块"""
        from scrape_new.extractors import video, image, document, table, article, links, api
        assert video is not None
        assert image is not None
        assert document is not None

    def test_import_services(self):
        """导入服务模块"""
        from scrape_new.services import history, reporter, organizer, batch, retry
        assert history is not None
        assert reporter is not None
        assert organizer is not None

    def test_import_workflows_runner(self):
        """导入工作流 runner"""
        from scrape_new.workflows.runner import run_platform_workflow, list_platforms
        assert run_platform_workflow is not None
        assert list_platforms is not None

    def test_import_upload_runner(self):
        """导入上传 runner"""
        from scrape_new.upload.runner import run_upload_cli
        assert run_upload_cli is not None

    def test_import_models(self):
        """导入数据模型"""
        from scrape_new.models import Config, JobRequest, JobResult, DownloadItem, DownloadResult
        assert Config is not None
        assert JobRequest is not None
        assert JobResult is not None

    def test_import_cli(self):
        """导入 CLI"""
        from scrape_new.cli import main, _create_parser
        assert main is not None
        assert _create_parser is not None


class TestSmokeConfig:
    """测试配置"""

    def test_config_example_loads(self):
        """示例配置文件可加载"""
        from scrape_new.config import load_config
        config_path = Path(__file__).parent.parent.parent / "config.example.json"
        if config_path.exists():
            config = load_config(config_path)
            assert config.max_retries == 3
            assert config.timeout == 30

    def test_default_config(self):
        """默认配置"""
        from scrape_new.models import Config
        config = Config()
        assert config.max_retries == 3
        assert config.check_cookie is False
        assert config.auto_organize is True


class TestSmokeCLI:
    """测试 CLI"""

    def test_parser_creation(self):
        """解析器创建"""
        from scrape_new.cli import _create_parser
        parser = _create_parser()
        assert parser is not None

    def test_version(self):
        """版本号"""
        import scrape_new
        assert scrape_new.__version__ == "0.2.0"