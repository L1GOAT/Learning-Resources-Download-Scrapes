#!/usr/bin/env python3
"""
中国大学MOOC (icourse163.org) 课程视频一键下载

两种运行方式(选一种):

  # 推荐:用模块方式跑
  python -m scrape_new.workflows.icourse163 "URL" mycourse

  # 也可以直接跑文件(脚本自动 bootstrap sys.path)
  python scrape_new/workflows/icourse163.py "URL" mycourse

示例:
  python -m scrape_new.workflows.icourse163 "URL" mycourse

选项:
  --resume / --retry-downloads / --scan-only / --verify-resume-only /
  --outline-only / --playwright / --cpi / --debug 等 chaoxing 旗标
  会被识别但**当前未实现**,会打 warning 忽略

前置条件:
  1. pip install requests
  2. 项目根目录有 cookies.txt(从浏览器导出的登录凭证)

注意:
  - 中国大学MOOC 的 API 是 POST 请求
  - 视频是 m3u8 格式,可能有 AES 加密
  - API 结构可能随平台更新而变化
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
DEFAULT_OUTPUT = "./中国大学MOOC下载"
DELAY_BETWEEN_DOWNLOADS = 1

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/149.0.0.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Content-Type": "application/x-www-form-urlencoded",
    "Referer": "https://www.icourse163.org/",
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
    """从中国大学MOOC URL 提取参数"""
    parsed = urlparse(url)
    params = parse_qs(parsed.query)

    # 从 URL 路径提取 course id
    # 格式：/learn/大学-课程名-XXXXX?tid=YYYYY
    path_parts = parsed.path.strip("/").split("/")

    course_id = None
    tid = params.get("tid", [None])[0]

    # 从路径中提取数字 ID
    for part in reversed(path_parts):
        # 尝试从最后的路径段提取
        nums = re.findall(r"\d{5,}", part)
        if nums:
            if not course_id:
                course_id = nums[0]
            break

    # 从查询参数提取
    if not course_id:
        course_id = params.get("cid", params.get("courseId", [None]))[0]

    return {"courseId": course_id, "tid": tid}


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
            if "登录" in text and ("login" in text.lower() or "passport" in text.lower()):
                print("[错误] Cookie 已过期！页面显示登录页")
                return False
        print("[OK] Cookie 有效")
        return True
    except Exception as e:
        print(f"[警告] 验证失败: {e}（继续尝试）")
        return True


# ─── 核心流程 ──────────────────────────────────────────────────

def get_course_term_info(session, course_id, tid):
    """获取课程学期信息（章节结构）"""
    print("[扫描] 正在获取课程章节列表...")

    api_url = "https://www.icourse163.org/mooc-api/mobCourse/v4/rpc/getCourseTermDto"
    data = {
        "courseId": course_id,
        "tid": tid or "",
        "utm": "",
    }

    try:
        resp = session.post(api_url, data=data, timeout=15)
        if resp.status_code == 200:
            result = resp.json()
            if result.get("code") == 0 or "result" in result:
                print(f"[OK] 从 API 获取到课程信息")
                return result
            else:
                print(f"[警告] API 返回: {result.get('msg', '未知错误')}")
    except Exception as e:
        print(f"[警告] API 调用失败: {e}")

    # 备用 API
    api_url2 = "https://www.icourse163.org/mooc-api/mobCourse/v4/rpc/getCourseLessonList"
    try:
        resp = session.post(api_url2, data=data, timeout=15)
        if resp.status_code == 200:
            result = resp.json()
            if result.get("code") == 0 or "result" in result:
                print(f"[OK] 从备用 API 获取到课程信息")
                return result
    except Exception:
        pass

    return None


def get_video_info(session, lesson_id, course_id, tid):
    """获取视频下载链接"""
    api_url = "https://www.icourse163.org/mooc-api/mobCourse/v4/rpc/getLessonUnitVideoInfo"
    data = {
        "lessonId": lesson_id,
        "courseId": course_id,
        "tid": tid or "",
    }

    try:
        resp = session.post(api_url, data=data, timeout=15)
        if resp.status_code == 200:
            result = resp.json()
            video_data = result.get("result", result.get("data", {}))
            if isinstance(video_data, dict):
                # 视频 URL 可能在不同字段中
                url = (video_data.get("videoUrl") or
                       video_data.get("url") or
                       video_data.get("m3u8Url") or
                       video_data.get("mp4Url") or
                       video_data.get("videoId", ""))
                if url:
                    return {
                        "url": url,
                        "size": video_data.get("size", 0),
                        "duration": video_data.get("duration", 0),
                    }
                # 可能在 urls 列表中
                urls = video_data.get("urls", [])
                if urls:
                    # 优先选择标清或高清
                    for u in urls:
                        if u.get("quality") in ("sd", "hd"):
                            return {"url": u.get("url", ""), "size": 0, "duration": 0}
                    return {"url": urls[0].get("url", ""), "size": 0, "duration": 0}
    except Exception as e:
        print(f"  [警告] API 调用失败: {e}")

    return None


def download_m3u8(session, m3u8_url, filepath):
    """下载 m3u8 视频(委托给 core.download_m3u8)"""
    return _core_download_m3u8(session, m3u8_url, filepath)


def download_file(session, url, filepath, expected_size=0):
    """下载文件(委托给 core.download_file)"""
    from scrape_new.core import download_file as _core_dl
    return _core_dl(session, url, filepath, size_hint=expected_size, max_retries=3)



# ─── 主流程 ──────────────────────────────────────────────────

def main():
    # absolute import:同时支持 `python -m` 和 `python scrape_new/workflows/icourse163.py`
    from scrape_new.workflows.cli_args import parse_workflow_args, print_workflow_usage
    # require_resume_for_verify=False:本 workflow 暂不支持 --verify-resume-only,
    # 即使用户传了也别在解析阶段就报"必须配 --resume",而是进入主流程后 warning 忽略
    parsed = parse_workflow_args(
        sys.argv[1:], default_output=DEFAULT_OUTPUT,
        require_resume_for_verify=False,
    )
    if parsed.error is not None or not parsed.url:
        print_workflow_usage("icourse163", DEFAULT_OUTPUT)
        if parsed.error:
            print(f"[错误] {parsed.error}")
        sys.exit(1)
    course_url = parsed.url
    output_dir = parsed.output_dir
    if parsed.scan_only:
        print("[警告] --scan-only 当前仅 chaoxing 支持,中国大学MOOC 忽略此 flag")
    if parsed.verify_resume_only:
        print("[警告] --verify-resume-only 当前仅 chaoxing 支持,中国大学MOOC 忽略此 flag")

    # 提取参数
    params = extract_params(course_url)
    course_id = params["courseId"]
    tid = params["tid"]

    if not course_id:
        print("[错误] 无法从 URL 提取课程 ID")
        print("  请确认 URL 格式：https://www.icourse163.org/learn/xxx?tid=yyy")
        sys.exit(1)

    print("=" * 60)
    print("中国大学MOOC 课程视频下载")
    print(f"  课程ID: {course_id}")
    print(f"  学期ID: {tid or '未指定'}")
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

    # 获取课程信息
    course_info = get_course_term_info(session, course_id, tid)
    if not course_info:
        print("[错误] 无法获取课程信息")
        print("  可能原因：Cookie 过期、课程 ID 错误、API 已变化")
        print("  请检查 COOKIE_GUIDE.md 中的中国大学MOOC 部分")
        sys.exit(1)

    # 解析章节和课时
    lessons = []
    result = course_info.get("result", course_info.get("data", {}))

    if isinstance(result, dict):
        chapters = result.get("chapters", result.get("chapterList", []))
        for chapter in chapters:
            chapter_name = chapter.get("name", chapter.get("chapterName", ""))
            sections = chapter.get("sections", chapter.get("sectionList", []))
            for section in sections:
                lesson_list = section.get("lessons", section.get("lessonList", []))
                for lesson in lesson_list:
                    lessons.append({
                        "id": lesson.get("id", lesson.get("lessonId", "")),
                        "name": lesson.get("name", lesson.get("lessonName", "")),
                        "chapter": chapter_name,
                    })
                # 有些结构没有 sections，直接在 chapter 下
            if not sections:
                lesson_list = chapter.get("lessons", chapter.get("lessonList", []))
                for lesson in lesson_list:
                    lessons.append({
                        "id": lesson.get("id", lesson.get("lessonId", "")),
                        "name": lesson.get("name", lesson.get("lessonName", "")),
                        "chapter": chapter_name,
                    })

    if not lessons:
        print("[错误] 未找到任何课程视频")
        print("  请确认课程 URL 正确，且已登录")
        sys.exit(1)

    print(f"[OK] 找到 {len(lessons)} 个视频")

    # 创建输出目录
    video_dir = os.path.join(output_dir, "视频")
    os.makedirs(video_dir, exist_ok=True)

    # 下载
    print(f"\n[下载] 开始下载 {len(lessons)} 个视频...\n")
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

        video_info = get_video_info(session, lesson["id"], course_id, tid)
        if not video_info:
            print("  [失败] 无法获取视频链接")
            failed += 1
            continue

        video_url = video_info["url"]
        if ".m3u8" in video_url:
            downloaded = download_m3u8(session, video_url, filepath)
        else:
            downloaded = download_file(session, video_url, filepath, video_info.get("size", 0))

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
                platform="icourse163",
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
