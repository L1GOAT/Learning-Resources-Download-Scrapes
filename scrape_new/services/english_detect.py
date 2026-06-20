"""
智能识别英文视频 / PPT / 讲义 — 不只靠 tab_num。

输入:某节课程下的所有视频/文档 + tab_num + 课程名
输出:每个资源 role("video" / "english" / "ppt" / "pdf" / "docx" / "doc" / "attachment")

判定顺序(命中即返回,后跳过):
  1. 文件名后缀 → 默认角色
  2. 文件名/标题关键字(English / 英文 / en / EN)
  3. 同节多个 mp4 标题英文比例(>= 50% 判 English)
  4. tab_num 兜底(2 → English)
  5. 仍不确定 → "video" 兜底

为什么独立出来:
  - 不同课程 tab_num 含义可能不同(有的 1=PPT,有的 1=English)
  - 用户手动重命名过的文件可能拿不到原标题
  - 旧 workflow 硬编码 tab_num=2 → English 容易误判
"""

from __future__ import annotations

import re
from typing import Optional


# 扩展名 → 默认 role
_EXT_TO_ROLE = {
    "mp4": "video", "flv": "video", "avi": "video", "mkv": "video", "mov": "video",
    "pptx": "ppt", "ppt": "ppt",
    "pdf": "pdf",
    "docx": "docx", "doc": "doc",
    "jpg": "image", "jpeg": "image", "png": "image",
}


# 标题/文件名里含英文关键字 → English 视频
# 匹配模式:独立单词或前后空格,避免误中 "england" 这类
_ENGLISH_KEYWORDS = re.compile(
    r"(?:^|[\s_\-\.\(\)\[\]])"
    r"(english|english\s*version|英\s*文|英文版?|english\s*audio)"
    r"(?:$|[\s_\-\.\(\)\[\]])",
    re.IGNORECASE,
)


def _role_from_ext(filename: str) -> str:
    """扩展名 → 默认 role"""
    import os
    ext = os.path.splitext(filename)[1].lstrip(".").lower()
    return _EXT_TO_ROLE.get(ext, "attachment")


def _is_english_text(text: str) -> bool:
    """文本里含 English 关键字"""
    return bool(text and _ENGLISH_KEYWORDS.search(text))


def detect_role(
    *,
    filename: str = "",
    title: str = "",
    tab_num: Optional[int] = None,
    same_lesson_videos: Optional[list[dict]] = None,
) -> str:
    """根据 filename / title / tab_num / 同节其他视频判定 role。

    Args:
        filename: 文件名(可空,空时按 title+tab_num 判定)
        title: 原始标题(可能 None)
        tab_num: cards API tab 编号(可能 None)
        same_lesson_videos: 同节其它 video 列表(每项至少含 name/title/tab_num),
                           用于"标题英文比例"判断
    Returns:
        role: "video" / "english" / "ppt" / "pdf" / "docx" / "doc" / "image" / "attachment"
    """
    # 文件名为空时,先看 title 后缀(防止"课1.1 视频" 这种没扩展名的)
    if not filename and title:
        filename = title
    base_role = _role_from_ext(filename)

    # 兜底场景:filename 和 title 都没扩展名(如"课1.1 视频"或纯 lesson 名)
    # 这种情况按"上下文信号"判定:tab_num==2 → English,否则 → video
    if base_role == "attachment" and not _has_extension(filename):
        # 1) tab_num 兜底
        if tab_num == 2:
            return "english"
        # 2) title 含 English 关键字
        if _is_english_text(title):
            return "english"
        # 3) 默认 → video(本次只是 lesson 名,主视频)
        return "video"

    # 非视频不判定英文(PPTPDF 都是文档)
    if base_role != "video":
        return base_role

    # 1) 文件名含 English 关键字 → English
    if _is_english_text(filename):
        return "english"

    # 2) 标题含 English 关键字
    if _is_english_text(title):
        return "english"

    # 3) 同节多个 mp4,标题英文比例 >= 50% → 这条也 English
    if same_lesson_videos:
        en_count = 0
        total = 0
        for v in same_lesson_videos:
            t = v.get("title") or v.get("name") or ""
            f = v.get("filename") or v.get("name") or ""
            if _role_from_ext(f) == "video" or (
                not _has_extension(f) and f
            ):
                total += 1
                if _is_english_text(t) or _is_english_text(f):
                    en_count += 1
        if total >= 2 and en_count == total:
            # 多个视频且 100% 都含 English 关键字 → 当前也是 English
            return "english"

    # 4) tab_num 兜底(用户硬编码场景)
    if tab_num == 2:
        return "english"

    # 5) 兜底
    return "video"


def _has_extension(text: str) -> bool:
    """文本里是否含扩展名(.mp4 / .pptx 等)"""
    import os
    _, ext = os.path.splitext(text or "")
    return bool(ext)


def classify_lesson_videos(
    lesson_title: str,
    videos: list[dict],
) -> list[dict]:
    """对一节课下的多个视频一次性判定 role,每个 video dict 加 role 字段。

    Args:
        lesson_title: 节标题(用作 fallback)
        videos: 视频列表,每项至少含 name/filename/title/tab_num

    Returns:
        同一个 videos 列表(原地修改),每个加 role 字段
    """
    for v in videos:
        if "role" in v:
            continue  # 已分类
        v["role"] = detect_role(
            filename=v.get("filename") or v.get("name") or "",
            title=v.get("title") or v.get("name") or "",
            tab_num=v.get("tab_num"),
            same_lesson_videos=videos,
        )
    return videos


def classify_lesson_docs(
    docs: list[dict],
) -> list[dict]:
    """对一组文档判定 role(只按扩展名,文档无英文/中文之分)。

    Args:
        docs: 文档列表,每项至少含 filename/name
    """
    for d in docs:
        if "role" in d:
            continue
        d["role"] = detect_role(
            filename=d.get("filename") or d.get("name") or "",
            title=d.get("title") or d.get("name") or "",
        )
    return docs
