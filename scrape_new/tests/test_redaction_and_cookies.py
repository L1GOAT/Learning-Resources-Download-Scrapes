"""
redaction + chaoxing --cookies-file 测试。

所有测试:
  - 不用真 cookie
  - tmp_path 写临时 cookie 文件
  - 不读 cookie 内容到测试日志
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from scrape_new.services.redaction import redact_sensitive
from scrape_new.tests._paths import PROJECT_ROOT


# ─── 1-9: redact_sensitive 各字段 ─────────────────────────

class TestRedactionPatterns:
    """覆盖你列的 13 个字段 + 边界。"""

    def test_sessionid(self):
        out = redact_sensitive("sessionid=abc123def456; csrftoken=xyz")
        assert "abc123def456" not in out
        assert "sessionid=[REDACTED]" in out

    def test_csrftoken(self):
        out = redact_sensitive("csrftoken=xyz789;")
        assert "xyz789" not in out
        assert "csrftoken=[REDACTED]" in out

    def test_p_auth_token(self):
        out = redact_sensitive("p_auth_token=eyJhbGc.payload.sig;")
        assert "eyJhbGc" not in out
        assert "p_auth_token=[REDACTED]" in out

    def test_vc3(self):
        out = redact_sensitive("vc3=TFXtNDLu; UID=61822056")
        assert "TFXtNDLu" not in out
        assert "vc3=[REDACTED]" in out

    def test_uf(self):
        out = redact_sensitive("uf=da0883eb5260151; jrose=BB591FD33")
        assert "da0883eb5260151" not in out
        assert "uf=[REDACTED]" in out

    def test_uid_and_uid(self):
        out = redact_sensitive("_uid=61822056; UID=61822056; route=f9c31469")
        assert "61822056" not in out
        assert "_uid=[REDACTED]" in out
        assert "UID=[REDACTED]" in out
        assert "route=[REDACTED]" in out

    def test_cx_p_token(self):
        out = redact_sensitive("cx_p_token=cd5e97403de2; DSSTASH_LOG=C_38")
        assert "cd5e97403de2" not in out
        assert "cx_p_token=[REDACTED]" in out

    def test_fanyamoocs_and_jrose(self):
        out = redact_sensitive("fanyamoocs=3C17E14A557B558411401F; jrose=BB591FD3322807E8AA94B")
        assert "3C17E14A557B558411401F" not in out
        assert "fanyamoocs=[REDACTED]" in out
        assert "BB591FD3322807E8AA94B" not in out

    def test_cookie_header(self):
        out = redact_sensitive("Cookie: k8s=1779102626; vc3=TFXtNDLu")
        assert "k8s=1779102626" not in out
        assert "TFXtNDLu" not in out
        assert "Cookie: [REDACTED]" in out

    def test_xtbz_cookie(self):
        # 规则只脱敏 XTBZ_COOKIE 自己的值。
        # 它**不**连带脱敏里面嵌的 k8s= / vc3= (那是别人的事)。
        # 所以测试只验证 XTBZ_COOKIE 后面那一长串被脱敏。
        out = redact_sensitive('XTBZ_COOKIE="k8s=1779102626; vc3=TFXtNDLu"')
        # XTBZ_COOKIE 后面到引号结束应该是 [REDACTED]
        # 即原文 "XTBZ_COOKIE=\"...\"" 中 "..." 部分被 [REDACTED] 替换
        # 检查: 等号后到第一个引号全是 [REDACTED]
        assert "XTBZ_COOKIE=[REDACTED]" in out
        # 验证"XTBZ_COOKIE= 到引号前不再含真实 cookie 值":
        # 抽引号内部分, 应该是 [REDACTED]
        import re
        m = re.search(r'XTBZ_COOKIE=([^"\s]+)', out)
        assert m is not None
        # m.group(1) 应该是 [REDACTED] 或 "[REDACTED]" 之类
        assert "1779102626" not in m.group(1)

    def test_authorization_header(self):
        out = redact_sensitive("Authorization: Bearer eyJhbGciOi.payload.sig")
        assert "eyJhbGciOi" not in out
        assert "Authorization: [REDACTED]" in out

    def test_access_token_and_refresh_token(self):
        out = redact_sensitive("?access_token=abc&refresh_token=xyz&other=keep")
        assert "abc" not in out
        assert "xyz" not in out
        assert "other=keep" in out  # 不误伤

    def test_jsessionid_and_study_sess(self):
        out = redact_sensitive("JSESSIONID=ABCDEF; STUDY_SESS=foo; NTES_SESS=bar")
        assert "ABCDEF" not in out
        assert "foo" not in out
        assert "bar" not in out
        assert "JSESSIONID=[REDACTED]" in out
        assert "STUDY_SESS=[REDACTED]" in out
        assert "NTES_SESS=[REDACTED]" in out

    def test_token_generic_does_not_overmatch(self):
        """token= 必须紧接 ? / & / ; 头, 不匹配普通单词如 tokenization / 空格后 token="""
        # 1. 空格后 token= — 应该不匹配(避免误伤 "is not token=...")
        out = redact_sensitive("tokenization is not token=secret_value")
        # "tokenization" 完整保留(普通词)
        assert "tokenization" in out
        # "is not token=" 因为前一个字符是空格, 规则不匹配,
        # 所以 secret_value **可能**保留 — 我们只验证"tokenization 没被误伤"
        # 真正要脱敏的场景是 URL query string (前面是 ? 或 &)

        # 2. URL query string 场景 — 应该匹配
        out2 = redact_sensitive("https://api.example.com?token=secret_value&other=keep")
        assert "secret_value" not in out2
        assert "other=keep" in out2  # 不误伤 other=

        # 3. & 分隔
        out3 = redact_sensitive("; token=secret_value; foo=bar")
        assert "secret_value" not in out3
        assert "foo=bar" in out3

    def test_empty_and_none(self):
        assert redact_sensitive("") == ""
        assert redact_sensitive(None) == ""


# ─── 10: chaoxing.py --cookies-file 能被解析 ─────────────────────────

class TestChaoxingCookiesFile:
    def test_cookies_file_arg_parsing(self, tmp_path, monkeypatch):
        """--cookies-file <path> 应被 main 识别, 不要求文件存在也能 parse 阶段过

        实际 scan 跑用真 cookie 文件(用户给), 测试只验证 arg parse 逻辑不抛。
        """
        cookie_file = tmp_path / "fake_cookie.txt"
        cookie_file.write_text(
            "k8s=12345; route=abc; vc3=test; UID=test; _uid=test; "
            "sessionid=test; csrftoken=test; fanyamoocs=test; "
            "jrose=test; cx_p_token=test; p_auth_token=test; uf=test",
            encoding="utf-8",
        )
        # 直接调 main 调 _load_cookies 测试
        import requests
        from scrape_new.workflows.chaoxing import load_cookies
        session = requests.Session()
        # 不会 cat / print cookie 内容, 只 verify session.cookies count
        load_cookies(session, "/nonexistent/cookies.txt", cookies_file=str(cookie_file))
        assert len(session.cookies) > 0, "session 应该有 cookie"

    def test_cookies_file_nonexistent_exits_cleanly(self, tmp_path, monkeypatch):
        """--cookies-file 路径不存在, 报错 + sys.exit(1)"""
        import requests
        from scrape_new.workflows import chaoxing
        session = requests.Session()
        with pytest.raises(SystemExit) as exc:
            chaoxing.load_cookies(session, "/nonexistent", cookies_file=str(tmp_path / "no_such.txt"))
        assert exc.value.code == 1

    def test_cookies_file_priority_over_env(self, tmp_path, monkeypatch):
        """--cookies-file 优先级 > XTBZ_COOKIE env

        验证: 设了 env + file, file 胜出(通过 cookie 字段数差异或特定值)。
        """
        cookie_file = tmp_path / "fake_cookie.txt"
        # file 含 5 个字段
        cookie_file.write_text("k8s=file1; route=file2; vc3=file3; UID=file4; _uid=file5", encoding="utf-8")
        # env 含 3 个不同字段
        monkeypatch.setenv("XTBZ_COOKIE", "sessionid=env1; csrftoken=env2; p_auth_token=env3")

        import requests
        from scrape_new.workflows.chaoxing import load_cookies
        session = requests.Session()
        load_cookies(session, "/nonexistent", cookies_file=str(cookie_file))
        # 期望 session 包含 file 的 5 个字段
        cookie_names = {c.name for c in session.cookies}
        assert "k8s" in cookie_names
        assert "vc3" in cookie_names
        # 不应包含 env 的字段(因为 file 优先, env 没读)
        assert "sessionid" not in cookie_names

    def test_cookies_file_priority_over_default_filepath(self, tmp_path, monkeypatch):
        """--cookies-file 优先级 > 默认 cookies.txt 路径

        验证: file 存在, 默认 cookies.txt 存在(都设了), file 胜出
        """
        # 1) file
        cookie_file = tmp_path / "external.txt"
        cookie_file.write_text("k8s=from_file; vc3=from_file", encoding="utf-8")
        # 2) 默认 cookies.txt
        default_file = tmp_path / "cookies.txt"
        default_file.write_text("UID=from_default; _uid=from_default", encoding="utf-8")

        import requests
        from scrape_new.workflows.chaoxing import load_cookies
        session = requests.Session()
        load_cookies(session, str(default_file), cookies_file=str(cookie_file))
        cookie_names = {c.name for c in session.cookies}
        assert "k8s" in cookie_names
        assert "vc3" in cookie_names
        # 默认文件不应被读
        assert "UID" not in cookie_names

    def test_no_cookies_file_no_env_no_default_exits(self, tmp_path):
        """三个都没有 → 报错 exit 1"""
        import requests
        from scrape_new.workflows import chaoxing
        session = requests.Session()
        with pytest.raises(SystemExit) as exc:
            chaoxing.load_cookies(session, str(tmp_path / "no_default.txt"))
        assert exc.value.code == 1

    def test_xor_env_fallback(self, tmp_path, monkeypatch):
        """没 --cookies-file 但有 XTBZ_COOKIE env, 走 env(兼容旧行为)"""
        monkeypatch.setenv("XTBZ_COOKIE", "k8s=from_env; vc3=from_env")
        import requests
        from scrape_new.workflows.chaoxing import load_cookies
        session = requests.Session()
        load_cookies(session, str(tmp_path / "no_default.txt"))
        cookie_names = {c.name for c in session.cookies}
        assert "k8s" in cookie_names
        assert "vc3" in cookie_names


# ─── 11: cookie 不出现在 subprocess stdout/stderr ─────────────────────────

class TestChaoxingCLICookieLeak:
    """端到端: 跑一次 chaoxing.py subprocess, 验证 cookie 不出现在 captured output。"""

    def test_chaoxing_subprocess_does_not_print_cookie(self, tmp_path):
        """写一个外部 cookie 文件, 跑 chaoxing.py subprocess,
        抓 stdout/stderr, 用 redact_sensitive 再过一遍, 确认 cookie 不外泄。

        注意: chaoxing 真 scan 跑会失败(URL 不对 / 反爬), 但即使失败, 也
        不应该 print 完整 cookie。
        """
        cookie_file = tmp_path / "scrape_cookie.txt"
        # 写一些"标志性" cookie 值, 含明文 session id
        SENSITIVE_SESSION_ID = "SENTINEL_SESSION_ID_abc123"
        SENSITIVE_VC3 = "SENTINEL_VC3_xyz789"
        cookie_file.write_text(
            f"sessionid={SENSITIVE_SESSION_ID}; "
            f"vc3={SENSITIVE_VC3}; "
            f"csrftoken=SENTINEL_CSRF; "
            f"p_auth_token=SENTINEL_PAT; "
            f"UID=61822056; _uid=61822056; "
            f"k8s=1779102626; "
            f"fanyamoocs=SENTINEL_FANYA; "
            f"jrose=SENTINEL_JROSE; "
            f"cx_p_token=SENTINEL_CXP",
            encoding="utf-8",
        )
        # 跑 subprocess, 用占位 URL(会失败, 但应不打印 cookie)
        cmd = [
            "X:/Python/Python3.10.11/python.exe",
            "-X", "utf8",
            "-m", "scrape_new.workflows.chaoxing",
            "https://mooc2-ans.chaoxing.com/INVALID",
            str(tmp_path / "out"),
            "--scan-only",
            "--max-tabs", "2",
            "--cookies-file", str(cookie_file),
        ]
        env = {
            "PYTHONIOENCODING": "utf-8",
            "PYTHONUTF8": "1",
            "PATH": "/c/Windows/System32:/c/Windows:/c/Windows/System32/Wbem:/usr/bin:/mingw64/bin",
            "SYSTEMROOT": "C:/Windows",
            "TEMP": str(tmp_path),
            "TMP": str(tmp_path),
        }
        proc = subprocess.run(
            cmd, capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=120, env=env, cwd=str(PROJECT_ROOT),
        )
        combined = proc.stdout + "\n" + proc.stderr

        # 原始 output(redact 前)应**不**含任何 SENTINEL 值
        for sentinel in [
            SENSITIVE_SESSION_ID, SENSITIVE_VC3,
            "SENTINEL_CSRF", "SENTINEL_PAT",
            "SENTINEL_FANYA", "SENTINEL_JROSE", "SENTINEL_CXP",
            "1779102626",  # k8s
        ]:
            assert sentinel not in combined, (
                f"cookie '{sentinel}' 出现在 subprocess output! 位置: "
                f"...{combined[max(0, combined.find(sentinel)-50):combined.find(sentinel)+100]}..."
            )

        # redact 之后再确认也没(冗余保险)
        redacted = redact_sensitive(combined)
        for sentinel in [
            SENSITIVE_SESSION_ID, SENSITIVE_VC3,
            "SENTINEL_CSRF", "SENTINEL_PAT",
        ]:
            assert sentinel not in redacted

        # 反过来: redact 后应该出现 [REDACTED] 标记 — 但这要求 output 原本
        # 含 cookie 字段。如果原本就 redact 过, output 里就不会有完整 cookie,
        # 也就不会有 [REDACTED] 标记。这是合理 — 我们只要求 "不外泄", 不要求
        # "有 redact 痕迹"。

    def test_chaoxing_with_nonexistent_cookies_file_exits_clean(self, tmp_path):
        """--cookies-file 不存在 → exit 1, 不打印 cookie 内容(也没东西可打印)

        用合法 URL(带 courseId/clazzid) 才能到 load_cookies 那步。
        """
        cmd = [
            "X:/Python/Python3.10.11/python.exe",
            "-X", "utf8",
            "-m", "scrape_new.workflows.chaoxing",
            # 合法 URL: 有 courseId + clazzid
            "https://mooc2-ans.chaoxing.com/mycourse/tchcourse?courseid=999999&clazzid=999999",
            str(tmp_path / "out"),
            "--scan-only",
            "--cookies-file", str(tmp_path / "no_such.txt"),
        ]
        env = {
            "PYTHONIOENCODING": "utf-8",
            "PYTHONUTF8": "1",
            "PATH": "/c/Windows/System32:/c/Windows:/c/Windows/System32/Wbem:/usr/bin:/mingw64/bin",
            "SYSTEMROOT": "C:/Windows",
            "TEMP": str(tmp_path),
            "TMP": str(tmp_path),
        }
        proc = subprocess.run(
            cmd, capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=60, env=env, cwd=str(PROJECT_ROOT),
        )
        assert proc.returncode == 1
        combined = proc.stdout + "\n" + proc.stderr
        # 应有"找不到 --cookies-file" 或类似错误
        assert "找不到" in combined or "--cookies-file" in combined
        # 不应含任何 SENTINEL 字符串(根本没读到 cookie)
        assert "SENTINEL" not in combined

# ─── 12-15: warmup + 400 fallback 行为 ─────────────────────────

class TestChaoxingWarmupAndFallback:
    """teacherstudycourselist 返 400 时的行为: warmup + 完整 headers + fallback debug 文件。"""

    def test_warmup_makes_two_gets_before_chapter_tree(self, monkeypatch):
        """_warmup_session 跑 2 个 GET(原始 URL + tchcourse)"""
        import requests
        from scrape_new.workflows.chaoxing import _warmup_session

        # mock 真实 session
        seen_urls = []

        class _FakeResp:
            def __init__(self, url, status=200, text="<html>warmup</html>"):
                self.url = url
                self.status_code = status
                self.text = text

        class _FakeSession:
            trust_env = True
            proxies = {}
            def get(self, url, timeout=None, allow_redirects=True, headers=None):
                seen_urls.append(url)
                return _FakeResp(url, status=200)

        fake = _FakeSession()
        result = _warmup_session(
            fake,
            "https://mooc2-ans.chaoxing.com/mycourse/teacherstudy?chapterId=111&courseId=999&clazzid=888",
            "888",
        )
        # 应至少 2 个 GET
        assert len(seen_urls) >= 2, f"应该至少 2 个 GET(原始 URL + tchcourse), 实际 {len(seen_urls)}: {seen_urls}"
        # 第一个是用户给的 URL
        assert "courseId=999" in seen_urls[0]
        # 第二个是 tchcourse
        assert "tchcourse" in seen_urls[1] or any("tchcourse" in u for u in seen_urls[1:])
        # 至少 1 个成功 → 返回 Referer
        assert result

    def test_warmup_disables_proxy_env(self):
        """_warmup_session 设 trust_env=False + proxies={None}"""
        import requests
        from scrape_new.workflows.chaoxing import _warmup_session

        class _FakeResp:
            status_code = 200
            text = ""
            def __init__(self, url): self.url = url

        class _FakeSession:
            trust_env = True
            proxies = {"http": "old-proxy", "https": "old-proxy"}
            def get(self, url, **kwargs):
                return _FakeResp(url)

        fake = _FakeSession()
        _warmup_session(fake, "https://example.com/?courseId=1&clazzid=1", "1")
        assert fake.trust_env is False
        # proxies 应被改成 {"http": None, "https": None}
        assert fake.proxies.get("http") is None
        assert fake.proxies.get("https") is None

    def test_get_chapter_tree_400_saves_redacted_debug(self, tmp_path, monkeypatch):
        """teacherstudycourselist 返 400 → 写 _chaoxing_tree_debug.html, 内容脱敏"""
        from scrape_new.workflows import chaoxing

        # mock session: 第一次 GET(tchcourse warmup) OK, 第二次 GET(teacherstudycourselist) 400
        class _FakeResp:
            def __init__(self, status=200, text=""):
                self.status_code = status
                self.text = text
            def __init__(self, status=200, text=""):  # noqa
                self.status_code = status
                self.text = text

        seen = []

        def fake_get(self, url, timeout=None, allow_redirects=True, headers=None):
            seen.append((url, headers))
            if "tchcourse" in url:
                return _FakeResp(200, "<html>tchcourse warmup ok</html>")
            if "teacherstudycourselist" in url:
                # 400 HTML 含**模拟** cookie 字段 — 验证脱敏
                return _FakeResp(400, (
                    "Bad Request <html>script>Cookie: vc3=12345; sessionid=abc; p_auth_token=xyz</script></html>"
                ))
            return _FakeResp(200, "")

        # 准备 output_dir 让 debug 文件能写
        out_dir = tmp_path / "out"
        out_dir.mkdir()

        # mock get_chapter_tree 的 URL, 让 debug 路径 = out_dir
        # (Path(course_url).parent 在测试里是空, 我们用 monkeypatch 替换)

        import requests
        session = requests.Session()
        monkeypatch.setattr(session, "get", fake_get.__get__(session))

        lessons = chaoxing.get_chapter_tree(
            session, "999", "888",
            course_url=str(out_dir / "fake_url"),
            output_dir=str(out_dir),
            debug=False,
        )
        # 返空 lessons
        assert lessons == []
        # debug 文件应被写
        debug_html = out_dir / "_chaoxing_tree_debug.html"
        assert debug_html.exists(), f"debug HTML 应被写, 但 {debug_html} 不存在"
        # 内容应**不**含明文 cookie 字段
        content = debug_html.read_text(encoding="utf-8")
        assert "vc3=12345" not in content, "vc3=12345 出现在 debug HTML(未脱敏)"
        assert "sessionid=abc" not in content
        assert "p_auth_token=xyz" not in content
        # 应有 [REDACTED] 痕迹
        assert "[REDACTED]" in content

    def test_get_chapter_tree_200_no_ul_also_writes_debug(self, tmp_path):
        """teacherstudycourselist 返 200 但 HTML 没 <ul> — 也要写脱敏 debug"""
        from scrape_new.workflows import chaoxing
        import requests

        class _FakeResp:
            def __init__(self, status, text):
                self.status_code = status
                self.text = text

        def fake_get(self, url, **kwargs):
            if "tchcourse" in url:
                return _FakeResp(200, "<html>tchcourse ok</html>")
            if "teacherstudycourselist" in url:
                # 200 但结构变了(超星改版), HTML 含 cookie 痕迹
                return _FakeResp(200,
                    "<html><body>sessionid=should_be_redacted; no_ul_here</body></html>"
                )
            return _FakeResp(200, "")

        out_dir = tmp_path / "out"
        out_dir.mkdir()
        session = requests.Session()
        # 直接覆盖 session.get
        original_get = session.get
        def wrapped_get(url, **kwargs):
            return fake_get(session, url, **kwargs)
        session.get = wrapped_get

        lessons = chaoxing.get_chapter_tree(
            session, "999", "888",
            course_url=str(out_dir / "fake"),
            output_dir=str(out_dir),
            debug=False,
        )
        assert lessons == []
        debug_html = out_dir / "_chaoxing_tree_debug.html"
        assert debug_html.exists()
        content = debug_html.read_text(encoding="utf-8")
        assert "sessionid=should_be_redacted" not in content
