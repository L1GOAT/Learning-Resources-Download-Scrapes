"""
子流程 A：把"章节目录文档 + 视频文件夹"变成 _mapping.json

步骤：
  1. parse_chapter_doc(doc_path) -> list[Chapter]
       嗅探 .doc 实际是 HTML 还是 OLE 二进制，分别处理
  2. scan_video_folder(folder) -> list[VideoFile]
       扫视频后缀，按 get_file_index() 排序
  3. match_videos_to_lessons(chapters, videos) -> CourseStructure
       标题匹配：先去标点+空格，再比 "包含" 和 "相等"
  4. write_mapping(structure, out_path)
       写 _mapping.json，给用户核对

设计：
  - 全程纯函数 + 路径操作，没有网络
  - 中文标题匹配时去掉【】：（）等装饰符号和标点
  - 匹配不上不报错，留 video=None，让 mapping.json 显示给用户修
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from datetime import datetime
from dataclasses import replace
from pathlib import Path
  # unified import
from typing import Iterable, Optional

from .models import (
    Chapter,
    ContentType,
    CourseStructure,
    Lesson,
    MatchConfidence,
)
from .naming import format_chapter_title
from ..services.organizer import get_file_index, is_hash_name

logger = logging.getLogger(__name__)

# 视频后缀
VIDEO_EXTS = {".mp4", ".flv", ".avi", ".wmv", ".mov", ".mkv", ".webm", ".mpg", ".mpeg"}


# ─── 1. 解析章节文档 ─────────────────────────────────────────────

def _is_html_file(path: Path) -> bool:
    """嗅探文件前几个字节，判断是不是 HTML（很多 .doc 实际是 HTML 改的扩展名）"""
    try:
        with open(path, "rb") as f:
            head = f.read(512).lstrip()
        # HTML 通常以 <!DOCTYPE、<html 或 <?xml 开头
        return head.startswith((b"<!DOCTYPE", b"<html", b"<?xml", b"<HTML"))
    except OSError:
        return False


def _is_ole2_file(path: Path) -> bool:
    """OLE2 复合文档特征：开头 8 字节是 d0cf11e0a1b11ae1"""
    try:
        with open(path, "rb") as f:
            head = f.read(8)
        return head == b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
    except OSError:
        return False


def _extract_text_blobs(path: Path) -> str:
    """从文件中抽取所有"看起来像中文文本"的内容并拼起来。
    兼容：
      - 纯 HTML 文件（博弈论的章节目录是这种）
      - OLE2 包装的 Word 文档，但 WordDocument 流里塞的是 HTML 文本（免疫学的章节目录）
      - 纯文本/Markdown
    """
    raw = path.read_bytes()
    # 先看是不是 OLE2
    if raw[:8] == b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1":
        try:
            import olefile
            ole = olefile.OleFileIO(path)
            # 把 WordDocument 流当成 UTF-8 文本读
            word_stream = ole.openstream("WordDocument").read()
            try:
                text = word_stream.decode("utf-8", errors="ignore")
            except Exception:
                text = ""
            ole.close()
        except Exception as e:
            logger.warning(f"OLE2 解析失败: {e}")
            text = ""
    else:
        try:
            text = raw.decode("utf-8", errors="ignore")
        except Exception:
            text = ""

    # 去掉 HTML 标签和实体，拿到纯文本
    text = re.sub(r"<style[^>]*>.*?</style>", " ", text, flags=re.DOTALL)
    text = re.sub(r"<script[^>]*>.*?</script>", " ", text, flags=re.DOTALL)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"&nbsp;", " ", text)
    text = re.sub(r"&[a-zA-Z]+;", " ", text)
    # 把连续空白压成单个空格（但保留单换行供调试）
    text = re.sub(r"[ \t\f\v]+", " ", text)
    text = re.sub(r"\n+", "\n", text)
    return text


def _parse_chapter_text(text: str) -> list[Chapter]:
    """从纯文本中提取章节结构。

    文本格式形如（不一定每条占一行，可能是空格分隔的连续条目）：
      1 免疫学基础知识概述：免疫系统的组成和作用
      1.1 【预习要求】
      1.2 【目的要求】
      1.3 影响健康的因素
      ...
      2 免疫学基础知识概述：免疫学的发展史和在医学中的作用
      ...
    """
    chapters: list[Chapter] = []
    current: Optional[Chapter] = None

    # 关键修改：不用 ^ $，让 pattern 在去标签后的纯文本里匹配所有"序号 空格 标题"
    # 章序号: 1-2 位数字
    # 课时序号: 数字.数字(.数字)? 形式
    pattern = re.compile(
        r"(?<![.\d])"           # 前面不是数字/点（避免误匹配日期等）
        r"(\d{1,2}(?:\.\d{1,2})?(?:\.\d+)?)"  # 序号
        r"\s+"                    # 至少一个空格
        r"([^\d][^0-9\n]{0,80}?)"  # 标题：以非数字开头，长度 0-80
        r"(?=\s+\d{1,2}(?:\.\d{1,2})?(?:\.\d+)?\s+[^\d]|\s*$)",  # 后面是下一个序号或结束
    )

    for m in pattern.finditer(text):
        seq = m.group(1).strip()
        title = m.group(2).strip()
        if not title:
            continue
        if "." not in seq:
            # 章:规范化(自动加中文/英文前缀,已有则不重复)
            normalized = format_chapter_title(int(seq), title)
            current = Chapter(index=int(seq), title=normalized, lessons=())
            chapters.append(current)
        else:
            # 课时
            if current is None:
                continue
            ct = _classify_lesson_title(title)
            lesson = Lesson(id=seq, title=title, content_type=ct)
            new_lessons = current.lessons + (lesson,)
            current = replace_chapter(current, new_lessons)
            chapters[-1] = current

    return chapters


def _parse_chapter_html(path: Path) -> list[Chapter]:
    """统一入口：嗅探格式后交给 _parse_chapter_text"""
    text = _extract_text_blobs(path)
    if not text.strip():
        logger.warning(f"从 {path} 没抽出任何文本")
        return []
    return _parse_chapter_text(text)


def _classify_lesson_title(title: str) -> ContentType:
    """根据课时标题猜测内容类型（启发式）

    真实样本里看到的非视频标题：
      【预习要求】【目的要求】思考题 章节测验/测试 课件 相关知识链接
      知识拓展 复习题 中国免疫学会 ... (链接/纯文本)
    """
    t = title.strip()
    # 结构性非视频项
    structural_markers = [
        "【预习要求】", "【目的要求】",
        "思考题", "章节测验", "章节测试", "章节练习",
        "课件", "相关知识链接", "知识拓展",
        "复习题",
        "公开课",  # 11.3.2 麻省理工学院公开课
    ]
    for marker in structural_markers:
        if marker in t:
            return ContentType.OTHER
    # 默认是视频（v1 唯一支持的类型）
    return ContentType.VIDEO


def replace_chapter(chapter: Chapter, new_lessons: tuple[Lesson, ...]) -> Chapter:
    """Chapter 是 frozen 的，替换 lessons 用这个辅助函数"""
    from dataclasses import replace
    return replace(chapter, lessons=new_lessons)


def parse_chapter_doc(doc_path: str | Path) -> list[Chapter]:
    """解析章节文档,返回 Chapter 列表。

    支持:
      - **_chapter_outline.json**(workflow 下载时输出,直接读,不做启发式)← 推荐
      - 纯 HTML(博弈论的章节目录是这种)
      - OLE2 包装但流里是 HTML 文本(免疫学的章节目录是这种)
      - 纯文本 / Markdown
    """
    doc_path = Path(doc_path)
    if not doc_path.exists():
        raise FileNotFoundError(f"章节文档不存在: {doc_path}")

    # 优先识别 _chapter_outline.json
    from .outline import is_outline_file, read_outline
    if is_outline_file(doc_path):
        logger.info("检测到 _chapter_outline.json,直接读")
        chapters, _meta = read_outline(doc_path)
        return chapters

    if _is_html_file(doc_path):
        logger.info("检测到纯 HTML 格式")
    elif _is_ole2_file(doc_path):
        logger.info("检测到 OLE2 格式(Word 文档容器)")
    else:
        logger.info("未知格式,尝试按通用文本解析")
    return _parse_chapter_html(doc_path)


def _parse_chapter_plain(doc_path: Path) -> list[Chapter]:
    """已废弃：保留作 fallback。
    实际解析统一走 _parse_chapter_text"""
    text = doc_path.read_text(encoding="utf-8", errors="ignore")
    return _parse_chapter_text(text)


# ─── 2. 扫视频文件夹 ────────────────────────────────────────────

@dataclass
class VideoFile:
    """扫到的单个视频"""
    filename: str           # "01_影响健康的因素.mp4"
    path: Path
    index: Optional[int]    # 01
    title: str              # "影响健康的因素"
    bytes: int
    is_hash: bool = False   # 文件名是哈希吗


def scan_video_folder(folder: str | Path) -> list[VideoFile]:
    """扫视频文件夹，返回 VideoFile 列表（按 index 升序）"""
    folder = Path(folder)
    if not folder.is_dir():
        raise NotADirectoryError(f"视频文件夹不存在: {folder}")

    videos: list[VideoFile] = []
    for entry in sorted(folder.iterdir()):
        if not entry.is_file():
            continue
        if entry.suffix.lower() not in VIDEO_EXTS:
            continue
        idx = get_file_index(entry.name)
        title = _strip_index_prefix(entry.name)
        videos.append(
            VideoFile(
                filename=entry.name,
                path=entry,
                index=idx,
                title=title,
                bytes=entry.stat().st_size,
                is_hash=is_hash_name(entry.name),
            )
        )
    # 按 index 升序，None 的排最后
    videos.sort(key=lambda v: (v.index is None, v.index or 0))
    return videos


def _strip_index_prefix(filename: str) -> str:
    """从 "01_影响健康的因素.mp4" 提取 "影响健康的因素" """
    stem = Path(filename).stem
    m = re.match(r"^\d{1,4}_(.+)$", stem)
    return m.group(1) if m else stem


# ─── 3. 视频 → 课时 匹配 ────────────────────────────────────────

# 匹配时去掉这些装饰符号（来自真实文件：免疫学 04_免疫系统的三大功能：防御）
_MATCH_PUNCT_RE = re.compile(r"[【】\[\]（）：:=\s]")
# 全角→半角 + 大小写归一（让 "MHC-I" 和 "mhci" 都能匹配）
_NORMALIZE_RE = re.compile(r"[！-～]")  # 全角 ASCII 范围


def _normalize_for_match(text: str) -> str:
    """归一化字符串用于匹配：去装饰符、全角转半角、转小写"""
    text = _NORMALIZE_RE.sub(lambda m: chr(ord(m.group()) - 0xFEE0), text)
    text = _MATCH_PUNCT_RE.sub("", text)
    return text.lower()


def match_videos_to_lessons(
    chapters: list[Chapter],
    videos: list[VideoFile],
) -> CourseStructure:
    """自动匹配：把每个视频按归一化后的标题，匹配到某个课时。
    匹配规则（按优先级）：
      1. 归一化后完全相等 → exact
      2. 课时标题归一化后包含视频标题（或反过来）→ contains
      3. 没匹配上 → video=None
    返回的 CourseStructure.chapters 里，lessons 字段被填充。
    """
    # 预归一化
    norm_videos = [(_normalize_for_match(v.title), v) for v in videos]

    new_chapters: list[Chapter] = []
    matched_video_ids: set[int] = set()  # 防止一个视频匹配多个课时

    for ch in chapters:
        new_lessons: list[Lesson] = []
        for ls in ch.lessons:
            if ls.content_type != ContentType.VIDEO:
                new_lessons.append(ls)
                continue
            norm_lesson = _normalize_for_match(ls.title)
            best_match: Optional[tuple[VideoFile, MatchConfidence]] = None

            for vidx, (norm_video, v) in enumerate(norm_videos):
                if vidx in matched_video_ids:
                    continue
                if not v.index:  # 没序号的视频不参与匹配
                    continue
                if norm_lesson == norm_video:
                    best_match = (v, MatchConfidence.EXACT)
                    break
                if (
                    best_match is None
                    and norm_lesson
                    and norm_video
                    and (norm_lesson in norm_video or norm_video in norm_lesson)
                ):
                    best_match = (v, MatchConfidence.CONTAINS)

            if best_match:
                matched_video_ids.add(
                    norm_videos.index((_normalize_for_match(best_match[0].title), best_match[0]))
                )
                
                new_lessons.append(replace(
                    ls,
                    video=best_match[0].filename,
                    match_confidence=best_match[1],
                ))
            else:
                # 视频缺失，留 video=None
                if not ls.video:
                    new_lessons.append(ls)
                else:
                    new_lessons.append(ls)

        new_chapters.append(replace_chapter(ch, tuple(new_lessons)))

    # 统计
    matched_count = len(matched_video_ids)
    unmatched_videos = [v for v in videos if v.index not in {
        v2.index for v2 in [norm_videos[i][1] for i in matched_video_ids]
    }]
    logger.info(
        f"匹配结果: {matched_count}/{len(videos)} 个视频已映射，"
        f"{len(unmatched_videos)} 个孤儿视频未匹配"
    )
    if unmatched_videos:
        for uv in unmatched_videos:
            logger.info(f"  孤儿: {uv.filename}")

    return CourseStructure(
        course_id="",
        course_title="",
        chapters=tuple(new_chapters),
        source_doc=None,
        generated_at=datetime.now().isoformat(timespec="seconds"),
    )


# ─── 4. 写 _mapping.json ────────────────────────────────────────

def write_mapping(structure: CourseStructure, out_path: str | Path) -> Path:
    """把 CourseStructure 写成 _mapping.json"""
    out_path = Path(out_path)
    from .models import to_json
    out_path.write_text(to_json(structure), encoding="utf-8")
    logger.info(f"已生成 {out_path}")
    return out_path


# ─── 一站式入口 ─────────────────────────────────────────────────

def build_mapping(
    videos_folder: str | Path,
    doc_path: str | Path,
    course_id: str = "",
    course_title: str = "",
) -> CourseStructure:
    """一站式:扫视频 + 解析文档 + 自动匹配 → 返回 CourseStructure(不写盘)。

    如果 doc_path 指向 `_chapter_outline.json`(workflow 下载时输出),
    跳过启发式匹配,直接用 outline 里已经对好的 video_filename。
    """
    # 如果 doc 是 outline,走快路径
    from .outline import is_outline_file, build_structure_from_outline
    if is_outline_file(Path(doc_path)):
        logger.info("doc 是 _chapter_outline.json,跳过启发式匹配")
        structure = build_structure_from_outline(
            outline_path=Path(doc_path),
            course_id=course_id,
            course_title_override=course_title,
        )
        return structure

    # 普通老师 .doc 路径(老流程)
    chapters = parse_chapter_doc(doc_path)
    logger.info(f"解析到 {len(chapters)} 个章节,"
                f"{sum(len(c.lessons) for c in chapters)} 个课时")
    videos = scan_video_folder(videos_folder)
    logger.info(f"扫到 {len(videos)} 个视频文件")
    structure = match_videos_to_lessons(chapters, videos)
    # 填充元数据
    
    structure = replace(
        structure,
        course_id=course_id or structure.course_id,
        course_title=course_title or structure.course_title,
        source_doc=str(doc_path),
    )
    return structure
