"""
capture_apis.py - 录制 next-studio.xuetangx.com 的所有 API 请求到 JSON

用法：
  1. 准备好 cookies_teacher.txt（你浏览器里 F12 抓的 Cookie 字符串）
  2. 跑：
     python -m scrape.upload.capture_apis ^
       --cookies cookies_teacher.txt ^
       --course-id 15927106 ^
       --out api_capture.json
  3. 浏览器弹出后**手动操作**一遍完整流程：
     - 新建一个章 → 填标题 → 保存
     - 在章里新建一个节 → 填标题 → 上传一个小视频 → 保存
     - 翻翻左边的目录面板、点几个 tab
  4. **关掉浏览器窗口**或 Ctrl+C，JSON 会自动保存

设计：
  - 只抓 next-studio.xuetangx.com 和 api.xuetangx.com 域的请求
  - 跳过静态资源（js/css/img/font）
  - 抓 request + response 双方向，body 也保存
  - 浏览器 headed 模式，让你看到点击效果

⚠️ 安全警告：
  输出的 JSON 文件包含会话凭证（Cookie/Authorization header 等）。
  保存时会自动脱敏这些敏感 header，但仍请：
  - 不要将输出文件提交到 git
  - 不要通过不安全的渠道分享输出文件
  - 使用后及时删除
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from datetime import datetime
from pathlib import Path

# 确保能 import playwright
try:
    from playwright.sync_api import sync_playwright, Response, Request
except ImportError:
    print("[错误] playwright 没装。请先：pip install playwright && playwright install chromium")
    sys.exit(1)


# ─── 过滤规则 ────────────────────────────────────────────────────

# 只抓这两个域的 API
KEEP_DOMAINS = ("next-studio.xuetangx.com", "api.xuetangx.com", "yun.xuetangx.com")

# 跳过这些后缀（静态资源）
SKIP_EXT = (".js", ".css", ".png", ".jpg", ".jpeg", ".gif", ".svg", ".ico",
            ".woff", ".woff2", ".ttf", ".eot", ".map", ".mp4", ".m3u8")

# 敏感 header，录制时自动脱敏
SENSITIVE_HEADERS = {"cookie", "authorization", "set-cookie", "x-csrftoken",
                     "x-xsrf-token", "x-csrf-token", "csrf-token"}

# HTTP 方法白名单（None = 全录，main() 里按 --method 设置）
METHOD_FILTER: list[str] | None = None


def _redact_headers(headers: dict) -> dict:
    """脱敏敏感 header，返回新 dict"""
    return {
        k: ("***REDACTED***" if k.lower() in SENSITIVE_HEADERS else v)
        for k, v in headers.items()
    }


def should_capture(url: str, method: str = "") -> bool:
    """判断这个 URL 是不是要抓的 API"""
    if not any(d in url for d in KEEP_DOMAINS):
        return False
    # method 过滤（白名单）
    if METHOD_FILTER is not None and method.upper() not in METHOD_FILTER:
        return False
    # 跳过静态资源
    path = url.split("?", 1)[0].lower()
    if any(path.endswith(ext) for ext in SKIP_EXT):
        return False
    return True


# ─── 录制器 ──────────────────────────────────────────────────────

class ApiRecorder:
    """挂在 Playwright page 上，抓所有 API 请求和响应"""

    def __init__(self) -> None:
        self.entries: list[dict] = []
        self.req_id = 0

    def attach(self, page) -> None:
        page.on("request", self._on_request)
        page.on("response", self._on_response)
        page.on("requestfailed", self._on_failed)

    def _on_request(self, request: "Request") -> None:
        if not should_capture(request.url, request.method):
            return
        try:
            post_data = request.post_data
        except Exception:
            post_data = None
        entry = {
            "req_id": self.req_id,
            "ts_request": datetime.now().isoformat(timespec="milliseconds"),
            "method": request.method,
            "url": request.url,
            "headers": _redact_headers(dict(request.headers)) if request.headers else {},
            "post_data": post_data,
            "resource_type": request.resource_type,
            "ts_response": None,
            "status": None,
            "response_headers": None,
            "response_body": None,
        }
        self.entries.append(entry)
        self.req_id += 1

    def _on_response(self, response: "Response") -> None:
        if not should_capture(response.url, response.request.method):
            return
        # 找对应的 request entry（用 URL + method 匹配最近一条）
        entry = self._find_pending(response.request.method, response.url)
        if entry is None:
            return
        entry["ts_response"] = datetime.now().isoformat(timespec="milliseconds")
        entry["status"] = response.status
        try:
            entry["response_headers"] = _redact_headers(dict(response.headers)) if response.headers else {}
        except Exception:
            entry["response_headers"] = {}
        # 只抓 JSON 响应（小一点，省内存）
        ctype = ""
        for k, v in (response.headers or {}).items():
            if k.lower() == "content-type":
                ctype = v.lower()
                break
        if "json" in ctype or "text" in ctype:
            try:
                body = response.text()
                # 限长，超过 200KB 的不要（防止视频分片等）
                if len(body) > 200_000:
                    entry["response_body"] = f"<<TRUNCATED, {len(body)} chars>>"
                else:
                    # 尝试解析 JSON
                    try:
                        entry["response_body"] = json.loads(body)
                    except Exception:
                        entry["response_body"] = body
            except Exception as e:
                entry["response_body"] = f"<<FETCH FAILED: {e}>>"

    def _on_failed(self, request: "Request") -> None:
        if not should_capture(request.url, request.method):
            return
        entry = self._find_pending(request.method, request.url)
        if entry is None:
            return
        entry["ts_response"] = datetime.now().isoformat(timespec="milliseconds")
        entry["status"] = "FAILED"
        entry["error"] = request.failure

    def _find_pending(self, method: str, url: str) -> dict | None:
        """找最近的同 method+url 的待响应 entry"""
        for e in reversed(self.entries):
            if e["method"] == method and e["url"] == url and e["ts_response"] is None:
                return e
        return None

    def save(self, out_path: Path) -> None:
        out_path = Path(out_path)
        payload = {
            "captured_at": datetime.now().isoformat(timespec="seconds"),
            "count": len(self.entries),
            "entries": self.entries,
        }
        out_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, default=str),
            encoding="utf-8",
        )
        print(f"\n[保存] {len(self.entries)} 个请求已写入 {out_path}")


# ─── Cookie 加载 ─────────────────────────────────────────────────

def load_cookie_string(path: Path) -> str:
    """从文件读 cookie 字符串（一行或多行，name=value; 格式）"""
    text = Path(path).read_text(encoding="utf-8", errors="ignore").strip()
    # 去掉可能的 "Cookie:" 前缀
    if text.lower().startswith("cookie:"):
        text = text.split(":", 1)[1].strip()
    return text


def to_storage_state(cookie_str: str) -> dict:
    """转 Playwright storage_state 格式"""
    cookies = []
    for pair in cookie_str.split(";"):
        pair = pair.strip()
        if "=" not in pair:
            continue
        name, value = pair.split("=", 1)
        name = name.strip()
        value = value.strip()
        if not name or not value:
            continue
        cookies.append({
            "name": name,
            "value": value,
            "domain": ".xuetangx.com",
            "path": "/",
            "expires": -1,
            "httpOnly": False,
            "secure": False,
            "sameSite": "Lax",
        })
    return {"cookies": cookies, "origins": []}


# ─── 主流程 ──────────────────────────────────────────────────────

def main() -> int:
    global METHOD_FILTER
    p = argparse.ArgumentParser(
        description="录制老师后台 API 请求到 JSON（headless 不可见，看不到点击效果）",
    )
    p.add_argument("--cookies", required=False, help="Cookie 文件路径（从浏览器导出的字符串）")
    p.add_argument("--cookie-string", required=False,
                   help="Cookie 字符串直接传（不落盘，优先于 --cookies）")
    p.add_argument("--course-id", required=True, help="要录的课程 ID")
    p.add_argument("--out", default="api_capture.json", help="输出 JSON 路径")
    p.add_argument("--url", default="https://next-studio.xuetangx.com",
                   help="老师后台根域")
    p.add_argument("--method", nargs="+", default=None,
                   help="只录这些 HTTP 方法（如 POST PUT）。不指定 = 全录")
    args = p.parse_args()

    # cookie 来源：--cookie-string 优先，其次 --cookies 文件
    if args.cookie_string:
        cookie_str = args.cookie_string.strip()
        print(f"[加载] --cookie-string: {len(cookie_str.split(';'))} 个 cookie 字段")
    elif args.cookies:
        cookies_path = Path(args.cookies)
        if not cookies_path.exists():
            print(f"[错误] Cookie 文件不存在: {cookies_path}")
            return 1
        cookie_str = load_cookie_string(cookies_path)
        print(f"[加载] {len(cookie_str.split(';'))} 个 cookie 字段（从 {cookies_path}）")
    else:
        print("[错误] 必须传 --cookie-string 'xxx' 或 --cookies <文件>")
        return 1
    out_path = Path(args.out)

    # 设置方法白名单
    if args.method:
        METHOD_FILTER = [m.upper() for m in args.method]
        print(f"[过滤] 只录 HTTP 方法: {METHOD_FILTER}")

    storage_state = to_storage_state(cookie_str)
    print(f"[转]  {len(storage_state['cookies'])} 个 cookie → Playwright storage_state")

    recorder = ApiRecorder()
    target_url = f"{args.url}/pro/editcoursemanage/teachcontent/{args.course_id}"

    print(f"\n[启动] 浏览器 headed 模式（你能看到）")
    print(f"[导航] {target_url}")
    print(f"\n请在弹出的浏览器里手动操作：")
    print(f"  1. 翻翻目录面板")
    print(f"  2. 新建一个章 → 填标题 → 保存")
    print(f"  3. 在章里新建一个节 → 填标题 → 上传个小视频 → 保存")
    print(f"  4. 切几个 tab 看下")
    print(f"\n** 完成后关闭浏览器窗口，JSON 会自动保存到 {out_path} **\n")

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=False, slow_mo=200)
            context = browser.new_context(
                storage_state=storage_state,
                viewport={"width": 1440, "height": 900},
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                           "AppleWebKit/537.36 (KHTML, like Gecko) "
                           "Chrome/149.0.0.0 Safari/537.36",
                locale="zh-CN",
            )
            page = context.new_page()
            recorder.attach(page)
            page.goto(target_url, wait_until="domcontentloaded", timeout=60000)

            # 等用户操作（关掉浏览器窗口就退出）
            try:
                page.wait_for_event("close", timeout=0)  # 永远等
            except KeyboardInterrupt:
                pass
            finally:
                context.close()
                browser.close()
    except Exception as e:
        print(f"[错误] {e}")
        import traceback
        traceback.print_exc()
        return 2

    recorder.save(out_path)
    print(f"\n[完成] 录制文件已保存: {out_path.absolute()}")
    print(f"⚠️  注意: 此文件包含会话凭证（已自动脱敏），请勿提交到 git 或公开分享")
    return 0


if __name__ == "__main__":
    sys.exit(main())
