"""
资源智能审计测试(第二十轮)

覆盖:
  - 角色识别(classify_resource_role)5 个
  - 漏扫检测(audit_scan_completeness)5 个
  - 错配检测(audit_mapping_alignment)5 个
  - 报告输出(write_resource_audit_reports)1 个

所有测试用本地 fixture / 纯函数,0 网络。
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from scrape_new.services.resource_audit import (
    AuditedResource, CourseAuditReport, LessonAudit, ResourceEvidence,
    classify_resource_role,
    audit_scan_completeness, audit_mapping_alignment,
    write_resource_audit_reports,
)


# ─── 角色识别(1-5) ────────────────────────────────

class TestClassifyRole:
    """1-5:classify_resource_role 置信度 + 证据链"""

    def test_mp4_is_video_high_confidence(self):
        """1:.mp4 → video,confidence 高"""
        role, conf, evs = classify_resource_role(
            {"filename": "lesson1.mp4", "type": ".mp4"},
        )
        assert role == "video"
        assert conf >= 0.85
        # 至少要有 ext 证据
        assert any(e.source == "ext" for e in evs)

    def test_pptx_is_ppt_high_confidence(self):
        """2:.pptx → ppt,confidence 高"""
        role, conf, evs = classify_resource_role(
            {"filename": "课件.pptx", "type": ".pptx"},
        )
        assert role == "ppt"
        assert conf >= 0.85
        assert any(e.source == "ext" for e in evs)

    def test_english_mp4_suggests_upload_as_video(self):
        """3:English mp4 → english,suggestion/evidence 说明按 video 上传"""
        role, conf, evs = classify_resource_role(
            {"filename": "1.1_English Version.mp4", "type": ".mp4"},
            lesson={"title": "1.1 Lesson 1"},
        )
        # ext 是 mp4 → video,title_keyword 是 English → english
        # 冲突 → confidence 降低,role 选最高分(扩展名胜)
        assert role in ("video", "english")
        # 重要:evidence 应有 upload_as_video 提示(english 仍按 video 上传)
        notes_texts = " ".join(e.note for e in evs)
        assert "video" in notes_texts.lower() or "upload" in notes_texts.lower()

    def test_tab0_with_pptx_causes_role_conflict(self):
        """4:tab=0 + .pptx 冲突 → ppt 优先,加 issue"""
        role, conf, evs = classify_resource_role(
            {"filename": "课件.pptx", "type": ".pptx", "tab_num": 0},
        )
        # 扩展名 .pptx 强 → ppt
        assert role == "ppt"
        # tab=0 给 video 弱证据(0.3),但扩展名 0.5 胜出
        # 不一定冲突(ext 单源,但 tab 也加了)
        # 关键:不报错,role = ppt
        assert conf >= 0.4

    def test_unknown_extension_low_confidence(self):
        """5:unknown 扩展名 → unknown + low_confidence_role"""
        role, conf, evs = classify_resource_role(
            {"filename": "unknown.xyz", "type": ".xyz"},
        )
        assert role == "unknown"
        # 无证据 → confidence 0
        assert conf == 0.0
        # 此时不算"low_confidence_role" issue(因为没有任何来源)


# ─── 漏扫检测(6-10) ────────────────────────────────

class TestAuditScanCompleteness:
    """6-10:audit_scan_completeness"""

    def _chapter_tree(self) -> dict:
        return {
            "course_title": "测试课", "platform": "chaoxing",
            "chapters": [
                {"index": 1, "title": "ch1", "lessons": [
                    {"id": "1.1", "title": "L1"},
                    {"id": "1.2", "title": "L2"},
                ]},
                {"index": 2, "title": "ch2", "lessons": [
                    {"id": "2.1", "title": "L1"},
                ]},
            ],
        }

    def test_empty_lesson_marked(self):
        """6:chapter_tree 有 lesson,scanned_resources 空 → empty_lesson"""
        ct = self._chapter_tree()
        report = audit_scan_completeness(ct, [])
        empty_lessons = [ls for ls in report.lessons if "empty_lesson" in ls.issues]
        # 1.1 / 1.2 / 2.1 都空
        assert len(empty_lessons) == 3
        assert all(ls.risk_level == "low" for ls in empty_lessons)

    def test_empty_chapter_marked(self):
        """7:整章空 → empty_chapter(进 global_issues)"""
        ct = self._chapter_tree()
        report = audit_scan_completeness(ct, [])
        # 1.x 和 2.1 都空 → ch1 和 ch2 都是整章空
        assert any("第 1 章" in g and "没有资源" in g for g in report.global_issues)
        assert any("第 2 章" in g and "没有资源" in g for g in report.global_issues)

    def test_count_mismatch_marked(self):
        """8:expected count 5,found 2 → count_mismatch / possible_missing_resource"""
        ct = self._chapter_tree()
        # 1.1 期望 5,实际给 2
        scanned = [
            {"ch_num": 1, "ls_num": 1, "type": ".mp4", "name": "1.mp4", "tab_num": 0,
             "objectid": "o1", "saved_name": "1.mp4", "size_bytes": 1000, "status": "found"},
            {"ch_num": 1, "ls_num": 1, "type": ".mp4", "name": "2.mp4", "tab_num": 0,
             "objectid": "o2", "saved_name": "2.mp4", "size_bytes": 1000, "status": "found"},
        ]
        # 给 expected_resource_count
        ct["chapters"][0]["lessons"][0]["expected_resource_count"] = 5
        report = audit_scan_completeness(ct, scanned)
        ls11 = next(ls for ls in report.lessons if ls.lesson_id == "1.1")
        assert "count_mismatch" in ls11.issues
        assert "possible_missing_resource" in ls11.issues
        assert ls11.risk_level == "medium"

    def test_duplicate_objectid_marked(self):
        """9:duplicate objectid 出现在多 lesson → global_issues"""
        ct = self._chapter_tree()
        scanned = [
            {"ch_num": 1, "ls_num": 1, "type": ".mp4", "name": "1.mp4",
             "objectid": "shared_oid", "saved_name": "1.mp4", "status": "found"},
            {"ch_num": 2, "ls_num": 1, "type": ".mp4", "name": "1b.mp4",
             "objectid": "shared_oid", "saved_name": "1b.mp4", "status": "found"},
        ]
        report = audit_scan_completeness(ct, scanned)
        assert any("shared_oid" in g for g in report.global_issues)

    def test_duplicate_saved_name_marked(self):
        """10:duplicate saved_name → global_issues"""
        ct = self._chapter_tree()
        scanned = [
            {"ch_num": 1, "ls_num": 1, "type": ".mp4", "name": "v.mp4",
             "objectid": "o1", "saved_name": "v.mp4", "status": "found"},
            {"ch_num": 1, "ls_num": 2, "type": ".mp4", "name": "v.mp4",
             "objectid": "o2", "saved_name": "v.mp4", "status": "found"},
        ]
        report = audit_scan_completeness(ct, scanned)
        assert any("v.mp4" in g and "重复" in g for g in report.global_issues)


# ─── 错配检测(11-15) ────────────────────────────────

class TestAuditMappingAlignment:
    """11-15:audit_mapping_alignment"""

    def _mapping(self) -> dict:
        return {
            "course_title": "测试课", "chapters": [
                {"index": 1, "title": "ch1", "lessons": [
                    {"id": "1.1", "title": "L1", "video": "1.1_课件.pptx", "attachments": []},
                ]},
                {"index": 2, "title": "ch2", "lessons": [
                    {"id": "2.1", "title": "L1", "video": "2.1.mp4",
                     "attachments": ["2.1_extra.pdf"]},
                ]},
            ],
        }

    def _manifest(self) -> dict:
        return {"records": [
            {"saved_name": "2.1.mp4", "status": "downloaded", "role": "video"},
            {"saved_name": "2.1_extra.pdf", "status": "downloaded", "role": "pdf"},
            {"saved_name": "orphan.mp4", "status": "downloaded", "role": "video"},
        ]}

    def test_pptx_in_video_field_marked(self):
        """11:video_filename 是 .pptx → non_video_in_video_slot"""
        m = self._mapping()
        report = audit_mapping_alignment(m, self._manifest())
        ls11 = next(ls for ls in report.lessons if ls.lesson_id == "1.1")
        assert "non_video_in_video_slot" in ls11.issues
        assert any("non_video_in_video_slot" in r.issues for r in ls11.resources)

    def test_attachment_field_holds_video_marked(self):
        """12:attachments 字段放视频 → attachment_as_video"""
        m = self._mapping()
        # 2.1 的 attachments 放 "2.1_extra.pdf"(其实不是视频)— 跳过
        # 改 2.1 attachments 放视频扩展名
        m["chapters"][1]["lessons"][0]["attachments"] = ["2.1_extra.mp4"]
        report = audit_mapping_alignment(m, self._manifest())
        # global_issues 应该有 attachment 放视频的提示
        assert any("attachment" in g.lower() and "2.1" in g for g in report.global_issues)

    def test_unused_downloaded_resource_marked(self):
        """13:manifest 里有 orphan.mp4 但 mapping 没用 → global_issues"""
        m = self._mapping()
        report = audit_mapping_alignment(m, self._manifest())
        # orphan.mp4 没被 mapping 引用
        assert any("orphan.mp4" in g and ("没用" in g or "unused" in g.lower()) for g in report.global_issues)

    def test_duplicate_file_use_marked(self):
        """14:同一文件被两个 lesson 引用 → duplicate_file_use"""
        m = self._mapping()
        # 两个 lesson 共用 2.1.mp4
        m["chapters"][1]["lessons"].append(
            {"id": "2.2", "title": "L2", "video": "2.1.mp4", "attachments": []}
        )
        report = audit_mapping_alignment(m, self._manifest())
        assert any("2.1.mp4" in g and "2" in g for g in report.global_issues)

    def test_ppt_only_lesson_marked_informational(self):
        """15:ppt-only lesson 标 informational issue,但 risk 不升到 high"""
        m = self._mapping()
        # 1.1 只有 video 字段,但没 manifest 找 → 当做 missing_local_file
        # 改:1.1 video = "课件.pdf",manifest 找得到
        m["chapters"][0]["lessons"][0]["video"] = "课件.pdf"
        manifest = {"records": [
            {"saved_name": "课件.pdf", "status": "downloaded", "role": "pdf"},
        ]}
        # 加一个真正 ppt-only lesson(没 video 字段,只有 attachment 是 .pptx)
        m["chapters"][0]["lessons"].append(
            {"id": "1.2", "title": "L2", "video": None, "attachments": ["课件1.2.pptx"]}
        )
        manifest["records"].append(
            {"saved_name": "课件1.2.pptx", "status": "downloaded", "role": "ppt"}
        )
        report = audit_mapping_alignment(m, manifest)
        ls12 = next(ls for ls in report.lessons if ls.lesson_id == "1.2")
        # informational 标在 issues
        assert "ppt_only_lesson_informational" in ls12.issues
        # 但 risk 还是 low / ok,不升 high
        assert ls12.risk_level in ("ok", "low")


# ─── 报告输出(16) ────────────────────────────────

class TestReportOutput:
    """16:write_resource_audit_reports 写 3 份文件,MD 含关键关键词"""

    def test_writes_json_md_csv_with_keywords(self, tmp_path: Path):
        """16:写 _resource_audit.{json,md,csv},MD 含"需要人工确认"或"可能漏扫" """
        # 构造一个能产生 high_risk 的 chapter_tree + scanned
        ct = {
            "course_title": "审计测试课", "platform": "chaoxing",
            "chapters": [{"index": 1, "title": "ch1", "lessons": [
                {"id": "1.1", "title": "L1", "expected_resource_count": 3},
            ]}],
        }
        scanned = [
            # 只给 1 个,但期望 3
            {"ch_num": 1, "ls_num": 1, "type": ".xyz", "name": "u.xyz",
             "objectid": "o1", "saved_name": "u.xyz", "size_bytes": 100, "status": "found"},
        ]
        report = audit_scan_completeness(ct, scanned)
        paths = write_resource_audit_reports(report, tmp_path)

        # 三份文件都写
        assert (tmp_path / "_resource_audit.json").exists()
        assert (tmp_path / "_resource_audit.md").exists()
        assert (tmp_path / "_resource_audit.csv").exists()

        # MD 含关键关键词
        md = paths["audit_md"].read_text(encoding="utf-8")
        # count_mismatch + low_confidence 至少出现一个
        assert ("可能漏扫" in md) or ("需要人工确认" in md) or ("count_mismatch" in md)

        # JSON 能 load
        data = json.loads(paths["audit_json"].read_text(encoding="utf-8"))
        assert data["course_title"] == "审计测试课"
        assert len(data["lessons"]) >= 1

        # CSV 第一行是 header
        csv_text = paths["audit_csv"].read_text(encoding="utf-8")
        first_line = csv_text.split("\n")[0]
        assert "course_title" in first_line
        assert "lesson_id" in first_line
        assert "role" in first_line