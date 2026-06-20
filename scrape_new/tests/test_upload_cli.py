"""
上传 CLI 测试

测试 upload 子命令的参数解析和调用。
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from scrape_new.cli import main, _create_parser


class TestUploadParser:
    """测试 upload 参数解析"""

    def test_upload_subcmd(self):
        """upload 子命令"""
        parser = _create_parser()
        args = parser.parse_args(["upload", "build-mapping", "--videos", "./videos"])
        assert args.subcmd == "upload"
        assert "build-mapping" in args.upload_args

    def test_upload_with_args(self):
        """upload 带参数"""
        parser = _create_parser()
        args = parser.parse_args([
            "upload", "upload",
            "--mapping", "./mapping.json",
            "--cookies-file", "./cookies.txt",
        ])
        assert args.subcmd == "upload"
        assert "--mapping" in args.upload_args
        assert "./mapping.json" in args.upload_args


class TestUploadMain:
    """测试 upload 主函数"""

    @patch("scrape_new.upload.runner.run_upload_cli")
    def test_upload_cli_calls_runner(self, mock_runner):
        """upload 命令调用 runner"""
        mock_runner.return_value = 0
        result = main(["upload", "build-mapping", "--videos", "./videos"])
        assert result == 0
        mock_runner.assert_called_once()


class TestUploadRunner:
    """测试 upload runner"""

    def test_missing_subcmd_fails(self):
        """缺少子命令返回失败"""
        from scrape_new.upload.runner import run_upload_cli
        result = run_upload_cli([])
        assert result == 1

    def test_unknown_subcmd_fails(self):
        """未知子命令返回失败"""
        from scrape_new.upload.runner import run_upload_cli
        result = run_upload_cli(["unknown"])
        assert result == 1


class TestPreflightJsonOutput:
    """preflight --json 模式:stdout 只输出 JSON,日志走 stderr(机器可读)"""

    def _run_cmd_preflight_with_capture(self, mapping_path, course_id, json_mode):
        """Helper:用 contextlib 捕获 stdout/stderr,直接调 cmd_preflight。

        mock 替掉 _build_context / verify_login / get_resource_tree,不走网络。
        """
        import contextlib
        import io

        from scrape_new.upload.cli import cmd_preflight
        from argparse import Namespace
        from unittest.mock import MagicMock
        from scrape_new.upload import api_uploader

        original_ctx = api_uploader._build_context
        original_verify = api_uploader.verify_login
        original_tree = api_uploader.get_resource_tree
        api_uploader._build_context = (
            lambda session, cid: MagicMock(course_id=str(cid), session=session)
        )
        api_uploader.verify_login = lambda ctx: True
        api_uploader.get_resource_tree = lambda ctx: {"chapter_list": []}
        try:
            stdout_buf = io.StringIO()
            stderr_buf = io.StringIO()
            with contextlib.redirect_stdout(stdout_buf), \
                 contextlib.redirect_stderr(stderr_buf):
                args = Namespace(
                    mapping=str(mapping_path),
                    course_id=course_id,
                    cookies=None,
                    cookies_string="csrftoken=fake; xtbz=cloud; university_id=1",
                    output=None,
                    drift_threshold=0.6,
                    json=json_mode,
                )
                rc = cmd_preflight(args)
            return rc, stdout_buf.getvalue(), stderr_buf.getvalue()
        finally:
            api_uploader._build_context = original_ctx
            api_uploader.verify_login = original_verify
            api_uploader.get_resource_tree = original_tree

    def test_json_mode_stdout_is_pure_json(self, tmp_path):
        """Codex 复核要求:--json 时 stdout 不能混 [1/3] 等日志"""
        import json
        mapping = {
            "course_id": "TEST_JSON",
            "course_title": "JSON测试",
            "chapters": [
                {"index": 1, "title": "第一章", "lessons": [
                    {"id": "1.1", "title": "a", "content_type": "video",
                     "video": "1.1_a.mp4", "attachments": []},
                ]},
            ],
        }
        mapping_path = tmp_path / "_mapping.json"
        mapping_path.write_text(
            json.dumps(mapping, ensure_ascii=False), encoding="utf-8",
        )

        rc, stdout, stderr = self._run_cmd_preflight_with_capture(
            mapping_path, "TEST_JSON", json_mode=True,
        )
        # 空真实树 + drift 100% → HIGH → exit code 3
        # (JSON 模式返回码跟 human 模式一样,只是 stdout 形式不同)
        assert rc == 3
        # stdout 必须是纯 JSON
        out = stdout.strip()
        assert not out.startswith("[1/3]"), \
            f"stdout 头部有日志: {out[:80]!r}"
        data = json.loads(out)
        assert data["course_id"] == "TEST_JSON"
        # 进度日志应在 stderr
        assert "[1/3]" in stderr
        assert "[2/3]" in stderr

    def test_human_mode_prints_to_stdout(self, tmp_path):
        """非 --json 模式:进度日志 + 文本报告都走 stdout(给人看)"""
        import json
        mapping = {
            "course_id": "TEST_HUMAN",
            "course_title": "人类模式",
            "chapters": [
                {"index": 1, "title": "第一章", "lessons": [
                    {"id": "1.1", "title": "a", "content_type": "video",
                     "video": "1.1_a.mp4", "attachments": []},
                ]},
            ],
        }
        mapping_path = tmp_path / "_mapping.json"
        mapping_path.write_text(
            json.dumps(mapping, ensure_ascii=False), encoding="utf-8",
        )

        rc, stdout, stderr = self._run_cmd_preflight_with_capture(
            mapping_path, "TEST_HUMAN", json_mode=False,
        )
        # 空真实树 + drift 100% → HIGH → exit code 3
        assert rc == 3
        # stdout 应该是文本报告
        assert "课程体检报告" in stdout
        assert "数量对比" in stdout
        assert "HIGH" in stdout
        # 非 --json 模式,日志也走 stdout
        assert "[1/3]" in stdout
        # stderr 应为空(非 --json 模式)
        assert "[1/3]" not in stderr