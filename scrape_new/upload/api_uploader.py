"""
API 路径的 uploader —— 用 requests 直接调老师后台的 POST 端点,不走 Playwright。

4 步流程（依据 next-studio.xuetangx.com.har 反推,2026-06-10 跑通）：
  1. POST /c27/online_courseware/instance/resource_tree/create_chapter/2/{cid}/
     body: {"name":"...","is_show":true}                → 返回 chapter_id
  2. POST /c27/online_courseware/instance/resource_tree/create_section/
     body: {"chapter_id":...,"name":"...","cover":"","remark":""}  → 返回 section_id
  3. GET  /c27/online_courseware/service/upload_video/?title=&filename=&filesize=
     → 返回 BokeCC 上传凭证 {chunkurl, metaurl, servicetype, userid, videoid}
     -- BokeCC 三步走 --
     a. GET {metaurl}?ccvid=&uid=&filename=&filesize=&servicetype=  → 看 received（断点续传位移）
     b. POST {chunkurl}?ccvid=  multipart/form-data，字段名 'file'   → 每片 1MB
     c. 最后一片返回 {result:1, msg:"Upload success"}
  4. POST /c27/online_courseware/instance/resource_tree/create_leaf/
     body 含 chapter_id / section_id / leaf_type:0 / content_info.media.ccid=videoid
     → 返回 leaf_id

复用:
  - scrape.core.load_cookies()                — Cookie 解析（.txt / .json）
  - scrape.upload.models.Asset.with_status()  — 不可变状态推进
  - scrape.upload.report.*                    — CSV / manifest / report 落盘

约束:
  - 全文用 type annotation
  - frozen dataclass + immutable 风格
  - 边界 try/except,核心路径 fail-fast raise
  - 中文 print 给用户,内部诊断走 logger
  - 不硬编码 cookie,允许"cookie 字符串 in-memory"(用户偏好)
"""

from __future__ import annotations

import json
import logging
import time
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable

import requests

from ..core import load_cookies
from .models import (
    Asset,
    AssetStatus,
    ContentType,
    CourseStructure,
    UploadResult,
)
from .naming import lesson_leaf_name
from .resource_key import make_resource_key
from .report import append_log_row, save_manifest, write_report, write_retry_resources
from .sync_tree import (
    DiffAction,
    LeafDiff,
    LessonDiff,
    ChapterDiff,
    TreeDiff,
    compute_diff,
    write_backup_snapshot,
)

logger = logging.getLogger(__name__)


# ─── 常量 ─────────────────────────────────────────────────────────

BASE_URL = "https://next-studio.xuetangx.com"
MIB = 1024 * 1024
CHUNK_SIZE = 1 * MIB                # BokeCC 每片 1MB(HAR 实测)
MIN_VIDEO_SIZE = 100 * 1024         # < 100KB 视为可疑,见 CLAUDE.md 项目阈值
HTTP_TIMEOUT = 60
UPLOAD_TIMEOUT = 600
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/149.0.0.0 Safari/537.36"
)


# ─── 数据结构 ─────────────────────────────────────────────────────

@dataclass(frozen=True)
class UploadCredentials:
    """BokeCC 上传凭证(由 service/upload_video/ 下发)"""
    chunkurl: str           # 分片 POST 地址
    metaurl: str            # 进度/init GET 地址
    servicetype: str
    userid: str             # BokeCC 用户 ID
    videoid: str            # ccid,后续 create_leaf 要用


@dataclass(frozen=True)
class TeacherContext:
    """老师后台会话上下文(从 cookie 解析出 csrftoken / university_id)

    cookie_created_at: 记录 cookie 注入时间,用于 30 分钟滑动窗口提醒(优化 B)
    """
    session: requests.Session
    csrftoken: str
    university_id: str
    course_id: str
    cookie_created_at: float = field(default_factory=time.time)

    def cookie_age_minutes(self) -> float:
        return (time.time() - self.cookie_created_at) / 60.0

    def cookie_remaining_minutes(self) -> float:
        return max(0.0, 30.0 - self.cookie_age_minutes())


# ─── Session 构建 ─────────────────────────────────────────────────

def _make_session(
    cookies_path: Path | None = None,
    cookies_string: str | None = None,
) -> requests.Session:
    """从 cookie 文件或字符串构建 Session。

    cookies_string 优先,允许"in-memory"模式(不落盘)。
    """
    session = requests.Session()
    session.headers.update({
        "User-Agent": USER_AGENT,
        "Accept-Language": "zh-CN,zh;q=0.9,en-US;q=0.8,en;q=0.7",
    })

    if cookies_string:
        for pair in cookies_string.split(";"):
            pair = pair.strip()
            if "=" in pair:
                k, v = pair.split("=", 1)
                session.cookies.set(k.strip(), v.strip())
    elif cookies_path:
        if not cookies_path.exists():
            raise FileNotFoundError(f"Cookie 文件不存在: {cookies_path}")
        load_cookies(session, cookies_path)
    else:
        raise ValueError("必须提供 cookies_path 或 cookies_string 之一")

    return session


def _build_context(
    session: requests.Session,
    course_id: str,
) -> TeacherContext:
    """从 session.cookies 解析出 csrftoken 和 university_id。"""
    csrftoken = session.cookies.get("csrftoken", "")
    university_id = session.cookies.get("university_id", "")

    if not csrftoken:
        raise RuntimeError(
            "Cookie 缺少 csrftoken — 请重新从浏览器导出（确保选了所有 cookie）"
        )
    if not university_id:
        raise RuntimeError(
            "Cookie 缺少 university_id — 请重新从浏览器导出"
        )
    if session.cookies.get("xtbz") != "cloud":
        logger.warning(
            "Cookie 的 xtbz 不是 'cloud',这是教师身份关键标志,可能登录的是学生账号"
        )

    return TeacherContext(
        session=session,
        csrftoken=csrftoken,
        university_id=university_id,
        course_id=str(course_id),
        cookie_created_at=time.time(),
    )


def _base_headers(ctx: TeacherContext) -> dict[str, str]:
    """所有写操作必带的 headers(同 capture_<date>.har 实测一致)"""
    return {
        "Accept": "application/json, text/plain, */*",
        "Content-Type": "application/json",
        "Origin": BASE_URL,
        "Referer": f"{BASE_URL}/pro/editcoursemanage/teachcontent/{ctx.course_id}",
        "x-csrftoken": ctx.csrftoken,
        "x-client": "web",
        "terminal-type": "web",
        "platform-id": "0",
        "university-id": ctx.university_id,
        "xtbz": "cloud",
    }


def _light_headers(ctx: TeacherContext) -> dict[str, str]:
    """轻量 headers,用于 GET service/upload_video/ 这类不需要 xtbz 的端点"""
    return {
        "Accept": "application/json, text/javascript, */*; q=0.01",
        "Referer": f"{BASE_URL}/pro/editcoursemanage/teachcontent/{ctx.course_id}",
        "x-csrftoken": ctx.csrftoken,
        "x-requested-with": "XMLHttpRequest",
    }


# ─── 登录态预检 ───────────────────────────────────────────────────

def verify_login(ctx: TeacherContext) -> bool:
    """探测 has-permission 接口,判断 cookie 是否有效。"""
    url = f"{BASE_URL}/c27/online_courseware/instance/has-permission/{ctx.course_id}/"
    try:
        resp = ctx.session.get(
            url,
            headers=_base_headers(ctx),
            timeout=HTTP_TIMEOUT,
            allow_redirects=False,
        )
    except requests.RequestException as e:
        print(f"  [失败] 网络异常: {e}")
        return False

    if resp.status_code in (301, 302):
        loc = resp.headers.get("Location", "").lower()
        if any(kw in loc for kw in ("login", "signin", "auth", "passport")):
            print(f"  [失败] Cookie 跳转到登录页,已过期")
            return False
    if resp.status_code in (401, 403):
        print(f"  [失败] {resp.status_code} {resp.reason} — Cookie 已过期或无权限")
        return False
    if resp.status_code != 200:
        print(f"  [失败] HTTP {resp.status_code}")
        return False

    try:
        data = resp.json()
        if not data.get("success", False):
            print(f"  [失败] 服务端返回 success=false: {data.get('msg')}")
            return False
    except ValueError as e:
        print(f"  [失败] 响应不是 JSON: {e}")
        return False

    print(f"  [成功] Cookie 有效,有课程 {ctx.course_id} 的编辑权限")

    # 优化 B:Cookie 30 分钟滑动窗口提醒
    remaining = ctx.cookie_remaining_minutes()
    if remaining < 5.0:
        print(f"  ⚠⚠⚠ Cookie 即将过期(剩 {remaining:.1f} 分钟)!")
        print(f"     建议立刻在浏览器 F5 刷后台,然后重新粘 cookie 给我")
    elif remaining < 10.0:
        print(f"  ⚠ Cookie 寿命过半(剩 {remaining:.1f} 分钟),计划后续")

    return True


def get_resource_tree(ctx: TeacherContext) -> dict:
    """拉取课程现有章节树,用于跳过已存在的章节。"""
    url = (
        f"{BASE_URL}/c27/online_courseware/instance/resource_tree/"
        f"get_resource_tree/2/{ctx.course_id}/"
    )
    resp = ctx.session.get(
        url,
        headers=_base_headers(ctx),
        params={"uv_id": ctx.university_id, "term": "latest"},
        timeout=HTTP_TIMEOUT,
    )
    resp.raise_for_status()
    data = resp.json()
    if not data.get("success", False):
        raise RuntimeError(f"拉章节树失败: {data.get('msg')}")
    return data.get("data", {})


# ─── 写操作:章 / 节 / leaf ───────────────────────────────────────

def create_chapter(ctx: TeacherContext, name: str, is_show: bool = True) -> int:
    """建章。返回新建章节的 chapter_id。"""
    url = (
        f"{BASE_URL}/c27/online_courseware/instance/resource_tree/"
        f"create_chapter/2/{ctx.course_id}/"
    )
    payload = {"name": name, "is_show": is_show}
    resp = ctx.session.post(
        url,
        headers=_base_headers(ctx),
        params={"uv_id": ctx.university_id, "term": "latest"},
        json=payload,
        timeout=HTTP_TIMEOUT,
    )
    resp.raise_for_status()
    data = resp.json()
    if not data.get("success", False):
        raise RuntimeError(f"建章失败 [{name}]: {data.get('msg')}")
    chapter_id = data["data"]["id"]
    logger.info(f"建章成功: {name} → chapter_id={chapter_id}")
    return chapter_id


def delete_chapter(ctx: TeacherContext, chapter_id: int) -> None:
    """删章(级联删节和 leaf)。HAR 没录到,2026-06-10 探测得到的端点。"""
    url = f"{BASE_URL}/c27/online_courseware/instance/resource_tree/delete_chapter/"
    resp = ctx.session.post(
        url,
        headers=_base_headers(ctx),
        params={"uv_id": ctx.university_id, "term": "latest"},
        json={"chapter_id": chapter_id},
        timeout=HTTP_TIMEOUT,
    )
    resp.raise_for_status()
    data = resp.json()
    if not data.get("success", False):
        raise RuntimeError(f"删章失败 [chapter_id={chapter_id}]: {data.get('msg')}")
    logger.info(f"删章成功: chapter_id={chapter_id}")


def reset_course_tree(ctx: TeacherContext) -> int:
    """清空课程所有章节(危险操作!正式跑全量前清场用)。返回删除的章节数。"""
    tree = get_resource_tree(ctx)
    chs = tree.get("chapter_list", [])
    for ch in chs:
        delete_chapter(ctx, ch["id"])
    return len(chs)


def create_section(
    ctx: TeacherContext,
    chapter_id: int,
    name: str,
    cover: str = "",
    remark: str = "",
) -> int:
    """建节。返回新建节的 section_id。"""
    url = f"{BASE_URL}/c27/online_courseware/instance/resource_tree/create_section/"
    payload = {
        "chapter_id": chapter_id,
        "name": name,
        "cover": cover,
        "remark": remark,
    }
    resp = ctx.session.post(
        url,
        headers=_base_headers(ctx),
        params={"uv_id": ctx.university_id, "term": "latest"},
        json=payload,
        timeout=HTTP_TIMEOUT,
    )
    resp.raise_for_status()
    data = resp.json()
    if not data.get("success", False):
        raise RuntimeError(
            f"建节失败 [chapter_id={chapter_id}, name={name}]: {data.get('msg')}"
        )
    section_id = data["data"]["section_id"]
    logger.info(f"建节成功: {name} → section_id={section_id}")
    return section_id


def create_video_leaf(
    ctx: TeacherContext,
    chapter_id: int,
    section_id: int,
    leaf_name: str,
    video_filename: str,
    video_size: int,
    ccid: str,
) -> int:
    """创建视频 leaf(挂到 section 下)。返回 leaf_id。"""
    url = f"{BASE_URL}/c27/online_courseware/instance/resource_tree/create_leaf/"
    payload = {
        "chapter_id": chapter_id,
        "section_id": section_id,
        "name": leaf_name,
        "leaf_type": 0,
        "content_info": {
            "content_id": ctx.course_id,
            "cover": "",
            "cover_desc": "",
            "remark": "",
            "context": "",
            "media": {
                "type": "video",
                "cover": "",
                "ccid": ccid,
                "duration": 0,
                "name": video_filename,
                "size": video_size,
                "isCloud": False,
            },
            "download": [],
            "is_discuss": True,
            "expand_discuss": False,
            "is_score": True,
            "score_evaluation": {"id": 6, "name": "视频", "score": 1},
            "is_attachment_download": True,
            "teaching_link": 0,
        },
    }
    resp = ctx.session.post(
        url,
        headers=_base_headers(ctx),
        params={"term": "latest", "uv_id": ctx.university_id},
        json=payload,
        timeout=HTTP_TIMEOUT,
    )
    resp.raise_for_status()
    data = resp.json()
    if not data.get("success", False):
        raise RuntimeError(
            f"建 leaf 失败 [section_id={section_id}, name={leaf_name}]: {data.get('msg')}"
        )
    leaf_id = data["data"]["leaf_id"]
    logger.info(f"建 leaf 成功: {leaf_name} → leaf_id={leaf_id}")
    return leaf_id


# ─── 七牛云附件上传(PPT/PDF/图片等) ──────────────────────────────

def _qiniu_upload_attachment(
    ctx: TeacherContext,
    file_path: Path,
) -> tuple[str, str]:
    """上传附件到七牛云,返回 (file_key, file_url)。

    流程 (HAR: next-studio.xuetangx.com.ppt.har 2026-06-16 反推):
      1. GET /service/get_token/?file_extension=<ext> → 拿 token + file_key
      2. 用 token + file_path 直传七牛(form-data, key=file)
      3. file_url = https://qn1-next.xuetangonline.com/{file_key}
    """
    file_path = Path(file_path)
    ext = file_path.suffix.lstrip(".").lower()
    if not ext:
        raise ValueError(f"无法识别文件扩展名: {file_path}")

    # Step 1: 拿 token
    url = f"{BASE_URL}/c27/online_courseware/service/get_token/"
    params = {
        "file_extension": ext,
        "term": "latest",
        "uv_id": ctx.university_id,
    }
    resp = ctx.session.get(
        url, headers=_light_headers(ctx), params=params, timeout=HTTP_TIMEOUT,
    )
    resp.raise_for_status()
    data = resp.json()
    if not data.get("success"):
        raise RuntimeError(f"申请附件上传凭证失败 [{file_path.name}]: {data.get('msg')}")
    token = data["data"]["token"]
    file_key = data["data"]["file_key"]

    # Step 2: 直传七牛(参考 HAR 中 next-studio 内部用 qiniu-sdk;这里用 requests multipart)
    # 解析七牛 token(base64 中包含 scope 字段,域名是 qn1-next.xuetangonline.com)
    file_url = f"https://qn1-next.xuetangonline.com/{file_key}"
    # 用 requests post 七牛(七牛 upload 接口从 token 中解析;简化:用 form 上传到 file_url)
    with open(file_path, "rb") as f:
        files = {"file": (file_key, f, f"application/{ext}")}
        form = {
            "token": token,
            "key": file_key,
        }
        # 七牛 upload endpoint 走 token 自带 endpoint,从 token 解不出,直接试通用 upload.qiniup.com
        r = requests.post(
            "https://upload.qiniup.com/",
            data=form, files=files, timeout=300,
        )
    if r.status_code != 200:
        raise RuntimeError(f"七牛上传失败 [{file_path.name}]: {r.status_code} {r.text[:200]}")
    return file_key, file_url


def create_attachment_leaf(
    ctx: TeacherContext,
    chapter_id: int,
    section_id: int,
    leaf_name: str,
    file_path: Path,
) -> int:
    """建附件 leaf(PPT/PDF/图片等),必须挂在某个 section 下。返回 leaf_id。

    HAR 反推的关键点:
      - leaf_type=3(图文/PPT)
      - section_id 必须为具体 section 的 id(不允许 =0,防止误挂到章下)
      - content_info.score_evaluation.id=7
      - content_info.download 数组含 file_url/file_type/file_size

    Raises:
        ValueError: section_id == 0(以前允许,现在强制要求显式 section)
    """
    if section_id <= 0:
        raise ValueError(
            f"create_attachment_leaf 必须显式传 section_id(>0),"
            f"不允许挂到章下: {leaf_name}"
        )
    file_path = Path(file_path)
    if not file_path.exists():
        raise FileNotFoundError(f"附件不存在: {file_path}")
    ext = file_path.suffix.lstrip(".").lower()
    file_size = file_path.stat().st_size

    file_key, file_url = _qiniu_upload_attachment(ctx, file_path)

    url = f"{BASE_URL}/c27/online_courseware/instance/resource_tree/create_leaf/"
    payload = {
        "chapter_id": chapter_id,
        "section_id": section_id,  # 0=挂章下,非0=挂指定 section
        "name": leaf_name,
        "leaf_type": 3,
        "content_info": {
            "content_id": ctx.course_id,
            "cover": "",
            "cover_desc": "",
            "remark": "",
            "context": "",
            "media": {
                "type": "",
                "cover": "",
                "ccid": "",
                "duration": 0,
            },
            "download": [{
                "file_name": file_path.name,
                "file_type": ext,
                "file_size": file_size,
                "file_url": file_url,
                "status": 1,
                "progress": 100,
            }],
            "is_discuss": True,
            "expand_discuss": False,
            "is_score": True,
            "score_evaluation": {"id": 7, "name": "图文", "score": 1},
            "is_attachment_download": True,
            "teaching_link": 0,
        },
    }
    resp = ctx.session.post(
        url,
        headers=_base_headers(ctx),
        params={"uv_id": ctx.university_id, "term": "latest"},
        json=payload,
        timeout=HTTP_TIMEOUT,
    )
    resp.raise_for_status()
    data = resp.json()
    if not data.get("success"):
        raise RuntimeError(f"建附件 leaf 失败 [{file_path.name}]: {data.get('msg')}")
    leaf_id = data["data"]["leaf_id"]
    logger.info(f"建附件 leaf 成功: {leaf_name} → leaf_id={leaf_id}")
    return leaf_id


# ─── BokeCC 视频上传 ─────────────────────────────────────────────

def request_upload_credentials(
    ctx: TeacherContext,
    title: str,
    filename: str,
    filesize: int,
) -> UploadCredentials:
    """GET service/upload_video/ 拿 BokeCC 凭证。"""
    url = f"{BASE_URL}/c27/online_courseware/service/upload_video/"
    params = {
        "title": title,
        "filename": filename,
        "filesize": filesize,
        "_": int(time.time() * 1000),
    }
    resp = ctx.session.get(
        url,
        headers=_light_headers(ctx),
        params=params,
        timeout=HTTP_TIMEOUT,
    )
    resp.raise_for_status()
    data = resp.json()
    if not data.get("success", False):
        raise RuntimeError(f"申请上传凭证失败 [{filename}]: {data.get('msg')}")
    d = data["data"]
    return UploadCredentials(
        chunkurl=d["chunkurl"],
        metaurl=d["metaurl"],
        servicetype=d["servicetype"],
        userid=d["userid"],
        videoid=d["videoid"],
    )


def bokecc_query_progress(
    ctx: TeacherContext,
    creds: UploadCredentials,
    filename: str,
    filesize: int,
) -> int:
    """GET metaurl 查询当前已传字节(支持断点续传)。返回 received。"""
    params = {
        "ccvid": creds.videoid,
        "uid": creds.userid,
        "filename": filename,
        "filesize": filesize,
        "servicetype": creds.servicetype,
        "_": int(time.time() * 1000),
    }
    resp = ctx.session.get(creds.metaurl, params=params, timeout=HTTP_TIMEOUT)
    resp.raise_for_status()
    data = resp.json()
    received = int(data.get("received", 0))
    logger.debug(f"BokeCC 进度: received={received}/{filesize}")
    return received


def bokecc_upload_chunks(
    creds: UploadCredentials,
    file_path: Path,
    filesize: int,
    start_offset: int = 0,
    progress_cb: Callable[[int, int], None] | None = None,
    creds_refresher: Callable[[], UploadCredentials] | None = None,
) -> int:
    """循环上传分片到 BokeCC。返回最终 received(等于 filesize 即成功)。

    BokeCC 协议要点(HAR 实测):
      - Content-Range: bytes <start>-<end>/<total>   ← 必带,缺则 Internal IO error
      - Origin: https://next-studio.xuetangx.com     ← 必带
      - Referer: https://next-studio.xuetangx.com/   ← 必带(根路径,不带子路径)
      - multipart/form-data,字段名 'file',文件名 'blob'
      - 每片 1MB,最后一片为剩余字节(filesize - start)

    凭证自动续期(优化 A):
      - 401/403/410 触发 → 调 creds_refresher 拿新 UploadCredentials
      - 续期后从已传的字节继续(BokeCC 服务端记录 received)
      - 重试上限 3 次,失败抛 RuntimeError
    """
    # 独立 session,跟 ctx.session 隔离(避免 cookie 串到 bokecc 域)
    with requests.Session() as session:
        session.headers.update({"User-Agent": USER_AGENT})
        received = start_offset
        retries_left = 3
        current_creds = creds
        with open(file_path, "rb") as f:
            f.seek(start_offset)
            while received < filesize:
                chunk = f.read(CHUNK_SIZE)
                if not chunk:
                    raise RuntimeError(
                        f"读到 EOF 但 received={received} < filesize={filesize},文件被截断?"
                    )
                end = received + len(chunk) - 1
                headers = {
                    "Content-Range": f"bytes {received}-{end}/{filesize}",
                    "Origin": BASE_URL,
                    "Referer": f"{BASE_URL}/",
                }
                files = {"file": ("blob", chunk, "application/octet-stream")}
                resp = session.post(
                    current_creds.chunkurl,
                    params={"ccvid": current_creds.videoid},
                    headers=headers,
                    files=files,
                    timeout=UPLOAD_TIMEOUT,
                )
                # 凭证过期检测(优化 A):BokeCC 401/403/410 → 重新拿凭证续传
                if resp.status_code in (401, 403, 410) and creds_refresher is not None and retries_left > 0:
                    retries_left -= 1
                    logger.warning(
                        f"BokeCC 凭证过期 ({resp.status_code}),重试 {3 - retries_left}/3"
                    )
                    current_creds = creds_refresher()
                    if progress_cb:
                        progress_cb(received, filesize)  # 进度不前进了,提示续期
                    continue  # 重传当前 chunk
                resp.raise_for_status()
                data = resp.json()
                new_received = int(data.get("received", received + len(chunk)))
                if new_received <= received:
                    raise RuntimeError(
                        f"BokeCC 返回的 received 没增长 ({received} → {new_received}),"
                        f"msg: {data.get('msg')}"
                    )
                received = new_received
                if progress_cb:
                    progress_cb(received, filesize)
                if data.get("result") == 1:
                    logger.info(f"BokeCC 上传完成: {file_path.name} ({received} bytes)")
                    return received
        return received


def upload_video_to_bokecc(
    ctx: TeacherContext,
    video_path: Path,
) -> tuple[str, int]:
    """完整上传一个视频。返回 (ccid, filesize)。

    凭证自动续期(优化 A):当 bokecc 返回 401/403/410 时,自动重新申请
    UploadCredentials 并从已传字节续传,不需要用户重新跑。
    """
    if not video_path.exists():
        raise FileNotFoundError(f"视频文件不存在: {video_path}")
    filesize = video_path.stat().st_size
    if filesize < MIN_VIDEO_SIZE:
        raise RuntimeError(
            f"视频 < {MIN_VIDEO_SIZE} bytes 可疑,跳过: {video_path} ({filesize} bytes)"
        )
    filename = video_path.name
    title = video_path.name

    print("    申请上传凭证...")
    creds = request_upload_credentials(ctx, title, filename, filesize)
    logger.info(f"获得凭证: ccid={creds.videoid}")

    def _refresher():
        """凭证过期时调用,重新拿 UploadCredentials。"""
        new = request_upload_credentials(ctx, title, filename, filesize)
        logger.info(f"凭证续期成功: ccid={new.videoid}")
        return new

    print("    探测断点续传位移...")
    received = bokecc_query_progress(ctx, creds, filename, filesize)
    if received > 0:
        print(f"    检测到已传 {received / MIB:.1f}MB,断点续传")

    if received >= filesize:
        print("    视频已完整上传,跳过分片")
        return creds.videoid, filesize

    print(f"    开始分片上传 ({filesize / MIB:.1f}MB,每片 {CHUNK_SIZE // MIB}MB)...")
    last_print = time.time()

    def progress(now: int, total: int) -> None:
        nonlocal last_print
        if time.time() - last_print > 1.0 or now >= total:
            pct = now * 100 // total
            print(f"    [{pct}%] {now / MIB:.1f} / {total / MIB:.1f} MB", end="\r")
            last_print = time.time()

    final = bokecc_upload_chunks(creds, video_path, filesize, received, progress, _refresher)
    print()  # 换行
    if final != filesize:
        raise RuntimeError(
            f"BokeCC 上传未完成: received={final}, expected={filesize}"
        )
    return creds.videoid, filesize


# ─── 主流程:状态机(diff → create / skip / rename) ──────────────

# 60% drift 阈值:diff/(diff+skip) >= 0.6 时拒绝继续,提示清空重建
DRIFT_THRESHOLD = 0.6


# 所有非视频的 leaf kind 都视作附件(PPT/PDF/DOCX/图片/通用 attachment)
_ATTACHMENT_KINDS = frozenset({
    "attachment", "ppt", "pdf", "docx", "doc", "image",
})


def _content_type_for_kind(kind: str) -> ContentType:
    # 视频类(主视频 / 英文视频)— 都走 BokeCC,content_type 记 VIDEO
    if kind in ("video", "english"):
        return ContentType.VIDEO
    # 文档/附件类(PPT/PDF/DOCX/图片/通用)— 都记 ATTACHMENT
    # 这样 report / manifest 统计能正确归类,不会全归到 OTHER
    if kind in _ATTACHMENT_KINDS:
        return ContentType.ATTACHMENT
    # 未知 kind(防御)— 兜底 ATTACHMENT(比 OTHER 更安全,至少能走附件流程)
    return ContentType.ATTACHMENT


def _empty_result(
    ctx: TeacherContext,
    course_title: str,
    started_at: str,
    assets: tuple[Asset, ...] = (),
    mode: str = "upload",
) -> UploadResult:
    """构造早返回的空/部分 UploadResult。"""
    if ctx is None:
        # fallback:无 csrftoken(plan-only 无 cookie 场景)
        return UploadResult(
            course_id="",
            course_title=course_title,
            started_at=started_at,
            finished_at=datetime.now().isoformat(timespec="seconds"),
            assets=assets,
            mode=mode,
        )
    return UploadResult(
        course_id=ctx.course_id,
        course_title=course_title,
        started_at=started_at,
        finished_at=datetime.now().isoformat(timespec="seconds"),
        assets=assets,
        mode=mode,
    )


def _print_header(
    structure: CourseStructure,
    ctx: TeacherContext,
    videos_folder: Path,
    dry_run: bool,
    verify_only: bool,
) -> None:
    mode = "verify-only" if verify_only else ("dry-run" if dry_run else "live")
    print("=" * 60)
    print("老师后台上传 (API 模式)")
    print(f"  课程: {structure.course_title or '(未填)'}")
    print(f"  课程ID: {ctx.course_id}")
    print(f"  视频目录: {videos_folder}")
    print(f"  模式: {mode}")
    print("=" * 60)


def _create_one_leaf(
    ctx: TeacherContext,
    chapter_id: int,
    section_id: int,
    lesson_id: str,
    lesson_title: str,
    leaf_diff: LeafDiff,
    videos_folder: Path,
    attachments_folder: Path,
) -> tuple[Optional[int], Optional[str], int]:
    """创建一个 leaf(视频 / 英文视频 / 附件)。返回 (leaf_id_or_none, ccid_or_none, bytes)。

    Raises:
        FileNotFoundError: 文件缺失
        RuntimeError: 上传失败
    """
    fname = leaf_diff.desired_name
    path = videos_folder / fname
    if not path.exists():
        path = attachments_folder / fname
    if not path.exists():
        raise FileNotFoundError(
            f"文件不存在: {fname} (在 视频/ 和 文档/ 都找不到)"
        )

    # 视频类(主视频 / 英文视频)— 都走 BokeCC + create_video_leaf
    # 区分:kind=video 是中文主视频,kind=english 是英文版(都是 mp4)
    if leaf_diff.kind in ("video", "english"):
        ccid, size = upload_video_to_bokecc(ctx, path)
        leaf_id = create_video_leaf(
            ctx, chapter_id=chapter_id, section_id=section_id,
            # 英文视频的 leaf_name 加 "| English" 区分;主视频不加
            leaf_name=lesson_leaf_name(
                lesson_id, lesson_title,
                "english" if leaf_diff.kind == "english" else "video",
            ),
            video_filename=fname, video_size=size, ccid=ccid,
        )
        return leaf_id, ccid, size

    # 附件(PPT/PDF/DOCX/图片)— 走七牛云 + create_attachment_leaf
    # 必须挂到 section(section_id>0 已强制)
    leaf_id = create_attachment_leaf(
        ctx, chapter_id=chapter_id, section_id=section_id,
        leaf_name=lesson_leaf_name(lesson_id, lesson_title, leaf_diff.kind),
        file_path=path,
    )
    return leaf_id, None, path.stat().st_size


def _execute_diff(
    ctx: TeacherContext,
    structure: CourseStructure,
    diff: TreeDiff,
    videos_folder: Path,
    dry_run: bool,
    confirm_rename: bool = False,
) -> list[Asset]:
    """按 diff 状态机创建/跳过/改名。返回所有操作的 Asset 列表。

    状态机:
      ChapterDiff.action == CREATE → 整章 CREATE,所有 lessons 走 _ensure_section
      ChapterDiff.action == SKIP   → 复用 chapter_id,逐个 lesson 走 _ensure_section
      ChapterDiff.action == RENAME → 默认待确认(返回 RENAME_PENDING 资产);
                                    需 confirm_rename=True 才走 delete+create。
      ChapterDiff.action == PRUNE  → 删章(仅 prune=True 时由 compute_diff 设置)

    每个 lesson 一个 section,主视频/英文视频/PPT 全部挂该 section 下的 leaf。
    """
    chapter_by_idx = {ch.index: ch for ch in structure.chapters}
    attachments_folder = videos_folder.parent / "文档"
    if not attachments_folder.exists():
        attachments_folder = videos_folder

    # 缓存:运行中创建的 chapter_id / section_id
    chapter_ids: dict[int, int] = {}        # ch_index -> actual chapter_id
    section_ids: dict[str, int] = {}        # "{ch_idx}.{ls_id}" -> actual section_id
    assets: list[Asset] = []

    def _ensure_chapter(cd: ChapterDiff) -> Optional[int]:
        if cd.index in chapter_ids:
            return chapter_ids[cd.index]
        ch = chapter_by_idx.get(cd.index)
        if ch is None:
            return None
        if cd.action == DiffAction.SKIP:
            assert cd.actual_id is not None
            chapter_ids[cd.index] = cd.actual_id
            return cd.actual_id
        if cd.action == DiffAction.CREATE:
            if dry_run:
                chapter_ids[cd.index] = -(len(chapter_ids) + 1)
                return chapter_ids[cd.index]
            new_id = create_chapter(ctx, ch.title)
            chapter_ids[cd.index] = new_id
            return new_id
        if cd.action == DiffAction.RENAME:
            if not confirm_rename:
                # 默认待确认:不删,不建,返回 PENDING 资产,让用户看清楚
                print(f"  [改名-待确认] ch {cd.index}: "
                      f"'{cd.actual_title}' → '{cd.desired_title}'")
                print(f"    当前未启用 confirm_rename,RENAME 不会执行。"
                      f"原章保留,后续 lessons 也会按 SKIP 处理。")
                print(f"    如需执行改名+重建(会清空原章所有 leaf),"
                      f"传 confirm_rename=True。")
                assets.append(Asset(
                    chapter_index=cd.index, lesson_id="-",
                    lesson_title=cd.actual_title or "",
                    content_type=ContentType.OTHER,
                    source_path=None,
                    status=AssetStatus.PENDING,
                    error=(
                        f"rename_pending: '{cd.actual_title}' → '{cd.desired_title}',"
                        f"需 confirm_rename=True"
                    ),
                ))
                # 把 chapter_id 设为实际 id(不删原章),后续 lessons 会走 SKIP
                assert cd.actual_id is not None
                chapter_ids[cd.index] = cd.actual_id
                return cd.actual_id
            # 用户显式确认 → delete + recreate
            print(f"  [改名] ch {cd.index}: '{cd.actual_title}' → '{cd.desired_title}' "
                  f"(删除重建,后续 lessons 全部 CREATE)")
            if not dry_run and cd.actual_id:
                delete_chapter(ctx, cd.actual_id)
            new_id = -1 if dry_run else create_chapter(ctx, ch.title)
            chapter_ids[cd.index] = new_id
            return new_id
        return None

    def _ensure_section(cd: ChapterDiff, ls: Lesson, ld: LessonDiff) -> Optional[int]:
        key = f"{cd.index}.{ls.id}"
        if key in section_ids:
            return section_ids[key]
        ch_id = _ensure_chapter(cd)
        if ch_id is None:
            return None
        if ld.action == DiffAction.SKIP:
            assert ld.actual_id is not None
            section_ids[key] = ld.actual_id
            return ld.actual_id
        # CREATE / RENAME → 创建新 section
        if dry_run:
            section_ids[key] = -(len(section_ids) + 1)
            return section_ids[key]
        sec_id = create_section(ctx, ch_id, ls.title)
        section_ids[key] = sec_id
        return sec_id

    for cd in diff.chapters:
        ch = chapter_by_idx.get(cd.index)
        if ch is None:
            continue

        if cd.action == DiffAction.PRUNE:
            if cd.actual_id and not dry_run:
                assets.append(Asset(
                    chapter_index=cd.index, lesson_id="-",
                    lesson_title=cd.actual_title or "",
                    content_type=ContentType.OTHER,
                    source_path=None,
                    status=AssetStatus.OK,
                    error=f"pruned chapter {cd.actual_id}",
                ))
                delete_chapter(ctx, cd.actual_id)
            continue

        # RENAME 待确认:整章不动,直接跳过
        # (否则会跑到 lesson 循环,误在旧章下补 leaf,违背"先确认再改"语义)
        if cd.action == DiffAction.RENAME and not confirm_rename:
            print(f"\n  [改名-待确认] ch {cd.index}: "
                  f"'{cd.actual_title}' → '{cd.desired_title}'")
            print(f"    当前未启用 confirm_rename,该章所有 lesson/leaf 暂不处理。")
            print(f"    原章保留(actual_id={cd.actual_id}),后续可传 --confirm-rename 真执行。")
            assets.append(Asset(
                chapter_index=cd.index, lesson_id="-",
                lesson_title=cd.actual_title or "",
                content_type=ContentType.OTHER,
                source_path=None,
                status=AssetStatus.PENDING,
                error=(
                    f"rename_pending: '{cd.actual_title}' → '{cd.desired_title}',"
                    f"需 confirm_rename=True"
                ),
            ))
            continue  # 整章跳过,不进 lesson 循环

        print(f"\n── 章 {cd.index}: {cd.desired_title} ({cd.action}) ──")

        for ld in cd.lesson_diffs:
            ls = next((l for l in ch.lessons if l.id == ld.id), None)
            if ls is None:
                continue

            # 跑该 lesson 所有 leaf diffs
            for lfd in ld.leaf_diffs:
                if lfd.action == DiffAction.SKIP:
                    continue

                # CREATE leaf
                if lfd.action in (DiffAction.CREATE,):
                    # 取 section_id
                    if ld.action == DiffAction.SKIP:
                        # 已有 section,直接 append
                        sec_id = ld.actual_id
                        ch_id = cd.actual_id
                    else:
                        sec_id = _ensure_section(cd, ls, ld)
                        ch_id = chapter_ids.get(cd.index) or cd.actual_id
                    if sec_id is None or ch_id is None:
                        continue
                    # 稳定 resource_key(用于 resume)
                    rkey = make_resource_key(
                        ctx.course_id, cd.index, ls.id, lfd.kind, lfd.desired_name,
                    )
                    try:
                        if dry_run:
                            print(f"  [dry-run] CREATE leaf "
                                  f"{ls.id} {lfd.kind} {lfd.desired_name}")
                            assets.append(Asset(
                                chapter_index=cd.index,
                                lesson_id=ls.id,
                                lesson_title=ls.title,
                                content_type=_content_type_for_kind(lfd.kind),
                                source_path=lfd.desired_name,
                                status=AssetStatus.SKIPPED,
                                error="dry-run",
                                resource_key=rkey,
                            ))
                        else:
                            leaf_id, ccid, size = _create_one_leaf(
                                ctx, ch_id, sec_id, ls.id, ls.title, lfd,
                                videos_folder, attachments_folder,
                            )
                            print(f"  [✓ {lfd.kind}] {ls.id} {lfd.desired_name} "
                                  f"→ leaf_id={leaf_id}"
                                  f"{f' ccid={ccid}' if ccid else ''}")
                            assets.append(Asset(
                                chapter_index=cd.index,
                                lesson_id=ls.id,
                                lesson_title=ls.title,
                                content_type=_content_type_for_kind(lfd.kind),
                                source_path=lfd.desired_name,
                                status=AssetStatus.OK,
                                target_url=(
                                    f"{BASE_URL}/pro/editcoursemanage/teachcontent/"
                                    f"{ctx.course_id}#leaf/{leaf_id}"
                                ),
                                attempts=1,
                                bytes_uploaded=size,
                                uploaded_at=datetime.now().isoformat(timespec="seconds"),
                                resource_key=rkey,
                            ))
                    except Exception as e:
                        logger.exception(f"创建 leaf 失败: {ls.id} {lfd.desired_name}")
                        print(f"  [✗ {lfd.kind}] {ls.id} {lfd.desired_name}: {e}")
                        assets.append(Asset(
                            chapter_index=cd.index,
                            lesson_id=ls.id,
                            lesson_title=ls.title,
                            content_type=_content_type_for_kind(lfd.kind),
                            source_path=lfd.desired_name,
                            status=AssetStatus.FAILED,
                            attempts=1,
                            error=str(e),
                            resource_key=rkey,
                        ))
    return assets


def _print_diff_summary(diff: TreeDiff) -> None:
    """打印 diff 摘要,让用户看清要做什么"""
    s = diff.stats
    print(f"  diff 摘要: SKIP={s.get('skip', 0)} "
          f"CREATE={s.get('create', 0)} "
          f"RENAME={s.get('rename', 0)} "
          f"PRUNE={s.get('prune', 0)}")
    total = diff.total_planned()
    skip = s.get("skip", 0)
    print(f"  drift = {total} / {total + skip} "
          f"(阈值 {DRIFT_THRESHOLD:.0%})")
    if diff.is_too_drifted(DRIFT_THRESHOLD):
        print(f"  ⚠⚠⚠ drift 超过阈值!建议 --reset-confirm {diff.course_id} 清空重建")
    if diff.extra_chapter_ids:
        print(f"  额外章(默认保留,需 --prune 才删): {list(diff.extra_chapter_ids)}")


def _mark_resume_keys(diff: TreeDiff, ok_keys: set[str]) -> TreeDiff:
    """把 diff 里 resource_key 在 ok_keys 里的 leaf 从 CREATE 转 SKIP。

    增量 resume 用:上次跑过的成功资源,这次默认跳过。
    复用:章节/节 不动,只对 leaf 维度做幂等。
    返回新 TreeDiff(原 diff 不动,frozen dataclass 友好)。
    """
    if not ok_keys:
        return diff
    new_chapters: list[ChapterDiff] = []
    for cd in diff.chapters:
        new_lessons: list[LessonDiff] = []
        for ld in cd.lesson_diffs:
            new_leaves: list[LeafDiff] = []
            for lfd in ld.leaf_diffs:
                if lfd.action == DiffAction.CREATE:
                    # 算 resource_key(同 _execute_diff 里的算法)
                    rkey = make_resource_key(
                        diff.course_id, cd.index, ld.id, lfd.kind, lfd.desired_name,
                    )
                    if rkey in ok_keys:
                        new_leaves.append(LeafDiff(
                            lesson_id=lfd.lesson_id,
                            kind=lfd.kind,
                            desired_name=lfd.desired_name,
                            actual_id=lfd.actual_id,
                            action=DiffAction.SKIP,
                        ))
                        continue
                new_leaves.append(lfd)
            new_lessons.append(LessonDiff(
                id=ld.id, desired_title=ld.desired_title,
                actual_id=ld.actual_id, actual_title=ld.actual_title,
                action=ld.action, matched_by=ld.matched_by,
                leaf_diffs=tuple(new_leaves),
            ))
        new_chapters.append(ChapterDiff(
            index=cd.index, desired_title=cd.desired_title,
            actual_id=cd.actual_id, actual_title=cd.actual_title,
            action=cd.action, matched_by=cd.matched_by,
            lesson_diffs=tuple(new_lessons),
        ))

    # 重新算 stats:被 resume 标记的 CREATE 转 SKIP
    new_stats = dict(diff.stats)
    flipped = 0
    for old_cd, new_cd in zip(diff.chapters, new_chapters):
        for old_ld, new_ld in zip(old_cd.lesson_diffs, new_cd.lesson_diffs):
            for old_lfd, new_lfd in zip(old_ld.leaf_diffs, new_ld.leaf_diffs):
                if old_lfd.action == DiffAction.CREATE and new_lfd.action == DiffAction.SKIP:
                    # CREATE → SKIP
                    new_stats["create"] = new_stats.get("create", 0) - 1
                    new_stats["create_leaves"] = new_stats.get("create_leaves", 0) - 1
                    new_stats["skip"] = new_stats.get("skip", 0) + 1
                    flipped += 1
    if flipped:
        logger.info(f"resume 跳过 {flipped} 个已成功的 resource_key")
    return TreeDiff(
        course_id=diff.course_id,
        chapters=tuple(new_chapters),
        extra_chapter_ids=diff.extra_chapter_ids,
        stats=new_stats,
    )


def _mark_retry_keys(diff: TreeDiff, retry_keys: set[str]) -> TreeDiff:
    """只保留 retry_keys 里的 CREATE leaf;其他 CREATE 全部转 SKIP。

    用于:失败资源清单 (_retry_resources.json) 触发的增量重试。
    跟 _mark_resume_keys 相反:这里"留下"的是要重试的,其他都跳过。
    """
    new_chapters: list[ChapterDiff] = []
    for cd in diff.chapters:
        new_lessons: list[LessonDiff] = []
        for ld in cd.lesson_diffs:
            new_leaves: list[LeafDiff] = []
            for lfd in ld.leaf_diffs:
                if lfd.action == DiffAction.CREATE:
                    rkey = make_resource_key(
                        diff.course_id, cd.index, ld.id, lfd.kind, lfd.desired_name,
                    )
                    if rkey not in retry_keys:
                        # 不在重试列表 → 跳过
                        new_leaves.append(LeafDiff(
                            lesson_id=lfd.lesson_id,
                            kind=lfd.kind,
                            desired_name=lfd.desired_name,
                            actual_id=lfd.actual_id,
                            action=DiffAction.SKIP,
                        ))
                        continue
                new_leaves.append(lfd)
            new_lessons.append(LessonDiff(
                id=ld.id, desired_title=ld.desired_title,
                actual_id=ld.actual_id, actual_title=ld.actual_title,
                action=ld.action, matched_by=ld.matched_by,
                leaf_diffs=tuple(new_leaves),
            ))
        new_chapters.append(ChapterDiff(
            index=cd.index, desired_title=cd.desired_title,
            actual_id=cd.actual_id, actual_title=cd.actual_title,
            action=cd.action, matched_by=cd.matched_by,
            lesson_diffs=tuple(new_lessons),
        ))

    new_stats = dict(diff.stats)
    flipped = 0
    for old_cd, new_cd in zip(diff.chapters, new_chapters):
        for old_ld, new_ld in zip(old_cd.lesson_diffs, new_cd.lesson_diffs):
            for old_lfd, new_lfd in zip(old_ld.leaf_diffs, new_ld.leaf_diffs):
                if old_lfd.action == DiffAction.CREATE and new_lfd.action == DiffAction.SKIP:
                    new_stats["create"] = new_stats.get("create", 0) - 1
                    new_stats["create_leaves"] = new_stats.get("create_leaves", 0) - 1
                    new_stats["skip"] = new_stats.get("skip", 0) + 1
                    flipped += 1
    if flipped:
        logger.info(f"retry 跳过 {flipped} 个非目标 leaf(只重跑 {len(retry_keys)} 个 key)")
    return TreeDiff(
        course_id=diff.course_id,
        chapters=tuple(new_chapters),
        extra_chapter_ids=diff.extra_chapter_ids,
        stats=new_stats,
    )


# ─── U1: 局部目标过滤 ─────────────────────────────────────

def _is_target_leaf(
    lesson_id: str,
    kind: str,
    *,
    only_lessons: set[str] | None,
    only_resources: set[tuple[str, str]] | None,
) -> bool:
    """判断 (lesson_id, kind) 是否在用户指定的局部目标范围内。

    匹配规则:
      - only_resources 优先(精确到 lesson + kind,形如 "1.2:english")
      - 然后 only_lessons(粗到 lesson,形如 "1.2")
      - 都为 None → 全 True(不限制)
    """
    if only_resources is not None:
        if (lesson_id, kind) in only_resources:
            return True
        if only_lessons is None:
            return False
    if only_lessons is not None:
        if lesson_id in only_lessons:
            return True
        return False
    return True


def _mark_only_targets(
    diff: TreeDiff,
    *,
    only_lessons: set[str] | None,
    only_resources: set[tuple[str, str]] | None,
) -> TreeDiff:
    """U1:把所有"非目标范围"的 leaf / lesson / chapter 标 SKIP。

    设计:
      - 非目标的 CREATE → SKIP(不创建)
      - 非目标的 RENAME → SKIP(不重命名)
      - 非目标的 PRUNE → SKIP(不删除,默认 prune=False 已经不删)
      - 非目标 leaf 所在 lesson → SKIP(空 lesson 也不创建)
      - 非目标 lesson 所在 chapter → SKIP(空 chapter 也不创建)
      - 全部标 SKIP 后,stats 调整到反映"实际会操作的资源数"

    跟 _mark_resume_keys 的区别:
      - resume:只看 resource_key 匹配,不影响 chapter/lesson
      - only_targets:全层级 SKIP,且作用在 lesson_id + kind 维度(更细)
    """
    new_chapters: list[ChapterDiff] = []
    new_stats = dict(diff.stats)
    flipped = 0

    for cd in diff.chapters:
        new_lessons: list[LessonDiff] = []
        any_lesson_active = False
        for ld in cd.lesson_diffs:
            new_leaves: list[LeafDiff] = []
            any_leaf_active = False
            for lfd in ld.leaf_diffs:
                if _is_target_leaf(
                    lfd.lesson_id, lfd.kind,
                    only_lessons=only_lessons, only_resources=only_resources,
                ):
                    new_leaves.append(lfd)
                    any_leaf_active = True
                else:
                    # 非目标 → SKIP,统计翻转
                    if lfd.action in (DiffAction.CREATE, DiffAction.RENAME):
                        if lfd.action == DiffAction.CREATE:
                            new_stats["create"] = new_stats.get("create", 0) - 1
                            new_stats["create_leaves"] = new_stats.get("create_leaves", 0) - 1
                        else:
                            new_stats["rename"] = new_stats.get("rename", 0) - 1
                        new_stats["skip"] = new_stats.get("skip", 0) + 1
                        flipped += 1
                    new_leaves.append(LeafDiff(
                        lesson_id=lfd.lesson_id, kind=lfd.kind,
                        desired_name=lfd.desired_name, actual_id=lfd.actual_id,
                        action=DiffAction.SKIP,
                    ))
            if any_leaf_active:
                # 至少有一个 leaf 是目标 → lesson 保持
                new_lessons.append(LessonDiff(
                    id=ld.id, desired_title=ld.desired_title,
                    actual_id=ld.actual_id, actual_title=ld.actual_title,
                    action=ld.action, matched_by=ld.matched_by,
                    leaf_diffs=tuple(new_leaves),
                ))
                any_lesson_active = True
            else:
                # 全部 leaf 非目标 → lesson 整体 SKIP
                # 关键:leaf 级已经为每个非目标 leaf 减过 create/create_leaves
                # 这里只补减 create_sections(lesson 创建数)
                if ld.action == DiffAction.CREATE:
                    new_stats["create_sections"] = new_stats.get("create_sections", 0) - 1
                elif ld.action == DiffAction.RENAME:
                    new_stats["rename"] = new_stats.get("rename", 0) - 1
                new_stats["skip"] = new_stats.get("skip", 0) + 1
                flipped += 1
                new_lessons.append(LessonDiff(
                    id=ld.id, desired_title=ld.desired_title,
                    actual_id=ld.actual_id, actual_title=ld.actual_title,
                    action=DiffAction.SKIP, matched_by=ld.matched_by,
                    leaf_diffs=tuple(new_leaves),
                ))
        if any_lesson_active:
            new_chapters.append(ChapterDiff(
                index=cd.index, desired_title=cd.desired_title,
                actual_id=cd.actual_id, actual_title=cd.actual_title,
                action=cd.action, matched_by=cd.matched_by,
                lesson_diffs=tuple(new_lessons),
            ))
        else:
            # 全部 lesson 非目标 → chapter 整体 SKIP
            # 关键:lesson 级已经减过 create_sections;leaf 级已经减过 create/create_leaves
            # 这里只补减 create_chapters(chapter 创建数)
            if cd.action == DiffAction.CREATE:
                new_stats["create_chapters"] = new_stats.get("create_chapters", 0) - 1
            elif cd.action == DiffAction.RENAME:
                new_stats["rename"] = new_stats.get("rename", 0) - 1
            new_stats["skip"] = new_stats.get("skip", 0) + 1
            flipped += 1
            new_chapters.append(ChapterDiff(
                index=cd.index, desired_title=cd.desired_title,
                actual_id=cd.actual_id, actual_title=cd.actual_title,
                action=DiffAction.SKIP, matched_by=cd.matched_by,
                lesson_diffs=tuple(new_lessons),
            ))

    if flipped:
        logger.info(f"only_targets 跳过 {flipped} 个非目标操作")
    return TreeDiff(
        course_id=diff.course_id,
        chapters=tuple(new_chapters),
        extra_chapter_ids=diff.extra_chapter_ids,
        stats=new_stats,
    )


def _parse_only_targets(
    only_chapters: set[int] | None = None,
    only_lessons: set[str] | None = None,
    only_resources: set[str] | None = None,
) -> tuple[set[int] | None, set[str] | None, set[tuple[str, str]] | None]:
    """CLI 形参 → _mark_only_targets 内部用的格式。

    Returns:
        (chapters, lessons, resources_parsed)
        resources_parsed 是 {("1.2", "english"), ...} 形式
    """
    del only_chapters  # 暂未在本函数使用,留给调用方
    resources_parsed: set[tuple[str, str]] | None = None
    if only_resources is not None:
        resources_parsed = set()
        for r in only_resources:
            if ":" not in r:
                logger.warning(f"--only-resource 格式应为 'lesson:kind',忽略 {r!r}")
                continue
            lesson_id, kind = r.split(":", 1)
            resources_parsed.add((lesson_id.strip(), kind.strip()))
        if not resources_parsed:
            resources_parsed = None
    return None, only_lessons, resources_parsed


def _parse_only_targets_only(
    only_lessons: set[str] | None,
    only_resources: set[str] | None,
) -> tuple[set[str] | None, set[tuple[str, str]] | None]:
    """run_upload_api 内部用的简化版:只解析 only_lessons / only_resources。

    跟 _parse_only_targets 区别:不处理 only_chapters(由 compute_diff 单独处理)。
    """
    resources_parsed: set[tuple[str, str]] | None = None
    if only_resources is not None:
        resources_parsed = set()
        for r in only_resources:
            if ":" not in r:
                logger.warning(f"--only-resource 格式应为 'lesson:kind',忽略 {r!r}")
                continue
            lesson_id, kind = r.split(":", 1)
            resources_parsed.add((lesson_id.strip(), kind.strip()))
        if not resources_parsed:
            resources_parsed = None
    return only_lessons, resources_parsed


# ─── U3: plan-only 模式 ─────────────────────────────────────

def write_upload_plan(
    diff: TreeDiff,
    output_dir: Path,
    *,
    course_id: str,
    only_lessons: set[str] | None = None,
    only_resources: set[tuple[str, str]] | None = None,
    only_chapters: set[int] | None = None,
    high_risk: bool = False,
    high_risk_reason: str = "",
    mapping_hash: str = "",
    tree_fingerprint: str = "",
    tree_source: str = "real",
    tree_chapter_count: int = 0,
    tree_is_empty: bool = False,
) -> dict[str, Path]:
    """U3:写 _upload_plan.json 和 _upload_plan.md,标记每项的 action + reason。

    元数据(P2):
      - generated_at:ISO 时间
      - course_id
      - mapping_hash:CourseStructure 的 SHA1(apply-plan 校验)
      - tree_fingerprint:后台真实树 SHA1(apply-plan 校验)
      - tree_source: "real"(真后台拉到) / "fallback"(无 cookie fallback 空树)
      - tree_chapter_count: 后台章数(0 = 空树)
      - tree_is_empty: 后台是否空
      - scope:only_chapters / only_lessons / only_resources
      - summary:动作计数 + high_risk 标志
      - items:每项 action + reason
    """
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    plan_items: list[dict[str, Any]] = []
    for cd in diff.chapters:
        for ld in cd.lesson_diffs:
            for lfd in ld.leaf_diffs:
                reason = _leaf_action_reason(cd, ld, lfd, only_lessons, only_resources)
                plan_items.append({
                    "ch_num": cd.index,
                    "lesson_id": ld.id,
                    "lesson_title": ld.desired_title,
                    "kind": lfd.kind,
                    "desired_name": lfd.desired_name,
                    "action": lfd.action.upper(),
                    "reason": reason,
                })
    actions_count: dict[str, int] = {}
    for it in plan_items:
        actions_count[it["action"]] = actions_count.get(it["action"], 0) + 1
    payload = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "course_id": str(course_id),
        "mapping_hash": mapping_hash,
        "tree_fingerprint": tree_fingerprint,
        "tree_source": tree_source,
        "tree_chapter_count": tree_chapter_count,
        "tree_is_empty": tree_is_empty,
        "scope": {
            "only_chapters": sorted(only_chapters) if only_chapters else None,
            "only_lessons": sorted(only_lessons) if only_lessons else None,
            "only_resources": sorted(f"{l}:{k}" for l, k in only_resources) if only_resources else None,
        },
        "summary": {
            **actions_count,
            "total": len(plan_items),
            "high_risk": high_risk,
            "high_risk_reason": high_risk_reason,
        },
        "items": plan_items,
    }
    plan_json = output_dir / "_upload_plan.json"
    plan_json.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    plan_md = output_dir / "_upload_plan.md"
    plan_md.write_text(_render_upload_plan_md(payload), encoding="utf-8")
    return {"plan_json": plan_json, "plan_md": plan_md}


# ─── P2/P3:plan 元数据 + apply-plan 校验 ──

def _structure_to_hash_input(structure: CourseStructure) -> str:
    """把 CourseStructure 序列化成稳定字符串(用于 SHA1)。

    关键:用 chapter/lesson 的稳定字段(不含 datetime),保证同样 mapping 算同样 hash。
    """
    parts: list[str] = []
    for ch in structure.chapters:
        parts.append(f"ch{ch.index}:{ch.title}")
        for ls in ch.lessons:
            parts.append(f"ls{ls.id}:{ls.title}:{ls.content_type}:{ls.video or ''}")
            for a in ls.attachments:
                parts.append(f"att:{a}")
    return "|".join(parts)


def compute_mapping_hash(structure: CourseStructure) -> str:
    """对 mapping 算 SHA1 前 16 字符 hex(写入 plan,用于 apply-plan 校验)。"""
    import hashlib
    raw = _structure_to_hash_input(structure)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def compute_tree_fingerprint(tree: dict) -> str:
    """对后台真实树算 SHA1 前 16 字符 hex(写入 plan,用于 apply-plan 校验)。

    设计(F1):
      - chapter_list / section_list / leaf_list 都按 (id, name) 排序后再 hash
      - 这样超星 API 返回的顺序变化(常见:重排、新增/删除中间项)不会影响 fingerprint
      - 真正改变 fingerprint 的:内容变化(id 变了、name 变了、新增/删除了 leaf)
    """
    import hashlib
    chapters_norm = []
    for ch in tree.get("chapter_list", []):
        cid = ch.get("id", "?")
        cname = ch.get("name", "")
        sections_norm = []
        for sec in ch.get("section_list", []):
            sid = sec.get("id", "?")
            sname = sec.get("name", "")
            leaves_norm = sorted(
                (
                    (leaf.get("id", "?"), leaf.get("name", ""))
                    for leaf in sec.get("leaf_list", [])
                ),
                key=lambda x: (str(x[0]), str(x[1])),
            )
            sections_norm.append((sid, sname, tuple(leaves_norm)))
        # chapter 间按 (id, name) 排序
        sections_norm = sorted(
            sections_norm,
            key=lambda x: (str(x[0]), str(x[1])),
        )
        chapters_norm.append((cid, cname, tuple(sections_norm)))
    chapters_norm = sorted(
        chapters_norm,
        key=lambda x: (str(x[0]), str(x[1])),
    )
    raw = repr(chapters_norm)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def _load_and_verify_plan(
    plan_path: Path,
    *,
    course_id: str,
    mapping_hash: str,
    tree_fingerprint: str,
    only_chapters: set[int] | None,
    only_lessons: set[str] | None,
    only_resources: set[tuple[str, str]] | None,
) -> dict:
    """P3:加载 _upload_plan.json 并校验 4 项一致性。

    Returns:
        校验通过的 plan 字典(load 出来的内容)

    Raises:
        SystemExit(如果任一校验失败)
    """
    import json as _json
    if not plan_path.exists():
        print(f"[错误] --apply-plan 文件不存在: {plan_path}")
        sys.exit(1)
    try:
        plan = _json.loads(plan_path.read_text(encoding="utf-8"))
    except Exception as e:
        print(f"[错误] --apply-plan 解析失败: {e}")
        sys.exit(1)

    # ── F2:必填字段 + 类型校验(残缺/旧版 plan 必须拒绝) ──

    # mapping_hash 必须存在(空字符串也不算)
    if "mapping_hash" not in plan:
        print(f"[错误] --apply-plan mapping_hash 字段缺失(可能是旧版 plan):")
        print("  拒绝执行,要求重新 plan")
        sys.exit(1)
    if not isinstance(plan.get("mapping_hash"), str):
        print(f"[错误] --apply-plan mapping_hash 不是字符串(类型错):")
        print(f"  实际类型: {type(plan.get('mapping_hash')).__name__}")
        sys.exit(1)

    # tree_fingerprint 必须存在
    if "tree_fingerprint" not in plan:
        print(f"[错误] --apply-plan tree_fingerprint 字段缺失(可能是旧版 plan):")
        print("  拒绝执行,要求重新 plan")
        sys.exit(1)
    if not isinstance(plan.get("tree_fingerprint"), str):
        print(f"[错误] --apply-plan tree_fingerprint 不是字符串(类型错):")
        print(f"  实际类型: {type(plan.get('tree_fingerprint')).__name__}")
        sys.exit(1)

    # scope 必须存在 + 是 dict
    if "scope" not in plan:
        print(f"[错误] --apply-plan scope 字段缺失(可能是旧版 plan):")
        print("  拒绝执行,要求重新 plan")
        sys.exit(1)
    if not isinstance(plan.get("scope"), dict):
        print(f"[错误] --apply-plan scope 不是 dict(类型错):")
        print(f"  实际类型: {type(plan.get('scope')).__name__}")
        sys.exit(1)
    plan_scope = plan["scope"]

    # 1) course_id 一致
    if str(plan.get("course_id")) != str(course_id):
        print(f"[错误] --apply-plan course_id 不一致:")
        print(f"  plan 中: {plan.get('course_id')!r}")
        print(f"  当前:    {course_id!r}")
        print("  拒绝执行,要求重新 plan")
        sys.exit(1)

    # 2) mapping_hash 一致
    if plan["mapping_hash"] != mapping_hash:
        print(f"[错误] --apply-plan mapping_hash 不一致(mapping 改了):")
        print(f"  plan 中: {plan['mapping_hash']}")
        print(f"  当前:    {mapping_hash}")
        print("  拒绝执行,要求重新 plan")
        sys.exit(1)

    # 3) scope 一致(only_chapters / only_lessons / only_resources)
    current_scope = {
        "only_chapters": sorted(only_chapters) if only_chapters else None,
        "only_lessons": sorted(only_lessons) if only_lessons else None,
        "only_resources": sorted(f"{l}:{k}" for l, k in only_resources) if only_resources else None,
    }
    for k in ("only_chapters", "only_lessons", "only_resources"):
        if current_scope[k] != plan_scope.get(k):
            print(f"[错误] --apply-plan scope.{k} 不一致:")
            print(f"  plan 中: {plan_scope.get(k)}")
            print(f"  当前:    {current_scope[k]}")
            print("  拒绝执行,要求重新 plan")
            sys.exit(1)

    # 4) tree_fingerprint 一致
    if plan["tree_fingerprint"] != tree_fingerprint:
        print(f"[错误] --apply-plan tree_fingerprint 不一致(后台树变了):")
        print(f"  plan 中: {plan['tree_fingerprint']}")
        print(f"  当前:    {tree_fingerprint}")
        print("  拒绝执行,后台可能被人改过,要求重新 plan")
        sys.exit(1)

    print(f"[apply-plan] 校验通过 — plan 来自 {plan.get('generated_at', '?')}")
    return plan


def _leaf_action_reason(
    cd: ChapterDiff, ld: LessonDiff, lfd: LeafDiff,
    only_lessons: set[str] | None,
    only_resources: set[tuple[str, str]] | None,
) -> str:
    """给每个 leaf 算 reason(plan 报告里给用户看)。"""
    if lfd.action == DiffAction.SKIP:
        if only_lessons or only_resources:
            return "局部目标外,不操作"
        return "已存在且匹配"
    if lfd.action == DiffAction.CREATE:
        return f"缺失:第 {cd.index} 章 '{cd.desired_title}' > 第 {ld.id} 节 '{ld.desired_title}' 缺 [{lfd.kind}]"
    if lfd.action == DiffAction.RENAME:
        return f"改名:原 '{lfd.desired_name}' 不匹配(需 confirm_rename=True 才执行)"
    if lfd.action == DiffAction.PRUNE:
        return "后台多余(需 --prune 才删)"
    return lfd.action


def _render_upload_plan_md(payload: dict[str, Any]) -> str:
    s = payload["summary"]
    lines: list[str] = []
    lines.append(f"# Upload Plan — {payload['course_id']}")
    lines.append("")
    lines.append(f"- 生成时间: {payload.get('generated_at', '?')}")
    lines.append(f"- mapping hash: `{payload.get('mapping_hash', '?')[:16]}...`")
    lines.append(f"- tree fingerprint: `{payload.get('tree_fingerprint', '?')[:16]}...`")
    # F1:清晰区分真树 vs fallback 空树
    tree_source = payload.get("tree_source", "real")
    tree_ch_count = payload.get("tree_chapter_count", 0)
    tree_empty = payload.get("tree_is_empty", tree_ch_count == 0)
    tree_kind = "empty_tree" if tree_empty else "has_content"
    lines.append(
        f"- 后台树: {tree_source}, {tree_ch_count} 章, {tree_kind}"
    )
    lines.append("")
    if s.get("high_risk"):
        lines.append("## ⚠ HIGH_RISK")
        lines.append("")
        lines.append(f"**{s.get('high_risk_reason', '')}**")
        lines.append("")
        lines.append("局部模式下,即使 HIGH_RISK 也只报告不阻断,不会 reset。")
        lines.append("")
    lines.append("## 摘要")
    lines.append("")
    lines.append(f"- CREATE:{s.get('CREATE', 0)}")
    lines.append(f"- SKIP:{s.get('SKIP', 0)}")
    lines.append(f"- RENAME:{s.get('RENAME', 0)}")
    lines.append(f"- PRUNE:{s.get('PRUNE', 0)}")
    lines.append(f"- 总数:{s.get('total', 0)}")
    lines.append("")
    scope = payload.get("scope", {})
    if scope.get("only_lessons") or scope.get("only_resources"):
        lines.append("## 局部目标")
        lines.append("")
        if scope.get("only_lessons"):
            lines.append(f"- only_lessons: `{'`, `'.join(scope['only_lessons'])}`")
        if scope.get("only_resources"):
            lines.append(f"- only_resources: `{'`, `'.join(scope['only_resources'])}`")
        lines.append("")
    lines.append("## 计划项")
    lines.append("")
    lines.append("| 章 | 节 | 资源类型 | 操作 | 原因 |")
    lines.append("|---|---|---|---|---|")
    for it in payload["items"]:
        if it["action"] in ("CREATE", "RENAME", "PRUNE"):
            lines.append(f"| {it['ch_num']} | {it['lesson_id']} | {it['kind']} | "
                         f"**{it['action']}** | {it['reason']} |")
    return "\n".join(lines)






def _run_plan_only(
    structure: CourseStructure,
    videos_folder: Path,
    output_dir: Path,
    started_at: str,
    *,
    cookies_path: Path | None = None,
    cookies_string: str | None = None,
    only_chapters: set[int] | None = None,
    only_lessons: set[str] | None = None,
    only_resources: set[tuple[str, str]] | None = None,
) -> UploadResult:
    """U3:plan-only 子流程 — 只读 + 算 diff + 写 plan,不调任何写 API。

    Cookie 缺失时 fallback:用空章节树跑 plan(plan 不准 = 100% CREATE,
    不校验 tree_fingerprint)。用户后续 --apply-plan 会触发完整 4 项校验。
    """
    session = _make_session(cookies_path=cookies_path, cookies_string=cookies_string)
    csrftoken = session.cookies.get("csrftoken", "")
    if not csrftoken:
        # 无 csrftoken(老师后台 cookie 缺失):plan-only 走本地空树 fallback
        print("[plan-only] Cookie 无 csrftoken(老师后台 cookie 缺失)")
        print("[plan-only] 走 fallback:用空章节树 + 本地 mapping 算 plan")
        print("[plan-only] 注意:apply-plan 时 tree_fingerprint 会不匹配,需 --yes 或重 plan")
        tree = {"chapter_list": []}
        tree_source = "fallback"
        tree_chapter_count = 0
        tree_is_empty = True
        high_risk = True
        high_risk_reason = (
            "无 csrftoken,无法拉后台真实树;plan 按 100% CREATE 计算。"
            "正式上传前必须先有完整 cookie 重新 plan-only。"
        )
    else:
        try:
            ctx = _build_context(session, structure.course_id)
        except RuntimeError as e:
            print(f"[plan-only] {e}")
            print("[plan-only] 走 fallback:空树")
            tree = {"chapter_list": []}
            tree_source = "fallback"
            tree_chapter_count = 0
            tree_is_empty = True
            high_risk = True
            high_risk_reason = str(e)
        else:
            _print_header(structure, ctx, videos_folder, dry_run=True, verify_only=False)
            if not verify_login(ctx):
                return _empty_result(ctx, structure.course_title, started_at, mode="plan_only")
            try:
                tree = get_resource_tree(ctx)
            except (requests.RequestException, RuntimeError) as e:
                logger.exception("拉章节树失败")
                print(f"  [警告] 拉章节树失败: {e}")
                tree = {"chapter_list": []}
                tree_source = "fallback"
                tree_chapter_count = 0
                tree_is_empty = True
            else:
                # 真后台树拉到了 — 区分真树和空树
                tree_chapter_count = len(tree.get("chapter_list", []))
                tree_source = "real"
                tree_is_empty = tree_chapter_count == 0
            high_risk = False
            high_risk_reason = ""

    diff = compute_diff(
        structure, tree,
        only_chapters=only_chapters, prune=False,
    )

    is_partial = bool(only_lessons or only_resources)
    if is_partial:
        diff = _mark_only_targets(
            diff, only_lessons=only_lessons, only_resources=only_resources,
        )

    # high_risk 已在 fallback / 正常路径上设置(空树或局部模式)
    # 正常路径 + is_too_drifted 检测补充(空树本身就是 100% CREATE,drift 必超)
    if not high_risk and diff.is_too_drifted(DRIFT_THRESHOLD):
        high_risk = True
        high_risk_reason = (
            f"drift = {diff.total_planned()}/"
            f"{diff.total_planned() + diff.stats.get('skip', 0)} "
            f">= {DRIFT_THRESHOLD:.0%}"
        )

    # 算 hash + fingerprint(P2 校验用)
    mapping_hash = compute_mapping_hash(structure)
    tree_fingerprint = compute_tree_fingerprint(tree)
    plan_course_id = structure.course_id

    paths = write_upload_plan(
        diff, output_dir,
        course_id=plan_course_id,
        only_lessons=only_lessons, only_resources=only_resources,
        only_chapters=only_chapters,
        high_risk=high_risk, high_risk_reason=high_risk_reason,
        mapping_hash=mapping_hash,
        tree_fingerprint=tree_fingerprint,
        tree_source=tree_source,
        tree_chapter_count=tree_chapter_count,
        tree_is_empty=tree_is_empty,
    )
    print(f"\n[plan-only] 计划已写:")
    for k, p in paths.items():
        print(f"  - {Path(p).name}")
    _print_diff_summary(diff)
    if high_risk:
        print(f"\n  ⚠ HIGH_RISK: {high_risk_reason}")
    print("\n[plan-only] 完毕。不会调用任何写 API。")
    # fallback 情况:无 ctx,直接构造 UploadResult
    if "ctx" in dir() and ctx is not None:
        return _empty_result(ctx, structure.course_title, started_at, mode="plan_only")
    return UploadResult(
        course_id=str(plan_course_id),
        course_title=structure.course_title,
        started_at=started_at,
        finished_at=datetime.now().isoformat(timespec="seconds"),
        assets=(),
        mode="plan_only",
    )


def run_upload_api(
    structure: CourseStructure,
    videos_folder: Path,
    cookies_path: Path | None = None,
    cookies_string: str | None = None,
    output_dir: Path | None = None,
    dry_run: bool = False,
    verify_only: bool = False,
    only_chapters: set[int] | None = None,
    only_lessons: set[str] | None = None,
    only_resources: set[str] | None = None,
    prune: bool = False,
    reset_confirm: str | None = None,
    confirm_rename: bool = False,
    resume_from_manifest: Path | None = None,
    retry_keys: set[str] | None = None,
    plan_only: bool = False,
    apply_plan_path: Path | None = None,
    yes: bool = False,
) -> UploadResult:
    """主入口:按 mapping 在老师后台建章/建节/上传视频(状态机版)。

    Args:
        structure: 课程结构(由 build_mapping 生成)
        videos_folder: 视频文件夹根目录
        cookies_path: 教师 cookie 文件路径
        cookies_string: 教师 cookie 字符串(优先于 path,允许 in-memory 模式)
        output_dir: 报告/日志输出目录,默认 videos_folder
        dry_run: 只打印计划,不调用 API
        verify_only: 只跑 cookie 验证和 resource_tree 读取,不写
        only_chapters: 仅处理这些 chapter index(可选,用于增量/调试)
        only_lessons: U1 局部目标 — 只处理这些 lesson_id(形如 "1.2")
        only_resources: U1 局部目标 — 只处理这些 (lesson_id, kind),形如 "1.2:english"
        prune: 是否删除 mapping 中没有的多余章(默认 False,仅 SKIP)
        reset_confirm: 显式传 course_id 才允许 reset_course_tree(危险操作,先备份)
                       U2:传了 only_lessons 或 only_resources 时此参数被忽略(禁止局部 reset)
        confirm_rename: 章名不一致时是否真的执行 RENAME(delete + recreate,清空原章所有 leaf);
                       默认 False → RENAME 进入 PENDING 状态,只标记不动
        resume_from_manifest: 旧 _upload_manifest.json 路径;若提供,跳过其中 status=OK
                              且 resource_key 仍匹配 mapping 的资源(增量上传)
        retry_keys: 显式列出的 resource_key 集合;若提供,只跑这些 key 对应的
                    CREATE leaf(其他 CREATE 全部 SKIP)。配合 _retry_resources.json 用。
        plan_only: U3 只输出 _upload_plan.json/md,不调用任何写 API
        apply_plan_path: 加载之前 plan-only 写的 _upload_plan.json,做 4 项校验
                        (course_id / mapping_hash / scope / tree_fingerprint),
                        通过才执行。跟 --yes 互斥。
        yes: 显式跳过 plan-first 安全闸(允许直接执行写 API)。
             即使传 --yes,局部模式仍禁 reset,drift > 60% 全量模式仍按旧逻辑拒绝。

    Returns:
        UploadResult 包含每个 leaf 的执行状态
    """
    started_at = datetime.now().isoformat(timespec="seconds")
    output_dir = output_dir or videos_folder
    output_dir.mkdir(parents=True, exist_ok=True)

    # ─── U1 解析局部目标 ──
    only_lessons_p, only_resources_p = _parse_only_targets_only(
        only_lessons=only_lessons, only_resources=only_resources,
    )
    is_partial = bool(only_lessons_p or only_resources_p)

    # ─── U2 局部模式禁止 reset(忽略 reset_confirm) ──
    if is_partial and reset_confirm is not None:
        print(f"\n[警告] 局部模式(--only-lesson / --only-resource)忽略 --reset-confirm,不会清空重建")
        reset_confirm = None

    # ─── U3 plan-only 早返(只算 diff + 写 plan,不调任何写 API) ──
    # 重要:plan_only 仍然要 cookie + 拉章节树(只读),不算写入
    if plan_only:
        return _run_plan_only(
            structure, videos_folder, output_dir, started_at,
            cookies_path=cookies_path, cookies_string=cookies_string,
            only_chapters=only_chapters, only_lessons=only_lessons_p,
            only_resources=only_resources_p,
        )

    # ─── P1:plan-first 安全闸 ──
    # 规则:
    #   - 显式 --yes:跳过 plan-first(走原执行路径)
    #   - 显式 --apply-plan <path>:加载 + 校验 plan,校验通过走执行路径
    #   - 否则(默认):自动转 plan-only,写 plan 后要求用户 review
    # 例外:dry_run / verify_only 走原行为(本来就不写)
    if not dry_run and not verify_only:
        if apply_plan_path is not None and yes:
            # 互斥报错 — 没 ctx 时用 dummy(只要走到这行,后面不会真用)
            from .models import TeacherContext  # 兜底
            try:
                dummy_ctx = TeacherContext(
                    session=requests.Session(),
                    course_id=str(structure.course_id),
                )
            except Exception:
                dummy_ctx = None
            print(f"[错误] --apply-plan 和 --yes 互斥,只能选一个")
            sys.exit(1)
        if apply_plan_path is None and not yes:
            # 默认 plan-first:跑 plan-only 子流程
            result = _run_plan_only(
                structure, videos_folder, output_dir, started_at,
                cookies_path=cookies_path, cookies_string=cookies_string,
                only_chapters=only_chapters, only_lessons=only_lessons_p,
                only_resources=only_resources_p,
            )
            plan_json = output_dir / "_upload_plan.json"
            print(f"\n[plan-first] 计划已写到: {plan_json}")
            print("[plan-first] 默认安全闸:必须显式确认才能执行写 API")
            print("  - review 后执行: --apply-plan " + str(plan_json))
            print("  - 跳过 review:    --yes")
            print("  - 仍只想看 plan: --plan-only")
            return result

    session = _make_session(cookies_path=cookies_path, cookies_string=cookies_string)
    ctx = _build_context(session, structure.course_id)

    _print_header(structure, ctx, videos_folder, dry_run, verify_only)

    # 登录态预检
    print("\n[1/4] 验证 Cookie...")
    if not verify_login(ctx):
        return _empty_result(ctx, structure.course_title, started_at, mode="verify_only")

    # 拉现有章节树
    print("\n[2/4] 拉取现有章节树...")
    try:
        tree = get_resource_tree(ctx)
        print(f"  现有 {len(tree.get('chapter_list', []))} 个章节")
    except (requests.RequestException, RuntimeError) as e:
        logger.exception("拉章节树失败")
        print(f"  [警告] 拉章节树失败: {e}")
        tree = {"chapter_list": []}

    # verify-only:直接返回
    if verify_only:
        print("\n[3/4] verify-only 模式,不写,返回。")
        return _empty_result(ctx, structure.course_title, started_at, mode="verify_only")

    # ─── P3:apply-plan 校验(在 diff 之前) ──
    if apply_plan_path is not None:
        mapping_hash = compute_mapping_hash(structure)
        tree_fingerprint = compute_tree_fingerprint(tree)
        _load_and_verify_plan(
            apply_plan_path,
            course_id=str(ctx.course_id),
            mapping_hash=mapping_hash,
            tree_fingerprint=tree_fingerprint,
            only_chapters=only_chapters,
            only_lessons=only_lessons_p,
            only_resources=only_resources_p,
        )
        print("[apply-plan] 校验通过,继续执行")

    # 计算 diff
    print("\n[3/4] 计算 diff...")
    diff = compute_diff(
        structure, tree,
        only_chapters=only_chapters, prune=prune,
    )
    _print_diff_summary(diff)

    # U1:应用局部目标(非目标全部 SKIP)
    if is_partial:
        print(f"\n  [only-targets] 应用局部目标:lessons={only_lessons_p}, resources={only_resources_p}")
        diff = _mark_only_targets(
            diff, only_lessons=only_lessons_p, only_resources=only_resources_p,
        )
        _print_diff_summary(diff)

    # 增量 resume:从旧 manifest 读已成功的 resource_key,
    # 在 diff 里把它们从 CREATE 转 SKIP,避免重复上传。
    if resume_from_manifest is not None and resume_from_manifest.exists():
        from .report import load_manifest
        prev = load_manifest(resume_from_manifest)
        if prev is not None:
            prev_ok_keys = {
                a.resource_key for a in prev.assets
                if a.status == AssetStatus.OK and a.resource_key
            }
            if prev_ok_keys:
                print(f"\n  [resume] 旧 manifest 有 {len(prev_ok_keys)} 个 OK 资源,跳过匹配项...")
                diff = _mark_resume_keys(diff, prev_ok_keys)
                _print_diff_summary(diff)
            else:
                print(f"\n  [resume] 旧 manifest 无 OK 资源,无需跳过")
        else:
            print(f"\n  [警告] resume manifest 格式不对,忽略: {resume_from_manifest}")
    elif resume_from_manifest is not None:
        print(f"\n  [警告] resume manifest 不存在(首次跑?),忽略: {resume_from_manifest}")

    # 失败重试:只跑指定 resource_key 对应的 leaf,其他 CREATE 全部 SKIP
    # 空集 = 显式"没什么要重试",早返(避免跑空流程还写空 manifest)
    if retry_keys is not None and not retry_keys:
        print(f"\n  [retry] retry_keys 为空(显式无重试目标),早返")
        return _empty_result(ctx, structure.course_title, started_at)

    if retry_keys is not None:
        print(f"\n  [retry] 只重跑 {len(retry_keys)} 个指定 resource_key...")
        diff = _mark_retry_keys(diff, retry_keys)
        _print_diff_summary(diff)

    # reset_confirm:用户显式确认清空重建 — 必须先于 drift 拦截
    # (空课程 drift=100% 也会被 reset_confirm 跳过,允许先备份+清空再重算)
    reset_confirm_match = (
        reset_confirm is not None
        and str(reset_confirm) == str(ctx.course_id)
    )
    reset_confirm_mismatch = (
        reset_confirm is not None
        and str(reset_confirm) != str(ctx.course_id)
    )
    if reset_confirm_mismatch:
        print(f"\n[警告] --reset-confirm {reset_confirm} ≠ 当前课程 {ctx.course_id},忽略")

    if reset_confirm_match:
        print(f"\n[!] --reset-confirm 已传 {reset_confirm},准备 reset_course_tree")
        print("    先备份当前真实树...")
        backup_path = write_backup_snapshot(tree, output_dir, ctx.course_id)
        print(f"    备份: {backup_path}")
        if not dry_run:
            n = reset_course_tree(ctx)
            print(f"    已清空 {n} 个章节")
        # 让 diff 全 CREATE(空树)
        tree = {"chapter_list": []}
        diff = compute_diff(
            structure, tree, only_chapters=only_chapters, prune=False,
        )
        # 局部模式:reset 后重新 mark(全 CREATE 转 SKIP,只剩目标)
        if is_partial:
            diff = _mark_only_targets(
                diff, only_lessons=only_lessons_p, only_resources=only_resources_p,
            )
        print(f"    reset 后 drift = {diff.total_planned()}/"
              f"{diff.total_planned() + diff.stats['skip']} (期望 100% 全 CREATE)")

    # drift 阈值检查:仅在没传 reset_confirm(且不匹配)时拦截
    elif diff.is_too_drifted(DRIFT_THRESHOLD):
        # U2:局部模式只 warn 不阻断(用户明确说"只动这几个",drift 大是预期的)
        if is_partial:
            print(f"\n  ⚠ HIGH_RISK(局部模式):drift = {diff.total_planned()}/"
                  f"{diff.total_planned() + diff.stats.get('skip', 0)} "
                  f">= {DRIFT_THRESHOLD:.0%}")
            print(f"    不会 reset,继续按 --only-lesson/--only-resource 执行")
        else:
            print(f"\n[拒绝] diff 超过 {DRIFT_THRESHOLD:.0%} drift,拒绝继续!")
            print(f"  建议: --reset-confirm {ctx.course_id} 先清空再重建")
            print(f"  或: 修正 mapping,减少 CREATE/RENAME 操作")
            diag_path = output_dir / f"_diff_drifted_{ctx.course_id}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
            diag_path.write_text(
                json.dumps(diff.report(), ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            print(f"  diff 详情已写: {diag_path}")
            return _empty_result(ctx, structure.course_title, started_at)

    # 写 diff 报告(留底)
    diff_report_path = output_dir / f"_upload_diff_{ctx.course_id}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    diff_report_path.write_text(
        json.dumps(diff.report(), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"  diff 报告: {diff_report_path}")

    # 执行
    print("\n[4/4] 执行上传...")
    log_path = output_dir / "_upload_log.csv"
    manifest_path = output_dir / "_upload_manifest.json"
    report_path = output_dir / "_upload_report.json"

    final_assets = _execute_diff(
        ctx, structure, diff, videos_folder, dry_run,
        confirm_rename=confirm_rename,
    )

    for a in final_assets:
        append_log_row(log_path, a, ctx.course_id)

    # 收尾
    result = UploadResult(
        course_id=ctx.course_id,
        course_title=structure.course_title,
        started_at=started_at,
        finished_at=datetime.now().isoformat(timespec="seconds"),
        assets=tuple(final_assets),
    )
    save_manifest(result, manifest_path)
    write_report(result, report_path, structure=structure)
    # 失败重试清单(独立 try,写失败不阻断)
    try:
        retry_path = write_retry_resources(result, output_dir)
        if retry_path:
            print(f"  失败重试清单: {retry_path}")
    except Exception as e:
        logger.warning(f"写 _retry_resources.json 失败: {e}")
    return result
