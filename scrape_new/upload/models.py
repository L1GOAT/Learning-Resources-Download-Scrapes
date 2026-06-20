"""
数据模型 - 不可变 dataclass

CourseStructure: 整个课程的章/节结构，由子流程 A 从文档+视频生成
Asset: 单个资源（视频/文字/附件/测验）的上传状态，由子流程 B 写入
UploadResult: 一整次上传的最终结果，喂给 report.py 写报告

不可变原则：每次修改都返回新对象（用 dataclasses.replace），不原地改字段。
这样多个阶段（mapping / dry-run / upload）共享同一组对象时不会出现意外状态。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field, asdict, replace
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Optional


# ─── 枚举 ────────────────────────────────────────────────────────

class ContentType(str, Enum):
    """节的内容类型，决定在后台用哪个组件"""
    VIDEO = "video"          # 视频课时
    TEXT = "text"            # 文字描述/讲义
    ATTACHMENT = "attachment"  # PDF/Word 附件
    QUIZ = "quiz"            # 测验/讨论题
    OTHER = "other"          # 其它（课件、知识拓展等，v1 跳过）


class AssetStatus(str, Enum):
    """单个资源的上传状态"""
    PENDING = "pending"      # 还没处理
    OK = "ok"                # 上传成功
    FAILED = "failed"        # 上传失败
    SKIPPED = "skipped"      # 跳过（已存在、缺素材、内容类型不在 v1 范围）
    SUSPICIOUS = "suspicious"  # 上传了但验证可疑


class MatchConfidence(str, Enum):
    """自动标题匹配的置信度"""
    EXACT = "exact"          # 完全相等
    CONTAINS = "contains"    # 包含关系
    MANUAL = "manual"        # 人工指定
    NONE = "none"            # 没匹配上


# ─── 核心模型 ────────────────────────────────────────────────────

@dataclass(frozen=True)
class Lesson:
    """一节课（在后台称为"课时"或"小节"）"""
    id: str                                    # "1.3" / "10.5" / "11.2"
    title: str                                 # "影响健康的因素"
    content_type: ContentType                  # 视频/文字/附件/测验
    video: Optional[str] = None                # 视频文件名（相对视频文件夹），None = 无视频
    description: Optional[str] = None          # 文字描述内容（v1 未用）
    attachments: tuple[str, ...] = ()          # 附件路径列表（v1 未用）
    quiz: Optional[str] = None                 # 测验定义文件路径（v1 未用）
    match_confidence: MatchConfidence = MatchConfidence.NONE
    note: Optional[str] = None                 # 备注，比如"本节视频缺失"


@dataclass(frozen=True)
class Chapter:
    """一章（后台称为"章"或"模块"）"""
    index: int                  # 1-based
    title: str                  # "免疫学基础知识概述：..."
    lessons: tuple[Lesson, ...] = ()


@dataclass(frozen=True)
class CourseStructure:
    """整个课程的结构"""
    course_id: str
    course_title: str
    chapters: tuple[Chapter, ...] = ()
    source_doc: Optional[str] = None
    generated_at: Optional[str] = None  # ISO 8601

    def lessons_with_video(self) -> list[tuple[Chapter, Lesson]]:
        """返回所有 (章, 节) 中 content_type=VIDEO 且 video 非空的项"""
        result = []
        for ch in self.chapters:
            for ls in ch.lessons:
                if ls.content_type == ContentType.VIDEO and ls.video:
                    result.append((ch, ls))
        return result

    def missing_video_lessons(self) -> list[tuple[Chapter, Lesson]]:
        """返回标记为视频但 video=None 的节（章10 这种）"""
        result = []
        for ch in self.chapters:
            for ls in ch.lessons:
                if ls.content_type == ContentType.VIDEO and not ls.video:
                    result.append((ch, ls))
        return result


# ─── 上传状态模型 ────────────────────────────────────────────────

@dataclass(frozen=True)
class Asset:
    """单个资源的上传状态（子流程 B 写入，喂给 report.py）"""
    chapter_index: int
    lesson_id: str
    lesson_title: str
    content_type: ContentType
    source_path: Optional[str]              # 本地源文件路径
    target_url: Optional[str] = None        # 后台返回的资源 URL（成功后填）
    status: AssetStatus = AssetStatus.PENDING
    attempts: int = 0
    bytes_uploaded: int = 0
    error: Optional[str] = None
    uploaded_at: Optional[str] = None       # ISO 8601
    # 稳定资源 key(用于增量 resume / 跨运行跳过 / 失败重试)
    # 由 _build_plan_assets / _execute_diff 在产出时写入
    # resume 时从旧 manifest 读出来匹配
    resource_key: str = ""

    def with_status(self, status: AssetStatus, **kwargs) -> "Asset":
        """返回新对象，status 和其它字段被更新（不可变原则）"""
        return replace(self, status=status, **kwargs)


@dataclass(frozen=True)
class UploadResult:
    """一次完整上传的结果"""
    course_id: str
    course_title: str
    started_at: str                                   # ISO 8601
    finished_at: Optional[str] = None
    assets: tuple[Asset, ...] = ()
    # 运行模式(print_summary 用来分流文案,避免 plan-only 误显示"上传完成")
    # - "upload":正常上传(默认)
    # - "plan_only":只生成 plan,没实际上传
    # - "plan_first":默认 plan-first,自动写 plan 后停
    # - "apply_plan":加载了 plan 校验通过后执行
    # - "verify_only" / "dry_run":只读验证,无上传
    mode: str = "upload"

    def count_by_status(self) -> dict[str, int]:
        """按 status 统计数量"""
        counts = {s.value: 0 for s in AssetStatus}
        for a in self.assets:
            counts[a.status.value] += 1
        return counts

    def delta(self) -> int:
        """discovered - (ok + failed + skipped + suspicious)，
        按项目规则 '差额必须为 0' """
        counts = self.count_by_status()
        total = sum(counts.values())
        accounted = counts["ok"] + counts["failed"] + counts["skipped"] + counts["suspicious"]
        return total - accounted


# ─── JSON 序列化 ─────────────────────────────────────────────────

def to_json(obj) -> str:
    """把 dataclass / 枚举 / tuple 序列化成 JSON 字符串"""
    def default(o):
        if isinstance(o, Enum):
            return o.value
        if isinstance(o, Path):
            return str(o)
        if hasattr(o, "__dataclass_fields__"):
            return asdict(o)
        raise TypeError(f"无法序列化 {type(o)}")
    return json.dumps(obj, default=default, ensure_ascii=False, indent=2)


def course_structure_from_dict(d: dict) -> CourseStructure:
    """从 dict 反序列化（用于读取 _mapping.json）"""
    chapters = tuple(
        Chapter(
            index=c["index"],
            title=c["title"],
            lessons=tuple(
                Lesson(
                    id=l["id"],
                    title=l["title"],
                    content_type=ContentType(l.get("content_type", "other")),
                    video=l.get("video"),
                    description=l.get("description"),
                    attachments=tuple(l.get("attachments", [])),
                    quiz=l.get("quiz"),
                    match_confidence=MatchConfidence(l.get("match_confidence", "none")),
                    note=l.get("note"),
                )
                for l in c.get("lessons", [])
            ),
        )
        for c in d.get("chapters", [])
    )
    return CourseStructure(
        course_id=d.get("course_id", ""),
        course_title=d.get("course_title", ""),
        chapters=chapters,
        source_doc=d.get("source_doc"),
        generated_at=d.get("generated_at"),
    )
