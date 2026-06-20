"""
测试:_execute_diff(dry_run=True) 状态机

覆盖:
  - 19 节 × 3 资源 = 57 leaves,挂 19 section(每节 1 个)
  - 英文视频走 BokeCC(kind=english,leaf_name 含 "| English")
  - 干跑不调用网络,只返回 SKIPPED assets
  - dry-run / prune 不再 crash(之前 Asset(note=) 崩)
  - only_chapters 隔离
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
from unittest.mock import MagicMock

import pytest

from scrape_new.upload.api_uploader import (
    _execute_diff,
    _create_one_leaf,
    TeacherContext,
    DRIFT_THRESHOLD,
)
from scrape_new.upload.models import (
    Chapter, ContentType, CourseStructure, Lesson, Asset, AssetStatus,
)
from scrape_new.upload.sync_tree import (
    compute_diff, DiffAction, LeafDiff,
)


# ─── 一个 fake ctx,不实际联网 ────────────────────────────────

@dataclass
class _FakeCtx:
    session: Any = field(default_factory=MagicMock)
    csrftoken: str = "fake"
    university_id: str = "0"
    course_id: str = "FAKE_COURSE"
    cookie_created_at: float = 0.0

    def cookie_age_minutes(self) -> float:
        return 0.0

    def cookie_remaining_minutes(self) -> float:
        return 30.0


def _ctx() -> TeacherContext:
    return _FakeCtx()  # type: ignore[return-value]


def _structure_19x3() -> CourseStructure:
    """构造 19 节 × 3 资源(主视频 + 英文视频 + PPT)= 57 leaves 的结构"""
    chapters = []
    ch_to_lessons = {
        1: ["技术", "媒体", "学习"],
        2: ["学习理论", "教学理论", "传播理论"],
        3: ["设计原则", "开发流程", "评估方法"],
        4: ["传统媒体", "数字媒体", "新媒体"],
        5: ["课堂应用", "在线应用", "混合应用"],
        6: ["评价标准", "评价方法"],
        7: ["Best Practices", "Case Studies"],
    }
    for ch_idx, lessons in ch_to_lessons.items():
        ch_title = f"第{ch_idx}章 测试章"
        ls_objs = []
        for ls_idx, ls_title in enumerate(lessons, start=1):
            lesson_id = f"{ch_idx}.{ls_idx}"
            ls_objs.append(Lesson(
                id=lesson_id, title=ls_title, content_type=ContentType.VIDEO,
                video=f"{lesson_id}_{ls_title}.mp4",
                attachments=(
                    f"{lesson_id}_{ls_title}_English.mp4",
                    f"{lesson_id}_{ls_title}_PPT.pptx",
                ),
            ))
        chapters.append(Chapter(index=ch_idx, title=ch_title, lessons=tuple(ls_objs)))
    return CourseStructure(
        course_id="FAKE_COURSE", course_title="DRYRUN_TEST", chapters=tuple(chapters),
    )


# ─── 1) _execute_diff(dry_run=True) 不崩 ─────────────────────

class TestExecuteDiffDryRun:
    def test_dry_run_no_crash_no_network(self, tmp_path):
        """dry-run 模式:不调 API,只返回 SKIPPED assets,不传 note 字段(防 crash)"""
        structure = _structure_19x3()
        diff = compute_diff(structure, {"chapter_list": []})
        # 干跑 19 节 × 3 leaves
        assets = _execute_diff(_ctx(), structure, diff, tmp_path, dry_run=True)
        # 19 lessons × 3 leaves = 57 个 SKIPPED assets
        assert len(assets) == 57, f"期望 57 assets, 实际 {len(assets)}"
        for a in assets:
            assert a.status == AssetStatus.SKIPPED
            assert a.error == "dry-run"
            # 验证没有 note 字段(防 Asset(note=) crash)
            assert not hasattr(a, "note") or a.note is None  # type: ignore[attr-defined]

    def test_dry_run_leaf_kinds(self, tmp_path):
        """每个 lesson 应包含 video / english / ppt 三种 kind,各 19 个"""
        structure = _structure_19x3()
        diff = compute_diff(structure, {"chapter_list": []})
        assets = _execute_diff(_ctx(), structure, diff, tmp_path, dry_run=True)
        # 按 source_path 后缀分桶
        videos = [a for a in assets if a.source_path and a.source_path.endswith(".mp4")
                  and "_English" not in a.source_path]
        english = [a for a in assets if a.source_path and "_English.mp4" in a.source_path]
        ppts = [a for a in assets if a.source_path and a.source_path.endswith(".pptx")]
        assert len(videos) == 19
        assert len(english) == 19
        assert len(ppts) == 19


# ─── 2) _create_one_leaf:kind=english 必须走视频(创建 video leaf) ──

class TestCreateOneLeafEnglish:
    """通过 mock 网络层验证 _create_one_leaf 的行为分支"""

    def test_english_video_routes_to_bokecc(self, tmp_path, monkeypatch):
        """kind=english 必须调 upload_video_to_bokecc + create_video_leaf,
        不调 create_attachment_leaf"""
        from scrape_new.upload import api_uploader as apimod

        # 准备一个真实文件
        video_file = tmp_path / "1.1_技术_English.mp4"
        video_file.write_bytes(b"x" * 1024)

        # mock 网络调用
        bokecc_called = []
        create_video_leaf_called = []
        create_attachment_leaf_called = []

        def fake_bokecc(ctx, path):
            bokecc_called.append(path)
            return ("CCID123", 1024)

        def fake_create_video_leaf(ctx, **kwargs):
            create_video_leaf_called.append(kwargs)
            return 999

        def fake_create_attachment_leaf(ctx, **kwargs):
            create_attachment_leaf_called.append(kwargs)
            raise AssertionError("create_attachment_leaf 不应被英文视频调用")

        monkeypatch.setattr(apimod, "upload_video_to_bokecc", fake_bokecc)
        monkeypatch.setattr(apimod, "create_video_leaf", fake_create_video_leaf)
        monkeypatch.setattr(apimod, "create_attachment_leaf", fake_create_attachment_leaf)

        leaf_diff = LeafDiff(
            lesson_id="1.1", kind="english",
            desired_name="1.1_技术_English.mp4", actual_id=None,
            action=DiffAction.CREATE,
        )
        leaf_id, ccid, size = _create_one_leaf(
            _ctx(), chapter_id=1, section_id=200,
            lesson_id="1.1", lesson_title="技术", leaf_diff=leaf_diff,
            videos_folder=tmp_path, attachments_folder=tmp_path,
        )

        assert leaf_id == 999
        assert ccid == "CCID123"
        assert size == 1024
        assert len(bokecc_called) == 1, "BokeCC 必须被调用(走视频路径)"
        assert len(create_video_leaf_called) == 1
        assert len(create_attachment_leaf_called) == 0
        # leaf_name 应该是 "1.1 技术 | English"
        assert create_video_leaf_called[0]["leaf_name"] == "1.1 技术 | English"

    def test_main_video_routes_to_bokecc(self, tmp_path, monkeypatch):
        """kind=video 也走 BokeCC + create_video_leaf"""
        from scrape_new.upload import api_uploader as apimod

        video_file = tmp_path / "1.1_技术.mp4"
        video_file.write_bytes(b"x" * 1024)

        bokecc_called = []
        create_video_leaf_called = []

        monkeypatch.setattr(apimod, "upload_video_to_bokecc",
                            lambda ctx, p: bokecc_called.append(p) or ("CCID", 1024))
        monkeypatch.setattr(apimod, "create_video_leaf",
                            lambda ctx, **kw: create_video_leaf_called.append(kw) or 100)
        monkeypatch.setattr(apimod, "create_attachment_leaf",
                            lambda *a, **kw: (_ for _ in ()).throw(
                                AssertionError("video kind 走附件路径")
                            ))

        leaf_diff = LeafDiff(
            lesson_id="1.1", kind="video",
            desired_name="1.1_技术.mp4", actual_id=None,
            action=DiffAction.CREATE,
        )
        leaf_id, ccid, _ = _create_one_leaf(
            _ctx(), chapter_id=1, section_id=200,
            lesson_id="1.1", lesson_title="技术", leaf_diff=leaf_diff,
            videos_folder=tmp_path, attachments_folder=tmp_path,
        )
        assert leaf_id == 100
        assert len(bokecc_called) == 1
        assert len(create_video_leaf_called) == 1
        assert create_video_leaf_called[0]["leaf_name"] == "1.1 技术"

    def test_ppt_routes_to_attachment(self, tmp_path, monkeypatch):
        """kind=ppt 走 create_attachment_leaf(确实是七牛云)"""
        from scrape_new.upload import api_uploader as apimod

        ppt_file = tmp_path / "1.1_技术_PPT.pptx"
        ppt_file.write_bytes(b"x" * 1024)

        create_video_leaf_called = []
        create_attachment_leaf_called = []

        def fake_qiniu(ctx, path):
            return ("key", "https://qn1-next.xuetangonline.com/key")

        def fake_create_attachment_leaf(ctx, **kwargs):
            create_attachment_leaf_called.append(kwargs)
            return 200

        monkeypatch.setattr(apimod, "_qiniu_upload_attachment", fake_qiniu)
        monkeypatch.setattr(apimod, "create_video_leaf",
                            lambda *a, **kw: create_video_leaf_called.append(kw) or (_ for _ in ()).throw(
                                AssertionError("PPT 走视频路径")
                            ))
        monkeypatch.setattr(apimod, "create_attachment_leaf", fake_create_attachment_leaf)

        leaf_diff = LeafDiff(
            lesson_id="1.1", kind="ppt",
            desired_name="1.1_技术_PPT.pptx", actual_id=None,
            action=DiffAction.CREATE,
        )
        leaf_id, ccid, size = _create_one_leaf(
            _ctx(), chapter_id=1, section_id=200,
            lesson_id="1.1", lesson_title="技术", leaf_diff=leaf_diff,
            videos_folder=tmp_path, attachments_folder=tmp_path,
        )
        assert leaf_id == 200
        assert ccid is None  # 附件不返回 ccid
        assert len(create_attachment_leaf_called) == 1
        # leaf_name 应该是 "1.1 技术 | PPT"
        assert create_attachment_leaf_called[0]["leaf_name"] == "1.1 技术 | PPT"


# ─── 3) 全部 19 节 × 3 资源 dry-run 端到端,完整走 execute_diff ────

class TestMappingV3DryRun:
    def test_19_lessons_57_leaves_dry_run(self, tmp_path):
        """P0 验收:19 节 × 3 资源 dry-run 不崩、不重复、英文走 BokeCC 路径

        流程:CourseStructure(19节×3)→ compute_diff(空树)→ _execute_diff(dry_run)
        期望:57 个 SKIPPED asset,无 Asset(note=) 崩
        """
        structure = _structure_19x3()
        diff = compute_diff(structure, {"chapter_list": []})

        # 必修:不传真实文件,空 videos_folder
        assets = _execute_diff(_ctx(), structure, diff, tmp_path, dry_run=True)
        # 57 = 19 lessons × 3 leaves
        assert len(assets) == 57

        # 验证 kind 分布
        kind_by_source: dict[str, str] = {
            a.source_path: a.source_path.lower().split(".")[-1] for a in assets
        }
        # 应该有 19 个 video(主,无 _English)
        n_main_video = sum(1 for a in assets
                           if a.source_path and a.source_path.endswith(".mp4")
                           and "_English" not in a.source_path)
        # 应该有 19 个 _English.mp4
        n_english = sum(1 for a in assets
                        if a.source_path and a.source_path.endswith("_English.mp4"))
        # 应该有 19 个 .pptx
        n_ppt = sum(1 for a in assets
                    if a.source_path and a.source_path.endswith(".pptx"))
        assert n_main_video == 19, f"主视频数 {n_main_video} ≠ 19"
        assert n_english == 19, f"英文视频数 {n_english} ≠ 19"
        assert n_ppt == 19, f"PPT 数 {n_ppt} ≠ 19"

        # 验证不重复(每个 source_path 唯一)
        paths = [a.source_path for a in assets]
        assert len(paths) == len(set(paths)), "有重名!"

        # 验证 lesson 收敛:每节所有 leaves 共享一个 lesson_id
        lessons_seen: dict[str, set[str]] = {}
        for a in assets:
            lessons_seen.setdefault(a.lesson_id, set()).add(a.source_path or "")
        for ls_id, paths_set in lessons_seen.items():
            assert len(paths_set) == 3, f"{ls_id} 应有 3 个资源, 实际 {len(paths_set)}"

    def test_only_chapters_dry_run_isolates(self, tmp_path):
        """only_chapters 只跑指定章,其他章的 leaves 也不该出现在 assets"""
        structure = _structure_19x3()
        diff = compute_diff(
            structure, {"chapter_list": []},
            only_chapters={1, 3},  # 只处理 1、3 章
        )
        assets = _execute_diff(
            _ctx(), structure, diff, tmp_path, dry_run=True,
        )
        # 1 + 3 章:每章 3 节,每节 3 leaves = 6 + 9 = 15... wait, 1 章 3 节, 3 章 3 节, 6 节 × 3 leaves = 18
        # chapter[0]=ch1: 3 lessons, chapter[2]=ch3: 3 lessons. 6 lessons × 3 leaves = 18
        assert len(assets) == 18
        # 验证 chapter_index 限定在 1, 3
        ch_indices = {a.chapter_index for a in assets}
        assert ch_indices == {1, 3}, f"chapter_index {ch_indices} 不在 only_chapters"


class TestRenameConfirm:
    """RENAME 默认不执行(delete+create 会清空 leaf,太危险),
    必须 confirm_rename=True 才执行。"""

    def test_rename_default_pending(self, tmp_path):
        """不传 confirm_rename → RENAME 转 PENDING,原章保留(实际 id 用 actual_id)"""
        structure = _structure_19x3()
        # 真实树:ch1 标题不同(模拟改名)
        tree = {
            "chapter_list": [
                {"id": 1001, "name": "第一章 老标题", "index": 1,
                 "section_list": []},
            ]
        }
        diff = compute_diff(structure, tree)
        # ch1 应该是 RENAME
        assert diff.chapters[0].action == DiffAction.RENAME

        assets = _execute_diff(
            _ctx(), structure, diff, tmp_path, dry_run=True,
            confirm_rename=False,  # 默认
        )
        # 应该看到 PENDING 资产
        pending = [a for a in assets if a.status == AssetStatus.PENDING]
        assert len(pending) == 1
        assert "rename_pending" in (pending[0].error or "")
        # 不应该有 OK 状态(没真删/真建)
        ok = [a for a in assets if a.status == AssetStatus.OK]
        assert len(ok) == 0

    def test_rename_pending_skips_chapter_lessons(self, tmp_path, monkeypatch):
        """P1-2 修:RENAME 待确认时,整章 lesson/leaf 全部跳过,不在旧章下补 leaf

        场景:ch1 章名改 + mapping 里 ch1 有 3 节(每个 3 leaves = 9 leaves)
        期望:只会产生 1 个 PENDING 资产(不是 9 个 leaves 都被 create)
        """
        from scrape_new.upload import api_uploader as apimod

        # ch1 章名不同(触发 RENAME),ch1 下有 1 个 lesson 配 3 leaves
        structure = _structure_19x3()  # 7 章,19 节
        tree = {
            "chapter_list": [
                # ch1 标题不同 + 只有 1 个 section(缺 1.2, 1.3, ch2-ch7 全缺)
                {"id": 1001, "name": "第一章 老标题", "index": 1,
                 "section_list": [
                     # ch1.1 已建,但 leaf_list 空 → 后续 3 leaves 都被算 CREATE
                     {"id": 2001, "name": "技术", "leaf_list": []},
                 ]},
            ]
        }
        diff = compute_diff(structure, tree)
        # ch1 应该是 RENAME(action=RENAME)
        assert diff.chapters[0].action == DiffAction.RENAME

        # 监控 create_section 被调次数(不该被调,RENAME 整章跳过)
        create_section_called = []
        monkeypatch.setattr(
            apimod, "create_section",
            lambda ctx, ch_id, name, cover="", remark="": (
                create_section_called.append(name) or 2000
            ),
        )
        # 监控 upload_video_to_bokecc(不该被调)
        upload_called = []
        monkeypatch.setattr(
            apimod, "upload_video_to_bokecc",
            lambda ctx, path: (
                upload_called.append(path) or ("CCID", 1024)
            ),
        )

        assets = _execute_diff(
            _ctx(), structure, diff, tmp_path, dry_run=False,
            confirm_rename=False,
        )
        # 关键断言:RENAME 待确认时,ch1 的 9 leaves 全部跳过(不在旧章下补)
        # 实际:outer loop `continue` 了 ch1,但 ch2-ch7 不在 RENAME 状态,正常 CREATE
        # 所以 create_section 被调 16 次(ch2-7 共 16 节),但 ch1 的 3 节都没被调
        assert "技术" not in create_section_called, \
            f"ch1.1 不应被调,但 create_section 列表含它: {create_section_called}"
        assert "媒体" not in create_section_called, \
            f"ch1.2 不应被调,但 create_section 列表含它: {create_section_called}"
        assert "学习" not in create_section_called, \
            f"ch1.3 不应被调,但 create_section 列表含它: {create_section_called}"
        # ch2-ch7 正常 CREATE(16 节,跟 mapping 一致)
        assert len(create_section_called) == 16, \
            f"ch2-ch7 应被调 16 次,实际 {len(create_section_called)}"
        # upload 只为 ch2-ch7 调,不为 ch1 调
        assert not any("1.1" in p.name or "1.2" in p.name or "1.3" in p.name
                       for p in upload_called), \
            f"ch1 文件不应被上传: {[p.name for p in upload_called]}"
        # 只产 1 个 PENDING 资产(就是 ch1 的 RENAME)
        pending = [a for a in assets if a.status == AssetStatus.PENDING]
        assert len(pending) == 1
        # ch1.1 实际是 0-leaf diff(即使 section 缺 leaves,也没生成 CREATE leaf,因为整章跳了)
        ch1_leaf_assets = [a for a in assets if a.chapter_index == 1 and a.lesson_id != "-"]
        assert len(ch1_leaf_assets) == 0, \
            f"ch1 应无 leaf 资产,实际 {len(ch1_leaf_assets)} 个"

    def test_rename_with_confirm_creates_chapter(self, tmp_path, monkeypatch):
        """confirm_rename=True → 走 delete+create,正常 RENAME"""
        from scrape_new.upload import api_uploader as apimod

        # 真实树:ch1 标题不同
        tree = {
            "chapter_list": [
                {"id": 1001, "name": "第一章 老标题", "index": 1,
                 "section_list": []},
            ]
        }
        structure = _structure_19x3()
        diff = compute_diff(structure, tree)
        assert diff.chapters[0].action == DiffAction.RENAME

        # mock 网络调用
        create_chapter_called = []
        delete_chapter_called = []

        monkeypatch.setattr(
            apimod, "create_chapter",
            lambda ctx, name, is_show=True: (
                create_chapter_called.append(name) or 9999
            ),
        )
        monkeypatch.setattr(
            apimod, "delete_chapter",
            lambda ctx, ch_id: delete_chapter_called.append(ch_id),
        )
        # confirm_rename=True → 走 RENAME 路径
        # 注意:dry_run=False 让 delete_chapter 真的被调(plan 模式也保留 delete)
        monkeypatch.setattr(
            apimod, "create_section",
            lambda ctx, ch_id, name, cover="", remark="": 2000,
        )
        # mock 视频上传/leaf 创建,避免没文件报错
        monkeypatch.setattr(
            apimod, "upload_video_to_bokecc",
            lambda ctx, path: (_ for _ in ()).throw(
                FileNotFoundError(f"mock no file: {path}")
            ),
        )

        assets = _execute_diff(
            _ctx(), structure, diff, tmp_path, dry_run=False,
            confirm_rename=True,
        )
        # 验证:delete_chapter 被调(actual_id=1001)
        assert 1001 in delete_chapter_called
        # create_chapter 被调(新标题)
        assert any("测试章" in name for name in create_chapter_called)


class TestContentTypeForKind:
    """P1 修:kind=ppt/pdf/docx/doc/attachment/image 全部返 ATTACHMENT(不再返 OTHER)"""

    def test_video_kinds(self):
        from scrape_new.upload.api_uploader import _content_type_for_kind
        from scrape_new.upload.models import ContentType
        assert _content_type_for_kind("video") == ContentType.VIDEO
        assert _content_type_for_kind("english") == ContentType.VIDEO

    def test_attachment_kinds(self):
        from scrape_new.upload.api_uploader import _content_type_for_kind
        from scrape_new.upload.models import ContentType
        # 关键:文档类都归 ATTACHMENT,不再归 OTHER
        for kind in ("ppt", "pdf", "docx", "doc", "attachment", "image"):
            assert _content_type_for_kind(kind) == ContentType.ATTACHMENT, \
                f"kind={kind} 应返 ATTACHMENT"

    def test_unknown_kind_defaults_to_attachment(self):
        from scrape_new.upload.api_uploader import _content_type_for_kind
        from scrape_new.upload.models import ContentType
        # 未知 kind 兜底 ATTACHMENT(比 OTHER 更安全)
        assert _content_type_for_kind("unknown_xyz") == ContentType.ATTACHMENT


class TestDriftThreshold:
    def test_drift_threshold_default_60(self):
        # 默认阈值是 0.6
        assert DRIFT_THRESHOLD == 0.6
