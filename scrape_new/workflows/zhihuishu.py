#!/usr/bin/env python3
"""
智慧树/知到 课程视频一键下载

两种运行方式(选一种):

  # 推荐:用模块方式跑
  python -m scrape_new.workflows.zhihuishu "URL" mycourse

  # 也可以直接跑文件(脚本自动 bootstrap sys.path)
  python scrape_new/workflows/zhihuishu.py "URL" mycourse

示例:
  python -m scrape_new.workflows.zhihuishu "URL" mycourse

选项:
  --resume / --retry-downloads / --scan-only / --verify-resume-only /
  --outline-only / --playwright / --cpi / --debug 等 chaoxing 旗标
  会被识别但**当前未实现**,会打 warning 忽略

前置条件:
  1. pip install requests
  2. 项目根目录有 cookies.txt(从浏览器导出的登录凭证)

注意:
  - 智慧树的视频可能是 m3u8 格式,需要 ffmpeg 或内置下载器
  - API 结构可能随平台更新而变化,如果报错请检查 API 是否还有效
"""

# ─── sys.path bootstrap ───────────────────────────────────
import os
import sys
_HERE = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.dirname(os.path.dirname(_HERE))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

import requests
import re
import json
import time
import sys
import os
from urllib.parse import urlparse, parse_qs
from scrape_new.core import DEFAULT_UA

# scrape_new.upload.outline 是 scrape_new.upload 包成员,
# _PROJECT_ROOT 已经在文件顶部 bootstrap 进 sys.path,无需再处理
try:
    from scrape_new.upload.outline import write_outline, videos_to_outline_chapters
    HAS_OUTLINE = True
except ImportError:
    HAS_OUTLINE = False

from scrape_new.core import download_m3u8 as _core_download_m3u8


# ─── 配置 ──────────────────────────────────────────────────────

COOKIES_FILE = "cookies.txt"
DEFAULT_OUTPUT = "./智慧树课程下载"
DELAY_BETWEEN_DOWNLOADS = 1

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/149.0.0.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
}


# ─── 工具函数 ──────────────────────────────────────────────────

def load_cookies(session, filepath):
    """从 cookies.txt 或 XTBZ_COOKIE 环境变量加载 Cookie。
    优先级: XTBZ_COOKIE 环境变量 > cookies.txt 文件 > 报错退出
    """
    # 1. 环境变量优先(用户偏好 in-memory,cookie 不落盘)
    env_cookie = os.environ.get("XTBZ_COOKIE", "").strip()
    if env_cookie:
        _parse_cookie_string(session, env_cookie)
        print(f"[OK] 已加载 Cookie 来自 XTBZ_COOKIE 环境变量({len(session.cookies)} 个字段)")
        return
    # 2. 文件 fallback
    if not os.path.exists(filepath):
        print(f"[错误] 找不到 {filepath}，请先导出 Cookie")
        print(f"  或者设环境变量 XTBZ_COOKIE='<原始 cookie 字符串>'")
        print(f"  导出方法见 MACOS_DEPLOY.md 或 SESSION_PROTOCOL.md")
        sys.exit(1)
    with open(filepath, "r", encoding="utf-8") as f:
        raw = f.read().strip()
    _parse_cookie_string(session, raw)
    print(f"[OK] 已加载 Cookie 来自 {filepath} ({len(session.cookies)} 个字段)")


def _parse_cookie_string(session, raw):
    """解析原始 cookie 字符串并塞到 session.cookies。"""
    for pair in raw.split(";"):
        pair = pair.strip()
        if "=" in pair:
            k, v = pair.split("=", 1)
            session.cookies.set(k.strip(), v.strip())


def sanitize_filename(name):
    """清理文件名中的非法字符"""
    return re.sub(r'[\\/:*?"<>|]', "_", name).strip()


def extract_params(url):
    """从 URL 提取课程参数"""
    parsed = urlparse(url)
    params = parse_qs(parsed.query)
    # 智慧树 URL 格式多样，尝试多种参数名
    return {
        "courseId": params.get("courseId", params.get("courseid", params.get("id", [None])))[0],
        "clazzId": params.get("clazzId", params.get("clazzid", [None]))[0],
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
                return False
        if resp.status_code == 200:
            text = resp.text[:2000]
            if "登录" in text and "passport" in text.lower():
                print("[错误] Cookie 已过期！页面显示登录页")
                return False
        print("[OK] Cookie 有效")
        return True
    except Exception as e:
        print(f"[警告] 验证失败: {e}（继续尝试）")
        return True


# ─── 核心流程 ──────────────────────────────────────────────────

def get_course_info(session, course_url):
    """获取课程信息和章节列表"""
    print("[扫描] 正在获取课程信息...")
    resp = session.get(course_url, timeout=30)
    html = resp.text

    # 从页面提取课程 ID 和章节数据
    # 智慧树的课程数据通常嵌在页面的 JavaScript 中
    course_info = {}

    # 尝试提取 JSON 数据
    json_patterns = [
        r'window\.__INITIAL_STATE__\s*=\s*({.*?});',
        r'var\s+courseData\s*=\s*({.*?});',
        r'"courseId"\s*:\s*"?(\d+)"?',
        r'"chapters"\s*:\s*(\[.*?\])',
    ]

    for pattern in json_patterns:
        m = re.search(pattern, html, re.DOTALL)
        if m:
            try:
                data = json.loads(m.group(1))
                if isinstance(data, dict):
                    course_info.update(data)
            except json.JSONDecodeError:
                pass

    return course_info


def get_chapter_list(session, course_id, clazz_id=None):
    """通过 API 获取章节列表"""
    # 智慧树 API 端点（需要根据实际情况调整）
    apis = [
        f"https://studyservice.zhihuishu.com/learning/v1/course/chapter/list?courseId={course_id}",
        f"https://api.zhihuishu.com/learning/v1/course/chapter/list?courseId={course_id}",
        f"https://studyh5.zhihuishu.com/course/chapter/list?courseId={course_id}",
    ]

    for api_url in apis:
        try:
            resp = session.get(api_url, timeout=15)
            if resp.status_code == 200:
                data = resp.json()
                if "data" in data or "result" in data:
                    print(f"[OK] 从 API 获取到章节列表")
                    return data
        except Exception as e:
            continue

    return None


def get_video_url(session, lesson_id, course_id):
    """获取视频下载链接"""
    apis = [
        f"https://studyservice.zhihuishu.com/learning/v1/course/lesson/video?lessonId={lesson_id}&courseId={course_id}",
        f"https://api.zhihuishu.com/learning/v1/course/lesson/video?lessonId={lesson_id}",
    ]

    for api_url in apis:
        try:
            resp = session.get(api_url, timeout=15)
            if resp.status_code == 200:
                data = resp.json()
                # 智慧树视频通常在 data.videoUrl 或 data.urls 中
                if isinstance(data, dict):
                    video_data = data.get("data", data.get("result", {}))
                    if isinstance(video_data, dict):
                        url = (video_data.get("videoUrl") or
                               video_data.get("url") or
                               video_data.get("mp4Url") or
                               video_data.get("m3u8Url", ""))
                        if url:
                            return {
                                "url": url,
                                "size": video_data.get("size", 0),
                                "duration": video_data.get("duration", 0),
                            }
        except Exception as e:
            continue

    return None


def download_video(session, url, filepath, expected_size=0):
    """下载视频文件"""
    try:
        resp = session.get(url, stream=True, timeout=600)
        ctype = resp.headers.get("content-type", "").lower()
        if "text/html" in ctype and "application" not in ctype:
            print(f"\n  [失败] 服务端返回 HTML(登录页/错误页),跳过")
            return 0
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
                        f"{expected_size / 1024 / 1024:.0f}MB)")
                    sys.stdout.flush()
        print()
        return downloaded
    except Exception as e:
        print(f"\n  [错误] 下载失败: {e}")
        return 0


def download_m3u8(session, m3u8_url, filepath):
    """下载 m3u8 视频(委托给 core.download_m3u8)"""
    return _core_download_m3u8(session, m3u8_url, filepath)



# ─── 主流程 ──────────────────────────────────────────────────

def main():
    # absolute import:同时支持 `python -m` 和 `python scrape_new/workflows/zhihuishu.py`
    from scrape_new.workflows.cli_args import parse_workflow_args, print_workflow_usage
    # require_resume_for_verify=False:本 workflow 暂不支持 --verify-resume-only,
    # 即使用户传了也别在解析阶段就报"必须配 --resume",而是进入主流程后 warning 忽略
    parsed = parse_workflow_args(
        sys.argv[1:], default_output=DEFAULT_OUTPUT,
        require_resume_for_verify=False,
    )
    if parsed.error is not None or not parsed.url:
        print_workflow_usage("zhihuishu", DEFAULT_OUTPUT)
        if parsed.error:
            print(f"[错误] {parsed.error}")
        sys.exit(1)
    course_url = parsed.url
    output_dir = parsed.output_dir
    # 暂不支持 --scan-only(智慧树没多 tab 扫描)
    if parsed.scan_only:
        print("[警告] --scan-only 当前仅 chaoxing 支持,智慧树忽略此 flag")
    if parsed.verify_resume_only:
        print("[警告] --verify-resume-only 当前仅 chaoxing 支持,智慧树忽略此 flag")

    print("=" * 60)
    print("智慧树/知到 课程视频下载")
    print(f"  URL: {course_url[:80]}...")
    print(f"  输出: {output_dir}")
    print("=" * 60)

    # 创建 Session
    session = requests.Session()
    session.headers.update(HEADERS)

    # 加载 Cookie
    script_dir = os.path.dirname(os.path.abspath(__file__))
    project_root = os.path.dirname(os.path.dirname(script_dir))
    cookies_path = os.path.join(project_root, COOKIES_FILE)
    if not os.path.exists(cookies_path):
        cookies_path = COOKIES_FILE
    load_cookies(session, cookies_path)

    # 验证 Cookie
    if not validate_cookie(session, course_url):
        sys.exit(1)

    # 提取参数
    params = extract_params(course_url)
    course_id = params.get("courseId")

    # 获取课程信息
    course_info = get_course_info(session, course_url)

    # 尝试从 API 获取章节列表
    chapters = None
    if course_id:
        chapters = get_chapter_list(session, course_id, params.get("clazzId"))

    if not chapters:
        print("[警告] 无法自动获取章节列表")
        print("  智慧树的 API 结构可能已变化")
        print("  请检查 COOKIE_GUIDE.md 中的智慧树部分，或手动提供章节信息")
        print()
        print("  你也可以尝试：")
        print("  1. 在浏览器中打开课程页面")
        print("  2. 按 F12 → 网络 → 筛选 video 或 m3u8")
        print("  3. 播放视频，找到 m3u8 链接")
        print("  4. 把链接发给我，我帮你下载")
        sys.exit(1)

    # 解析章节并下载视频
    print(f"\n[下载] 开始处理...")
    video_dir = os.path.join(output_dir, "视频")
    os.makedirs(video_dir, exist_ok=True)

    # 从 API 响应中提取视频列表
    # 注意：智慧树的 API 结构可能变化，以下代码需要根据实际情况调整
    lessons = []
    data = chapters.get("data", chapters.get("result", {}))
    if isinstance(data, list):
        for chapter in data:
            chapter_name = chapter.get("name", chapter.get("chapterName", ""))
            lesson_list = chapter.get("lessons", chapter.get("lessonList", []))
            for lesson in lesson_list:
                lessons.append({
                    "id": lesson.get("id", lesson.get("lessonId", "")),
                    "name": lesson.get("name", lesson.get("lessonName", "")),
                    "chapter": chapter_name,
                })
    elif isinstance(data, dict):
        chapter_list = data.get("chapterList", data.get("chapters", []))
        for chapter in chapter_list:
            chapter_name = chapter.get("name", chapter.get("chapterName", ""))
            lesson_list = chapter.get("lessons", chapter.get("lessonList", []))
            for lesson in lesson_list:
                lessons.append({
                    "id": lesson.get("id", lesson.get("lessonId", "")),
                    "name": lesson.get("name", lesson.get("lessonName", "")),
                    "chapter": chapter_name,
                })

    if not lessons:
        print("[错误] 未找到任何课程章节")
        print("  请确认课程 URL 正确，且已登录")
        sys.exit(1)

    print(f"[OK] 找到 {len(lessons)} 节课")

    # 下载
    success = 0
    failed = 0
    for i, lesson in enumerate(lessons):
        safe_name = sanitize_filename(lesson["name"])
        ch_match = re.search(r"第(\d+)章", lesson["chapter"])
        ch_num = int(ch_match.group(1)) if ch_match else 0
        filename = f'{ch_num:02d}_{safe_name}.mp4'
        filepath = os.path.join(video_dir, filename)

        lesson["filename"] = filename  # 给 outline 用
        if os.path.exists(filepath) and os.path.getsize(filepath) > 100000:
            print(f"[{i + 1}/{len(lessons)}] [已存在] {filename}")
            success += 1
            continue

        print(f"[{i + 1}/{len(lessons)}] {filename}")

        video_info = get_video_url(session, lesson["id"], course_id)
        if not video_info:
            print("  [失败] 无法获取视频链接")
            failed += 1
            continue

        video_url = video_info["url"]
        if ".m3u8" in video_url:
            downloaded = download_m3u8(session, video_url, filepath)
        else:
            downloaded = download_video(session, video_url, filepath, video_info.get("size", 0))

        if downloaded > 100000:
            print(f"  [完成] {downloaded / 1024 / 1024:.1f}MB")
            success += 1
        else:
            print(f"  [可疑] 文件过小")
            failed += 1
            if os.path.exists(filepath):
                os.remove(filepath)

        time.sleep(DELAY_BETWEEN_DOWNLOADS)
    # 写章节目录文件(让 scrape.upload 能直接读建课)
    if HAS_OUTLINE and lessons:
        try:
            # 从 chapter 名提取章号: "第X章" → X
            _ch_match = re.search(r"第(\d+)章", ls.get("chapter", "").split()[0] if " " in ls.get("chapter", "") else ls.get("chapter", ""))
            _ch_num = int(_ch_match.group(1)) if _ch_match else 0
            # 重新构造 lessons,补上 ch_num
            _lessons_with_ch = []
            for ls in lessons:
                _m = re.search(r"第(\d+)章", ls.get("chapter", ""))
                _cn = int(_m.group(1)) if _m else 0
                _lessons_with_ch.append({**ls, "ch_num": _cn})
            lessons = _lessons_with_ch
            chapters_data = videos_to_outline_chapters([
                {
                    "ch_num": v.get("ch_num", 0) if isinstance(v, dict) else 0,
                    "chapter": v.get("chapter", "") if isinstance(v, dict) else "",
                    "lesson": v.get("name") or v.get("lesson", "") if isinstance(v, dict) else "",
                    "filename": v.get("filename", "") if isinstance(v, dict) else "",
                }
                for v in lessons
            ])
            outline_path = write_outline(
                out_dir=video_dir,
                chapters=chapters_data,
                source_url=course_url,
                platform="zhihuishu",
                course_title="",
            )
            print(f"  目录: {outline_path}")
        except Exception as e:
            print(f"  [警告] 写章节目录失败: {e}")


    print("下载完成!")
    print(f"  发现: {len(lessons)}")
    print(f"  成功: {success}")
    print(f"  失败: {failed}")
    print(f"  输出: {os.path.abspath(video_dir)}")
    print("=" * 60)


if __name__ == "__main__":
    main()
