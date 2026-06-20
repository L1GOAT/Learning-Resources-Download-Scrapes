"""
CLI 测试

测试命令行参数解析和函数调用。
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from scrape_new.cli import main, _create_parser


class TestCLIParser:
    """测试 CLI 参数解析"""

    def test_help(self):
        """帮助信息"""
        parser = _create_parser()
        with pytest.raises(SystemExit) as exc_info:
            parser.parse_args(["--help"])
        assert exc_info.value.code == 0

    def test_version(self):
        """版本信息"""
        parser = _create_parser()
        with pytest.raises(SystemExit) as exc_info:
            parser.parse_args(["--version"])
        assert exc_info.value.code == 0

    def test_batch_command(self):
        """批量命令"""
        parser = _create_parser()
        args = parser.parse_args(["batch", "视频", "urls.txt", "./output"])
        assert args.subcmd == "batch"
        assert args.intent == "视频"
        assert args.urls_file == Path("urls.txt")
        assert args.output == Path("./output")

    def test_history_flag(self):
        """历史标志"""
        parser = _create_parser()
        args = parser.parse_args(["--history"])
        assert args.history is True

    def test_retry_flag(self):
        """重试标志"""
        parser = _create_parser()
        args = parser.parse_args(["--retry", "./output"])
        assert args.retry == Path("./output")

    def test_test_flag(self):
        """测试标志"""
        parser = _create_parser()
        args = parser.parse_args(["--test"])
        assert args.test is True

    def test_verbose_flag(self):
        """详细模式"""
        parser = _create_parser()
        args = parser.parse_args(["-v", "--test"])
        assert args.verbose is True

    def test_config_flag(self):
        """配置文件"""
        parser = _create_parser()
        args = parser.parse_args(["-c", "config.json", "--test"])
        assert args.config == Path("config.json")

    def test_no_dedup_flag(self):
        """跳过去重"""
        parser = _create_parser()
        args = parser.parse_args(["--no-dedup", "--test"])
        assert args.no_dedup is True


class TestCLIMain:
    """测试 CLI 主函数"""

    @patch("scrape_new.cli.show_history")
    def test_history_command(self, mock_show_history):
        """历史命令"""
        result = main(["--history"])
        assert result == 0
        mock_show_history.assert_called_once()

    @patch("scrape_new.cli.retry_job")
    def test_retry_command(self, mock_retry_job):
        """重试命令"""
        mock_retry_job.return_value = MagicMock(error="", elapsed=1.0)
        result = main(["--retry", "./output"])
        assert result == 0
        mock_retry_job.assert_called_once()

    def test_keyboard_interrupt(self):
        """键盘中断"""
        with patch("scrape_new.cli._run_test", side_effect=KeyboardInterrupt):
            result = main(["--test"])
        assert result == 130