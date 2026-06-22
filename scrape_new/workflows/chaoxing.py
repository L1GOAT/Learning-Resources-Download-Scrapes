#!/usr/bin/env python3
"""
超星学习通课程视频一键下载

两种运行方式(选一种):

  # 推荐:用模块方式跑(无需关心路径)
  python -m scrape_new.workflows.chaoxing "URL" mycourse --scan-only

  # 也可以直接跑文件(脚本自动 bootstrap sys.path)
  python scrape_new/workflows/chaoxing.py "URL" mycourse --scan-only

示例:
  python -m scrape_new.workflows.chaoxing "URL" mycourse --scan-only
  python -m scrape_new.workflows.chaoxing "URL" mycourse --max-tabs 6
  python -m scrape_new.workflows.chaoxing "URL" mycourse --cookies-file %TEMP%\\scrape_cookie_xxx.txt
  # (完整选项见下; 详细 --cookies-file 用法见 scrape_new/services/redaction.py)

选项:
  --scan-only            只扫描章节和资源,不下载文件
  --max-tabs N           多 tab 探测数(默认 4)
  --include-empty-lessons  scan-only 报告里包含 0 资源章/节
  --resume <path>        从历史 manifest 跳过已下资源
  --retry-downloads <path>  只重下 retry 列表里的资源
  --verify-resume-only   不下载,只判断哪些会跳过
  --outline-only         只扒章节树不下载视频
  --playwright           用 Playwright 真点视频建立 ananas 会话
  --cpi <数字>           手动指定 cpi
  --cookies-file <path>  从外部路径读 cookie(repo 外, 优先级最高)
  --debug                打印调试信息

cookie 优先级(从高到低):
  1. --cookies-file <外部路径>  (推荐)
  2. XTBZ_COOKIE 环境变量
  3. 项目根 cookies.txt         (默认 fallback)
  详细见 scrape_new/services/redaction.py

输出:
  <输出目录>/视频/01_xxx.mp4 ...
  <输出目录>/_chapter_tree.json (章节目录)
  <输出目录>/_resource_naming_manifest.json
"""

# ─── sys.path bootstrap ───────────────────────────────────
# 直跑文件时 `scrape_new` 包不可见(因为 sys.path[0] 是脚本目录)
# 用模块方式(python -m)则不需要,这里只是兜底
import os
import sys
_HERE = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.dirname(os.path.dirname(_HERE))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

import asyncio
import requests
from scrape_new.core import DEFAULT_UA
from scrape_new.upload.naming import lesson_filename, lesson_leaf_name  # noqa: E501
import re
import json
import time
import sys
import os
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urlparse, parse_qs

# 新版超星章节树用 HTML(非 JSON),要 BS4 解析
try:
    from bs4 import BeautifulSoup
    HAS_BS4 = True
except ImportError:
    HAS_BS4 = False

# Playwright(可选,用于 --playwright 模式建立 ananas 会话)
try:
    from playwright.async_api import async_playwright
    HAS_PLAYWRIGHT = True
except ImportError:
    HAS_PLAYWRIGHT = False

# scrape_new.upload.outline 是 scrape_new.upload 包成员,
# _PROJECT_ROOT 已经在文件顶部 bootstrap 进 sys.path,无需再处理
try:
    from scrape_new.upload.outline import write_outline, videos_to_outline_chapters
    HAS_OUTLINE = True
except ImportError:
    HAS_OUTLINE = False

try:
    from scrape_new.services.resource_manifest import (
        write_download_resource_manifests,
        STATUS_DOWNLOADED as _STATUS_DOWNLOADED,
        STATUS_FAILED as _STATUS_FAILED,
        STATUS_SKIPPED_EXISTING as _STATUS_SKIPPED_EXISTING,
        STATUS_SUSPICIOUS as _STATUS_SUSPICIOUS,
    )
    HAS_MANIFEST = True
except ImportError:
    HAS_MANIFEST = False


# ─── 配置 ──────────────────────────────────────────────────────

COOKIES_FILE = "cookies.txt"
DEFAULT_OUTPUT = "./超星课程下载"
DELAY_BETWEEN_DOWNLOADS = 1  # 秒

HEADERS = {
    "User-Agent": DEFAULT_UA,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    "Accept-Encoding": "gzip, deflate, br",
    "sec-ch-ua": '"Google Chrome";v="149", "Chromium";v="149", "Not)A;Brand";v="24"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"Windows"',
    "Cache-Control": "max-age=0",
    "Connection": "keep-alive",
}


def _browser_nav_headers(referer: str | None = None) -> dict:
    """Headers for Chaoxing document navigation endpoints."""
    headers = dict(HEADERS)
    headers.update({
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/149.0.0.0 Safari/537.36"
        ),
        "Accept": (
            "text/html,application/xhtml+xml,application/xml;q=0.9,"
            "image/avif,image/webp,image/apng,*/*;q=0.8,"
            "application/signed-exchange;v=b3;q=0.7"
        ),
        "Accept-Language": "zh-CN,zh;q=0.9,en-US;q=0.8,en;q=0.7",
        "Cache-Control": "max-age=0",
        "Connection": "keep-alive",
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "same-origin",
        "Sec-Fetch-User": "?1",
        "Upgrade-Insecure-Requests": "1",
    })
    if referer:
        headers["Referer"] = referer
    return headers


# ─── 工具函数 ──────────────────────────────────────────────────

def load_cookies(session, filepath, cookies_file=None):
    """加载 Cookie。优先级:
        1. cookies_file(外部路径参数,如 %TEMP%\\scrape_cookie_xxx.txt)
        2. XTBZ_COOKIE 环境变量
        3. filepath(默认 scrape_new 项目根 cookies.txt)
    cookie 内容**只**进 session 对象, 不打印 / 不写任何中间文件 / 不进 shell。
    """
    # 1. 外部 cookie 文件参数(本会话安全加载)
    if cookies_file:
        if not os.path.exists(cookies_file):
            print(f"[错误] 找不到 --cookies-file: {cookies_file}")
            sys.exit(1)
        with open(cookies_file, "r", encoding="utf-8") as f:
            raw = f.read().strip()
        raw = _extract_cookie_string(raw)
        _parse_cookie_string(session, raw)
        print(f"[OK] 已加载 Cookie 来自 --cookies-file ({len(session.cookies)} 个字段)")
        return

    # 2. 环境变量优先(用户偏好 in-memory,cookie 不落盘)
    env_cookie = os.environ.get("XTBZ_COOKIE", "").strip()
    if env_cookie:
        raw = _extract_cookie_string(env_cookie)
        _parse_cookie_string(session, raw)
        print(f"[OK] 已加载 Cookie 来自 XTBZ_COOKIE 环境变量({len(session.cookies)} 个字段)")
        return

    # 3. 文件 fallback(原 cookies.txt 行为, 不破坏兼容)
    if not os.path.exists(filepath):
        print(f"[错误] 找不到 {filepath}，请先导出 Cookie")
        print(f"  或者设环境变量 XTBZ_COOKIE='<原始 cookie 字符串>'")
        print(f"  或者传 --cookies-file <外部路径>")
        print(f"  导出方法见 MACOS_DEPLOY.md 或 SESSION_PROTOCOL.md")
        sys.exit(1)
    with open(filepath, "r", encoding="utf-8") as f:
        raw = f.read().strip()
    raw = _extract_cookie_string(raw)
    _parse_cookie_string(session, raw)
    print(f"[OK] 已加载 Cookie 来自 {filepath} ({len(session.cookies)} 个字段)")


def _extract_cookie_string(raw: str) -> str:
    """Accept a raw cookie string or a copied curl command and return cookies only."""
    text = raw.strip()
    if not text.lower().lstrip().startswith("curl "):
        return text

    # curl ... -b 'a=b; c=d'
    m = re.search(r"(?:^|\s)-b\s+(['\"])(.*?)\1", text, re.S)
    if m:
        return m.group(2).strip()

    # curl ... -H 'Cookie: a=b; c=d'
    m = re.search(r"(?:^|\s)-H\s+(['\"])Cookie:\s*(.*?)\1", text, re.I | re.S)
    if m:
        return m.group(2).strip()

    return text


def _parse_cookie_string(session, raw, domain=".chaoxing.com"):
    """解析原始 cookie 字符串并塞到 session.cookies。"""
    from http.cookies import SimpleCookie

    jar = SimpleCookie()
    try:
        jar.load(raw)
    except Exception:
        jar = SimpleCookie()

    if jar:
        for key, morsel in jar.items():
            session.cookies.set(key, morsel.value, domain=domain)
        return

    # Fallback for non-standard cookie snippets.
    for pair in raw.split(";"):
        pair = pair.strip()
        if "=" in pair:
            k, v = pair.split("=", 1)
            session.cookies.set(k.strip(), v.strip().strip('"'), domain=domain)


def extract_params(url):
    """从 URL 提取 courseId、clazzid、chapterId"""
    parsed = urlparse(url)
    params = parse_qs(parsed.query)
    return {
        "courseId": params.get("courseId", params.get("courseid", [None]))[0],
        "clazzid": params.get("clazzid", params.get("classId", [None]))[0],
        "chapterId": params.get("chapterId", params.get("chapterid", [None]))[0],
    }


def validate_cookie(session, url):
    """验证 Cookie 是否有效"""
    print("[检查] 验证 Cookie 有效性...")
    try:
        resp = session.get(url, timeout=30, allow_redirects=False)
        if resp.status_code in (301, 302):
            location = resp.headers.get("Location", "").lower()
            if "login" in location or "passport" in location:
                print("[错误] Cookie 已过期！页面跳转到登录页")
                print("  请重新导出 Cookie")
                return False
        if resp.status_code == 200:
            text = resp.text[:2000]
            if "用户登录" in text or "passport" in text.lower():
                print("[错误] Cookie 已过期！页面显示登录页")
                return False
        print("[OK] Cookie 有效")
        return True
    except Exception as e:
        print(f"[警告] 验证失败: {e}（继续尝试）")
        return True


def get_cpi(session, url, cpi_override=None):
    """获取 cpi 参数(优先:命令行 override > URL query > tchcourse 页面搜)"""
    if cpi_override:
        return cpi_override
    # 尝试从 tchcourse 页面获取
    parsed = urlparse(url)
    params = parse_qs(parsed.query)
    # 先从 URL 里找
    if "cpi" in params:
        return params["cpi"][0]
    # 从 tchcourse 页面找
    course_id = params.get("courseId", params.get("courseid", [None]))[0]
    clazz_id = params.get("clazzid", params.get("classId", [None]))[0]
    if course_id and clazz_id:
        tch_url = (
            f"https://mooc2-ans.chaoxing.com/mooc2-ans/mycourse/tchcourse"
            f"?courseid={course_id}&clazzid={clazz_id}"
        )
        try:
            resp = session.get(tch_url, timeout=30)
            m = re.search(r'cpi[=:]\s*["\']?(\d+)', resp.text)
            if m:
                return m.group(1)
        except Exception as e:
            print(f"  [注意] 从 tchcourse 页面取 cpi 失败: {type(e).__name__}: {e}")
    return None


def sanitize_filename(name):
    """清理文件名中的非法字符(统一版)"""
    return re.sub(r'[\\/:*?"<>|]', "_", name).strip(". ")


# ─── 核心流程 ──────────────────────────────────────────────────

def _warmup_session(session, course_url: str, clazz_id: str | int) -> str:
    """会话预热: 让超星认为这是浏览器行为而不是直接 API 调用。

    超星新版(2024+)对直接 GET teacherstudycourselist 返 400。
    需要先 GET 课程主页 + tchcourse 页面建立 session + 拿 csrf/enc,
    再用完整 Referer / Origin 头拉章节树。

    Returns:
        warmup_url 用于章节树请求的 Referer 头(成功 warmup 后的 URL)
    """
    print("[扫描] 预热会话(warmup)...")

    # 强制禁用代理环境变量影响(本机可能设了 HTTP_PROXY)
    session.trust_env = False
    session.proxies = {"http": None, "https": None}

    # 1) 先 GET 用户给的课程 URL(可能是 teacherstudy / tchcourse / studentstudy)
    try:
        r1 = session.get(
            course_url,
            timeout=30,
            allow_redirects=True,
            headers=_browser_nav_headers(course_url),
        )
        print(f"  [warmup] GET 课程主页 → {r1.status_code} (len={len(r1.text)})")
    except Exception as e:
        print(f"  [warmup] GET 课程主页失败(忽略): {type(e).__name__}: {e}")

    # 2) 再 GET tchcourse 页面(warmup 拿 enc / openc / cpi)
    tchcourse_url = (
        f"https://mooc2-ans.chaoxing.com/mooc2-ans/mycourse/tchcourse"
        f"?courseid={_extract_course_id_from_url(course_url)}"
        f"&clazzid={clazzid_for_request(clazz_id)}"
    )
    try:
        r2 = session.get(
            tchcourse_url,
            timeout=30,
            allow_redirects=True,
            headers=_browser_nav_headers(course_url),
        )
        print(f"  [warmup] GET tchcourse → {r2.status_code} (len={len(r2.text)})")
        # 成功就用这个当 Referer(更稳)
        if r2.status_code == 200:
            return tchcourse_url
    except Exception as e:
        print(f"  [warmup] GET tchcourse 失败(忽略): {type(e).__name__}: {e}")

    # fallback: 用原 URL
    return course_url


def _extract_course_id_from_url(url: str) -> str:
    """从 URL 提取 courseId / courseid(支持多种大小写)。"""
    from urllib.parse import urlparse, parse_qs
    parsed = urlparse(url)
    params = parse_qs(parsed.query)
    return (
        params.get("courseId", params.get("courseid", [None]))[0]
        or params.get("courseid", [None])[0]
        or ""
    )


def get_chapter_tree(
    session,
    course_id,
    clazz_id,
    course_url=None,
    output_dir=None,
    debug=False,
    cpi=None,
):
    """从新版超星 /teacherstudycourselist 拉章节树(HTML 格式)。

    新版(2024+)结构:
      - 顶层 <ul> 包含所有 <li> 章
      - 每章 <li> 内 <div class="posCatalog_select" id="<chapter_id>"> 放章名
      - 章内 <div class="posCatalog_level"> 嵌套 <ul> 包含该章所有课时
      - 每节 <li> 内 <div class="posCatalog_select" id="cur<lesson_id>"> 放节名
      - 节的 <span> 有 onclick="getTeacherAjax('cid','clzid','chapterid')"

    Args:
        course_url: 原始课程 URL(warmup 用), 默认从 course_id 拼。
    """
    print("[扫描] 正在获取课程章节列表...")

    # 用传入的 course_url, 否则拼一个
    if not course_url:
        course_url = (
            f"https://mooc2-ans.chaoxing.com/mooc2-ans/mycourse/teacherstudy"
            f"?courseId={course_id}&chapterId=&clazzid={clazzid_for_request(clazz_id)}"
        )

    # 会话预热(超星新版必需)
    referer_url = _warmup_session(session, course_url, clazz_id)

    # 完整 URL(带 chapterId=&isMicroCourse 等参数)
    url = (
        f"https://mooc2-ans.chaoxing.com/mooc2-ans/mycourse/teacherstudycourselist"
        f"?courseId={course_id}&chapterId=&clazzid={clazzid_for_request(clazz_id)}"
        f"&isMicroCourse=false&topicModelId=0&microTopicId=0"
    )
    if cpi:
        url += f"&cpi={cpi}"

    # 给这个请求加完整 headers(超星反爬依赖 Referer / Origin)
    req_headers = _browser_nav_headers(referer_url)
    req_headers["Origin"] = "https://mooc2-ans.chaoxing.com"

    resp = session.get(url, timeout=30, headers=req_headers)
    html = resp.text

    if debug:
        print(f"\n  [DEBUG] 章节树 URL: {url}")
        print(f"  [DEBUG] Referer: {referer_url}")
        print(f"  [DEBUG] 状态: {resp.status_code}, 长度: {len(html)}")
        print(f"  [DEBUG] 前 1500 字符:")
        print(html[:1500])
        print(f"  [DEBUG] ---END---\n")

    if resp.status_code != 200:
        print(f"[错误] teacherstudycourselist 返回 {resp.status_code} (期望 200)")
        # 保存脱敏 debug(不写 cookie, 但写 HTML 和 URL 给用户排查)
        try:
            from scrape_new.services.redaction import redact_sensitive
            # 用 output_dir 写 debug(如果给了); fallback 到 cwd
            debug_dir = Path(output_dir) if output_dir else Path.cwd()
            debug_dir.mkdir(parents=True, exist_ok=True)
            debug_path = debug_dir / "_chaoxing_tree_debug.html"
            # 注意: 这只写 HTML, 不写 cookie 文件
            # 但 HTML 可能含 cookie(超星有时把 cookie 渲染在 inline script 里),
            # 所以先脱敏
            debug_path.write_text(redact_sensitive(html), encoding="utf-8")
            print(f"  [debug] HTML 已脱敏保存: {debug_path}")
        except Exception as e:
            print(f"  [debug] 保存 debug HTML 失败: {e}")
        return []

    if not HAS_BS4:
        print("[错误] 需要 beautifulsoup4 — pip install beautifulsoup4")
        return []

    soup = BeautifulSoup(html, "html.parser")

    # 找最外层 ul(包含全部章)
    root_ul = soup.find("ul")
    if not root_ul:
        print("[错误] 找不到章节树根 <ul>")
        # 即使 200, HTML 结构可能变了(超星改版) — 保存脱敏 HTML
        try:
            from scrape_new.services.redaction import redact_sensitive
            debug_dir = Path(output_dir) if output_dir else Path.cwd()
            debug_dir.mkdir(parents=True, exist_ok=True)
            debug_path = debug_dir / "_chaoxing_tree_debug.html"
            debug_path.write_text(redact_sensitive(html), encoding="utf-8")
            print(f"  [debug] HTML 已脱敏保存(根 ul 缺失): {debug_path}")
        except Exception as e:
            print(f"  [debug] 保存失败: {e}")
        return []

    chapters: list[dict] = []
    lessons: list[dict] = []

    # 遍历顶层 <li> = 各章
    for ch_idx, ch_li in enumerate(root_ul.find_all("li", recursive=False), start=1):
        ch_div = ch_li.find("div", class_="posCatalog_select", recursive=False)
        if not ch_div:
            continue
        ch_id = ch_div.get("id", "")
        # 章 title 在 <span> 的 title 属性或文本
        ch_span = ch_div.find("span", recursive=False)
        ch_name = ch_span.get("title", "").strip() if ch_span else ch_div.get_text(strip=True)
        if not ch_name:
            ch_name = ch_div.get_text(strip=True).lstrip("0123456789. ").strip()
        if not ch_name:
            continue
        chapters.append({
            "id": int(ch_id) if ch_id.isdigit() else 0,
            "name": ch_name,
            "order": ch_idx,
        })

        # 嵌套的课时(在 <div class="posCatalog_level"> 里的 <ul>)
        for ls_num, ls_li in enumerate(ch_li.select("div.posCatalog_level ul li"), start=1):
            ls_div = ls_li.find("div", class_="posCatalog_select", recursive=False)
            if not ls_div:
                continue
            ls_id = ls_div.get("id", "")
            # 去掉 "cur" 前缀
            if ls_id.startswith("cur"):
                ls_id = ls_id[3:]
            ls_span = ls_div.find("span", recursive=False)
            ls_name = ls_span.get("title", "").strip() if ls_span else ls_div.get_text(strip=True)
            if not ls_name:
                ls_name = ls_div.get_text(strip=True)
            lessons.append({
                "id": int(ls_id) if ls_id.isdigit() else 0,
                "name": ls_name,
                "is_leaf": True,
                "parent": ch_name,
                "ch_num": ch_idx,
                "ls_num": ls_num,
            })

    print(f"[OK] 找到 {len(chapters)} 章，{len(lessons)} 节课")
    return lessons


def clazzid_for_request(clazz_id: str | int) -> str:
    """clazz_id 直接返回(兼容 string/int)。占位,以后可加脱敏。"""
    return str(clazz_id)


def _fetch_cards_tab(session, url):
    """拉取单个 cards Tab,返回 (videos_list, docs_list)"""
    try:
        resp = session.get(url, timeout=30)
        if resp.status_code == 202:
            return [], [], 1  # 202 = rate limited
        if resp.status_code != 200:
            return [], [], 0
        videos = []
        docs = []
        for m in re.finditer(
            r'<(?:iframe|div)[^>]*class="[^"]*ans-attach-online[^"]*"[^>]*data="([^"]+)"',
            resp.text,
        ):
            try:
                d = json.loads(
                    m.group(1).replace("&quot;", '"').replace("&amp;", "&")
                )
                oid = d.get("objectid")
                if not oid:
                    continue
                rtype = d.get("type", "")
                if ".mp4" in rtype or ".flv" in rtype:
                        videos.append({
                            "type": rtype,
                            "name": d.get("name", "?"),
                            "objectid": oid,
                            "mid": d.get("mid", ""),
                            "jobid": d.get("jobid") or d.get("_jobid", ""),
                            "size": d.get("size", 0),
                        })
                else:  # insertdoc
                    docs.append({
                        "type": d.get("type", ""),
                        "name": d.get("name", "?"),
                        "objectid": oid,
                        "size": d.get("size", 0),
                    })
            except json.JSONDecodeError:
                pass
        # 静默失败诊断: regex 无匹配时检查页面是否为登录/重定向
        if not videos and not docs:
            if resp.status_code in (301, 302):
                location = resp.headers.get("Location", "")
                if "login" in location.lower() or "passport" in location.lower():
                    print(f"  [警告] cards API 302 重定向到登录页(Cookie 未跨域或已过期)")
                    print(f"          重定向到: {location[:120]}")
            elif resp.status_code == 200:
                resp_sample = resp.text[:500].lower()
                if "login" in resp_sample or "passport" in resp_sample or "用户登录" in resp_sample:
                    print(f"  [警告] cards API 返回登录页(Cookie 未跨域携带或已过期)")
                    print(f"          URL: {url[:120]}")
        return videos, docs, 0
    except Exception as e:
        status = getattr(getattr(e, "response", None), "status_code", 0) if hasattr(e, "response") else 0
        if status in (401, 403, 404):
            pass  # auth error, individual tab may fail silently
        else:
            print(f"  [注意] cards tab 拉取失败: {type(e).__name__}: {e}")
        return [], [], 0


def scan_lesson_resources(
    session, lesson, course_id, clazz_id, cpi,
    debug_once=False, max_tabs: int = 4,
):
    """扫描单节课的资源(串行逐个Tab,仿人类浏览,不并行以避免触发限流)。

    改动(vs 旧版):
      - max_tabs 参数化(默认 4,覆盖 视频/PPT/英文/未知 tab)
      - 用 scrape_new.services.scan_chaoxing.scan_lesson_tabs 做空 tab 停止 + 限流中断
      - 用 scrape_new.services.scan_chaoxing.detect_resource_role 智能 role 判定
      - 返回 4 元组(videos, docs, rate_limited, failed_tabs)
    """
    from scrape_new.services.scan_chaoxing import (
        scan_lesson_tabs, detect_resource_role,
        ROLE_VIDEO, ROLE_UNKNOWN,
    )
    if isinstance(lesson, dict):
        kid = lesson.get("id", lesson.get("knowledge_id"))
    else:
        kid = lesson
    if kid is None:
        return [], [], 0, 0

    base = (
        f"https://mooc1.chaoxing.com/knowledge/cards"
        f"?clazzid={clazz_id}&courseid={course_id}&knowledgeid={kid}"
        f"&v=20160407&ut=t&cpi={cpi}&mooc2=1"
        f"&isMicroCourse=false&crossId=0&videoWidth=0&videoHeight=0"
        f"&targetVideoJobId=&isPreviewVideo=false"
        f"&num={{n}}"
    )
    import random as _random
    referer = (
        f"https://mooc2-ans.chaoxing.com/mooc2-ans/mycourse/teacherstudy"
        f"?courseId={course_id}&clazzid={clazz_id}&_t={int(time.time()*1000)}"
    )
    session.headers["Referer"] = referer
    # 分散请求特征:随机 Accept(偶尔变一下)
    if _random.random() < 0.3:
        session.headers["Accept"] = "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"

    def fetcher(tab_num: int):
        url = base.format(n=tab_num)
        v, d, rl = _fetch_cards_tab(session, url)
        return v, d, bool(rl), False, ""

    tabs, stopped = scan_lesson_tabs(fetcher, max_tabs=max_tabs)

    all_videos: list = []
    all_docs: list = []
    rate_limited_count = 0
    failed_tabs = 0
    for t in tabs:
        if t.rate_limited:
            rate_limited_count += 1
        if t.failed:
            failed_tabs += 1
        for _v in t.videos:
            _v["tab_num"] = t.tab_num
            # 智能 role 判定:type 字段(扩展名/mimetype)+ filename + title + tab_num
            _v["role"] = detect_resource_role(
                type_or_mimetype=_v.get("type", ""),
                filename=_v.get("name", ""),
                title=_v.get("name", ""),
                tab_num=t.tab_num,
            )
            # role == unknown 仍保留在 videos,但加 unknown flag(报告层分流)
            if _v["role"] == ROLE_UNKNOWN:
                _v["role_unknown"] = True
            all_videos.append(_v)
        for _d in t.docs:
            _d["tab_num"] = t.tab_num
            _d["role"] = detect_resource_role(
                type_or_mimetype=_d.get("type", ""),
                filename=_d.get("name", ""),
                title=_d.get("name", ""),
                tab_num=t.tab_num,
            )
            if _d["role"] == ROLE_UNKNOWN:
                _d["role_unknown"] = True
            all_docs.append(_d)
        # Tab 间短延迟(限流/失败时跳过,避免空延迟死循环)
        if t.tab_num < max_tabs - 1 and not t.rate_limited and not t.failed:
            time.sleep(1 + _random.random())

    if debug_once:
        print(f"\n  [DEBUG] {len(all_videos)}V+{len(all_docs)}D from {len(tabs)} tabs (stopped={stopped})")

    # 向后兼容:旧调用方期望 (v, d, rl_count) 3-tuple。这里返回 4-tuple。
    return all_videos, all_docs, rate_limited_count, failed_tabs


# ─── Playwright cards 扫描(绕过 202 反爬) ──────────────────────────

async def scan_all_resources_playwright(
    cookie_str: str,
    course_url: str,
    lessons: list[dict],
    course_id: str,
    clazz_id: str,
    cpi: str,
) -> tuple[list[dict], list[dict]]:
    """用 Playwright 在 teacherstudy 页面内通过 getTeacherAjax + iframe 提取全部资源。

    cards API (mooc1.chaoxing.com) 的反爬要求请求来自页面内的 iframe 导航。
    策略: getTeacherAjax 加载课时 → iframe 加载 num=0
         → 读 iframe 内容 → 改 iframe.src 参数切换 Tab → 再读。
    """
    print("[Playwright] 启动浏览器扫描全部资源...")
    all_videos = []
    all_docs = []
    seen_objids = set()
    _consec_202 = 0  # 连续全 202 计数器(用于限流中断)

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=False,  # 有头模式:模拟真实浏览器,绕过 mooc1 指纹检测
            slow_mo=300,     # 每步 300ms 延迟,类人操作
            args=[
                "--disable-blink-features=AutomationControlled",
                "--window-size=1440,900",
            ],
        )
        ctx = await browser.new_context(
            user_agent=DEFAULT_UA,
            viewport={"width": 1440, "height": 900},
        )
        # 注入 cookie(含完整域)
        cookies = []
        for pair in cookie_str.split(";"):
            pair = pair.strip()
            if "=" in pair:
                k, v = pair.split("=", 1)
                cookies.append({
                    "name": k.strip(), "value": v.strip(),
                    "domain": ".chaoxing.com", "path": "/",
                })
        await ctx.add_cookies(cookies)

        page = await ctx.new_page()

        # Step 0: 暖会话 - 先访问 tchcourse 页面(真实用户行为)
        tchcourse_url = (
            f"https://mooc2-ans.chaoxing.com/mooc2-ans/mycourse/tchcourse"
            f"?courseid={course_id}&clazzid={clazz_id}"
        )
        print("  [0] Warming up session via tchcourse...")
        await page.goto(tchcourse_url, wait_until="domcontentloaded", timeout=30000)
        await page.wait_for_timeout(1500)

        # 打开课程页面(带 chapterId 让第一节自动加载)
        print("  [1] Loading teacherstudy page...")
        await page.goto(course_url, wait_until="domcontentloaded", timeout=30000)
        await page.wait_for_timeout(3000)

        # 等 getTeacherAjax 函数就绪
        try:
            await page.wait_for_function(
                "typeof getTeacherAjax === 'function'",
                timeout=15000,
            )
        except Exception:
            print("  [警告] getTeacherAjax 未找到,尝试继续...")

        cards_base = (
            "https://mooc1.chaoxing.com/knowledge/cards"
            "?clazzid={clazzid}&courseid={courseid}&knowledgeid={kid}"
            "&num={num}&v=20160407&ut=t&cpi={cpi}&mooc2=1"
            "&isMicroCourse=false&crossId=0&videoWidth=0&videoHeight=0"
            "&targetVideoJobId=&isPreviewVideo=false"
        )

        for idx, lesson in enumerate(lessons):
            kid = lesson["id"]

            # 点击课时触发 iframe 加载(比 getTeacherAjax 更可靠)
            try:
                await page.click(f'#cur{kid} span.posCatalog_name', timeout=5000)
            except Exception:
                try:
                    # 备用:用 getTeacherAjax
                    await page.evaluate(
                        "({cid, clzid, kid}) => {"
                        "  if (typeof getTeacherAjax === 'function') {"
                        "    getTeacherAjax(cid, clzid, kid);"
                        "  }"
                        "}",
                        {"cid": course_id, "clzid": clazz_id, "kid": str(kid)},
                    )
                except Exception:
                    pass
            # 等 iframe 完全加载(跨域页面需要更长时间)
            await page.wait_for_timeout(5000)

            # 读取 iframe 内容(不改 src,直接读页面自动加载的)
            try:
                html = ""
                try:
                    frame = page.frame(name="iframe")
                    if frame:
                        html = await frame.content()
                except Exception:
                    pass

                if not html:
                    try:
                        html = await page.evaluate(
                            "() => {"
                            "  var ifr = document.getElementById('iframe');"
                            "  if (ifr && ifr.contentDocument) {"
                            "    return ifr.contentDocument.documentElement.outerHTML;"
                            "  }"
                            "  return '';"
                            "}"
                        )
                    except Exception:
                        pass

                # 提取 module="X" data="{...}" 资源
                for m in re.finditer(
                    r'class="ans-attach-online ans-(insertvideo[^"]*|insertdoc[^"]*)"[^>]*?\s+data="(\{[^"]+\})"',
                    html,
                ):
                    try:
                        decoded = (
                            m.group(2)
                            .replace("&quot;", '"')
                            .replace("&amp;", "&")
                        )
                        d = json.loads(decoded)
                        oid = d.get("objectid")
                        if oid and oid not in seen_objids:
                            seen_objids.add(oid)
                            mt = m.group(1)
                            if "insertvideo" in mt:
                                rtype = d.get("type", ".mp4")
                                if ".mp4" in rtype or ".flv" in rtype:
                                    all_videos.append({
                                        "objectid": oid,
                                        "name": d.get("name", lesson["name"]),
                                        "lesson": lesson["name"],
                                        "chapter": lesson.get("parent", ""),
                                        "ch_num": lesson["ch_num"],
                                        "ls_num": lesson.get("ls_num", 0),
                                    })
                            elif "insertdoc" in mt:
                                all_docs.append({
                                    "objectid": oid,
                                    "name": d.get("name", lesson["name"]),
                                    "type": d.get("type", ""),
                                    "size": d.get("size", 0),
                                    "lesson": lesson["name"],
                                    "chapter": lesson.get("parent", ""),
                                    "ch_num": lesson["ch_num"],
                                    "ls_num": lesson.get("ls_num", 0),
                                })
                    except json.JSONDecodeError:
                        pass
            except Exception:
                pass

            sys.stdout.write(
                f"\r  [PW] {idx+1}/{len(lessons)}: "
                f"V={len(all_videos)} D={len(all_docs)}"
            )
            sys.stdout.flush()

        await browser.close()

    return all_videos, all_docs

async def build_ananas_session(cookie_str: str, course_url: str) -> dict[str, str]:
    """用 Playwright 真点视频 5 秒,建立完整 ananas 会话。

    背景:ananas 服务端要求 videojs_id / k8s-ed 等会话级 cookie,
    只有浏览器真点开视频播放后才下发。纯 HTTP 模拟不出来(403)。

    返回:完整 cookie 字典(name → value),可直接注入 requests.Session。
    """
    print("[会话] 启动 Playwright 真点视频建立 ananas 会话 (30 秒)...")
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        ctx = await browser.new_context(
            user_agent=HEADERS.get("User-Agent", ""),
        )
        # 注入初始 cookie
        cookies = []
        for pair in cookie_str.split(";"):
            pair = pair.strip()
            if "=" in pair:
                k, v = pair.split("=", 1)
                cookies.append({
                    "name": k.strip(),
                    "value": v.strip(),
                    "domain": ".chaoxing.com",
                    "path": "/",
                })
        await ctx.add_cookies(cookies)
        print(f"  注入 {len(cookies)} 个初始 cookie")

        page = await ctx.new_page()
        await page.goto(course_url, wait_until="domcontentloaded", timeout=30000)
        await page.wait_for_timeout(2000)

        # 提取参数
        params = parse_qs(urlparse(course_url).query)
        clazz_id = params.get("clazzid", params.get("classId", [None]))[0]
        course_id = params.get("courseId", params.get("courseid", [None]))[0]
        first_chapter = params.get("chapterId", [None])[0]

        # 触发 getTeacherAjax 加载 lesson 内容
        if first_chapter:
            try:
                await page.evaluate(
                    "(courseId, clazzId, chapterId) => "
                    "typeof getTeacherAjax === 'function' ? "
                    "getTeacherAjax(courseId, clazzId, chapterId) : null",
                    course_id,
                    clazz_id,
                    first_chapter,
                )
            except Exception:
                pass
        await page.wait_for_timeout(3000)

        # 找 video 元素触发播放
        print("  找视频播放器并触发播放...")
        played = False
        for fr in page.frames:
            try:
                if "ananas" in fr.url or "video" in fr.url:
                    await fr.evaluate("""
                        (() => {
                            const v = document.querySelector('video');
                            if (v) { v.muted = true; v.play(); }
                        })();
                    """)
                    played = True
                    break
            except Exception:
                pass
        if not played:
            try:
                await page.evaluate("""
                    (() => {
                        const v = document.querySelector('video');
                        if (v) { v.muted = true; v.play(); }
                    })();
                """)
            except Exception:
                pass

        # 等 5 秒让 ananas 服务端发 learning_id / videojs_id
        print("  等 5 秒,让 ananas 服务端发会话级 cookie...")
        await page.wait_for_timeout(5000)

        # 抓所有 cookie
        all_cookies = await ctx.cookies()
        cookie_dict = {c["name"]: c["value"] for c in all_cookies}
        has_videojs = "videojs_id" in cookie_dict
        has_k8s_ed = "k8s-ed" in cookie_dict
        print(f"  [OK] 抓到 {len(all_cookies)} 个 cookie")
        print(f"    videojs_id: {'[OK]' if has_videojs else '[MISSING]'}")
        print(f"    k8s-ed:     {'[OK]' if has_k8s_ed else '[MISSING]'}")
        if not has_videojs and not has_k8s_ed:
            print("  [警告] 没拿到 learning_id 级 cookie,后面下载可能 403")

        await browser.close()
        return cookie_dict


def inject_cookies(session: requests.Session, cookie_dict: dict[str, str]) -> None:
    """把 Playwright 抓到的 cookie 注入 requests.Session。"""
    for k, v in cookie_dict.items():
        session.cookies.set(k, v, domain=".chaoxing.com")


# ─── 下载 URL ─────────────────────────────────────────────────

def get_video_download_url(session, objectid, role: str = "video"):
    """调用 ananas/status API 获取视频下载链接。

    返回:dict {download, http, filename, size, duration, referer}
    - referer 跟 role 对应(下载 cldisk.com 时必须用):
      - video/english → ananas/modules/video/index.html
      - ppt/pdf/attachment → ananas/modules/ppt/index.html(更通用的 document index 也行)
    """
    url = (
        f"https://mooc1.chaoxing.com/ananas/status/{objectid}"
        f"?k=262&flag=normal&ro=0&_dc={int(time.time() * 1000)}"
    )
    # 按 role 选 ananas 域 Referer(让 cldisk 后续能下载成功)
    if role in ("ppt", "pdf", "docx", "doc", "attachment"):
        referer = "https://mooc1.chaoxing.com/ananas/modules/ppt/index.html"
    else:
        referer = "https://mooc1.chaoxing.com/ananas/modules/video/index.html"
    session.headers["Referer"] = referer
    # 同时注入 Origin(Chrome 必带),避免 CORS 预检触发风控
    session.headers["Origin"] = "https://mooc2-ans.chaoxing.com"
    try:
        resp = session.get(url, timeout=30)
        data = resp.json()
        if data.get("status") == "success":
            return {
                "download": data.get("download", ""),
                "http": data.get("http", ""),
                "filename": data.get("filename", ""),
                "size": data.get("length", 0),
                "duration": data.get("duration", 0),
                "referer": referer,
            }
    except Exception as e:
        print(f"  [警告] API 调用失败: {e}")
    return None


def download_video(session, url, filepath, expected_size=0, referer=""):
    """下载视频/文件。

    Args:
        referer: 必填(cldisk.com 403 时需要)— 通常来自 get_video_download_url 返回的 referer。
    """
    try:
        if referer:
            session.headers["Referer"] = referer
        resp = session.get(url, stream=True, timeout=600)
        downloaded = 0
        with open(filepath, "wb") as f:
            for chunk in resp.iter_content(chunk_size=1024 * 1024):
                f.write(chunk)
                downloaded += len(chunk)
                if expected_size:
                    pct = downloaded * 100 // expected_size
                    sys.stdout.write(
                        f"\r  下载中: {pct}% "
                        f"({downloaded / 1024 / 1024:.0f}/"
                        f"{expected_size / 1024 / 1024:.0f}MB)"
                    )
                    sys.stdout.flush()
        print()
        return downloaded
    except Exception as e:
        print(f"\n  [错误] 下载失败: {e}")
        return 0


# ─── 主流程 ──────────────────────────────────────────────────

# 向后兼容:旧版 chaoxing 内部测试还引用这些常量
# 实际解析已迁到 workflows/cli_args.py(4 个 workflow 共享)
_FLAGS_WITH_VALUE = ("--resume", "--retry-downloads", "--max-tabs", "--cpi")
_FLAGS_NO_VALUE = {
    "--scan-only",
    "--verify-resume-only",
    "--outline-only",
    "--playwright",
    "--debug",
    "--include-empty-lessons",
}


def _extract_positional_args(argv: list[str]) -> list[str]:
    """过滤所有 --flag + value / --flag(无 value),只留真正的 positional 参数。

    跟 workflows/cli_args.extract_positional_args 同源,保留为 chaoxing 内部别名
    (避免破坏外部直接 import chaoxing._extract_positional_args 的代码)。

    用 absolute import(不是 `from .cli_args`):直跑文件也能用,
    模块路径跑也能用(只要 _PROJECT_ROOT 在 sys.path)。
    """
    from scrape_new.workflows.cli_args import extract_positional_args
    return extract_positional_args(argv)


def main():
    # 解析参数(走共享 cli_args)
    # absolute import:同时支持 `python -m` 和 `python scrape_new/workflows/chaoxing.py`
    from scrape_new.workflows.cli_args import parse_workflow_args, print_workflow_usage
    parsed = parse_workflow_args(sys.argv[1:], default_output=DEFAULT_OUTPUT)
    if parsed.error is not None or not parsed.url:
        print_workflow_usage("chaoxing", DEFAULT_OUTPUT)
        if parsed.error:
            print(f"[错误] {parsed.error}")
        sys.exit(1)

    course_url = parsed.url
    output_dir = parsed.output_dir

    # 课程标题(用于审计文件)— 当前未自动抽取,默认用 output_dir basename
    # 后续可从 tchcourse 页面解析 <h1 class="course-title"> 之类
    course_title = os.path.basename(os.path.abspath(output_dir))

    # resume / retry(走共享 cli_args)
    resume_manifest = parsed.resume_manifest
    retry_only_keys = parsed.retry_only_keys
    if resume_manifest is not None and not resume_manifest.exists():
        print(f"[警告] --resume manifest 不存在(首次跑?),忽略: {resume_manifest}")
        resume_manifest = None

    # --max-tabs / --scan-only / --verify-resume-only(走共享 cli_args)
    max_tabs = parsed.max_tabs
    scan_only = parsed.scan_only
    verify_resume_only = parsed.verify_resume_only
    include_empty_lessons = "--include-empty-lessons" in sys.argv
    if include_empty_lessons and not scan_only:
        print("[警告] --include-empty-lessons 仅对 --scan-only 生效,忽略")
        include_empty_lessons = False

    # 提取参数
    params = extract_params(course_url)
    course_id = params["courseId"]
    clazz_id = params["clazzid"]

    if not course_id or not clazz_id:
        print("[错误] 无法从 URL 提取 courseId 或 clazzid")
        print("  请确认 URL 格式正确，包含 courseId 和 clazzid 参数")
        sys.exit(1)

    print("=" * 60)
    print("超星学习通课程视频下载")
    print(f"  课程ID: {course_id}")
    print(f"  班级ID: {clazz_id}")
    print(f"  输出: {output_dir}")
    print("=" * 60)

    # 创建 Session
    session = requests.Session()
    session.headers.update(HEADERS)

    # 解析 --cookies-file(外部 cookie 路径, 可在 repo 外)
    cookies_file_arg: str | None = None
    if "--cookies-file" in sys.argv:
        idx = sys.argv.index("--cookies-file")
        if idx + 1 >= len(sys.argv):
            print("[错误] --cookies-file 需要路径参数")
            sys.exit(1)
        cookies_file_arg = sys.argv[idx + 1]

    # 加载 Cookie
    script_dir = os.path.dirname(os.path.abspath(__file__))
    project_root = os.path.dirname(os.path.dirname(script_dir))
    cookies_path = os.path.join(project_root, COOKIES_FILE)
    if not os.path.exists(cookies_path):
        cookies_path = COOKIES_FILE
    load_cookies(session, cookies_path, cookies_file=cookies_file_arg)

    # 验证 Cookie
    if not validate_cookie(session, course_url):
        sys.exit(1)

    # --playwright 模式:真点视频建立 ananas 会话(绕 learning_id 风控)
    if "--playwright" in sys.argv:
        if not HAS_PLAYWRIGHT:
            print("[错误] 需要 playwright — pip install playwright && playwright install chromium")
            sys.exit(1)
        import asyncio
        # 把当前 cookie 转成字符串给 Playwright 用
        cookie_str = "; ".join(f"{c.name}={c.value}" for c in session.cookies)
        cookie_dict = asyncio.run(build_ananas_session(cookie_str, course_url))
        inject_cookies(session, cookie_dict)
        print(f"  [OK] ananas 会话 cookie 已注入 requests session")

    # 获取 cpi(parsed.cpi 已经从 --cpi 旗标提取)
    cpi = get_cpi(session, course_url, cpi_override=parsed.cpi)
    if cpi:
        print(f"[OK] cpi: {cpi}")
    else:
        print("[警告] 未找到 cpi，尝试继续...")

    # 获取章节树
    lessons = get_chapter_tree(
        session, course_id, clazz_id,
        course_url=course_url,
        output_dir=output_dir,
        debug=("--debug" in sys.argv),
        cpi=cpi,
    )
    if not lessons:
        print("[错误] 未找到任何章节")
        sys.exit(1)

    # outline-only 模式:直接从 lessons 构造 outline,跳过资源扫描
    outline_only = "--outline-only" in sys.argv
    if outline_only:
        video_dir = os.path.join(output_dir, "视频")
        os.makedirs(video_dir, exist_ok=True)
        if HAS_OUTLINE:
            try:
                # 不依赖 all_videos(那需要扫描每个 lesson 资源)
                # 直接从 lessons 列表构造 outline
                lessons_for_outline = [
                    {
                        "ch_num": ls["ch_num"],
                        "chapter": ls["parent"],
                        "lesson": ls["name"],
                        "objectid": "",  # outline-only 不查 objectid
                    }
                    for ls in lessons
                ]
                chapters_data = videos_to_outline_chapters([
                    {
                        "ch_num": v.get("ch_num", 0),
                        "chapter": v.get("chapter", ""),
                        "lesson": v.get("lesson", ""),
                        "filename": None,  # 没下载,没文件名
                        "platform_meta": {
                            "lesson_id": v.get("id", 0),
                            "objectid": v.get("objectid", ""),
                        },
                    }
                    for v in lessons_for_outline
                ])
                outline_path = write_outline(
                    out_dir=video_dir,
                    chapters=chapters_data,
                    source_url=course_url,
                    platform="chaoxing",
                    course_title="",
                )
                print(f"  [目录] {outline_path}")
                print(f"  [统计] {len(chapters_data)} 个 lesson(无 video_filename,等下载后补)")
            except Exception as e:
                print(f"  [警告] 写章节目录失败: {e}")
        print()
        print("=" * 60)
        print("扫描完成! (outline-only 模式,未下载视频)")
        print(f"  找到 {len(lessons)} 个课时,章节树已保存到 _chapter_outline.json")
        print(f"  输出: {os.path.abspath(video_dir)}")
        print("=" * 60)
        return


    # ─── 扫描全部资源(串行,逐课逐Tab,存盘可恢复) ──────────
    # 注:cache 文件名改 _scan_cache.json(原来是 _scanned_resources.json)
    #    避免和 scan-only 模式写的 _scanned_resources.json 冲突
    scan_cache = os.path.join(output_dir, "_scan_cache.json")
    if os.path.exists(scan_cache):
        with open(scan_cache, "r", encoding="utf-8") as _f:
            cached = json.load(_f)
        all_videos = cached.get("videos", [])
        all_docs = cached.get("docs", [])
        # 空缓存=上次限流空跑,直接丢弃
        if not all_videos and not all_docs:
            os.remove(scan_cache)
            print(f"\n[扫描] 上次缓存为空(限流中),已删除,重新扫描")
        else:
            print(f"\n[扫描] 发现缓存 {os.path.basename(scan_cache)},跳过扫描")
            seen_objids = {r["objectid"] for r in all_videos if r.get("objectid")}
            seen_objids |= {r["objectid"] for r in all_docs if r.get("objectid")}
            print(f"  从缓存加载: {len(all_videos)} 视频 + {len(all_docs)} 文档")
    else:
        total_lessons = len(lessons)
        all_videos = []
        all_docs = []

        def _save_scan_cache():
            os.makedirs(output_dir, exist_ok=True)
            with open(scan_cache, "w", encoding="utf-8") as _f:
                json.dump({"videos": all_videos, "docs": all_docs}, _f, ensure_ascii=False)

        # --playwright 模式:用真浏览器扫描(cards API 反爬拦截 requests)
        if "--playwright" in sys.argv and HAS_PLAYWRIGHT:
            print(f"\n[扫描] Playwright 模式:用真浏览器扫描 {total_lessons} 节课...")
            cookie_str = "; ".join(f"{c.name}={c.value}" for c in session.cookies)
            all_videos, all_docs = asyncio.run(
                scan_all_resources_playwright(
                    cookie_str, course_url, lessons,
                    course_id, clazz_id, cpi or "",
                )
            )
            _save_scan_cache()
        else:
            # 限流节流:只串行,每课只拉 num=0(视频Tab),课间 8-15 秒随机延迟
            # 45课 × 1请求 × 2秒 = 耗时 ~8-10分钟,请求密度 ~6次/分钟(远低于限流阈值)
            estimate_min = (total_lessons * 10) // 60
            print(f"\n[扫描] {total_lessons} 节课,串行逐课扫描(预计 {estimate_min} 分钟)...")
            print(f"  策略: 每课扫 max_tabs={max_tabs} 个 Tab + 课间 8-15s 随机延迟(仿人类浏览)")
            seen_objids = set()
            _consec_202 = 0

            for i, lesson in enumerate(lessons):
                v_count_before = len(all_videos)
                d_count_before = len(all_docs)
                videos, docs, rl_count, failed_tabs = scan_lesson_resources(
                    session, lesson, course_id, clazz_id, cpi or "",
                    debug_once=(i == 0), max_tabs=max_tabs,
                )
                # 限流检测: 首次遇到 202 且尚未扫到任何资源 → 冷却期
                if rl_count >= 1 and len(all_videos) == 0 and len(all_docs) == 0:
                    _consec_202 += 1
                    if _consec_202 >= 3:
                        print(f"\n  [冷却] UID 仍在限流中")
                        print(f"  缓存已存盘,等冷却后重跑自动恢复")
                        _save_scan_cache()
                        sys.exit(2)
                elif rl_count >= 1:
                    _consec_202 += 1
                    if _consec_202 >= 3:
                        print(f"\n  [中断] 连续 {_consec_202} 课 202(限流)")
                        print(f"  已扫 {len(all_videos)}V+{len(all_docs)}D,缓存已存盘,等冷却后重跑自动恢复")
                        _save_scan_cache()
                        sys.exit(2)
                else:
                    _consec_202 = 0

                for r in videos:
                    if r["objectid"] and r["objectid"] not in seen_objids:
                        seen_objids.add(r["objectid"])
                        all_videos.append({
                            "objectid": r["objectid"],
                            "name": r["name"],
                            "lesson": lesson["name"],
                            "chapter": lesson["parent"],
                            "ch_num": lesson["ch_num"],
                            "ls_num": lesson.get("ls_num", 0),
                            "tab_num": r.get("tab_num", 0),
                            "role": r.get("role", "video"),
                            "type": r.get("type", ""),
                        })
                for r in docs:
                    if r["objectid"] and r["objectid"] not in seen_objids:
                        seen_objids.add(r["objectid"])
                        all_docs.append({
                            "objectid": r["objectid"],
                            "name": r["name"],
                            "type": r.get("type", ""),
                            "size": r.get("size", 0),
                            "lesson": lesson["name"],
                            "chapter": lesson["parent"],
                            "ch_num": lesson["ch_num"],
                            "ls_num": lesson.get("ls_num", 0),
                            "tab_num": r.get("tab_num", 0),
                            "role": r.get("role", "attachment"),
                        })
                new_v = len(all_videos) - v_count_before
                new_d = len(all_docs) - d_count_before
                sys.stdout.write(
                    f"\r  [{i+1}/{total_lessons}] "
                    f"+{new_v}V +{new_d}D (共 {len(all_videos)}V {len(all_docs)}D)"
                )
                sys.stdout.flush()
                # 仿人类浏览:课间 8-15 秒随机延迟
                delay = 8 + (i * 7 % 8)
                time.sleep(delay)

        # 扫描完成,存盘
        _save_scan_cache()

    print(f"\n[OK] 共找到 {len(all_videos)} 个视频 + {len(all_docs)} 个文档")

    # ─── scan-only 早返:写 4 个报告,不进入下载循环 ──────
    if scan_only:
        from scrape_new.services.scan_chaoxing import (
            build_scan_context, write_scan_reports, detect_suspicious_lessons,
            LessonScanResult,
        )
        # 1) 把 all_videos / all_docs 拼回 lessons(每节一组),便于报告
        lessons_grouped: dict[tuple[int, int], list[dict]] = {}
        lesson_meta: dict[tuple[int, int], dict] = {}
        for v in all_videos:
            key = (v.get("ch_num", 0), v.get("ls_num", 0))
            lessons_grouped.setdefault(key, []).append(v)
            lesson_meta.setdefault(key, {
                "chapter": v.get("chapter", ""),
                "lesson": v.get("lesson", ""),
            })
        for d in all_docs:
            key = (d.get("ch_num", 0), d.get("ls_num", 0))
            lessons_grouped.setdefault(key, []).append(d)
            lesson_meta.setdefault(key, {
                "chapter": d.get("chapter", ""),
                "lesson": d.get("lesson", ""),
            })

        # 关键:用完整 lessons 章节树(chaoxing.py 顶部 get_chapter_tree 的结果)
        # 遍历所有章/节,即使该节没资源也要进 lesson_results(标 empty_lesson)
        # 旧实现只遍历 lessons_grouped.keys(),漏了没资源的节
        lesson_results: list[LessonScanResult] = []
        seen_keys: set[tuple[int, int]] = set()
        for ls in lessons:
            ch = ls.get("ch_num", 0)
            ls_n = ls.get("ls_num", 0)
            key = (ch, ls_n)
            if key in seen_keys:
                continue  # chaoxing 的 lessons 可能含重复(防御)
            seen_keys.add(key)
            resources = lessons_grouped.get(key, [])
            meta = lesson_meta.get(key, {
                "chapter": ls.get("parent", ""),
                "lesson": ls.get("name", ""),
            })
            videos = [r for r in resources if r.get("role") in ("video", "english")]
            docs = [r for r in resources if r.get("role") not in ("video", "english", "unknown")]
            unknown = [r for r in resources if r.get("role") == "unknown"]
            lesson_results.append(LessonScanResult(
                ch_num=ch, ls_num=ls_n,
                chapter=meta.get("chapter", "") or ls.get("parent", ""),
                lesson=meta.get("lesson", "") or ls.get("name", ""),
                lesson_id=f"{ch}.{ls_n}",
                videos=videos, docs=docs, unknown_resources=unknown,
            ))
        # 2) 构造 ScanContext(内部自动 detect_suspicious_lessons,避免重复)
        ctx = build_scan_context(
            course_id=str(course_id), course_title=course_title,
            lessons=lesson_results, stopped_reason="",
        )
        # 4) 写 4 个报告
        os.makedirs(output_dir, exist_ok=True)
        paths = write_scan_reports(ctx, Path(output_dir))
        # 5) 生成 _chapter_tree.json/md(走 resource_manifest 既有 API)
        try:
            from scrape_new.services.resource_manifest import (
                build_chapter_tree_data, write_chapter_tree_json, write_chapter_tree_md,
            )
            # 关键:build_chapter_tree_data 形参是 (course_title, platform, source_url, all_videos, all_docs, *, lessons_meta)
            # lessons_meta 用本节扫描出来的 lessons 列表(每个含 ch_num/ls_num/chapter/lesson/...)
            # 注:此处的 lessons 是 chaoxing.py 扫描结果(含 id/ch_num/ls_num/parent/name),
            # build_chapter_tree_data 期望 (chapter_index, chapter_title, lesson_index, lesson_title) — 通过 lessons_meta 透传
            lessons_meta = [
                {
                    "chapter_index": ls.get("ch_num", 0),
                    "chapter_title": ls.get("parent", ""),
                    "lesson_index": ls.get("ls_num", 0),
                    "lesson_title": ls.get("name", ""),
                    "knowledge_id": str(ls.get("id", "")),
                }
                for ls in lessons
            ]
            tree = build_chapter_tree_data(
                course_title=course_title,
                platform="chaoxing",
                source_url=course_url,
                all_videos=all_videos,
                all_docs=all_docs,
                lessons_meta=lessons_meta,
            )
            # 关键:write_chapter_tree_json/md 第二参数是 output_dir(目录),不是文件路径
            # 旧代码 Path(output_dir) / "_chapter_tree.json" 会让它在 output_dir 下再创建 _chapter_tree.json/_chapter_tree.json
            write_chapter_tree_json(tree, Path(output_dir))
            write_chapter_tree_md(tree, Path(output_dir))
            print(f"\n[scan-only] 报告已写入 {output_dir}:")
            for k, p in paths.items():
                print(f"  - {os.path.basename(p)}")
            print(f"  - _chapter_tree.json / _chapter_tree.md")
        except Exception as e:
            logger.warning(f"写 _chapter_tree 失败: {e}")
        s = ctx.summary()
        print()
        print("=" * 60)
        print("scan-only 完成!")
        print(f"  发现 {s['discovered']} 资源(可下载 {s['downloadable']}, 未知 {s['unknown']})")
        print(f"  失败 Tab:{s['failed_tabs']} / 可疑节:{s['suspicious_lessons']}")
        print(f"  课数:{s['lessons_total']}")
        print("=" * 60)
        return

    # ─── verify-resume-only 早返:不下载,只判断哪些会跳过/重下 ──────
    if verify_resume_only:
        # 1) normalize 给每个资源加 resource_key
        try:
            from scrape_new.services.download_resume import normalize_download_resources
            normalize_download_resources(all_videos, all_docs, str(course_id))
        except Exception as e:
            logger.warning(f"normalize 失败: {e}")
        # 2) 模拟 apply_resume_decisions(不真改 status,只算计划)
        from scrape_new.services.download_resume import _is_file_ok
        video_dir = os.path.join(output_dir, "视频")
        doc_dir = os.path.join(output_dir, "文档")
        os.makedirs(video_dir, exist_ok=True)
        os.makedirs(doc_dir, exist_ok=True)
        import json as _json
        raw = Path(resume_manifest).read_text(encoding="utf-8")
        cached = _json.loads(raw)
        records = cached.get("records", [])

        will_skip: list[dict] = []
        will_retry: list[dict] = []
        for v in all_videos:
            saved = v.get("filename", "")
            local = Path(video_dir) / saved
            prev_size = 0
            prev_status = ""
            for r in records:
                if r.get("resource_key") == v.get("resource_key") or r.get("saved_name") == saved:
                    prev_status = r.get("status", "")
                    prev_size = int(r.get("size_bytes") or 0)
                    break
            if prev_status in ("downloaded", "skipped_existing") and _is_file_ok(local, prev_size):
                will_skip.append({"kind": "video", "name": saved,
                                  "reason": f"resume: 旧 manifest {prev_status} + 文件存在"})
            else:
                will_retry.append({"kind": "video", "name": saved,
                                   "reason": f"将重下(状态={prev_status or 'new'} or 文件缺失)"})
        for d in all_docs:
            saved = d.get("filename", "")
            local = Path(doc_dir) / saved
            prev_size = 0
            prev_status = ""
            for r in records:
                if r.get("resource_key") == d.get("resource_key") or r.get("saved_name") == saved:
                    prev_status = r.get("status", "")
                    prev_size = int(r.get("size_bytes") or 0)
                    break
            if prev_status in ("downloaded", "skipped_existing") and _is_file_ok(local, prev_size):
                will_skip.append({"kind": "doc", "name": saved,
                                  "reason": f"resume: 旧 manifest {prev_status} + 文件存在"})
            else:
                will_retry.append({"kind": "doc", "name": saved,
                                   "reason": f"将重下(状态={prev_status or 'new'} or 文件缺失)"})
        # 写报告
        plan_path = Path(output_dir) / "_resume_plan.json"
        plan_path.parent.mkdir(parents=True, exist_ok=True)
        plan_path.write_text(_json.dumps({
            "course_id": str(course_id),
            "resume_manifest": str(resume_manifest),
            "summary": {
                "will_skip": len(will_skip),
                "will_retry": len(will_retry),
            },
            "will_skip": will_skip,
            "will_retry": will_retry,
        }, ensure_ascii=False, indent=2), encoding="utf-8")
        print()
        print("=" * 60)
        print("verify-resume-only 完成!")
        print(f"  将跳过:{len(will_skip)} 资源")
        print(f"  将重下:{len(will_retry)} 资源")
        print(f"  报告:{plan_path}")
        print("=" * 60)
        return

    if not all_videos and not all_docs:
        print("[结束] 没有找到任何资源")
        sys.exit(0)

    # 步骤 1:统一初始化 role / filename / resource_key / source_meta
    # 必须在 resume / retry 决策之前,否则 apply_resume_decisions 看不到 key
    try:
        from scrape_new.services.download_resume import normalize_download_resources
        normalize_download_resources(all_videos, all_docs, str(course_id))
    except Exception as e:
        logger.warning(f"normalize 失败(继续,字段可能不齐): {e}")

    # 步骤 2:--retry-downloads 不依赖 --resume
    # 用独立函数 apply_retry_filter — 空集/非空集都处理,不依赖 resume manifest
    if retry_only_keys is not None:
        try:
            from scrape_new.services.download_resume import apply_retry_filter
            fstats = apply_retry_filter(all_videos, all_docs, retry_only_keys)
            if not retry_only_keys:
                print(
                    f"\n  [retry-downloads] retry_only_keys 为空,"
                    f"全标 skipped_existing,不走下载"
                )
            else:
                print(
                    f"\n  [retry-downloads] 保留 {fstats['kept_videos']} 视频 + "
                    f"{fstats['kept_docs']} 文档,"
                    f"过滤 {fstats['filtered_videos']} 视频 + {fstats['filtered_docs']} 文档"
                )
        except Exception as e:
            logger.warning(f"apply_retry_filter 失败(继续): {e}")

    # 步骤 3:--resume:从历史 manifest 标记可跳过的资源(在创建目录前做,免得空目录)
    if resume_manifest is not None:
        try:
            from scrape_new.services.download_resume import apply_resume_decisions
            # 视频/文档目录可能还没建,这里按 _is_file_ok 检查会返 False,
            # 反正 apply_resume_decisions 内部会优雅处理
            stats = apply_resume_decisions(
                all_videos, all_docs,
                resume_manifest,
                Path(os.path.join(output_dir, "视频")),
                Path(os.path.join(output_dir, "文档")),
                retry_only_keys=None,  # 已被步骤 2 处理,这里不重复过滤
            )
            msg = (
                f"\n  [resume] 跳过 {stats['skipped_videos']} 视频 + "
                f"{stats['skipped_docs']} 文档,missing={stats['missing_keys']}"
            )
            if stats.get("retry_filtered", 0):
                msg += f",retry 过滤 {stats['retry_filtered']}"
            print(msg)
        except Exception as e:
            logger.warning(f"resume 处理失败(继续,全重下): {e}")

    # 创建输出目录
    video_dir = os.path.join(output_dir, "视频")
    doc_dir = os.path.join(output_dir, "文档")
    os.makedirs(video_dir, exist_ok=True)
    os.makedirs(doc_dir, exist_ok=True)
    video_dir = os.path.join(output_dir, "视频")
    doc_dir = os.path.join(output_dir, "文档")
    os.makedirs(video_dir, exist_ok=True)
    os.makedirs(doc_dir, exist_ok=True)

    # ─── 下载视频 ───
    print(f"\n[下载] 开始下载 {len(all_videos)} 个视频...\n")
    success_v = 0
    failed_v = 0
    skipped_v = 0

    # 同一课时内多视频按 (ch_num, ls_num, role) 分桶,按 role 选名:
    #   主视频 (num=0/其它):单文件无序号;多文件 _1 / _2
    #   英文视频 (num=2):_English,多个 _English_2 / _English_3
    #
    # role 判定走 english_detect(智能):文件名/标题关键字 > 同节英文比例 > tab_num 兜底
    # 此时 filename 还没生成,所以先用 lesson 名 + tab_num 启发;下载循环结束后再 refine 一次
    from scrape_new.services.english_detect import detect_role, classify_lesson_videos

    # normalize_download_resources 已经在 main() 早期调用过,
    # 这里直接读 v["role"] / v["filename"] / v["resource_key"](不再重算)
    # 如果某 v 意外没 normalize(防御),兜底按"video"处理
    for i, v in enumerate(all_videos):
        ch_num = v.get("ch_num", 0)
        ls_num = v.get("ls_num", 0)
        tab_num = v.get("tab_num", 0)
        role = v.get("role") or "video"
        filename = v.get("filename") or ""
        # 按 role 分流目录:视频/英文视频 → 视频/,PPT/PDF/文档 → 文档/
        # (chaoxing 把 PPT 也挂 tab=0,跟视频混一起;但实际是非视频资源,必须分流)
        if role in ("ppt", "pdf", "docx", "doc", "attachment", "image"):
            target_dir = doc_dir
        else:
            target_dir = video_dir
        filepath = os.path.join(target_dir, filename) if filename else ""
        # 审计字段 — 只在空时 init,保留 normalize / resume / retry 设的状态
        if not v.get("status"):
            v["status"] = _STATUS_FAILED
        v["role"] = role
        v["filename"] = filename
        v["size_bytes"] = v.get("size_bytes", 0) or 0
        v["reason"] = v.get("reason", "") or ""
        if v.get("source_meta") is None:
            v["source_meta"] = {
                "objectid": v.get("objectid", ""),
                "knowledge_id": v.get("lesson_id") or v.get("id", ""),
                "tab_num": tab_num,
                "mid": v.get("mid", ""),
                "jobid": v.get("jobid", ""),
            }
        # resource_key 也从 normalize 读(防御兜底,正常情况下 normalize 已写)
        if not v.get("resource_key"):
            try:
                from scrape_new.upload.resource_key import make_resource_key
                v["resource_key"] = make_resource_key(
                    course_id=str(course_id),
                    chapter_index=int(ch_num),
                    lesson_id=str(ls_num),
                    role=role,
                    saved_name=filename,
                )
            except Exception as _rk_err:
                logger.debug(f"video resource_key 兜底失败: {_rk_err}")
                v["resource_key"] = ""

        # 早期跳过:resume / retry 已标 skipped_existing → 不调任何网络
        if v.get("status") == _STATUS_SKIPPED_EXISTING:
            print(f"[{i + 1}/{len(all_videos)}] [跳过] {filename}  ({v.get('reason','')})")
            skipped_v += 1
            continue

        print(f"[{i + 1}/{len(all_videos)}] {filename}")
        info = get_video_download_url(session, v["objectid"], role=v.get("role", "video"))
        if not info:
            print("  [失败] 无法获取下载链接")
            v["reason"] = "无法获取下载链接"
            failed_v += 1
            continue

        download_url = info["download"] or info["http"]
        if not download_url:
            print("  [失败] 无下载链接")
            v["reason"] = "无下载链接"
            v["source_meta"]["url"] = ""
            failed_v += 1
            continue
        v["source_meta"]["url"] = download_url

        file_size = info["size"]
        if file_size:
            print(f"  大小: {file_size / 1024 / 1024:.0f}MB")

        # 用 API 返回的文件大小校验已有文件(不再用硬编码 100KB)
        if os.path.exists(filepath):
            existing = os.path.getsize(filepath)
            if file_size > 0 and existing >= file_size * 0.95:
                print(f"  [已存在] 完整 ({existing/1024/1024:.1f}MB)")
                v["status"] = _STATUS_SKIPPED_EXISTING
                v["size_bytes"] = existing
                v["reason"] = "file already exists (完整)"
                skipped_v += 1
                continue
            elif existing > 5 * 1024 * 1024:
                print(f"  [已存在] ({existing/1024/1024:.1f}MB, 无 API 大小参考)")
                v["status"] = _STATUS_SKIPPED_EXISTING
                v["size_bytes"] = existing
                v["reason"] = "file already exists (无 API 大小参考)"
                skipped_v += 1
                continue
            else:
                print(f"  [残缺] 仅 {existing/1024/1024:.1f}MB，重新下载...")
                v["reason"] = "残缺,重新下载"
        try:
            downloaded = download_video(session, download_url, filepath, file_size,
                                        referer=info.get("referer", ""))
        except Exception as e:
            print(f"  [失败] 下载异常: {e}")
            v["reason"] = f"下载异常: {e}"
            failed_v += 1
            continue
        if downloaded > 100000:
            print(f"  [完成] {downloaded / 1024 / 1024:.1f}MB")
            v["status"] = _STATUS_DOWNLOADED
            v["size_bytes"] = downloaded
            success_v += 1
        elif downloaded > 0:
            print(f"  [可疑] 文件过小: {downloaded / 1024:.1f}KB")
            v["status"] = _STATUS_SUSPICIOUS
            v["size_bytes"] = downloaded
            v["reason"] = f"文件过小({downloaded / 1024:.1f}KB < 100KB 阈值)"
            failed_v += 1
        else:
            v["reason"] = "下载返回 0 字节"
            failed_v += 1
            if os.path.exists(filepath):
                os.remove(filepath)
        time.sleep(DELAY_BETWEEN_DOWNLOADS)

    # ─── 下载文档 ───
    success_d = 0
    failed_d = 0
    skipped_d = 0  # P2:文档 skipped_existing 单独统计,不混入 success_d
    if all_docs:
        print(f"\n[下载] 开始下载 {len(all_docs)} 个文档...\n")
        # 同课多文档:1 个 → 角色后缀(_PPT/_课件);多个 → "_附件_N" 追加
        _per_lesson_doc_role: dict[tuple[int, int, str], int] = {}
        for i, d in enumerate(all_docs):
            ch_num = d.get("ch_num", 0)
            ls_num = d.get("ls_num", 0)
            ext = os.path.splitext(d["name"])[1].lstrip(".").lower() or "pdf"
            # role 决定后缀标签
            role = {
                "pptx": "ppt", "ppt": "ppt",
                "pdf": "pdf",
                "docx": "docx", "doc": "doc",
            }.get(ext, "attachment")
            seq_key = (ch_num, ls_num, role)
            _per_lesson_doc_role[seq_key] = _per_lesson_doc_role.get(seq_key, 0) + 1
            seq = _per_lesson_doc_role[seq_key]
            lesson_id = f"{ch_num}.{ls_num}"
            filename = lesson_filename(
                lesson_id, d["lesson"], role=role,
                index=seq if seq > 1 else None,
                ext=ext,
            )
            filepath = os.path.join(doc_dir, filename)
            # 审计字段 — 只在空时 init,保留 normalize / resume / retry 设的状态
            if not d.get("status"):
                d["status"] = _STATUS_FAILED
            d["role"] = role
            d["filename"] = filename
            d["size_bytes"] = d.get("size_bytes", 0) or 0
            d["reason"] = d.get("reason", "") or ""
            if d.get("source_meta") is None:
                d["source_meta"] = {
                    "objectid": d.get("objectid", ""),
                    "knowledge_id": d.get("lesson_id") or d.get("id", ""),
                }
            # 稳定 resource_key
            if not d.get("resource_key"):
                try:
                    from scrape_new.upload.resource_key import make_resource_key
                    d["resource_key"] = make_resource_key(
                        course_id=str(course_id),
                        chapter_index=int(ch_num),
                        lesson_id=str(ls_num),
                        role=role,
                        saved_name=filename,
                    )
                except Exception as _rk_err:
                    logger.debug(f"doc resource_key 失败: {_rk_err}")
                    d["resource_key"] = ""

            # 早期跳过:resume / retry 已标 skipped_existing → 不调任何网络
            if d.get("status") == _STATUS_SKIPPED_EXISTING:
                print(f"[{i + 1}/{len(all_docs)}] [跳过] {filename}  ({d.get('reason','')})")
                skipped_d += 1
                continue

            if os.path.exists(filepath) and os.path.getsize(filepath) > 500:
                print(f"[{i + 1}/{len(all_docs)}] [已存在] {filename}")
                d["status"] = _STATUS_SKIPPED_EXISTING
                d["size_bytes"] = os.path.getsize(filepath)
                d["reason"] = "file already exists"
                skipped_d += 1
                continue

            print(f"[{i + 1}/{len(all_docs)}] {filename} ({d.get('size', 0)/1024:.0f}KB)")
            # 文档也用 ananas/status API(走统一入口,自动按 role 设 Referer)
            doc_info = get_video_download_url(session, d["objectid"],
                                              role=d.get("role", "attachment"))
            try:
                if not doc_info:
                    raise RuntimeError("无法获取下载链接")
                dl = doc_info["download"] or doc_info["http"] or ""
                if dl:
                    d["source_meta"]["url"] = dl
                    downloaded = download_video(session, dl, filepath, d.get("size", 0),
                                                referer=doc_info.get("referer", ""))
                    if downloaded > 500:
                        print(f"  [完成] {downloaded / 1024:.1f}KB")
                        d["status"] = _STATUS_DOWNLOADED
                        d["size_bytes"] = downloaded
                        success_d += 1
                    else:
                        print(f"  [失败] 文件过小")
                        d["reason"] = f"文件过小({downloaded}B)"
                        failed_d += 1
                        if os.path.exists(filepath):
                            os.remove(filepath)
                else:
                    print(f"  [失败] 无下载链接")
                    d["reason"] = "无下载链接"
                    failed_d += 1
            except Exception as e:
                print(f"  [失败] {e}")
                d["reason"] = f"异常: {e}"
                failed_d += 1
            time.sleep(DELAY_BETWEEN_DOWNLOADS)
    else:
        print(f"\n[文档] 没有找到文档资源")

    # ─── 写章节目录文件 ───
    if HAS_OUTLINE and (all_videos or all_docs):
        try:
            # 按 (ch_num, ls_num) 分桶:主视频 → video_filename,其它(英文视频/文档) → extra_filenames
            bucket: dict[tuple[int, int], dict[str, Any]] = {}
            for v in all_videos:
                key = (v.get("ch_num", 0), v.get("ls_num", 0))
                if key not in bucket:
                    bucket[key] = {
                        "ch_num": key[0], "ls_num": key[1],
                        "chapter": v.get("chapter", ""),
                        "lesson": v.get("lesson", ""),
                        "filename": None, "extra_filenames": [],
                    }
                if v.get("tab_num", 0) == 2:
                    # 英文视频 → 附件
                    if v.get("filename"):
                        bucket[key]["extra_filenames"].append(v["filename"])
                else:
                    # 主视频(取第一个 num=0)
                    if bucket[key]["filename"] is None and v.get("filename"):
                        bucket[key]["filename"] = v["filename"]
            # 文档(PPT/PDF)统一进 extra_filenames
            for d in all_docs:
                key = (d.get("ch_num", 0), d.get("ls_num", 0))
                if key not in bucket:
                    bucket[key] = {
                        "ch_num": key[0], "ls_num": key[1],
                        "chapter": d.get("chapter", ""),
                        "lesson": d.get("lesson", ""),
                        "filename": None, "extra_filenames": [],
                    }
                if d.get("filename"):
                    bucket[key]["extra_filenames"].append(d["filename"])

            all_items = list(bucket.values())
            chapters_data = videos_to_outline_chapters(all_items)
            outline_path = write_outline(
                out_dir=video_dir,
                chapters=chapters_data,
                source_url=course_url,
                platform="chaoxing",
                course_title="",
            )
            print(f"  目录: {outline_path}")
        except Exception as e:
            print(f"  [警告] 写章节目录失败: {e}")

    # ─── 报告 ───
    total_found = len(all_videos) + len(all_docs)
    total_ok = success_v + success_d
    total_failed = failed_v + failed_d
    total_skipped = skipped_v + skipped_d  # P2:文档跳过计入 skipped
    print()
    print("=" * 60)
    print("下载完成!")
    print(f"  发现: {len(all_videos)} 视频 + {len(all_docs)} 文档 = {total_found}")
    print(f"  成功: {success_v} 视频 + {success_d} 文档 = {total_ok}")
    print(f"  跳过: {skipped_v} 视频 + {skipped_d} 文档 = {total_skipped}")
    print(f"  失败: {failed_v} 视频 + {failed_d} 文档 = {total_failed}")
    print(f"  差额: {total_found - total_ok - total_skipped - total_failed}")
    print(f"  视频: {os.path.abspath(video_dir)}")
    print(f"  文档: {os.path.abspath(doc_dir)}")
    print("=" * 60)

    # ─── 资源审计:写 4 个文件(_chapter_tree.json/md + _resource_naming_manifest.json/csv)
    # 用 try/finally 保护:即使主流程异常,前面已记录的资源也能落到清单
    if HAS_MANIFEST and (all_videos or all_docs):
        # 二次精化 role:此时 filename 已生成,可以根据文件名再 verify 一次
        # (比如用户拿到的原始资源有 "english" 关键字但 tab_num 异常,或反之)
        try:
            from scrape_new.services.english_detect import (
                classify_lesson_videos, classify_lesson_docs,
            )
            # 按 lesson 分桶
            refine_by_lesson: dict[tuple[int, int], list[dict]] = {}
            for v in all_videos:
                key = (v.get("ch_num", 0), v.get("ls_num", 0))
                refine_by_lesson.setdefault(key, []).append(v)
            for key, vids in refine_by_lesson.items():
                # 把每个 video 的 name/title/filename 喂进 detect_role 看是否需要 override
                for v in vids:
                    new_role = detect_role(
                        filename=v.get("filename", ""),
                        title=v.get("name") or v.get("lesson", ""),
                        tab_num=v.get("tab_num"),
                        same_lesson_videos=vids,
                    )
                    # 仅当结果跟初判不同时打 INFO,方便人审核
                    if new_role != v.get("role") and new_role in ("video", "english"):
                        v["role"] = new_role
            classify_lesson_docs(all_docs)
        except Exception as e:
            logger.warning(f"role 二次精化失败(继续): {e}")

        try:
            paths = write_download_resource_manifests(
                course_title=course_title,
                platform="chaoxing",
                source_url=course_url,
                all_videos=all_videos,
                all_docs=all_docs,
                output_dir=output_dir,
                lessons_meta=[
                    {
                        "ch_num": ls.get("ch_num", 0),
                        "ls_num": ls.get("ls_num", 0),
                        "chapter": ls.get("parent", ""),
                        "lesson": ls.get("name", ""),
                        "knowledge_id": str(ls.get("id", "")),
                    }
                    for ls in lessons
                ],
            )
            print("  资源审计:")
            for k, p in paths.items():
                print(f"    - {k}: {p}")
        except Exception as e:
            logger.exception("写资源审计文件失败")
            print(f"  [警告] 写资源审计失败: {e}")

    # ─── 速览表 ───
    if HAS_OUTLINE and (all_videos or all_docs):
        try:
            from scrape_new.upload.outline import build_structure_from_outline, read_outline
            structure = build_structure_from_outline(outline_path)
            _print_mapping_summary(structure, Path(video_dir))
        except Exception as e:
            print(f"  [警告] 速览表生成失败: {e}")


def _print_mapping_summary(structure, video_dir) -> None:
    """打印 mapping 速览表:章节/课/缺视频(用户不用打开 Finder)。"""
    print()
    print("┌" + "─" * 58 + "┐")
    print(f"│  📊 mapping 速览: {structure.course_title or '课程'}")
    print("├" + "─" * 58 + "┤")
    n_chapters = len(structure.chapters)
    total_lessons = sum(len(c.lessons) for c in structure.chapters)
    mapped = sum(1 for c in structure.chapters for l in c.lessons if l.video)
    print(f"│  章节: {n_chapters}    课时: {total_lessons}    已匹配视频: {mapped}")
    print("├" + "─" * 58 + "┤")

    # 找缺视频的 lesson(纯文字节)
    missing = []
    for ch in structure.chapters:
        for ls in ch.lessons:
            if not ls.video:
                missing.append(f"ch{ch.index} {ls.id} {ls.title}")
    if missing:
        print(f"│  ⚠ 缺视频(纯文字) {len(missing)} 个:")
        for m in missing[:10]:
            print(f"│    - {m[:54]}")
        if len(missing) > 10:
            print(f"│    ... 还有 {len(missing)-10} 个")
    else:
        print("│  [OK] 无缺视频")
    print("├" + "─" * 58 + "┤")

    # 检查本地缺的文件
    missing_files = []
    for ch in structure.chapters:
        for ls in ch.lessons:
            if ls.video:
                f = video_dir / ls.video
                if not f.exists():
                    missing_files.append(f"  [X] {ls.video}")
    if missing_files:
        print(f"│  ⚠ 本地缺文件 {len(missing_files)} 个:")
        for m in missing_files[:5]:
            print(f"│{m[:56]}")
    else:
        print("│  [OK] 本地文件齐全")
    print("└" + "─" * 58 + "┘")
    print()
    print("  后续:")
    print("    # 把视频传老师后台,直接跑:")
    print(f"    python3 -m scrape.upload build-mapping \\")
    print(f"      --videos {video_dir} \\")
    print(f"      --doc {video_dir}/_chapter_outline.json \\")
    print(f"      --course-id <课程ID>")
    print(f"    python3 -m scrape.upload upload --mapping <输出>/_mapping.json \\")
    print(f"      --cookies-string \"$XTBZ_COOKIE\"")


if __name__ == "__main__":
    main()
