"""
资源树 diff/sync —— 真实章节树 vs mapping,按状态机执行最小修改。

设计目标:
  1. 拉一次 get_resource_tree,跟 CourseStructure 做精确对比
  2. 已存在且匹配的章/节/leaf → 标记 SKIP,复用
  3. 缺失的章/节/leaf → 标记 CREATE,后续流程创建
  4. 多余的章/节/leaf → 默认不动,只有 --prune 才删
  5. 名称不一致但能匹配 → 标记 RENAME,默认自动改
  6. 差异/错误超过 60% → 拒绝继续,建议 --reset-confirm <course_id>

API 形状:
  sync_diff = compute_diff(structure, tree, only_chapters=None)
  diff.is_too_drifted(threshold=0.6) -> bool
  diff.report() -> dict
  write_backup_snapshot(tree, output_dir, course_id) -> Path
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Optional

from .models import Chapter, ContentType, CourseStructure, Lesson

logger = logging.getLogger(__name__)


# ─── 状态枚举 ─────────────────────────────────────────────────────

class DiffAction:
    """每个章/节/leaf 的 diff 决定"""
    SKIP = "skip"                # 已存在且匹配,不操作
    CREATE = "create"            # 缺失,需创建
    RENAME = "rename"            # 已存在但 title 不一致,改名
    PRUNE = "prune"              # 真实树有但 mapping 没有,默认不动,带 --prune 才删
    UPDATE_VIDEO = "update_video"  # section/lesson 存在但视频不同(rare)


# ─── 数据结构 ─────────────────────────────────────────────────────

@dataclass(frozen=True)
class ChapterDiff:
    index: int
    desired_title: str
    actual_id: Optional[int]      # 真实树里的 id(若有)
    actual_title: Optional[str]   # 真实树里的 title(若有)
    action: str                   # SKIP / CREATE / RENAME / PRUNE
    matched_by: str = "exact"     # exact / fuzzy / none(检测方式)
    lesson_diffs: tuple["LessonDiff", ...] = ()


@dataclass(frozen=True)
class LessonDiff:
    id: str
    desired_title: str
    actual_id: Optional[int]
    actual_title: Optional[str]
    action: str
    matched_by: str = "exact"
    leaf_diffs: tuple["LeafDiff", ...] = ()


@dataclass(frozen=True)
class LeafDiff:
    lesson_id: str
    kind: str                     # "video" / "english" / "ppt" / "pdf" / "docx"
    desired_name: str
    actual_id: Optional[int]
    action: str


@dataclass(frozen=True)
class TreeDiff:
    """完整 diff 结果,喂给 _execute_diff"""
    course_id: str
    chapters: tuple[ChapterDiff, ...] = ()
    extra_chapter_ids: tuple[int, ...] = ()   # 真实树里有但 mapping 没有(默认 PRUNE)
    stats: dict[str, int] = field(default_factory=dict)

    def total_planned(self) -> int:
        """应该执行的操作总数(向后兼容:CREATE + RENAME + PRUNE)

        旧版只有 create / rename / prune。新版 create 已拆成 create_chapters /
        create_sections / create_leaves,这里为了兼容老代码,沿用 stats["create"]。
        想要更细粒度看 drift 估算,直接用 stats["create_leaves"](更接近实际改动量)。
        """
        return self.stats.get("create", 0) + self.stats.get("rename", 0) + self.stats.get("prune", 0)

    def is_too_drifted(self, threshold: float = 0.6) -> bool:
        """差异/错误比例超过阈值则提示用户清空重建。

        公式: (CREATE + RENAME + PRUNE) / total_assets >= threshold
        """
        n_planned = self.total_planned()
        n_existing = self.stats.get("skip", 0)
        total = n_planned + n_existing
        if total == 0:
            return False
        return (n_planned / total) >= threshold

    def report(self) -> dict[str, Any]:
        """JSON 友好报告"""
        return {
            "course_id": self.course_id,
            "stats": self.stats,
            "is_drifted": self.is_too_drifted(),
            "extra_chapter_ids": list(self.extra_chapter_ids),
            "chapters": [
                {
                    "index": cd.index,
                    "desired_title": cd.desired_title,
                    "actual_id": cd.actual_id,
                    "actual_title": cd.actual_title,
                    "action": cd.action,
                    "matched_by": cd.matched_by,
                    "lessons": [
                        {
                            "id": ld.id,
                            "desired_title": ld.desired_title,
                            "actual_id": ld.actual_id,
                            "actual_title": ld.actual_title,
                            "action": ld.action,
                            "matched_by": ld.matched_by,
                            "leaves": [
                                {
                                    "lesson_id": lfd.lesson_id,
                                    "kind": lfd.kind,
                                    "desired_name": lfd.desired_name,
                                    "actual_id": lfd.actual_id,
                                    "action": lfd.action,
                                }
                                for lfd in ld.leaf_diffs
                            ],
                        }
                        for ld in cd.lesson_diffs
                    ],
                }
                for cd in self.chapters
            ],
        }


# ─── 计算 diff ───────────────────────────────────────────────────

def _norm_title_for_match(title: str) -> str:
    """归一化标题用于匹配:去前后空白 + 全/半角转小写 + 去标点"""
    import re
    s = (title or "").strip().lower()
    s = re.sub(r"\s+", "", s)
    s = re.sub(r"[【】\[\]（）：:。,\s,，\.;；'\"!?！？]", "", s)
    return s


def _find_existing_chapter(
    actual: list[dict[str, Any]],
    desired: Chapter,
) -> tuple[Optional[dict[str, Any]], str]:
    """在真实树的 chapter_list 里找和 desired 等价的章。

    Returns:
        (chapter_dict, matched_by) — 没找到 → (None, "none")
        matched_by 取值: "exact" / "fuzzy" / "none"
    """
    norm_desired = _norm_title_for_match(desired.title)

    # 1) 精确:normalized 完全相等
    for ch in actual:
        if _norm_title_for_match(ch.get("name", "")) == norm_desired:
            return ch, "exact"

    # 2) 模糊:编号匹配(用 index 找,防止用户改了标题)
    for ch in actual:
        if int(ch.get("index", -1)) == desired.index:
            return ch, "fuzzy"

    return None, "none"


def _find_existing_lesson(
    actual_sections: list[dict[str, Any]],
    desired: Lesson,
) -> tuple[Optional[dict[str, Any]], str]:
    """在 chapter['section_list'] 里找和 desired 等价的节。"""
    norm_desired = _norm_title_for_match(desired.title)

    # 精确
    for sec in actual_sections:
        if _norm_title_for_match(sec.get("name", "")) == norm_desired:
            return sec, "exact"
    # 用 lesson_id 后缀(1.1)模糊
    for sec in actual_sections:
        if sec.get("name", "").strip().startswith(desired.id):
            return sec, "fuzzy"
    return None, "none"


def compute_diff(
    structure: CourseStructure,
    tree: dict[str, Any],
    *,
    only_chapters: Optional[Iterable[int]] = None,
    prune: bool = False,
) -> TreeDiff:
    """根据真实 resource_tree 计算 diff。

    Args:
        structure: mapping 出来的 CourseStructure
        tree: get_resource_tree() 的 data 字段(包含 chapter_list)
        only_chapters: 只考虑这些 chapter index
        prune: True 时多余章/节/leaf 标 PRUNE(否则默认 SKIP)
    """
    only_set = set(only_chapters) if only_chapters is not None else None
    actual_chapters: list[dict[str, Any]] = tree.get("chapter_list", []) or []
    used_actual_ids: set[int] = set()

    chapter_diffs: list[ChapterDiff] = []
    # 统计字段:
    #   create_chapters / create_sections / create_leaves — 拆分后的粒度
    #   create — 老字段,= 三者之和,保持向后兼容
    #   skip / rename / prune — 同旧
    stats: dict[str, int] = {
        "skip": 0,
        "create": 0,
        "create_chapters": 0,
        "create_sections": 0,
        "create_leaves": 0,
        "rename": 0,
        "prune": 0,
    }

    for ch in structure.chapters:
        if only_set is not None and ch.index not in only_set:
            # 过滤掉:不计入 diff
            continue

        actual, matched_by = _find_existing_chapter(actual_chapters, ch)
        lesson_diffs: list[LessonDiff] = []

        if actual is None:
            # CREATE 整个章 + 它所有 lessons + sections + leaves
            stats["create_chapters"] += 1
            stats["create_sections"] += len(ch.lessons)  # 每 lesson 一个 section
            total_leaves = sum(
                1 + len(ls.attachments)  # video + 每个 attachment
                for ls in ch.lessons
            )
            stats["create_leaves"] += total_leaves
            stats["create"] += 1  # 老字段兼容(章级)
            for ls in ch.lessons:
                leaf_diffs = _leaves_for_lesson(ls, actual_section=None)
                lesson_diffs.append(LessonDiff(
                    id=ls.id, desired_title=ls.title,
                    actual_id=None, actual_title=None,
                    action=DiffAction.CREATE,
                    matched_by="none",
                    leaf_diffs=tuple(leaf_diffs),
                ))
            chapter_diffs.append(ChapterDiff(
                index=ch.index, desired_title=ch.title,
                actual_id=None, actual_title=None,
                action=DiffAction.CREATE,
                matched_by="none",
                lesson_diffs=tuple(lesson_diffs),
            ))
            continue

        used_actual_ids.add(int(actual["id"]))
        action = DiffAction.SKIP
        rename = False
        if _norm_title_for_match(actual.get("name", "")) != _norm_title_for_match(ch.title):
            action = DiffAction.RENAME
            rename = True
            stats["rename"] += 1
        else:
            stats["skip"] += 1

        # 找 lesson:遍历 actual 的 section_list
        actual_sections = actual.get("section_list", []) or []
        used_section_ids: set[int] = set()

        for ls in ch.lessons:
            sec, ls_matched = _find_existing_lesson(actual_sections, ls)
            if sec is None:
                # CREATE lesson(整节 + 1 section + N leaves)
                leaf_diffs = _leaves_for_lesson(ls, actual_section=None)
                lesson_diffs.append(LessonDiff(
                    id=ls.id, desired_title=ls.title,
                    actual_id=None, actual_title=None,
                    action=DiffAction.CREATE,
                    matched_by="none",
                    leaf_diffs=tuple(leaf_diffs),
                ))
                stats["create_sections"] += 1
                stats["create_leaves"] += 1 + len(ls.attachments)
                stats["create"] += 1
                continue
            used_section_ids.add(int(sec["id"]))
            # section 标题不一致?
            sec_action = DiffAction.SKIP
            if _norm_title_for_match(sec.get("name", "")) != _norm_title_for_match(ls.title):
                sec_action = DiffAction.RENAME
            # leaf:精确比较 video_filename + attachments vs sec['leaf_list']
            actual_leaves = sec.get("leaf_list", []) or []
            leaf_diffs = _compare_leaves(ls, actual_leaves, stats)
            lesson_diffs.append(LessonDiff(
                id=ls.id, desired_title=ls.title,
                actual_id=int(sec["id"]), actual_title=sec.get("name"),
                action=sec_action,
                matched_by=ls_matched,
                leaf_diffs=tuple(leaf_diffs),
            ))

        chapter_diffs.append(ChapterDiff(
            index=ch.index, desired_title=ch.title,
            actual_id=int(actual["id"]), actual_title=actual.get("name"),
            action=action,
            matched_by=matched_by,
            lesson_diffs=tuple(lesson_diffs),
        ))

    # 处理 mapping 没有的多余章
    extra_ids: list[int] = []
    for ch in actual_chapters:
        cid = int(ch["id"])
        if cid in used_actual_ids:
            continue
        # only_chapters 过滤时,只关心范围内的章
        if only_set is not None and int(ch.get("index", -1)) not in only_set:
            continue
        if prune:
            extra_ids.append(cid)
            stats["prune"] += 1
        else:
            # 默认 SKIP 保留(不删)
            stats["skip"] += 1
            # 但加个虚拟 ChapterDiff 标 PRUNE 待定,方便上层报告
            chapter_diffs.append(ChapterDiff(
                index=int(ch.get("index", -1)),
                desired_title="",
                actual_id=cid,
                actual_title=ch.get("name"),
                action=DiffAction.PRUNE,
                matched_by="none",
                lesson_diffs=(),
            ))

    return TreeDiff(
        course_id=structure.course_id,
        chapters=tuple(chapter_diffs),
        extra_chapter_ids=tuple(extra_ids),
        stats=stats,
    )


def _leaves_for_lesson(lesson: Lesson, actual_section: Optional[dict[str, Any]]) -> list[LeafDiff]:
    """lesson 全 CREATE 时:算出每个 leaf 的 diff(全部 CREATE)。

    即使实际树已有 section 也可能缺 leaf(用户补资源)。
    """
    leaves: list[LeafDiff] = []
    if lesson.video:
        leaves.append(LeafDiff(
            lesson_id=lesson.id, kind="video",
            desired_name=lesson.video, actual_id=None,
            action=DiffAction.CREATE,
        ))
    for att in lesson.attachments:
        kind = _kind_from_filename(att)
        leaves.append(LeafDiff(
            lesson_id=lesson.id, kind=kind,
            desired_name=att, actual_id=None,
            action=DiffAction.CREATE,
        ))
    return leaves


def _compare_leaves(
    lesson: Lesson,
    actual_leaves: list[dict[str, Any]],
    stats: dict[str, int],
) -> list[LeafDiff]:
    """对 lesson 的 expected leaves vs actual leaves 做 diff。

    匹配规则:按 leaf 的 "name"(后台返回的 video_filename 或 download 里的 file_name)
    """
    diffs: list[LeafDiff] = []

    # 构造 expected:[{"name": ..., "kind": ...}, ...]
    expected: list[dict[str, Any]] = []
    if lesson.video:
        expected.append({"name": lesson.video, "kind": "video"})
    for att in lesson.attachments:
        expected.append({"name": att, "kind": _kind_from_filename(att)})

    actual_names = {_leaf_name(al) for al in actual_leaves}

    for exp in expected:
        if exp["name"] in actual_names:
            # 已存在
            stats["skip"] += 1
            diffs.append(LeafDiff(
                lesson_id=lesson.id, kind=exp["kind"],
                desired_name=exp["name"],
                actual_id=_leaf_id_by_name(actual_leaves, exp["name"]),
                action=DiffAction.SKIP,
            ))
        else:
            stats["create_leaves"] += 1
            stats["create"] += 1
            diffs.append(LeafDiff(
                lesson_id=lesson.id, kind=exp["kind"],
                desired_name=exp["name"],
                actual_id=None,
                action=DiffAction.CREATE,
            ))
    return diffs


def _leaf_name(leaf: dict[str, Any]) -> str:
    """从后台 leaf dict 提取可比较的文件名(优先 media.name,否则 download[0].file_name)"""
    media = leaf.get("content_info", {}).get("media", {}) or {}
    if media.get("name"):
        return str(media["name"])
    downloads = leaf.get("content_info", {}).get("download", []) or []
    if downloads and downloads[0].get("file_name"):
        return str(downloads[0]["file_name"])
    return str(leaf.get("name", ""))


def _leaf_id_by_name(actual_leaves: list[dict[str, Any]], name: str) -> Optional[int]:
    for al in actual_leaves:
        if _leaf_name(al) == name:
            return int(al.get("id") or al.get("leaf_id") or 0) or None
    return None


def _kind_from_filename(filename: str) -> str:
    """根据文件名后缀 + 角色标记判断 leaf kind(用于 diff / 上传路由)。

    命名约定见 scrape_new.upload.naming.lesson_filename:
      主视频:"1.1_技术.mp4"         → "video"
      英文视频:"1.1_技术_English.mp4" → "english"
      多英文:"1.1_技术_English_2.mp4"  → "english"
      PPT:"1.1_技术_PPT.pptx"         → "ppt"
      PDF:"1.1_技术_课件.pdf"         → "pdf"
    """
    f = (filename or "").lower()
    # _English 在 .mp4 之前 → 英文视频(后缀判断优先)
    if "_english" in f and f.endswith((".mp4", ".flv")):
        return "english"
    if f.endswith((".mp4", ".flv", ".avi", ".mkv", ".mov")):
        return "video"
    if f.endswith((".ppt", ".pptx")):
        return "ppt"
    if f.endswith(".pdf"):
        return "pdf"
    if f.endswith((".doc", ".docx")):
        return "docx"
    return "attachment"


# ─── 备份真实树快照 ───────────────────────────────────────────

def write_backup_snapshot(
    tree: dict[str, Any],
    output_dir: Path,
    course_id: str,
) -> Path:
    """把当前真实树写到 _resource_tree_backup_<ts>.json,reset 前的最后机会。"""
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = output_dir / f"_resource_tree_backup_{course_id}_{ts}.json"
    path.write_text(json.dumps(tree, ensure_ascii=False, indent=2), encoding="utf-8")
    logger.info(f"已备份 resource_tree: {path}")
    return path
