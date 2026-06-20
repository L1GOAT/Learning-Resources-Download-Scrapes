"""
统一的章节目录格式 — workflow 下载时输出 + uploader 建课时读取

JSON Schema:
{
  "source_url":   "https://...",          // 原始课程页面
  "platform":     "chaoxing|xuetangx|zhihuishu|icourse163",
  "course_title": "...",                  // 课程名
  "generated_at": "2026-06-10T22:30:00",
  "chapters": [
    {
      "index":  1,
      "title":  "第一章 概述",            // write_outline 自动加前缀
      "lessons": [
        {
          "id":             "1.1",
          "title":          "课程介绍",
          "video_filename": "01_课程介绍.mp4",  // null = 未下载
          "content_type":   "video",            // video|text|attachment|quiz|other
          "attachments":    [                   // 可选,同一课时其它资源
            "1.1_课程介绍_English.mp4",
            "1.1_课程介绍_PPT.pptx"
          ],
          "platform_meta":  {                   // 可选,平台原始 ID,排错用
            "objectid": "...",
            "knowledge_id": "...",
            ...
          }
        }
      ]
    }
  ]
}

设计:
  - 输出文件名固定 `_chapter_outline.json`(下划线前缀 = 元数据,跟 _download_log.csv 风格统一)
  - 写在视频文件夹根(跟 _download_log.csv 同级,所有 _mapping.json / _upload_*.json 同级)
  - uploader 读到这个文件 → 直接构造 CourseStructure,跳过启发式标题匹配
  - 字段缺失时(老 workflow 没产出)优雅降级:platform 给 "unknown",course_title 用文件夹名
  - chapter.title 由 write_outline / read_outline 走 naming.format_chapter_title 规范化
    (中文→"第一章 N xxx" / 英文→"Chapter N: xxx"),已有合法前缀不重复加
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any

from .models import (
    Chapter,
    ContentType,
    CourseStructure,
    Lesson,
    MatchConfidence,
)
from .naming import format_chapter_title

logger = logging.getLogger(__name__)

# 统一文件名,所有 workflow 和 uploader 都用这个常量
OUTLINE_FILENAME = "_chapter_outline.json"


# ─── 写:workflow 下载完成后调用 ──────────────────────────────────

def write_outline(
    out_dir: Path,
    chapters: list[dict[str, Any]],
    source_url: str = "",
    platform: str = "unknown",
    course_title: str = "",
) -> Path:
    """把章节结构写到 <out_dir>/_chapter_outline.json。

    Args:
        out_dir: 输出目录(视频文件夹根)
        chapters: 章节列表,每章格式:
            {
              "index": 1,
              "title": "第一章 ...",
              "lessons": [
                {"id": "1.1", "title": "...", "video_filename": "01_xxx.mp4",
                 "content_type": "video", "platform_meta": {...}},
                ...
              ]
            }
        source_url: 课程页面 URL(溯源用)
        platform: 平台标识(chaoxing/xuetangx/zhihuishu/icourse163)
        course_title: 课程名

    Returns:
        写入的文件路径
    """
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / OUTLINE_FILENAME

    # 规范化章名:每个 chapter 都过 format_chapter_title(已有前缀的不会重复加)
    normalized_chapters: list[dict[str, Any]] = []
    for ch in chapters:
        idx = ch.get("index", 0)
        title = ch.get("title", "") or f"第 {idx} 章"
        normalized_chapters.append({**ch, "title": format_chapter_title(idx, title)})

    payload = {
        "source_url": source_url,
        "platform": platform,
        "course_title": course_title,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "chapters": normalized_chapters,
    }
    out_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    logger.info(f"已写章节目录: {out_path}")
    return out_path


# ─── 读:uploader build-mapping 时调用 ────────────────────────────

def is_outline_file(path: Path) -> bool:
    """判断给定路径是不是章节目录 JSON 文件(后缀 .json + 含 chapters 字段)"""
    path = Path(path)
    if path.suffix.lower() != ".json":
        return False
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return isinstance(data, dict) and "chapters" in data
    except (OSError, json.JSONDecodeError):
        return False


def read_outline(path: Path) -> tuple[list[Chapter], dict[str, Any]]:
    """读 _chapter_outline.json,返回 (chapter 列表, 元数据 dict)。

    元数据 dict 含:source_url / platform / course_title / generated_at
    """
    path = Path(path)
    data = json.loads(path.read_text(encoding="utf-8"))
    if "chapters" not in data:
        raise ValueError(f"不是合法的章节目录文件(缺 'chapters' 字段): {path}")

    chapters: list[Chapter] = []
    for ch in data["chapters"]:
        lessons: list[Lesson] = []
        for ls in ch.get("lessons", []):
            content_type = ContentType(ls.get("content_type", "video"))
            video_filename = ls.get("video_filename") or None
            attachments = tuple(ls.get("attachments", []) or [])
            # 有视频文件名 → match_confidence 标 MANUAL(workflow 已保证对得上)
            # 没视频 → match_confidence = NONE
            mc = MatchConfidence.MANUAL if video_filename else MatchConfidence.NONE
            lessons.append(Lesson(
                id=ls["id"],
                title=ls["title"],
                content_type=content_type,
                video=video_filename,
                attachments=attachments,
                match_confidence=mc,
                note=ls.get("note"),
            ))
        # 章名规范化(防御性:write_outline 已加过,这里二次保险)
        ch_idx = ch["index"]
        ch_title = format_chapter_title(ch_idx, ch.get("title", ""))
        chapters.append(Chapter(
            index=ch_idx,
            title=ch_title,
            lessons=tuple(lessons),
        ))

    meta = {
        "source_url": data.get("source_url", ""),
        "platform": data.get("platform", "unknown"),
        "course_title": data.get("course_title", ""),
        "generated_at": data.get("generated_at", ""),
    }
    return chapters, meta


def build_structure_from_outline(
    outline_path: Path,
    course_id: str = "",
    course_title_override: str = "",
) -> CourseStructure:
    """从 outline 文件直接构造 CourseStructure(跳过启发式匹配)。"""
    chapters, meta = read_outline(outline_path)
    return CourseStructure(
        course_id=course_id,
        course_title=course_title_override or meta.get("course_title", ""),
        chapters=tuple(chapters),
        source_doc=str(outline_path),
        generated_at=meta.get("generated_at") or datetime.now().isoformat(timespec="seconds"),
    )


# ─── workflow 辅助:从扁平视频列表构造 chapter 结构 ──────────────

def videos_to_outline_chapters(
    videos: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """把 workflow 下载时产生的扁平视频列表组装成 outline 章节结构。

    videos 每项至少含:
      - ch_num: int      章序号
      - chapter: str     章名(原始,带"第N章"前缀也可以)
      - lesson: str      节名
      - filename: str    实际下载的文件名
      - (可选) ch_id, lesson_id, platform_meta
      - (可选) extra_filenames: list[str]  同一课时的额外资源(英文视频/PPT 等)

    返回章节结构(可直接喂给 write_outline)。
    章名由 write_outline / read_outline 统一规范化,这里只传原始值。
    """
    by_chapter: dict[int, dict[str, Any]] = {}

    for v in videos:
        ch_num = v.get("ch_num", 0)
        ch_title = v.get("chapter", f"第 {ch_num} 章")
        lesson_title = v.get("lesson", "")
        filename = v.get("filename", "")
        extras = v.get("extra_filenames", []) or []

        if ch_num not in by_chapter:
            by_chapter[ch_num] = {
                "index": ch_num,
                "title": ch_title,
                "lessons": [],
            }

        lesson_index = len(by_chapter[ch_num]["lessons"]) + 1
        lesson: dict[str, Any] = {
            "id": f"{ch_num}.{lesson_index}",
            "title": lesson_title,
            "video_filename": filename or None,
            "content_type": "video",
        }
        if extras:
            lesson["attachments"] = list(extras)
        if v.get("platform_meta"):
            lesson["platform_meta"] = v["platform_meta"]
        by_chapter[ch_num]["lessons"].append(lesson)

    # 按章序号排序
    return [by_chapter[k] for k in sorted(by_chapter.keys())]
