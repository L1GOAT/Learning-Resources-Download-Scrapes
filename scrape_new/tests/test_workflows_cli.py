"""
平台工作流 CLI 测试

测试 platform 子命令的参数解析和调用。
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from scrape_new.cli import main, _create_parser


class TestPlatformParser:
    """测试 platform 参数解析"""

    def test_platform_chaoxing(self):
        """超星平台参数"""
        parser = _create_parser()
        args = parser.parse_args(["platform", "chaoxing", "https://example.com/course"])
        assert args.subcmd == "platform"
        assert args.platform == "chaoxing"
        assert args.platform_url == "https://example.com/course"

    def test_platform_with_output(self):
        """平台命令带输出目录"""
        parser = _create_parser()
        args = parser.parse_args(["platform", "zhihuishu", "https://example.com/course", "output"])
        assert args.platform == "zhihuishu"
        assert args.platform_url == "https://example.com/course"
        assert args.platform_output is not None

    def test_platform_all_platforms(self):
        """所有平台参数"""
        parser = _create_parser()
        for platform in ["chaoxing", "zhihuishu", "xuetangx", "icourse163"]:
            args = parser.parse_args(["platform", platform, "https://example.com"])
            assert args.platform == platform


class TestPlatformMain:
    """测试 platform 主函数"""

    @patch("scrape_new.workflows.runner.run_platform_workflow")
    def test_platform_cli_calls_runner(self, mock_runner):
        """platform 命令调用 runner"""
        mock_runner.return_value = 0
        result = main(["platform", "chaoxing", "https://example.com/course"])
        assert result == 0
        mock_runner.assert_called_once()

    @patch("scrape_new.workflows.runner.run_platform_workflow")
    def test_platform_cli_returns_runner_exit_code(self, mock_runner):
        """platform 命令返回 runner 退出码"""
        mock_runner.return_value = 1
        result = main(["platform", "chaoxing", "https://example.com/course"])
        assert result == 1


class TestPlatformRunner:
    """测试 platform runner"""

    def test_unknown_platform_fails(self):
        """未知平台返回失败"""
        from scrape_new.workflows.runner import run_platform_workflow
        result = run_platform_workflow("unknown", "https://example.com")
        assert result == 1

    def test_list_platforms(self):
        """列出所有平台"""
        from scrape_new.workflows.runner import list_platforms
        platforms = list_platforms()
        assert "chaoxing" in platforms
        assert "zhihuishu" in platforms
        assert "xuetangx" in platforms
        assert "icourse163" in platforms