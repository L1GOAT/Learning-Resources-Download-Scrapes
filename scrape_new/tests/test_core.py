"""
核心模块测试

测试 paths、verifier、blockers 等核心功能。
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from scrape_new.core.paths import sanitize_filename, check_path_traversal
from scrape_new.core.verifier import verify_file
from scrape_new.core.blockers import detect_blockers


class TestSanitizeFilename:
    """测试文件名清理"""

    def test_normal_filename(self):
        """正常文件名"""
        assert sanitize_filename("test.txt") == "test.txt"

    def test_empty_filename(self):
        """空文件名"""
        assert sanitize_filename("") == "unnamed"
        assert sanitize_filename(None) == "unnamed"

    def test_windows_illegal_chars(self):
        """Windows 非法字符"""
        assert '<' not in sanitize_filename('test<file>.txt')
        assert ':' not in sanitize_filename('test:file.txt')
        assert '"' not in sanitize_filename('test"file.txt')
        assert '|' not in sanitize_filename('test|file.txt')
        assert '?' not in sanitize_filename('test?file.txt')
        assert '*' not in sanitize_filename('test*file.txt')

    def test_control_chars(self):
        """控制字符"""
        result = sanitize_filename("test\x00\x1f\x7ffile.txt")
        assert '\x00' not in result
        assert '\x1f' not in result
        assert '\x7f' not in result

    def test_reserved_names(self):
        """Windows 保留文件名"""
        assert sanitize_filename("CON").startswith("_")
        assert sanitize_filename("PRN").startswith("_")
        assert sanitize_filename("AUX").startswith("_")
        assert sanitize_filename("NUL").startswith("_")
        assert sanitize_filename("COM1").startswith("_")
        assert sanitize_filename("LPT1").startswith("_")

    def test_trailing_dots_spaces(self):
        """尾部点和空格"""
        assert sanitize_filename("test...") == "test"
        assert sanitize_filename("test   ") == "test"
        assert sanitize_filename(" test.txt ") == "test.txt"

    def test_long_filename(self):
        """长文件名"""
        long_name = "a" * 300 + ".txt"
        result = sanitize_filename(long_name, max_length=200)
        assert len(result) <= 200
        assert result.endswith(".txt")

    def test_chinese_filename(self):
        """中文文件名"""
        assert sanitize_filename("测试文件.txt") == "测试文件.txt"

    def test_hash_filename(self):
        """哈希文件名"""
        hash_name = "abc123def456.txt"
        assert sanitize_filename(hash_name) == "abc123def456.txt"


class TestPathTraversal:
    """测试路径穿越防御"""

    def test_normal_path(self):
        """正常路径"""
        output_dir = Path("/tmp/output")
        filepath = output_dir / "test.txt"
        # 不应该抛出异常
        check_path_traversal(filepath, output_dir)

    def test_traversal_attack(self):
        """路径穿越攻击"""
        output_dir = Path("/tmp/output")
        filepath = output_dir / "../../../etc/passwd"
        with pytest.raises(ValueError, match="路径穿越"):
            check_path_traversal(filepath, output_dir)

    def test_absolute_path_outside(self):
        """绝对路径在外面"""
        output_dir = Path("/tmp/output")
        filepath = Path("/etc/passwd")
        with pytest.raises(ValueError, match="路径穿越"):
            check_path_traversal(filepath, output_dir)


class TestVerifyFile:
    """测试文件校验"""

    def test_missing_file(self):
        """文件不存在"""
        filepath = Path("/nonexistent/file.txt")
        assert verify_file(filepath) == "missing"

    def test_empty_file(self, tmp_path):
        """空文件"""
        filepath = tmp_path / "empty.txt"
        filepath.touch()
        assert verify_file(filepath) == "suspicious"

    def test_small_file(self, tmp_path):
        """文件过小"""
        filepath = tmp_path / "small.txt"
        filepath.write_bytes(b"x" * 100)
        assert verify_file(filepath, min_size=200) == "suspicious"

    def test_ok_file(self, tmp_path):
        """正常文件"""
        filepath = tmp_path / "ok.txt"
        filepath.write_bytes(b"x" * 1000)
        assert verify_file(filepath, min_size=100) == "ok"

    def test_incomplete_file(self, tmp_path):
        """不完整文件"""
        filepath = tmp_path / "incomplete.txt"
        filepath.write_bytes(b"x" * 100)
        assert verify_file(filepath, size_hint=1000, suspicious_ratio=0.5) == "incomplete"

    def test_ok_with_size_hint(self, tmp_path):
        """有 size_hint 的正常文件"""
        filepath = tmp_path / "ok_hint.txt"
        filepath.write_bytes(b"x" * 600)
        assert verify_file(filepath, size_hint=1000, suspicious_ratio=0.5) == "ok"


class TestDetectBlockers:
    """测试阻断条件检测"""

    def test_no_blockers(self):
        """无阻断条件"""
        html = "<html><body>正常内容</body></html>"
        result = detect_blockers(html)
        assert result["blocked"] is False

    def test_login_wall_chinese(self):
        """中文登录墙"""
        html = "<html><body>请先登录后查看</body></html>"
        result = detect_blockers(html)
        assert result["blocked"] is True
        assert "登录" in result["reason"]

    def test_login_wall_english(self):
        """英文登录墙"""
        html = "<html><body>Please login to continue</body></html>"
        result = detect_blockers(html)
        assert result["blocked"] is True
        assert "login" in result["reason"].lower()

    def test_captcha(self):
        """验证码"""
        html = '<html><body><div class="captcha">请输入验证码</div></body></html>'
        result = detect_blockers(html)
        assert result["blocked"] is True
        assert "验证码" in result["reason"]

    def test_payment_wall(self):
        """付费墙"""
        html = "<html><body>开通VIP会员查看完整内容</body></html>"
        result = detect_blockers(html)
        assert result["blocked"] is True
        assert "付费" in result["reason"] or "会员" in result["reason"]

    def test_login_form(self):
        """登录表单"""
        html = '''
        <html><body>
        <form action="/login">
            <input name="username" type="text">
            <input name="password" type="password">
        </form>
        </body></html>
        '''
        result = detect_blockers(html)
        assert result["blocked"] is True
        assert "登录表单" in result["reason"]

    def test_empty_html(self):
        """空 HTML"""
        result = detect_blockers("")
        assert result["blocked"] is False


class TestDownloadResult:
    """测试下载结果"""

    def test_download_bad_url(self, tmp_path):
        """下载不存在的 URL"""
        from scrape_new.core.downloader import download_file
        from scrape_new.core.session import create_session
        from scrape_new.config import Config
        from scrape_new.models import DownloadItem

        config = Config(max_retries=1, retry_delay=0)
        session = create_session(config)

        item = DownloadItem(
            url="http://nonexistent.example.com/file.txt",
            filename="test.txt",
        )

        result = download_file(session, item, tmp_path, config)
        assert result.status == "failed"
        assert result.error != ""

    def test_download_skips_existing(self, tmp_path):
        """跳过已存在文件"""
        from scrape_new.core.downloader import download_file
        from scrape_new.core.session import create_session
        from scrape_new.config import Config
        from scrape_new.models import DownloadItem

        config = Config()
        session = create_session(config)

        # 创建已存在文件
        filepath = tmp_path / "existing.txt"
        filepath.write_bytes(b"existing content")

        item = DownloadItem(
            url="http://example.com/file.txt",
            filename="existing.txt",
        )

        result = download_file(session, item, tmp_path, config)
        assert result.status == "skipped"
        assert result.path == filepath