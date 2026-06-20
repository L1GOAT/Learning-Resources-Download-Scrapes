"""
资源智能审计层 — 解决下载/建课前最易出错的 4 类问题:

A. 漏资源:后台有 video/PPT/doc,但 scan 没扫到
B. 错分类:PPT 当视频、English 当附件、quiz 当 video
C. 错配课时:build-mapping 挂错节、漏挂、重复挂
D. 不完整不自知:课程残缺但工具没告诉用户

设计原则:
  - 纯函数,无 IO(只读 dict 输入,返回 dataclass)
  - dataclass + to_dict/to_json,可被 GUI 直接消费
  - 角色识别带置信度 + 证据链(让审计员看清"为什么判它是 PPT")
  - 不动 scrape/、不改 scrape_new/ 现有 API
"""

from __future__ import annotations

import csv
import json
import re
from dataclasses import dataclass, field, asdict
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Optional


# ─── 角色识别常量 ──────────────────────────────────────────

# 扩展名 → role(强证据,权重高)
_EXT_TO_ROLE: dict[str, str] = {
    "mp4": "video", "flv": "video", "m3u8": "video",
    "avi": "video", "mkv": "video", "mov": "video",
    "ppt": "ppt", "pptx": "ppt",
    "pdf": "pdf",
    "doc": "doc", "docx": "doc",
    "xls": "doc", "xlsx": "doc",
    "zip": "doc", "rar": "doc", "7z": "doc",
    "jpg": "image", "jpeg": "image", "png": "image",
    "gif": "image", "bmp": "image",
}

# MIME / content-type → role
_MIME_TO_ROLE: dict[str, str] = {
    "video/mp4": "video", "video/flv": "video", "video/x-flv": "video",
    "application/vnd.ms-powerpoint": "ppt",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation": "ppt",
    "application/pdf": "pdf",
    "application/msword": "doc",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": "doc",
}

# 标题/文件名关键字正则 → role
_TITLE_PATTERNS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"(?i)english|英文|en\s*version|english\s*audio|english\s*audio"), "english"),
    (re.compile(r"(?i)\bppt\b|课件|演示稿|slides?"), "ppt"),
    (re.compile(r"(?i)pdf|讲义|教案"), "pdf"),
    (re.compile(r"(?i)docx?|word|讲义|教案"), "doc"),
    (re.compile(r"(?i)quiz|测验|测试|exam|考试"), "quiz"),
    (re.compile(r"(?i)note|笔记|备注|reading|reading|阅读|扩展"), "note"),
]

# tab 倾向(弱证据,跟标题冲突时降权)
_TAB_HINT: dict[int, tuple[str, float]] = {
    0: ("video", 0.3),
    1: ("ppt", 0.4),
    2: ("quiz", 0.3),
    3: ("note", 0.3),
}

# 置信度阈值
CONF_HIGH = 0.85
CONF_MEDIUM = 0.65
# < CONF_MEDIUM → low_confidence_role,触发 issue


# ─── Dataclass ─────────────────────────────────────────────

@dataclass
class ResourceEvidence:
    """一条证据(扩展名 / MIME / 标题 / tab / objectid 等)"""
    source: str     # "ext" / "mime" / "title" / "filename" / "tab" / "objectid"
    key: str        # 实际值(如 ".pptx" / "课件" / "tab_num=1")
    value: str      # 资源名 / 标题 / 类型 / tab_num 等
    confidence: float  # 0~1,本证据单独的可信度
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class AuditedResource:
    """一个资源经过审计后的最终判定"""
    lesson_id: str
    lesson_title: str
    resource_key: str = ""
    original_name: str = ""
    saved_name: str = ""
    role: str = "unknown"      # video / english / ppt / pdf / doc / quiz / note / image / attachment / unknown
    role_confidence: float = 0.0
    role_evidence: list[ResourceEvidence] = field(default_factory=list)
    source_tab: int | None = None
    objectid: str = ""
    local_path: str = ""
    status: str = "unknown"    # found / downloaded / skipped / failed / missing / suspicious
    size_bytes: int = 0
    issues: list[str] = field(default_factory=list)
    suggestions: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["role_evidence"] = [e.to_dict() for e in self.role_evidence]
        return d


@dataclass
class LessonAudit:
    """一节课的审计结果"""
    lesson_id: str
    lesson_title: str
    ch_num: int = 0
    expected_resource_count: int | None = None   # 从 catalog_points_yi 等推断
    found_resource_count: int = 0
    resources: list[AuditedResource] = field(default_factory=list)
    issues: list[str] = field(default_factory=list)
    risk_level: str = "ok"      # ok / low / medium / high

    def to_dict(self) -> dict[str, Any]:
        return {
            "lesson_id": self.lesson_id,
            "lesson_title": self.lesson_title,
            "ch_num": self.ch_num,
            "expected_resource_count": self.expected_resource_count,
            "found_resource_count": self.found_resource_count,
            "resources": [r.to_dict() for r in self.resources],
            "issues": list(self.issues),
            "risk_level": self.risk_level,
        }


@dataclass
class CourseAuditReport:
    """整门课的审计报告"""
    course_title: str = ""
    platform: str = ""
    generated_at: str = ""
    summary: dict[str, int] = field(default_factory=dict)
    lessons: list[LessonAudit] = field(default_factory=list)
    global_issues: list[str] = field(default_factory=list)
    recommendations: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "course_title": self.course_title,
            "platform": self.platform,
            "generated_at": self.generated_at,
            "summary": dict(self.summary),
            "lessons": [l.to_dict() for l in self.lessons],
            "global_issues": list(self.global_issues),
            "recommendations": list(self.recommendations),
        }

    def to_json(self, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=indent)


# ─── A:角色识别(置信度 + 证据链) ──────────────────────────

def classify_resource_role(
    resource: dict,
    lesson: dict | None = None,
) -> tuple[str, float, list[ResourceEvidence]]:
    """根据资源的多维证据识别 role。

    Args:
        resource: 至少含 type / filename / name(可选 mime / tab_num / objectid)
        lesson: 可选,含 title(用作辅助证据)

    Returns:
        (role, confidence, evidence_list)

    规则:
      1. 扩展名 + MIME 各 +0.5 / +0.6(强证据)
      2. 标题/文件名关键字 +0.4 / +0.5
      3. tab 倾向 +0.3
      4. 同节同 role 多证据累积,否则冲突降权
      5. confidence < 0.65 → 加 "需要人工确认资源类型"
    """
    evidences: list[ResourceEvidence] = []
    role_scores: dict[str, float] = {}

    def bump(role: str, conf: float, source: str, key: str, value: str, note: str = ""):
        role_scores[role] = role_scores.get(role, 0.0) + conf
        evidences.append(ResourceEvidence(
            source=source, key=key, value=value, confidence=conf, note=note,
        ))

    # 1. 扩展名(强证据;常见视频/PPT/pdf/doc 格式,单源就足以高置信度)
    type_str = str(resource.get("type") or "").lower().lstrip(".")
    filename = str(resource.get("filename") or resource.get("saved_name") or resource.get("name") or "")
    name_lower = filename.lower()
    ext = ""
    for e in (type_str, *name_lower.split(".")[-1:]):
        if e and e in _EXT_TO_ROLE:
            ext = e
            break

    if ext:
        role = _EXT_TO_ROLE[ext]
        # 扩展名是最强证据(几乎不会骗人),单源就给 0.85
        bump(role, 0.85, "ext", f".{ext}", filename, f"扩展名 .{ext} → {role}")

    # 2. MIME / content-type(强证据)
    mime = str(resource.get("mime") or resource.get("content_type") or "").lower()
    if mime:
        for k, r in _MIME_TO_ROLE.items():
            if k in mime:
                # 如果 MIME 与扩展名角色一致,补充证据;不一致则冲突
                bump(r, 0.85, "mime", k, mime, f"MIME {k} → {r}")
                break

    # 3. 标题 / 文件名关键字
    title = str(resource.get("title") or resource.get("lesson_title") or "")
    if lesson:
        title = title or str(lesson.get("title") or "")
    text = f"{title} {filename}".strip()
    if text:
        for pattern, role in _TITLE_PATTERNS:
            if pattern.search(text):
                conf = 0.45 if role == "english" else 0.4
                bump(role, conf, "title_keyword", role,
                     text[:60], f"标题/文件名命中 '{role}' 关键字")

    # 4. tab 倾向
    tab_num = resource.get("tab_num")
    if tab_num is None and lesson:
        tab_num = lesson.get("tab_num")
    if tab_num is not None and isinstance(tab_num, int) and tab_num in _TAB_HINT:
        role, conf = _TAB_HINT[tab_num]
        bump(role, conf, "tab", f"tab_num={tab_num}", str(tab_num),
             f"tab {tab_num} 通常是 {role}(弱证据)")

    # 5. 决定最终 role + 处理冲突
    if not role_scores:
        return ("unknown", 0.0, evidences)

    # 最高分 role
    final_role = max(role_scores, key=role_scores.get)
    max_score = role_scores[final_role]
    # 总分 = 全部证据加权和,但归一到 0~1(每条 ≥0.4 上限 0.95)
    confidence = min(0.95, max(0.4, max_score))

    # 冲突检测:有 ≥2 个不同 role 的强证据(各 ≥0.5)
    # 弱证据(tab 0.3 / 单关键词 0.4)不参与冲突判定,避免单扩展名+tab=0 被误判冲突
    roles_with_strong_ev = [r for r, s in role_scores.items() if s >= 0.5]
    if len(roles_with_strong_ev) >= 2:
        # 冲突 → confidence 降低
        confidence = max(0.4, confidence - 0.2)

    # 英文视频:role=english 但 upload 仍当 video 上传 → 加 suggestion 提醒
    if final_role == "english":
        evidences.append(ResourceEvidence(
            source="note", key="upload_as_video", value="english→video",
            confidence=1.0,
            note="英文视频在 upload 侧 role=video(content_type=VIDEO),不是 attachment",
        ))

    return (final_role, round(confidence, 2), evidences)


# ─── 助手:从 evidence 推 issues + suggestions ─────────────────

def _add_resource_issues(resource: AuditedResource) -> None:
    """根据 resource 的 role_confidence / role 冲突加 issues + suggestions。"""
    if resource.role_confidence < CONF_MEDIUM:
        resource.issues.append("low_confidence_role")
        resource.suggestions.append("需要人工确认资源类型")
    if len(set(e.source for e in resource.role_evidence if e.source in ("ext", "mime", "title_keyword"))) >= 2:
        # 多源冲突
        ext_ev = next((e for e in resource.role_evidence if e.source == "ext"), None)
        title_ev = next((e for e in resource.role_evidence if e.source == "title_keyword"), None)
        if ext_ev and title_ev and ext_ev.value != title_ev.key:
            # 例:扩展名 mp4 + 标题"课件" → 冲突
            resource.issues.append("role_conflict")
            resource.suggestions.append("扩展名和标题暗示不同类型,需要人工确认")
    # 状态相关的提示
    if resource.status == "missing":
        resource.suggestions.append("可以安全跳过(本节课无此资源)" if not resource.role_evidence else
                                  "建议补资源")
    if resource.status == "suspicious":
        resource.suggestions.append("建议只重扫该节")


# ─── B:漏扫检测 ─────────────────────────────────────────────

def audit_scan_completeness(
    chapter_tree: dict,
    scanned_resources: list[dict],
    *,
    expected_tab_count: int | None = None,
) -> CourseAuditReport:
    """漏扫检测:扫描产物 vs chapter_tree,产出 CourseAuditReport。

    Args:
        chapter_tree: build_chapter_tree_data 输出(chapters: [{index, title, lessons: [...]}])
        scanned_resources: cards API 拿到的扁平资源列表(每项含 ch_num / ls_num / objectid / type / name / tab_num)
        expected_tab_count: 期望的 tab 扫描数,实际 scan_lesson_tabs 跑得少就提示"漏扫"

    Returns:
        CourseAuditReport
    """
    report = CourseAuditReport(
        course_title=chapter_tree.get("course_title", ""),
        platform=chapter_tree.get("platform", ""),
        generated_at=datetime.now().isoformat(timespec="seconds"),
    )

    # 按 (ch_num, ls_num) 分桶 scanned resources
    by_ls: dict[tuple[int, int], list[dict]] = {}
    objectid_to_lessons: dict[str, list[tuple[int, int]]] = {}
    saved_name_count: dict[str, list[tuple[int, int, str]]] = {}
    for r in scanned_resources:
        ch = int(r.get("ch_num") or 0)
        ls = int(r.get("ls_num") or 0)
        by_ls.setdefault((ch, ls), []).append(r)
        oid = r.get("objectid") or ""
        if oid:
            objectid_to_lessons.setdefault(oid, []).append((ch, ls))
        sn = (r.get("saved_name") or r.get("name") or "").strip()
        if sn:
            saved_name_count.setdefault(sn, []).append((ch, ls, sn))

    total_lesson_expected = 0
    total_lesson_found = 0
    empty_count = 0
    empty_chapters: list[int] = []

    for ch in chapter_tree.get("chapters", []):
        ch_idx = int(ch.get("index") or 0)
        ch_lessons = ch.get("lessons", [])
        all_empty = True
        for ls in ch_lessons:
            ls_id = ls.get("id") or ""
            ls_title = ls.get("title") or ""
            try:
                # ls id 是 "1.1" 格式
                _, ls_num_str = ls_id.split(".")
                ls_num = int(ls_num_str)
            except (ValueError, AttributeError):
                ls_num = 0
            total_lesson_expected += 1
            resources_here = by_ls.get((ch_idx, ls_num), [])
            audit = LessonAudit(
                lesson_id=ls_id,
                lesson_title=ls_title,
                ch_num=ch_idx,
                found_resource_count=len(resources_here),
            )
            # expected count 推断:catalog_points_yi 等
            expected = ls.get("expected_resource_count") or ls.get("resource_count")
            audit.expected_resource_count = expected

            # 单个资源审计
            for r in resources_here:
                role, conf, evs = classify_resource_role(r, lesson=ls)
                ar = AuditedResource(
                    lesson_id=ls_id,
                    lesson_title=ls_title,
                    resource_key=r.get("resource_key") or "",
                    original_name=r.get("name") or r.get("original_name") or "",
                    saved_name=r.get("saved_name") or "",
                    role=role,
                    role_confidence=conf,
                    role_evidence=evs,
                    source_tab=r.get("tab_num"),
                    objectid=r.get("objectid") or "",
                    size_bytes=r.get("size_bytes") or 0,
                    status=r.get("status") or "found",
                )
                _add_resource_issues(ar)
                audit.resources.append(ar)

            # lesson 级 issues
            if not resources_here:
                audit.issues.append("empty_lesson")
                audit.risk_level = "low"
                empty_count += 1
            else:
                all_empty = False
                total_lesson_found += 1
                # count mismatch
                if expected is not None and len(resources_here) < expected:
                    audit.issues.append("count_mismatch")
                    audit.issues.append("possible_missing_resource")
                    audit.risk_level = "medium" if audit.risk_level == "ok" else audit.risk_level
                # tab 数量检查
                tabs_found = {r.get("tab_num") for r in resources_here if r.get("tab_num") is not None}
                if expected_tab_count is not None and len(tabs_found) < expected_tab_count:
                    audit.issues.append("scan_incomplete")
                    audit.risk_level = "medium" if audit.risk_level == "ok" else audit.risk_level

            report.lessons.append(audit)

        if all_empty and ch_lessons:
            empty_chapters.append(ch_idx)
            report.global_issues.append(f"第 {ch_idx} 章所有课时都没有资源(可能漏扫或真没资源)")

    # duplicate_objectid
    for oid, lessons in objectid_to_lessons.items():
        if len(lessons) > 1:
            report.global_issues.append(
                f"objectid {oid[:16]}... 出现在多个 lesson: {[(c, l) for c, l in lessons]}"
            )

    # duplicate_saved_name
    for sn, info in saved_name_count.items():
        if len(info) > 1:
            report.global_issues.append(
                f"saved_name '{sn[:30]}...' 重复出现 {len(info)} 次"
            )

    report.summary = {
        "total_lessons": total_lesson_expected,
        "lessons_with_resources": total_lesson_found,
        "empty_lessons": empty_count,
        "empty_chapters": len(empty_chapters),
        "resources_audited": len(scanned_resources),
    }
    if empty_chapters:
        report.recommendations.append(
            f"整章空的章: {empty_chapters} — 建议确认是否真没资源 / 重扫"
        )
    if report.summary["empty_lessons"] > report.summary["total_lessons"] * 0.3:
        report.recommendations.append(
            f"空节占比 > 30%,可能后台真没资源或限流中断扫描"
        )
    if expected_tab_count is not None and any(
        "scan_incomplete" in ls.issues for ls in report.lessons
    ):
        report.recommendations.append(
            "部分 lesson tab 数 < 期望,可能 scan-only 提前停了,建议重扫"
        )
    if not report.recommendations:
        report.recommendations.append("扫描基本完整,可进入 build_mapping")

    return report


# ─── C:错配课时检测 ────────────────────────────────────────

def audit_mapping_alignment(
    mapping: dict,
    manifest_or_tree: dict,
) -> CourseAuditReport:
    """build-mapping 产物 vs 实际下载/章节树,检查挂错节、漏挂、重复。

    Args:
        mapping: CourseStructure.to_dict()(chapters: [{index, title, lessons: [{id, title, video, attachments}]}])
        manifest_or_tree: _resource_naming_manifest.json(records: [...]) 或 _chapter_tree.json
    """
    report = CourseAuditReport(
        course_title=mapping.get("course_title", ""),
        platform="chaoxing",
        generated_at=datetime.now().isoformat(timespec="seconds"),
    )

    # 收集 mapping 里出现的所有 saved_name + lesson_id → file
    mapping_lesson_to_files: dict[str, list[str]] = {}
    file_to_lessons: dict[str, list[str]] = {}
    for ch in mapping.get("chapters", []):
        ch_idx = int(ch.get("index") or 0)
        for ls in ch.get("lessons", []):
            ls_id = ls.get("id") or ""
            files: list[str] = []
            vid = ls.get("video")
            if vid:
                files.append(vid)
            for a in ls.get("attachments") or []:
                files.append(a)
            mapping_lesson_to_files[ls_id] = files
            for f in files:
                file_to_lessons.setdefault(f, []).append(f"{ch_idx}.{ls_id}")

    # 收集 manifest 里所有 file + 它的 lesson 信息
    manifest_files: dict[str, dict] = {}
    if "records" in manifest_or_tree:
        # _resource_naming_manifest.json 格式
        for r in manifest_or_tree.get("records", []):
            sn = r.get("saved_name") or ""
            if sn:
                manifest_files[sn] = r
    elif "chapters" in manifest_or_tree:
        # _chapter_tree.json 格式
        for ch in manifest_or_tree.get("chapters", []):
            for ls in ch.get("lessons", []):
                for r in ls.get("resources") or []:
                    sn = r.get("saved_name") or ""
                    if sn:
                        manifest_files[sn] = r

    # 1. missing_local_file
    for ls_id, files in mapping_lesson_to_files.items():
        for f in files:
            if f and f not in manifest_files:
                # 找对应的 lesson
                lesson_audit = _ensure_lesson(report, ls_id, mapping)
                ar = AuditedResource(
                    lesson_id=ls_id, lesson_title=lesson_audit.lesson_title,
                    saved_name=f, role="unknown", status="missing",
                    issues=["missing_local_file"],
                    suggestions=["建议重新下载该文件 / 检查文件名"],
                )
                lesson_audit.resources.append(ar)
                lesson_audit.issues.append("missing_local_file")
                _raise_risk(lesson_audit, "medium")

    # 2. unused_downloaded_resource
    used_files = set()
    for files in mapping_lesson_to_files.values():
        for f in files:
            if f:
                used_files.add(f)
    for sn in manifest_files:
        if sn and sn not in used_files:
            report.global_issues.append(f"下载了 '{sn[:40]}...' 但 mapping 没用上(可能是 orphan)")

    # 3. attachment_as_video / non_video_in_video_slot
    for ch in mapping.get("chapters", []):
        ch_idx = int(ch.get("index") or 0)
        for ls in ch.get("lessons", []):
            ls_id = ls.get("id") or ""
            vid = ls.get("video") or ""
            if not vid:
                continue
            ext = vid.rsplit(".", 1)[-1].lower() if "." in vid else ""
            if ext and ext not in ("mp4", "flv", "m3u8", "avi", "mkv", "mov"):
                # video 字段放了非视频扩展名
                lesson_audit = _ensure_lesson(report, ls_id, mapping)
                ar = AuditedResource(
                    lesson_id=ls_id, lesson_title=ls.get("title", ""),
                    saved_name=vid, role="unknown",
                    role_confidence=0.5,
                    issues=["non_video_in_video_slot"],
                    suggestions=["扩展名不是视频,可能挂错字段,建议改成 attachment"],
                    status="suspicious",
                )
                lesson_audit.resources.append(ar)
                lesson_audit.issues.append("non_video_in_video_slot")
                _raise_risk(lesson_audit, "medium")

            # attachment 字段里放 .mp4 也算 attachment_as_video(说明上传会卡)
            for a in ls.get("attachments") or []:
                a_ext = a.rsplit(".", 1)[-1].lower() if "." in a else ""
                if a_ext in ("mp4", "flv", "m3u8", "avi", "mkv", "mov"):
                    lesson_audit = _ensure_lesson(report, ls_id, mapping)
                    lesson_audit.issues.append("attachment_as_video")
                    lesson_audit.risk_level = "low" if lesson_audit.risk_level == "ok" else lesson_audit.risk_level
                    report.global_issues.append(
                        f"{ch_idx}.{ls_id} attachment 字段放了视频文件 {a[:40]}..."
                    )

    # 4. duplicate_file_use(同一文件被多个 lesson 引用)
    for f, lessons in file_to_lessons.items():
        if len(lessons) > 1:
            report.global_issues.append(
                f"文件 '{f[:40]}...' 被 {len(lessons)} 个 lesson 引用: {lessons}"
            )

    # 5. ppt-only lesson(informational)
    for ch in mapping.get("chapters", []):
        ch_idx = int(ch.get("index") or 0)
        for ls in ch.get("lessons", []):
            ls_id = ls.get("id") or ""
            if ls.get("video"):
                continue  # 有视频不算 ppt-only
            atts = ls.get("attachments") or []
            if atts:
                ppt_atts = [a for a in atts if a.lower().endswith((".ppt", ".pptx"))]
                if ppt_atts:
                    lesson_audit = _ensure_lesson(report, ls_id, mapping)
                    lesson_audit.issues.append("ppt_only_lesson_informational")
                    # 不 raise risk(informational)

    # 6. lesson_id 不匹配但标题高度相似 → 简化版:本章有 lesson 但 mapping 缺失
    mapping_lesson_ids = set()
    for ch in mapping.get("chapters", []):
        for ls in ch.get("lessons", []):
            mapping_lesson_ids.add(ls.get("id") or "")

    # 统计
    total_lessons = len(mapping_lesson_ids)
    missing_local = sum(
        1 for ls in report.lessons if "missing_local_file" in ls.issues
    )
    report.summary = {
        "mapping_lessons": total_lessons,
        "lessons_with_missing_local_file": missing_local,
        "global_issues_count": len(report.global_issues),
    }

    # recommendations
    if missing_local:
        report.recommendations.append(
            f"{missing_local} 个 lesson 引用了不存在的本地文件,需重新下载"
        )
    if any("attachment_as_video" in ls.issues for ls in report.lessons):
        report.recommendations.append(
            "检测到附件字段放了视频文件,建议改成 video 字段"
        )
    if report.global_issues and "duplicate_file_use" in str(report.global_issues):
        report.recommendations.append(
            "发现文件被多个 lesson 复用,请检查 mapping 是否重复挂载"
        )
    if not report.recommendations:
        report.recommendations.append("mapping 与本地文件对齐良好,可以上传")

    return report


def _ensure_lesson(report: CourseAuditReport, ls_id: str, mapping: dict) -> LessonAudit:
    """从 mapping 找 lesson_audit,找不到就新建"""
    for ls in report.lessons:
        if ls.lesson_id == ls_id:
            return ls
    # 从 mapping 找 title
    title = ""
    for ch in mapping.get("chapters", []):
        for ls in ch.get("lessons", []):
            if ls.get("id") == ls_id:
                title = ls.get("title", "")
                break
        if title:
            break
    la = LessonAudit(lesson_id=ls_id, lesson_title=title)
    report.lessons.append(la)
    return la


def _raise_risk(lesson_audit: LessonAudit, level: str) -> None:
    """升 lesson risk_level(只升不降)"""
    order = {"ok": 0, "low": 1, "medium": 2, "high": 3}
    if order[level] > order[lesson_audit.risk_level]:
        lesson_audit.risk_level = level


# ─── D:报告输出(JSON / Markdown / CSV) ──────────────────────

def write_resource_audit_reports(report: CourseAuditReport, output_dir: Path) -> dict[str, Path]:
    """写 _resource_audit.json / _resource_audit.md / _resource_audit.csv。"""
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    paths: dict[str, Path] = {}

    # JSON
    json_path = output_dir / "_resource_audit.json"
    json_path.write_text(report.to_json(), encoding="utf-8")
    paths["audit_json"] = json_path

    # CSV
    csv_path = output_dir / "_resource_audit.csv"
    with open(csv_path, "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow([
            "course_title", "platform", "lesson_id", "lesson_title",
            "resource_key", "saved_name", "role", "confidence",
            "status", "issues", "suggestions",
        ])
        for ls in report.lessons:
            for r in ls.resources:
                w.writerow([
                    report.course_title, report.platform,
                    ls.lesson_id, ls.lesson_title,
                    r.resource_key, r.saved_name, r.role,
                    r.role_confidence, r.status,
                    "; ".join(r.issues) if r.issues else "",
                    "; ".join(r.suggestions) if r.suggestions else "",
                ])
            # 没有资源的 lesson 也写一行(只填 lesson_id/title/status)
            if not ls.resources:
                w.writerow([
                    report.course_title, report.platform,
                    ls.lesson_id, ls.lesson_title,
                    "", "", "", "", "empty",
                    "; ".join(ls.issues) if ls.issues else "",
                    "",
                ])
    paths["audit_csv"] = csv_path

    # Markdown(人类可读)
    md_path = output_dir / "_resource_audit.md"
    md_path.write_text(_render_audit_md(report), encoding="utf-8")
    paths["audit_md"] = md_path
    return paths


def _render_audit_md(report: CourseAuditReport) -> str:
    """人类可读 Markdown 报告。"""
    lines: list[str] = []
    lines.append(f"# 资源智能审计报告 — {report.course_title or '(未填)'}")
    lines.append("")
    lines.append(f"- 平台: {report.platform or '(未填)'}")
    lines.append(f"- 生成时间: {report.generated_at}")
    lines.append("")

    # 总览
    lines.append("## 总览")
    lines.append("")
    lines.append("| 指标 | 值 |")
    lines.append("|---|---|")
    for k, v in report.summary.items():
        lines.append(f"| {k} | {v} |")
    lines.append("")

    # 风险分布
    risk_dist: dict[str, int] = {"ok": 0, "low": 0, "medium": 0, "high": 0}
    for ls in report.lessons:
        risk_dist[ls.risk_level] = risk_dist.get(ls.risk_level, 0) + 1
    lines.append("## 风险分布")
    lines.append("")
    for level in ("high", "medium", "low", "ok"):
        n = risk_dist.get(level, 0)
        if n:
            lines.append(f"- **{level.upper()}**: {n} 节")
    lines.append("")

    # 高风险列表(头部)
    high = [ls for ls in report.lessons if ls.risk_level == "high"]
    medium = [ls for ls in report.lessons if ls.risk_level == "medium"]
    if high:
        lines.append("## ⚠️ 高风险节")
        lines.append("")
        for ls in high:
            issues = ", ".join(ls.issues) if ls.issues else ""
            lines.append(f"- **ch{ls.ch_num}.{ls.lesson_id}** {ls.lesson_title} — {issues}")
        lines.append("")
    if medium:
        lines.append("## ⚡ 中风险节")
        lines.append("")
        for ls in medium:
            issues = ", ".join(ls.issues) if ls.issues else ""
            lines.append(f"- ch{ls.ch_num}.{ls.lesson_id} {ls.lesson_title} — {issues}")
        lines.append("")

    # 全局 issues
    if report.global_issues:
        lines.append("## 全局问题")
        lines.append("")
        for gi in report.global_issues:
            lines.append(f"- {gi}")
        lines.append("")

    # 建议
    if report.recommendations:
        lines.append("## 建议")
        lines.append("")
        for r in report.recommendations:
            lines.append(f"- {r}")
        lines.append("")

    # 资源明细(只列有 issue 或非 ok 状态的)
    flagged = [
        ls for ls in report.lessons
        if ls.issues or any(r.issues or r.role_confidence < CONF_MEDIUM for r in ls.resources)
    ]
    if flagged:
        lines.append("## 问题资源明细")
        lines.append("")
        for ls in flagged:
            lines.append(f"### ch{ls.ch_num}.{ls.lesson_id} {ls.lesson_title}")
            lines.append("")
            if ls.issues:
                lines.append(f"- lesson issues: {', '.join(ls.issues)}")
            for r in ls.resources:
                if not (r.issues or r.role_confidence < CONF_MEDIUM):
                    continue
                ev_summary = ", ".join(
                    f"{e.source}={e.key}" for e in r.role_evidence[:3]
                ) or "(无证据)"
                lines.append(f"  - `{r.saved_name or '(无文件名)'}` role=`{r.role}` "
                             f"conf={r.role_confidence} status=`{r.status}`")
                if r.issues:
                    lines.append(f"    - issues: {', '.join(r.issues)}")
                if r.suggestions:
                    lines.append(f"    - 建议: {' / '.join(r.suggestions)}")
                lines.append(f"    - 证据: {ev_summary}")
            lines.append("")

    return "\n".join(lines)