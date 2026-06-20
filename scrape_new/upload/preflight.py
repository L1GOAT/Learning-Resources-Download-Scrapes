"""
Pre-flight 体检报告:上传前快速判断"能不能跑"。

输入: CourseStructure(mapping) + 真实 resource_tree
输出: 文本报告(可打印)+ 结构化 dict(可 JSON)

体检 4 段:
  1. 数量对比   mapping 章/节/leaf  vs  后台 章/节/leaf
  2. RENAME 章  标题不一致的章列表(待 confirm_rename)
  3. 风险等级   LOW / MEDIUM / HIGH(基于 drift 比例 + RENAME 数)
  4. 缺资源     每节是否同时有 video + english + ppt;缺哪个列出来

CLI:
  python -m scrape.upload preflight --mapping _mapping.json \
    --course-id 15939407 --cookies-string "..."
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

from .sync_tree import (
    ChapterDiff,
    DiffAction,
    LeafDiff,
    LessonDiff,
    TreeDiff,
    compute_diff,
)

logger = logging.getLogger(__name__)


# ─── 风险等级 ─────────────────────────────────────────────────────

class RiskLevel(str, Enum):
    """preflight 风险等级(按 drift + RENAME 数)"""
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"

    @property
    def emoji(self) -> str:
        return {
            RiskLevel.LOW: "✅",
            RiskLevel.MEDIUM: "⚠️ ",
            RiskLevel.HIGH: "🚨",
        }[self]


def _calc_risk_level(
    drift_ratio: float,
    rename_count: int,
    *,
    drift_threshold: float = 0.6,
) -> RiskLevel:
    """风险等级规则:
    HIGH  : drift >= threshold(60% 缺失)
    MEDIUM: drift >= 30% OR rename > 0
    LOW   : 其他
    """
    if drift_ratio >= drift_threshold:
        return RiskLevel.HIGH
    if drift_ratio >= 0.3 or rename_count > 0:
        return RiskLevel.MEDIUM
    return RiskLevel.LOW


# ─── 数量对比 ─────────────────────────────────────────────────────

@dataclass(frozen=True)
class CountDelta:
    """mapping vs 真实树的章/节/leaf 数对比"""
    mapping_chapters: int
    actual_chapters: int
    mapping_lessons: int
    actual_lessons: int
    mapping_leaves: int
    actual_leaves: int

    @property
    def chapter_delta(self) -> int:
        return self.mapping_chapters - self.actual_chapters

    @property
    def lesson_delta(self) -> int:
        return self.mapping_lessons - self.actual_lessons

    @property
    def leaf_delta(self) -> int:
        return self.mapping_leaves - self.actual_leaves


def _count_structure(structure, tree) -> CountDelta:
    """mapping 数 + 真实树数。"""
    mapping_chapters = len(structure.chapters)
    mapping_lessons = sum(len(c.lessons) for c in structure.chapters)
    mapping_leaves = sum(
        1 + len(ls.attachments)
        for ch in structure.chapters
        for ls in ch.lessons
        if ls.video or ls.attachments
    )
    # 真实树
    actual_chapters = len(tree.get("chapter_list", []) or [])
    actual_lessons = 0
    actual_leaves = 0
    for ch in tree.get("chapter_list", []) or []:
        secs = ch.get("section_list", []) or []
        actual_lessons += len(secs)
        for sec in secs:
            actual_leaves += len(sec.get("leaf_list", []) or [])
    return CountDelta(
        mapping_chapters=mapping_chapters,
        actual_chapters=actual_chapters,
        mapping_lessons=mapping_lessons,
        actual_lessons=actual_lessons,
        mapping_leaves=mapping_leaves,
        actual_leaves=actual_leaves,
    )


# ─── RENAME 清单 ────────────────────────────────────────────────

@dataclass(frozen=True)
class RenameEntry:
    """标题不一致的章"""
    chapter_index: int
    desired_title: str
    actual_title: str
    actual_id: int
    matched_by: str  # "fuzzy" / "exact"


def _collect_renames(diff: TreeDiff) -> list[RenameEntry]:
    out: list[RenameEntry] = []
    for cd in diff.chapters:
        if cd.action != DiffAction.RENAME:
            continue
        out.append(RenameEntry(
            chapter_index=cd.index,
            desired_title=cd.desired_title,
            actual_title=cd.actual_title or "",
            actual_id=cd.actual_id or 0,
            matched_by=cd.matched_by,
        ))
    return out


# ─── 缺资源检查 ───────────────────────────────────────────────

@dataclass(frozen=True)
class MissingResource:
    """某节缺哪个 role"""
    chapter_index: int
    lesson_id: str
    lesson_title: str
    missing_roles: tuple[str, ...]  # e.g. ("english", "ppt")


# 想每节都有这 3 个 role(可配)
DEFAULT_REQUIRED_ROLES = ("video", "english", "ppt")


def _collect_missing_resources(
    structure,
    *,
    required_roles: tuple[str, ...] = DEFAULT_REQUIRED_ROLES,
) -> list[MissingResource]:
    """扫 mapping 找出缺资源的节(不查真实树,只看 mapping 自报)。

    规则:
      - video 必须有 ls.video
      - english 必须有 ls.attachments 里含 _English.mp4
      - ppt 必须有 ls.attachments 里含 .pptx 或 _PPT.pptx
    """
    out: list[MissingResource] = []
    for ch in structure.chapters:
        for ls in ch.lessons:
            if ls.content_type.value not in ("video", "attachment"):
                continue
            missing: list[str] = []
            if "video" in required_roles:
                if not ls.video:
                    missing.append("video")
            if "english" in required_roles:
                if not any("_English" in a and a.lower().endswith(".mp4")
                           for a in ls.attachments):
                    missing.append("english")
            if "ppt" in required_roles:
                if not any(a.lower().endswith((".pptx", ".ppt"))
                           or "_PPT" in a
                           for a in ls.attachments):
                    missing.append("ppt")
            if missing:
                out.append(MissingResource(
                    chapter_index=ch.index,
                    lesson_id=ls.id,
                    lesson_title=ls.title,
                    missing_roles=tuple(missing),
                ))
    return out


# ─── 风险等级(组合所有信号) ──────────────────────────────────

def _count_action(diff: TreeDiff, action: str) -> int:
    n = 0
    for cd in diff.chapters:
        if cd.action == action:
            n += 1
        for ld in cd.lesson_diffs:
            if ld.action == action:
                n += 1
            for lfd in ld.leaf_diffs:
                if lfd.action == action:
                    n += 1
    return n


# ─── 报告主类 ───────────────────────────────────────────────────

@dataclass(frozen=True)
class PreflightReport:
    """体检报告(纯数据,无 UI)"""
    course_id: str
    course_title: str
    counts: CountDelta
    rename_count: int
    rename_entries: tuple[RenameEntry, ...]
    missing_resources: tuple[MissingResource, ...]
    risk_level: RiskLevel
    drift_ratio: float           # 0-1,create/(create+skip)
    drift_threshold: float
    stats: dict[str, int]        # 透传 compute_diff 的 stats
    extra_chapter_ids: tuple[int, ...]  # 真实树多出章

    # 操作摘要(机器友好)
    @property
    def will_create_chapters(self) -> int:
        return self.stats.get("create_chapters", 0)

    @property
    def will_create_sections(self) -> int:
        return self.stats.get("create_sections", 0)

    @property
    def will_create_leaves(self) -> int:
        return self.stats.get("create_leaves", 0)

    @property
    def will_skip_leaves(self) -> int:
        return self.stats.get("skip", 0)

    def to_dict(self) -> dict[str, Any]:
        return {
            "course_id": self.course_id,
            "course_title": self.course_title,
            "counts": {
                "mapping": {
                    "chapters": self.counts.mapping_chapters,
                    "lessons": self.counts.mapping_lessons,
                    "leaves": self.counts.mapping_leaves,
                },
                "actual": {
                    "chapters": self.counts.actual_chapters,
                    "lessons": self.counts.actual_lessons,
                    "leaves": self.counts.actual_leaves,
                },
                "delta": {
                    "chapters": self.counts.chapter_delta,
                    "lessons": self.counts.lesson_delta,
                    "leaves": self.counts.leaf_delta,
                },
            },
            "renames": [
                {
                    "chapter_index": r.chapter_index,
                    "desired_title": r.desired_title,
                    "actual_title": r.actual_title,
                    "actual_id": r.actual_id,
                    "matched_by": r.matched_by,
                }
                for r in self.rename_entries
            ],
            "missing_resources": [
                {
                    "chapter_index": m.chapter_index,
                    "lesson_id": m.lesson_id,
                    "lesson_title": m.lesson_title,
                    "missing_roles": list(m.missing_roles),
                }
                for m in self.missing_resources
            ],
            "risk_level": self.risk_level.value,
            "drift_ratio": round(self.drift_ratio, 4),
            "drift_threshold": self.drift_threshold,
            "stats": dict(self.stats),
            "extra_chapter_ids": list(self.extra_chapter_ids),
            "plan": {
                "create_chapters": self.will_create_chapters,
                "create_sections": self.will_create_sections,
                "create_leaves": self.will_create_leaves,
                "skip_leaves": self.will_skip_leaves,
            },
        }


def build_preflight(
    structure,
    tree: dict[str, Any],
    *,
    drift_threshold: float = 0.6,
    required_roles: tuple[str, ...] = DEFAULT_REQUIRED_ROLES,
    only_chapters=None,
) -> PreflightReport:
    """从 mapping + 真实树算体检报告。"""
    counts = _count_structure(structure, tree)
    diff = compute_diff(
        structure, tree,
        only_chapters=only_chapters, prune=False,
    )
    rename_entries = _collect_renames(diff)
    missing = _collect_missing_resources(structure, required_roles=required_roles)

    # drift 比例:基于 leaf 级(create_leaves / (create_leaves + skip))
    # 之前 stats["create"] 是章级,现在用 create_leaves 更准
    create_leaves = diff.stats.get("create_leaves", 0)
    skip = diff.stats.get("skip", 0)
    denom = create_leaves + skip
    drift_ratio = (create_leaves / denom) if denom > 0 else 0.0

    risk = _calc_risk_level(
        drift_ratio, len(rename_entries),
        drift_threshold=drift_threshold,
    )

    return PreflightReport(
        course_id=structure.course_id,
        course_title=structure.course_title,
        counts=counts,
        rename_count=len(rename_entries),
        rename_entries=tuple(rename_entries),
        missing_resources=tuple(missing),
        risk_level=risk,
        drift_ratio=drift_ratio,
        drift_threshold=drift_threshold,
        stats=dict(diff.stats),
        extra_chapter_ids=diff.extra_chapter_ids,
    )


# ─── 文本报告(给人看) ────────────────────────────────────────

def format_preflight_text(report: PreflightReport) -> str:
    """生成适合打印的 preflight 报告(可贴终端)。"""
    lines: list[str] = []
    r = report
    c = r.counts

    lines.append("=" * 60)
    lines.append(f" 课程体检报告 (Pre-flight Check)")
    lines.append("=" * 60)
    lines.append(f"  课程: {r.course_title or '(未填)'} (id={r.course_id or '?'})")
    lines.append(f"  风险等级: {r.risk_level.emoji} {r.risk_level.value}")
    lines.append("")

    # 数量对比
    lines.append("─" * 60)
    lines.append(" 数量对比")
    lines.append("─" * 60)
    lines.append(f"             mapping     后台     差")
    lines.append(
        f"  章     {c.mapping_chapters:>8}  {c.actual_chapters:>8}  {c.chapter_delta:+d}"
    )
    lines.append(
        f"  节     {c.mapping_lessons:>8}  {c.actual_lessons:>8}  {c.lesson_delta:+d}"
    )
    lines.append(
        f"  leaf   {c.mapping_leaves:>8}  {c.actual_leaves:>8}  {c.leaf_delta:+d}"
    )
    lines.append("")

    # 计划
    lines.append("─" * 60)
    lines.append(" 计划")
    lines.append("─" * 60)
    lines.append(f"  将新增: {r.will_create_chapters} 章 / {r.will_create_sections} 节 "
                 f"/ {r.will_create_leaves} leaf")
    lines.append(f"  将跳过: {r.will_skip_leaves} leaf")
    if r.rename_count:
        lines.append(f"  将待确认: {r.rename_count} 章改名(需 --confirm-rename)")
    if r.extra_chapter_ids:
        lines.append(f"  额外章(默认保留,需 --prune 才删): {list(r.extra_chapter_ids)}")
    lines.append("")

    # RENAME
    if r.rename_entries:
        lines.append("─" * 60)
        lines.append(f" 改名章 (RENAME,待确认)")
        lines.append("─" * 60)
        for entry in r.rename_entries:
            lines.append(
                f"  ch{entry.chapter_index} (id={entry.actual_id}, matched={entry.matched_by})"
            )
            lines.append(f"    后台: {entry.actual_title!r}")
            lines.append(f"    mapping: {entry.desired_title!r}")
        lines.append("")

    # 缺资源
    if r.missing_resources:
        lines.append("─" * 60)
        lines.append(f" 缺资源(每节自检)")
        lines.append("─" * 60)
        # 按 chapter 聚合
        by_ch: dict[int, list[MissingResource]] = {}
        for m in r.missing_resources:
            by_ch.setdefault(m.chapter_index, []).append(m)
        for ch_idx in sorted(by_ch.keys()):
            lines.append(f"  第{ch_idx}章:")
            for m in by_ch[ch_idx]:
                roles = ", ".join(m.missing_roles)
                lines.append(
                    f"    {m.lesson_id} {m.lesson_title!r} → 缺 [{roles}]"
                )
        lines.append("")

    # 建议
    lines.append("─" * 60)
    lines.append(" 建议")
    lines.append("─" * 60)
    if r.risk_level == RiskLevel.HIGH:
        lines.append("  🚨 高风险:drift 超过阈值,建议 --reset-confirm 清空重建")
    elif r.risk_level == RiskLevel.MEDIUM:
        if r.rename_count:
            lines.append("  ⚠️  有章改名待确认,默认不动(传 --confirm-rename 才执行)")
        if r.drift_ratio >= 0.3:
            lines.append("  ⚠️  drift 偏高(>= 30%),增量前先 verify-only 看后台状态")
        lines.append("  建议:增量上传(--only-chapters 限定范围更稳)")
    else:
        lines.append("  ✅ 低风险:直接增量上传即可")
        lines.append("  建议命令:python -m scrape.upload upload --mapping ... --cookies-string ...")

    if r.missing_resources:
        lines.append(f"  ⚠️  有 {len(r.missing_resources)} 节缺资源,跑上传前先补素材")

    lines.append("=" * 60)
    return "\n".join(lines)


def write_preflight_text(
    report: PreflightReport,
    output_dir: Path,
) -> Path:
    """写 _preflight_report.txt"""
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / "_preflight_report.txt"
    path.write_text(format_preflight_text(report), encoding="utf-8")
    logger.info(f"已写 preflight 报告: {path}")
    return path
