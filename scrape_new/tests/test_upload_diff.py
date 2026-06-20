"""
测试:compute_diff + write_backup_snapshot

覆盖:
  - 空真实树 → 全 CREATE
  - 真实树全匹配 → 全 SKIP
  - 多余章(默认 SKIP,prune=True 标 PRUNE)
  - 改名 RENAME
  - 差异阈值 60% → is_too_drifted True
  - 叶子匹配(主视频 + 英文视频 + PPT 同一个 section)
  - only_chapters 隔离(过滤掉的章不计入 diff)
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from scrape_new.upload.models import (
    Chapter, ContentType, CourseStructure, Lesson,
)
from scrape_new.upload.sync_tree import (
    compute_diff,
    write_backup_snapshot,
    DiffAction,
)


def _make_structure(chapters_data: list[dict]) -> CourseStructure:
    """辅助:从 dict 列表构造 CourseStructure"""
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


class TestComputeDiffEmpty:
    def test_empty_real_tree_all_create(self):
        """真实树空 → 所有章/节/leaf 都是 CREATE(章级 create 1 次,叶子级 create 多次)"""
        structure = _make_structure([
            {"index": 1, "title": "第一章 概述", "lessons": [
                {"id": "1.1", "title": "技术", "video": "1.1_技术.mp4"},
            ]},
        ])
        diff = compute_diff(structure, {"chapter_list": []})
        # chapter-level: 1 create; 加上 lesson+leaf 全部 CREATE
        assert diff.stats["create"] >= 1
        assert diff.stats["skip"] == 0
        assert diff.chapters[0].action == DiffAction.CREATE
        # 课时也是 CREATE
        assert diff.chapters[0].lesson_diffs[0].action == DiffAction.CREATE


class TestComputeDiffAllSkip:
    def test_full_match_all_skip(self):
        """真实树 100% 匹配 → 全 SKIP,drift = 0"""
        structure = _make_structure([
            {"index": 1, "title": "第一章 概述", "lessons": [
                {"id": "1.1", "title": "技术", "video": "1.1_技术.mp4"},
            ]},
        ])
        tree = {
            "chapter_list": [{
                "id": 100, "name": "第一章 概述", "index": 1,
                "section_list": [{
                    "id": 200, "name": "技术",
                    "leaf_list": [{
                        "id": 300, "name": "1.1_技术.mp4",
                        "content_info": {"media": {"name": "1.1_技术.mp4"}},
                    }],
                }],
            }],
        }
        diff = compute_diff(structure, tree)
        # chapter skip + section skip + leaf skip = 至少 2 skip(章+叶)
        assert diff.stats["skip"] >= 2
        assert diff.stats["create"] == 0
        assert not diff.is_too_drifted()


class TestComputeDiffExtra:
    def test_extra_chapter_default_skip(self):
        """真实树多 1 章,mapping 没,默认 SKIP(prune=False)"""
        structure = _make_structure([
            {"index": 1, "title": "第一章 概述", "lessons": [
                {"id": "1.1", "title": "技术", "video": "1.1_技术.mp4"},
            ]},
        ])
        tree = {
            "chapter_list": [
                {"id": 100, "name": "第一章 概述", "index": 1,
                 "section_list": []},
                {"id": 999, "name": "多余章", "index": 99,
                 "section_list": []},
            ],
        }
        diff = compute_diff(structure, tree)
        # 多余章不计入 prune,因为 prune=False
        assert diff.stats["prune"] == 0
        # 但会以 action=PRUNE 的 ChapterDiff 形式出现,方便报告
        prune_diffs = [cd for cd in diff.chapters if cd.action == DiffAction.PRUNE]
        assert len(prune_diffs) == 1
        assert prune_diffs[0].actual_id == 999

    def test_extra_chapter_with_prune_flag(self):
        """prune=True 时,多余章 → stats['prune'] = 1"""
        structure = _make_structure([
            {"index": 1, "title": "第一章 概述", "lessons": [
                {"id": "1.1", "title": "技术", "video": "1.1_技术.mp4"},
            ]},
        ])
        tree = {
            "chapter_list": [
                {"id": 100, "name": "第一章 概述", "index": 1, "section_list": []},
                {"id": 999, "name": "多余章", "index": 99, "section_list": []},
            ],
        }
        diff = compute_diff(structure, tree, prune=True)
        assert diff.stats["prune"] == 1
        assert diff.extra_chapter_ids == (999,)


class TestComputeDiffRename:
    def test_rename_detected(self):
        """章 title 不一致 → RENAME"""
        structure = _make_structure([
            {"index": 1, "title": "第一章 免疫学概述", "lessons": [
                {"id": "1.1", "title": "技术", "video": "1.1_技术.mp4"},
            ]},
        ])
        tree = {
            "chapter_list": [{
                "id": 100, "name": "第一章 免疫学概论", "index": 1,
                "section_list": [],
            }],
        }
        diff = compute_diff(structure, tree)
        assert diff.chapters[0].action == DiffAction.RENAME
        assert diff.stats["rename"] == 1


class TestComputeDiffDrift:
    def test_drift_over_threshold(self):
        """drift > 60% → is_too_drifted True"""
        # 真实树空,mapping 1 章 1 节 1 leaf
        structure = _make_structure([
            {"index": 1, "title": "第一章 概述", "lessons": [
                {"id": "1.1", "title": "技术", "video": "1.1_技术.mp4"},
            ]},
        ])
        diff = compute_diff(structure, {"chapter_list": []})
        # total_planned = 1 (chapter), skip = 0 → drift = 1/(1+0) = 100%
        assert diff.is_too_drifted(0.6) is True
        assert diff.is_too_drifted(0.99) is True
        # 100% drift → 仍然报 too_drifted (>= 比较)
        assert diff.is_too_drifted(1.0) is True

    def test_drift_under_threshold(self):
        """drift < 60% → 不报 too drifted"""
        # 真实树有 1 章,mapping 2 章 → 1 create, 1 skip = 50% drift
        structure = _make_structure([
            {"index": 1, "title": "第一章 概述", "lessons": [
                {"id": "1.1", "title": "技术", "video": "1.1_技术.mp4"},
            ]},
            {"index": 2, "title": "第二章 应用", "lessons": [
                {"id": "2.1", "title": "案例", "video": "2.1_案例.mp4"},
            ]},
        ])
        tree = {
            "chapter_list": [{
                "id": 100, "name": "第一章 概述", "index": 1,
                "section_list": [{
                    "id": 200, "name": "技术",
                    "leaf_list": [{
                        "id": 300, "name": "1.1_技术.mp4",
                        "content_info": {"media": {"name": "1.1_技术.mp4"}},
                    }],
                }],
            }],
        }
        diff = compute_diff(structure, tree)
        # skip = 3 (chapter+section+leaf), create = 3 (chapter+section+leaf)
        # drift = 3 / (3+3) = 50% < 60%
        assert not diff.is_too_drifted(0.6)


class TestComputeDiffMultiLeaves:
    def test_one_section_multiple_leaves(self):
        """同一节内有主视频+英文视频+PPT 三个 leaf,全部 SKIP(都已存在)"""
        structure = _make_structure([
            {"index": 1, "title": "第一章 概述", "lessons": [
                {
                    "id": "1.1", "title": "技术",
                    "video": "1.1_技术.mp4",
                    "attachments": [
                        "1.1_技术_English.mp4",
                        "1.1_技术_PPT.pptx",
                    ],
                },
            ]},
        ])
        tree = {
            "chapter_list": [{
                "id": 100, "name": "第一章 概述", "index": 1,
                "section_list": [{
                    "id": 200, "name": "技术",
                    "leaf_list": [
                        {"id": 301, "name": "1.1_技术.mp4",
                         "content_info": {"media": {"name": "1.1_技术.mp4"}}},
                        {"id": 302, "name": "1.1_技术_English.mp4",
                         "content_info": {"media": {"name": "1.1_技术_English.mp4"}}},
                        {"id": 303, "name": "1.1_技术_PPT.pptx",
                         "content_info": {"download": [
                             {"file_name": "1.1_技术_PPT.pptx"}
                         ]}},
                    ],
                }],
            }],
        }
        diff = compute_diff(structure, tree)
        leaves = diff.chapters[0].lesson_diffs[0].leaf_diffs
        assert len(leaves) == 3
        assert all(l.action == DiffAction.SKIP for l in leaves)

    def test_partial_leaves_to_create(self):
        """section 已存在,但缺英文视频/PPT → leaf CREATE"""
        structure = _make_structure([
            {"index": 1, "title": "第一章 概述", "lessons": [
                {
                    "id": "1.1", "title": "技术",
                    "video": "1.1_技术.mp4",
                    "attachments": [
                        "1.1_技术_English.mp4",
                        "1.1_技术_PPT.pptx",
                    ],
                },
            ]},
        ])
        tree = {
            "chapter_list": [{
                "id": 100, "name": "第一章 概述", "index": 1,
                "section_list": [{
                    "id": 200, "name": "技术",
                    "leaf_list": [
                        {"id": 301, "name": "1.1_技术.mp4",
                         "content_info": {"media": {"name": "1.1_技术.mp4"}}},
                        # 缺 English 和 PPT
                    ],
                }],
            }],
        }
        diff = compute_diff(structure, tree)
        # chapter skip, section skip, video skip, English CREATE, PPT CREATE
        assert diff.chapters[0].action == DiffAction.SKIP
        ld = diff.chapters[0].lesson_diffs[0]
        assert ld.action == DiffAction.SKIP
        leaves = ld.leaf_diffs
        assert len(leaves) == 3
        actions = [l.action for l in leaves]
        kinds = [l.kind for l in leaves]
        assert actions.count(DiffAction.SKIP) == 1
        assert actions.count(DiffAction.CREATE) == 2
        # English 和 PPT 是 CREATE
        assert "english" in kinds
        assert "ppt" in kinds


class TestComputeDiffOnlyChapters:
    def test_only_chapters_filter(self):
        """only_chapters 过滤掉的章不计入 diff(即使真实树里有)"""
        structure = _make_structure([
            {"index": 1, "title": "第一章", "lessons": []},
            {"index": 2, "title": "第二章", "lessons": []},
            {"index": 3, "title": "第三章", "lessons": []},
        ])
        tree = {
            "chapter_list": [
                {"id": 100, "name": "第一章", "index": 1, "section_list": []},
                {"id": 200, "name": "第二章", "index": 2, "section_list": []},
                {"id": 300, "name": "第三章", "index": 3, "section_list": []},
            ],
        }
        diff = compute_diff(structure, tree, only_chapters={1, 3})
        # 只考虑 ch1, ch3
        assert len(diff.chapters) == 2
        indices = {cd.index for cd in diff.chapters}
        assert indices == {1, 3}


class TestWriteBackupSnapshot:
    def test_backup_creates_file(self, tmp_path: Path):
        tree = {
            "msg": "", "success": True,
            "data": {"chapter_list": [{"id": 1, "name": "test"}]},
        }
        path = write_backup_snapshot(tree, tmp_path, course_id="12345")
        assert path.exists()
        assert path.name.startswith("_resource_tree_backup_12345_")
        # 文件内容 JSON 可读
        data = json.loads(path.read_text(encoding="utf-8"))
        assert data["data"]["chapter_list"][0]["name"] == "test"


class TestCreateStatsBreakdown:
    """P2:stats 拆 create_chapters / create_sections / create_leaves,
    整章缺失时能精确统计实际改动量"""

    def test_full_chapter_create_counts_leaves(self):
        """19 节 × 3 leaves 整章缺失 → create_chapters=7, create_sections=19,
        create_leaves=57,老字段 create 仍 = 7(向后兼容)"""
        structure = _make_structure([
            {"index": 1, "title": "第一章 概述", "lessons": [
                {"id": "1.1", "title": "技术", "video": "1.1_技术.mp4",
                 "attachments": ["1.1_技术_English.mp4", "1.1_技术_PPT.pptx"]},
                {"id": "1.2", "title": "媒体", "video": "1.2_媒体.mp4",
                 "attachments": ["1.2_媒体_English.mp4", "1.2_媒体_PPT.pptx"]},
                {"id": "1.3", "title": "学习", "video": "1.3_学习.mp4",
                 "attachments": ["1.3_学习_English.mp4", "1.3_学习_PPT.pptx"]},
            ]},
            {"index": 2, "title": "第二章 应用", "lessons": [
                {"id": "2.1", "title": "案例", "video": "2.1_案例.mp4",
                 "attachments": ["2.1_案例_English.mp4", "2.1_案例_PPT.pptx"]},
            ]},
        ])
        diff = compute_diff(structure, {"chapter_list": []})
        # 2 章, 4 lessons, 12 leaves (4 lessons × 3 leaves)
        assert diff.stats["create_chapters"] == 2
        assert diff.stats["create_sections"] == 4  # 3 + 1
        assert diff.stats["create_leaves"] == 12   # 4 lessons × 3 leaves
        # 老字段向后兼容:仍 = create_chapters
        assert diff.stats["create"] == 2
        # total_planned 仍用老 create,够用
        assert diff.total_planned() == 2

    def test_partial_chapter_create_section_only(self):
        """章已建但缺 1 个 section → 只 +1 section, +N leaves,不 +chapter"""
        structure = _make_structure([
            {"index": 1, "title": "第一章 概述", "lessons": [
                {"id": "1.1", "title": "技术", "video": "1.1_技术.mp4"},
                {"id": "1.2", "title": "媒体", "video": "1.2_媒体.mp4",
                 "attachments": ["1.2_媒体_PPT.pptx"]},
            ]},
        ])
        # 真实树:ch1 已有,sections 含 1.1 但缺 1.2
        tree = {
            "chapter_list": [{
                "id": 100, "name": "第一章 概述", "index": 1,
                "section_list": [
                    {"id": 200, "name": "技术", "leaf_list": [
                        {"id": 300, "name": "1.1_技术.mp4",
                         "content_info": {"media": {"name": "1.1_技术.mp4"}}}
                    ]},
                ],
            }],
        }
        diff = compute_diff(structure, tree)
        # ch1 已有 → create_chapters = 0
        assert diff.stats["create_chapters"] == 0
        # 1.2 section 缺 → create_sections = 1
        assert diff.stats["create_sections"] == 1
        # 1.2 有 1 video + 1 PPT = 2 leaves
        assert diff.stats["create_leaves"] == 2
        # 老字段向后兼容
        assert diff.stats["create"] == 1
        # skip 包括 1.1 chapter/section/leaf
        assert diff.stats["skip"] >= 2

    def test_partial_leaves_create_only_leaves(self):
        """section 已建,只是补 1 个新 leaf → 只 create_leaves +1"""
        structure = _make_structure([
            {"index": 1, "title": "第一章 概述", "lessons": [
                {"id": "1.1", "title": "技术",
                 "video": "1.1_技术.mp4",
                 "attachments": ["1.1_技术_English.mp4"]},
            ]},
        ])
        tree = {
            "chapter_list": [{
                "id": 100, "name": "第一章 概述", "index": 1,
                "section_list": [{
                    "id": 200, "name": "技术",
                    "leaf_list": [
                        {"id": 300, "name": "1.1_技术.mp4",
                         "content_info": {"media": {"name": "1.1_技术.mp4"}}}
                        # 缺 _English.mp4
                    ],
                }],
            }],
        }
        diff = compute_diff(structure, tree)
        # chapter/section 都 SKIP,只补 1 个 leaf
        assert diff.stats["create_chapters"] == 0
        assert diff.stats["create_sections"] == 0
        assert diff.stats["create_leaves"] == 1
        assert diff.stats["create"] == 1

    def test_total_planned_compat(self):
        """total_planned() 老 API 仍 = create + rename + prune(向后兼容)"""
        structure = _make_structure([
            {"index": 1, "title": "第一章", "lessons": [
                {"id": "1.1", "title": "技术", "video": "1.1_技术.mp4",
                 "attachments": ["1.1_技术_PPT.pptx"]},
            ]},
        ])
        diff = compute_diff(structure, {"chapter_list": []})
        # 老字段:create = 1(章级)
        assert diff.stats["create"] == 1
        assert diff.total_planned() == 1
        # 新字段:create_leaves = 2(video + ppt)
        assert diff.stats["create_leaves"] == 2
        # 后续 drift 阈值检查仍基于 total_planned(用老 create),行为不变


class TestTreeDiffReport:
    def test_report_structure(self):
        structure = _make_structure([
            {"index": 1, "title": "第一章", "lessons": [
                {"id": "1.1", "title": "技术", "video": "1.1_技术.mp4"},
            ]},
        ])
        diff = compute_diff(structure, {"chapter_list": []})
        report = diff.report()
        assert "course_id" in report
        assert "stats" in report
        assert "is_drifted" in report
        assert "chapters" in report
        assert report["chapters"][0]["action"] == "create"
        assert report["chapters"][0]["index"] == 1
