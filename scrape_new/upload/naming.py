"""
公共命名工具 —— 章节前缀、课时文件名、PPT/英文视频多资源命名。

设计目标:
  1. format_chapter_title(idx, title) — 中英文章节统一加前缀,识别已有前缀避免重复
  2. lesson_filename(lesson_id, lesson_title, role, ...) — 单节内多资源(中文视频/英文视频/PPT/课件)的命名
  3. 所有输出走 sanitize_filename,保证 Windows 合法

复用方:
  - scrape_new.upload.outline.write_outline     写 outline 时加 chapter 前缀
  - scrape_new.upload.mapping.build_mapping     老 doc 路径解析时加 prefix
  - scrape_new.upload.api_uploader               上传时 section / leaf 命名
  - scrape_new.workflows.*                       下载视频/PPT 时落盘文件名

Why not reuse organizer.sanitize ?
  organizer.sanitize 是基于文件后缀的归档工具;这里我们要的是"由 lesson_id+title+role
  生成一致且可预测的文件名",且要和 outline/mapping JSON 字段对齐,所以独立写。
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Optional


# ─── CJK 判定 ─────────────────────────────────────────────────────

# CJK Unified Ideographs 基本平面 + 扩展 A 区,够用(覆盖 99% 课程标题)
_CJK_RE = re.compile(r"[一-鿿㐀-䶿]")


def is_cjk_text(text: str) -> bool:
    """字符串中是否含 CJK 统一汉字(扩展 A 范围)"""
    return bool(_CJK_RE.search(text or ""))


# ─── 已有前缀识别(防止重复加) ──────────────────────────────────

# 中文章前缀:第1章 / 第01章 / 第一章 / 第1章: / 第 1 章 / 1.
_CN_CHAPTER_PREFIX_RE = re.compile(
    r"^\s*"
    r"第\s*[一-鿿\d]{1,4}\s*章"
    r"\s*[:：]?\s*"
)

# 英文章前缀:Chapter 1. / Chapter 1: / Chapter 1 / Chapter One / 1.
_EN_CHAPTER_PREFIX_RE = re.compile(
    r"^\s*"
    r"(?:chapter\s+\d+|chapter\s+[a-zA-Z]+|\d+)"
    r"\.?\s*[:：]?\s*",
    re.IGNORECASE,
)


# ─── 中文/阿拉伯数字互转 ─────────────────────────────────────

_CN_DIGITS = "〇一二三四五六七八九十"

def _cn_ordinal(idx: int) -> str:
    """1 → 一, 2 → 二, ..., 10 → 十, 11 → 十一, 20 → 二十, 99 → 九十九

    课程章数通常 < 100(1-2 位数),够用。更大数字走通用拼接(百/千)。
    """
    if idx <= 0:
        return str(idx)
    if idx <= 10:
        return _CN_DIGITS[idx]
    if idx < 20:
        # 11→十一,12→十二,...,19→十九
        return "十" + _CN_DIGITS[idx - 10]
    if idx < 100:
        tens, ones = divmod(idx, 10)
        s = _CN_DIGITS[tens] + "十"
        if ones:
            s += _CN_DIGITS[ones]
        return s
    # 100+ 不在常见范围,fallback 到阿拉伯数字(比乱写好)
    return str(idx)


# ─── format_chapter_title ───────────────────────────────────────

def format_chapter_title(idx: int, title: str, *, force_language: Optional[str] = None) -> str:
    """为章节标题加前缀。

    中文:idx=1 → "第一章 标题";idx=5 → "第五章 标题";idx=12 → "第十二章 标题"
    英文:idx=1 → "Chapter 1: 标题";idx=5 → "Chapter 5: 标题"

    Args:
        idx: 章序号(1-based)
        title: 原始标题(可能已含"第1章"或"Chapter 1")
        force_language: None=自动判("cn" / "en"),"cn" / "en" 强制

    Returns:
        已规范化带前缀的标题

    Examples:
        >>> format_chapter_title(1, "免疫学基础知识概述")
        '第一章 免疫学基础知识概述'
        >>> format_chapter_title(5, "热力学定律")
        '第五章 热力学定律'
        >>> format_chapter_title(2, "第二章 教学媒体理论基础")  # 已有
        '第二章 教学媒体理论基础'
        >>> format_chapter_title(1, "Foundations of Physical Chemistry")
        'Chapter 1: Foundations of Physical Chemistry'
        >>> format_chapter_title(3, "Chapter 3. Electrochemistry")  # 已有
        'Chapter 3. Electrochemistry'
    """
    if not title:
        title = ""

    # 决定语言
    if force_language == "cn":
        is_cn = True
    elif force_language == "en":
        is_cn = False
    else:
        # 自动判:优先看 title 本身(已有前缀的不影响)
        is_cn = is_cjk_text(title)

    title = title.strip()

    # 已有中文前缀?识别后保留(不再二次加),去掉"第N章:"后的冒号
    if is_cn and _CN_CHAPTER_PREFIX_RE.match(title):
        m = _CN_CHAPTER_PREFIX_RE.match(title)
        prefix_end = m.end()
        rest = title[prefix_end:].lstrip()
        prefix = m.group(0).rstrip(" :：").rstrip()
        if rest:
            return f"{prefix} {rest}"
        return prefix

    # 已有英文前缀?
    if not is_cn and _EN_CHAPTER_PREFIX_RE.match(title):
        m = _EN_CHAPTER_PREFIX_RE.match(title)
        rest = title[m.end():].lstrip()
        # 区分两种格式:
        # 1) "Chapter 1. xxx" / "1. xxx" — 点号是编号,保留
        # 2) "Chapter 1: xxx" — 冒号是分隔符,保留
        # 3) "Chapter 1 xxx" — 无分隔符,补" : "插入到 rest 之前
        matched = m.group(0)
        prefix = matched.rstrip()  # 去掉末尾空白,不切掉点/冒号
        # 检查 prefix 是否以 ":" 或 "." 结尾
        ends_with_sep = bool(re.search(r"[:：.]$", prefix))
        if not ends_with_sep and rest:
            # 没有分隔符但有 rest → 补 ": "
            return f"{prefix}: {rest}"
        if rest:
            return f"{prefix} {rest}"
        return prefix.rstrip()

    # 没有合法前缀,加一个
    if is_cn:
        return f"第{_cn_ordinal(idx)}章 {title}".strip()
    return f"Chapter {idx}: {title}".strip()


# ─── lesson_filename ────────────────────────────────────────────

# role → 文件名后缀标签映射
_ROLE_SUFFIX = {
    "video": None,           # 主视频不加后缀
    "english": "_English",   # 英文视频
    "english_2": "_English_2",
    "english_3": "_English_3",
    "ppt": "_PPT",
    "pdf": "_课件",
    "docx": "_讲义",
    "doc": "_讲义",
    "attachment": "_附件",
}


def lesson_filename(
    lesson_id: str,
    lesson_title: str,
    role: str = "video",
    *,
    index: Optional[int] = None,
    ext: Optional[str] = None,
) -> str:
    """生成单节内某资源的目标文件名(无路径)。

    Args:
        lesson_id: "1.1" / "10.5"
        lesson_title: 节标题,作为文件名主体
        role: 资源角色("video" / "english" / "english_N" / "ppt" / "pdf" / "docx" / "attachment")
        index: 多同 role 资源序号(None = 单文件);english 多个时给 index=2 → "_English_2"
        ext: 文件扩展名(无点号)。video 缺省 .mp4;attachment 缺省按 role 推

    Examples:
        >>> lesson_filename("1.1", "技术")
        '1.1_技术.mp4'
        >>> lesson_filename("1.1", "技术", role="english")
        '1.1_技术_English.mp4'
        >>> lesson_filename("1.1", "技术", role="english", index=2)
        '1.1_技术_English_2.mp4'
        >>> lesson_filename("1.1", "技术", role="ppt", ext="pptx")
        '1.1_技术_PPT.pptx'
        >>> lesson_filename("1.1", "技术", role="pdf", ext="pdf")
        '1.1_技术_课件.pdf'
    """
    # 1) 基础: lesson_id + "_" + title
    safe_id = _safe_segment(lesson_id)
    safe_title = _safe_segment(lesson_title)

    # 2) 加 role 后缀
    if role == "video":
        # 单文件不加序号(主名);多文件从 1 开始("_1", "_2")
        if index is not None and index > 1:
            suffix = f"_{index}"
        else:
            suffix = ""
    elif role.startswith("english"):
        # english / english_N
        suffix = "_English"
        if role != "english" and role.startswith("english_"):
            try:
                suffix += f"_{int(role.split('_', 1)[1])}"
            except (ValueError, IndexError):
                pass
        # index 显式参数覆盖(用于 "english" + index=2 → "_English_2")
        if index is not None and index > 1:
            suffix = f"_English_{index}"
    elif role == "attachment" and index is not None and index > 1:
        suffix = f"_附件_{index}"
    else:
        suffix = _ROLE_SUFFIX.get(role, f"_{role}")
        if not suffix and role not in _ROLE_SUFFIX:
            suffix = f"_{role}"

    # 3) 决定扩展名
    if ext is None:
        if role == "video":
            ext = "mp4"
        elif role.startswith("english"):
            ext = "mp4"
        else:
            ext = "bin"
    ext = ext.lstrip(".").lower()

    name = f"{safe_id}_{safe_title}{suffix}.{ext}"
    # 最终过 sanitize(防止 title 含 \ / 等)
    from ..core.paths import sanitize_filename
    return sanitize_filename(name)


def _safe_segment(text: str) -> str:
    """轻量 sanitize:把换行/非法字符替换掉,保留中文/英文/数字/下划线。

    不在这里过 sanitize_filename 是为了避免 max_length 截断;最终文件名还要再过一次。
    """
    if not text:
        return ""
    s = str(text)
    # 把 Windows 非法字符 / 控制字符换成下划线
    s = re.sub(r'[\\/:*?"<>|\x00-\x1f\x7f]', "_", s)
    s = s.strip(" ._")
    return s


# ─── 多资源命名生成器 ────────────────────────────────────────────

def build_lesson_filenames(
    lesson_id: str,
    lesson_title: str,
    primary_video: Optional[str] = None,
    *,
    english_videos: list[str] | None = None,
    attachments: list[str] | None = None,
) -> dict[str, list[str]]:
    """根据原始素材路径/标志生成 lesson 内每个角色对应的目标文件名。

    Args:
        lesson_id: "1.1"
        lesson_title: "技术"
        primary_video: 主视频文件路径(只取 .stem,生成新文件名)或 None
        english_videos: 英文视频文件路径列表(可空)
        attachments: 其它附件文件路径列表(PPT/PDF/DOCX 等)

    Returns:
        {
          "video_filename": "1.1_技术.mp4"  or None,
          "attachments":   ["1.1_技术_English.mp4", "1.1_技术_PPT.pptx", ...]
        }
    """
    out_video: Optional[str] = None
    out_attachments: list[str] = []

    # 主视频
    if primary_video:
        out_video = lesson_filename(lesson_id, lesson_title, role="video", ext="mp4")

    # 英文视频:1 个 → "_English",N 个 → "_English_2" 起
    english_videos = english_videos or []
    for i, _path in enumerate(english_videos, start=1):
        if i == 1:
            out_attachments.append(lesson_filename(lesson_id, lesson_title, role="english", ext="mp4"))
        else:
            out_attachments.append(
                lesson_filename(lesson_id, lesson_title, role="english", index=i, ext="mp4")
            )

    # 其它附件:按扩展名决定 role
    attachments = attachments or []
    for i, path in enumerate(attachments, start=1):
        p = Path(path)
        ext = p.suffix.lstrip(".").lower()
        role = _EXT_TO_ROLE.get(ext, "attachment")
        out_attachments.append(
            lesson_filename(lesson_id, lesson_title, role=role, index=i if i > 1 else None, ext=ext)
        )

    return {"video_filename": out_video, "attachments": out_attachments}


# 扩展名 → role 映射
_EXT_TO_ROLE = {
    "mp4": "video",
    "flv": "video",
    "pptx": "ppt",
    "ppt": "ppt",
    "pdf": "pdf",
    "docx": "docx",
    "doc": "doc",
}


# ─── leaf 显示名(给 create_video_leaf / create_attachment_leaf 用) ──

def lesson_leaf_name(
    lesson_id: str,
    lesson_title: str,
    role: str = "video",
    index: Optional[int] = None,
) -> str:
    """生成 leaf 的 name(用 | 分隔,例如 "1.1 技术 | English")。

    比 lesson_filename 更可读,用于后台展示。
    """
    base = f"{lesson_id} {lesson_title}".strip()
    if role == "video":
        return base
    if role.startswith("english"):
        if index is not None and index > 1:
            return f"{base} | English_{index}"
        return f"{base} | English"
    if role == "ppt":
        return f"{base} | PPT"
    if role == "pdf":
        return f"{base} | 课件"
    if role in ("docx", "doc"):
        return f"{base} | 讲义"
    if role == "attachment":
        return f"{base} | 附件" + (f"_{index}" if index and index > 1 else "")
    return base
