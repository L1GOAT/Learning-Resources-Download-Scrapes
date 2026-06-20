"""
测试:scrape_new.upload.preflight

覆盖(8 测试):
  1. 数量对比:mapping vs 后台
  2. RENAME 章待确认清单
  3. 风险等级 LOW / MEDIUM / HIGH
  4. 缺资源统计(每节漏 English / PPT)
  5. 报告文本格式(可打印)
  6. JSON to_dict 机器友好
  7. 落盘 _preflight_report.txt
  8. only_chapters 隔离
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from scrape_new.upload.models import (
    Chapter, ContentType, CourseStructure, Lesson,
)
from scrape_new.upload.preflight import (
    RiskLevel,
    build_preflight,
    format_preflight_text,
    write_preflight_text,
    _calc_risk_level,
    _collect_renames,
    _collect_missing_resources,
)


def _make_structure(chapters_data: list[dict]) -> CourseStructure:
    chapters = tuple(
        Chapter(
            index=cd["index"],
            title=cd["title"],
            lessons=tuple(
                Lesson(
                    id=ls["id"],
                    title=ls["title"],
                    content_type=ContentType.VIDEO,
                    video=ls.get("video"),
                    attachments=tuple(ls.get("attachments", [])),
                )
                for ls in cd["lessons"]
            ),
        )
        for cd in chapters_data
    )
    return CourseStructure(
        course_id="test-course", course_title="测试课", chapters=chapters,
    )


def _empty_tree() -> dict:
    return {"chapter_list": []}


# ─── 1) 数量对比 ──────────────────────────────────────────

class TestCountDelta:
    def test_mapping_vs_actual(self):
        structure = _make_structure([
            {"index": 1, "title": "第一章", "lessons": [
                {"id": "1.1", "title": "a", "video": "1.1_a.mp4",
                 "attachments": ["1.1_a_English.mp4", "1.1_a_PPT.pptx"]},
                {"id": "1.2", "title": "b", "video": "1.2_b.mp4",
                 "attachments": ["1.2_b_English.mp4", "1.2_b_PPT.pptx"]},
            ]},
        ])
        # 后台已有 1 个 chapter 1 个 section 1 个 leaf
        tree = {
            "chapter_list": [{
                "id": 100, "name": "第一章", "index": 1,
                "section_list": [{
                    "id": 200, "name": "a", "leaf_list": [
                        {"id": 300, "name": "1.1_a.mp4",
                         "content_info": {"media": {"name": "1.1_a.mp4"}}},
                    ],
                }],
            }],
        }
        report = build_preflight(structure, tree)
        c = report.counts
        # mapping: 1 章, 2 节, 6 leaves (2 lessons × 3)
        assert c.mapping_chapters == 1
        assert c.mapping_lessons == 2
        assert c.mapping_leaves == 6
        # 后台: 1 章, 1 节, 1 leaf
        assert c.actual_chapters == 1
        assert c.actual_lessons == 1
        assert c.actual_leaves == 1
        # delta
        assert c.chapter_delta == 0
        assert c.lesson_delta == 1
        assert c.leaf_delta == 5


# ─── 2) RENAME 待确认清单 ────────────────────────────────

class TestRenameList:
    def test_rename_detected(self):
        structure = _make_structure([
            {"index": 1, "title": "第一章 概述", "lessons": [
                {"id": "1.1", "title": "技术", "video": "1.1_技术.mp4"},
            ]},
        ])
        # 后台 ch1 标题不同
        tree = {
            "chapter_list": [
                {"id": 100, "name": "第一章 旧标题", "index": 1, "section_list": []},
            ]
        }
        report = build_preflight(structure, tree)
        assert report.rename_count == 1
        entry = report.rename_entries[0]
        assert entry.chapter_index == 1
        assert entry.desired_title == "第一章 概述"
        assert entry.actual_title == "第一章 旧标题"
        assert entry.actual_id == 100

    def test_no_rename_when_match(self):
        structure = _make_structure([
            {"index": 1, "title": "第一章 概述", "lessons": [
                {"id": "1.1", "title": "技术", "video": "1.1_技术.mp4"},
            ]},
        ])
        tree = {
            "chapter_list": [
                {"id": 100, "name": "第一章 概述", "index": 1, "section_list": []},
            ]
        }
        report = build_preflight(structure, tree)
        assert report.rename_count == 0
        assert report.rename_entries == ()


# ─── 3) 风险等级 ──────────────────────────────────────────

class TestRiskLevel:
    def test_risk_high_when_drift_over_threshold(self):
        # drift 100% → HIGH
        structure = _make_structure([
            {"index": 1, "title": "第一章", "lessons": [
                {"id": "1.1", "title": "a", "video": "1.1_a.mp4"},
            ]},
        ])
        report = build_preflight(structure, _empty_tree())
        assert report.risk_level == RiskLevel.HIGH

    def test_risk_medium_when_drift_30_60(self):
        # 部分建好,drift 50%
        structure = _make_structure([
            {"index": 1, "title": "第一章", "lessons": [
                {"id": "1.1", "title": "a", "video": "1.1_a.mp4",
                 "attachments": ["1.1_a_English.mp4", "1.1_a_PPT.pptx"]},
                {"id": "1.2", "title": "b", "video": "1.2_b.mp4",
                 "attachments": ["1.2_b_English.mp4", "1.2_b_PPT.pptx"]},
            ]},
        ])
        # 后台已有 1.1 完整
        tree = {
            "chapter_list": [{
                "id": 100, "name": "第一章", "index": 1,
                "section_list": [{
                    "id": 200, "name": "a", "leaf_list": [
                        {"id": 301, "name": "1.1_a.mp4",
                         "content_info": {"media": {"name": "1.1_a.mp4"}}},
                        {"id": 302, "name": "1.1_a_English.mp4",
                         "content_info": {"media": {"name": "1.1_a_English.mp4"}}},
                        {"id": 303, "name": "1.1_a_PPT.pptx",
                         "content_info": {"download": [{"file_name": "1.1_a_PPT.pptx"}]}},
                    ],
                }],
            }],
        }
        report = build_preflight(structure, tree)
        # 1.1 全 SKIP,1.2 全 CREATE → drift(leaf 级)= 3/(3+3) = 50%
        assert 0.3 <= report.drift_ratio < 0.6
        assert report.risk_level == RiskLevel.MEDIUM

    def test_risk_low_when_match(self):
        structure = _make_structure([
            {"index": 1, "title": "第一章", "lessons": [
                {"id": "1.1", "title": "a", "video": "1.1_a.mp4"},
            ]},
        ])
        tree = {
            "chapter_list": [{
                "id": 100, "name": "第一章", "index": 1,
                "section_list": [{
                    "id": 200, "name": "a", "leaf_list": [
                        {"id": 300, "name": "1.1_a.mp4",
                         "content_info": {"media": {"name": "1.1_a.mp4"}}},
                    ],
                }],
            }],
        }
        report = build_preflight(structure, tree)
        assert report.risk_level == RiskLevel.LOW

    def test_risk_medium_when_rename_only(self):
        # drift 0,但有 1 个 RENAME → MEDIUM
        structure = _make_structure([
            {"index": 1, "title": "第一章 概述", "lessons": [
                {"id": "1.1", "title": "技术", "video": "1.1_技术.mp4"},
            ]},
        ])
        tree = {
            "chapter_list": [{
                "id": 100, "name": "第一章 旧标题", "index": 1,
                "section_list": [{
                    "id": 200, "name": "技术", "leaf_list": [
                        {"id": 300, "name": "1.1_技术.mp4",
                         "content_info": {"media": {"name": "1.1_技术.mp4"}}},
                    ],
                }],
            }],
        }
        report = build_preflight(structure, tree)
        assert report.risk_level == RiskLevel.MEDIUM  # rename > 0
        assert report.rename_count == 1


# ─── 4) 缺资源统计 ──────────────────────────────────────────

class TestMissingResources:
    def test_missing_english(self):
        # 没有 English 视频的节
        structure = _make_structure([
            {"index": 1, "title": "第一章", "lessons": [
                {"id": "1.1", "title": "技术",
                 "video": "1.1_技术.mp4",
                 "attachments": ["1.1_技术_PPT.pptx"]},  # 无 English
            ]},
        ])
        report = build_preflight(structure, _empty_tree())
        assert len(report.missing_resources) == 1
        m = report.missing_resources[0]
        assert "english" in m.missing_roles
        assert "ppt" not in m.missing_roles
        assert "video" not in m.missing_roles

    def test_missing_ppt(self):
        structure = _make_structure([
            {"index": 1, "title": "第一章", "lessons": [
                {"id": "1.1", "title": "技术",
                 "video": "1.1_技术.mp4",
                 "attachments": ["1.1_技术_English.mp4"]},  # 无 PPT
            ]},
        ])
        report = build_preflight(structure, _empty_tree())
        assert len(report.missing_resources) == 1
        assert "ppt" in report.missing_resources[0].missing_roles

    def test_missing_video(self):
        structure = _make_structure([
            {"index": 1, "title": "第一章", "lessons": [
                {"id": "1.1", "title": "技术",
                 "attachments": ["1.1_技术_English.mp4", "1.1_技术_PPT.pptx"]},
                # 无 video
            ]},
        ])
        report = build_preflight(structure, _empty_tree())
        m = [x for x in report.missing_resources if "video" in x.missing_roles]
        assert len(m) == 1

    def test_complete_lesson_no_missing(self):
        structure = _make_structure([
            {"index": 1, "title": "第一章", "lessons": [
                {"id": "1.1", "title": "技术",
                 "video": "1.1_技术.mp4",
                 "attachments": ["1.1_技术_English.mp4", "1.1_技术_PPT.pptx"]},
            ]},
        ])
        report = build_preflight(structure, _empty_tree())
        assert report.missing_resources == ()


# ─── 5) 报告文本格式 ──────────────────────────────────────────

class TestTextReport:
    def test_text_contains_required_sections(self):
        structure = _make_structure([
            {"index": 1, "title": "第一章", "lessons": [
                {"id": "1.1", "title": "技术",
                 "video": "1.1_技术.mp4",
                 "attachments": ["1.1_技术_English.mp4", "1.1_技术_PPT.pptx"]},
            ]},
        ])
        report = build_preflight(structure, _empty_tree())
        text = format_preflight_text(report)
        # 4 个段必须出现
        assert "课程体检报告" in text
        assert "数量对比" in text
        assert "计划" in text
        assert "建议" in text
        # 风险等级
        assert "HIGH" in text
        # 计数
        assert "1" in text

    def test_text_contains_missing_section(self):
        structure = _make_structure([
            {"index": 1, "title": "第一章", "lessons": [
                {"id": "1.1", "title": "技术", "video": "1.1_技术.mp4"},
                # 无 English,无 PPT
            ]},
        ])
        report = build_preflight(structure, _empty_tree())
        text = format_preflight_text(report)
        assert "缺资源" in text
        assert "english" in text


# ─── 6) to_dict ──────────────────────────────────────────

class TestToDict:
    def test_to_dict_structure(self):
        structure = _make_structure([
            {"index": 1, "title": "第一章", "lessons": [
                {"id": "1.1", "title": "技术", "video": "1.1_技术.mp4"},
            ]},
        ])
        report = build_preflight(structure, _empty_tree())
        d = report.to_dict()
        # 机器友好结构
        assert d["course_id"] == "test-course"
        assert d["course_title"] == "测试课"
        assert d["counts"]["mapping"]["chapters"] == 1
        assert d["counts"]["actual"]["chapters"] == 0
        assert d["counts"]["delta"]["chapters"] == 1
        assert d["risk_level"] == "HIGH"
        assert 0 <= d["drift_ratio"] <= 1
        assert "create_chapters" in d["plan"]
        assert "create_leaves" in d["plan"]
        # 可 JSON 序列化
        json.dumps(d, ensure_ascii=False)


# ─── 7) 写文件 ──────────────────────────────────────────

class TestWriteFile:
    def test_write_creates_file(self, tmp_path: Path):
        structure = _make_structure([
            {"index": 1, "title": "第一章", "lessons": [
                {"id": "1.1", "title": "技术", "video": "1.1_技术.mp4"},
            ]},
        ])
        report = build_preflight(structure, _empty_tree())
        path = write_preflight_text(report, tmp_path)
        assert path.exists()
        assert path.name == "_preflight_report.txt"
        assert "课程体检报告" in path.read_text(encoding="utf-8")


# ─── 8) only_chapters 隔离 ────────────────────────────────

class TestOnlyChaptersFilter:
    def test_only_chapters_excludes_other_chapters(self):
        structure = _make_structure([
            {"index": 1, "title": "第一章", "lessons": [
                {"id": "1.1", "title": "a", "video": "1.1_a.mp4"},
            ]},
            {"index": 2, "title": "第二章", "lessons": [
                {"id": "2.1", "title": "b", "video": "2.1_b.mp4"},
            ]},
        ])
        # 只看 ch1,ch2 视为"已建"
        tree = {
            "chapter_list": [
                {"id": 100, "name": "第二章", "index": 2, "section_list": []},
            ]
        }
        report = build_preflight(
            structure, tree, only_chapters={1},
        )
        # 数量对比是全 mapping(mapping 数据永远完整)
        # 但 stats / drift 是 only_chapters 后的
        # ch1 全 CREATE,ch2 被过滤
        assert report.stats["create_chapters"] == 1
        # counts.actual 还是全 tree 数(2 章里有 1 个)— 因为 only_chapters 只影响 stats
        # 实际报告里 counts 给的是全量,这是有意为之(用户看全局)