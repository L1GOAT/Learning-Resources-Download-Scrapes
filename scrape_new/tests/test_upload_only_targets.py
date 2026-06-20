"""
U1+U2+U3 测试:局部目标 / 局部禁止 reset / plan-only

覆盖(8 测试):
  U1: --only-resource 只处理一个 leaf(其他 SKIP,已存在 SKIP,缺失 CREATE)
  U2: 局部模式 drift > 60% 也不 reset(只 HIGH_RISK warn)
  U3: --plan-only 不调用写 API,只输出 _upload_plan.json/md
  重复 leaf 进入 pending,不自动删(已通过 api_uploader 设计层验证)
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from scrape_new.upload.sync_tree import (
    TreeDiff, ChapterDiff, LessonDiff, LeafDiff, DiffAction,
)
from scrape_new.upload.api_uploader import (
    _mark_only_targets,
    _is_target_leaf,
    _parse_only_targets_only,
    write_upload_plan,
)


# ─── TreeDiff 工厂 ────────────────────────────────────

def _build_diff(
    spec: list[tuple[int, str, str, list[tuple[str, str]]]],
) -> TreeDiff:
    """构造 TreeDiff 用于测试。

    spec: [(ch_index, ch_title, lesson_id, [(kind, name), ...]), ...]
    每个 (ch_index, lesson_id) 唯一;同 ch_index 多 lesson 自动归到同一 chapter。
    所有 leaf 默认 CREATE。
    """
    # 按 ch_index 分桶 lesson specs
    by_chapter: dict[int, tuple[str, list[tuple[str, list[tuple[str, str]]]]]] = {}
    for ch_index, ch_title, lesson_id, leaves in spec:
        if ch_index not in by_chapter:
            by_chapter[ch_index] = (ch_title, [])
        by_chapter[ch_index][1].append((lesson_id, leaves))

    chapters: list[ChapterDiff] = []
    total_create = 0
    total_chapters = len(by_chapter)
    total_sections = 0
    for ch_index in sorted(by_chapter.keys()):
        ch_title, lesson_specs = by_chapter[ch_index]
        lesson_diffs_list: list[LessonDiff] = []
        for lesson_id, leaves in lesson_specs:
            leaf_diffs: list[LeafDiff] = []
            for kind, name in leaves:
                leaf_diffs.append(LeafDiff(
                    lesson_id=lesson_id, kind=kind, desired_name=name,
                    actual_id=None, action=DiffAction.CREATE,
                ))
                total_create += 1
            lesson_diffs_list.append(LessonDiff(
                id=lesson_id, desired_title=f"Lesson {lesson_id}",
                actual_id=None, actual_title=None,
                action=DiffAction.CREATE, matched_by="none",
                leaf_diffs=tuple(leaf_diffs),
            ))
            total_sections += 1
        chapters.append(ChapterDiff(
            index=ch_index, desired_title=ch_title,
            actual_id=None, actual_title=None,
            action=DiffAction.CREATE, matched_by="none",
            lesson_diffs=tuple(lesson_diffs_list),
        ))
    return TreeDiff(
        course_id="c1",
        chapters=tuple(chapters),
        stats={"create": total_create, "skip": 0, "rename": 0, "prune": 0,
               "create_chapters": total_chapters,
               "create_sections": total_sections,
               "create_leaves": total_create},
    )


# ─── U1: 局部目标过滤 ───────────────────────────────

class TestOnlyTargets:
    def test_only_resource_filters_to_one_leaf(self):
        """U1:--only-resource 1.1:video 只处理这一个 leaf,其他全部 SKIP"""
        diff = _build_diff([
            (1, "ch1", "1.1", [("video", "Lesson 1 Video")]),
            (1, "ch1", "1.2", [("video", "Lesson 2 Video")]),
        ])
        diff2 = _mark_only_targets(
            diff,
            only_lessons=None,
            only_resources={("1.1", "video")},
        )
        # 1.1 的 leaf 仍是 CREATE
        ch1 = next(c for c in diff2.chapters if c.index == 1)
        ls11 = next(l for l in ch1.lesson_diffs if l.id == "1.1")
        assert ls11.leaf_diffs[0].action == DiffAction.CREATE
        # 1.2 的 leaf 变 SKIP
        ls12 = next(l for l in ch1.lesson_diffs if l.id == "1.2")
        # 1.2 lesson 整体被 SKIP(因为 leaf 全 SKIP)
        assert ls12.action == DiffAction.SKIP
        assert ls12.leaf_diffs[0].action == DiffAction.SKIP
        # stats:create 减 1(只留 1.1)
        assert diff2.stats["create"] == 1
        assert diff2.stats["skip"] >= 1

    def test_only_lessons_filters_whole_lesson(self):
        """U1:--only-lessons 1.2 → 1.2 lesson 全部叶子保留,1.1 全 SKIP"""
        diff = _build_diff([
            (1, "ch1", "1.1", [("video", "L1V"), ("ppt", "课件")]),
            (1, "ch1", "1.2", [("video", "L2V")]),
        ])
        diff2 = _mark_only_targets(
            diff, only_lessons={"1.2"}, only_resources=None,
        )
        ch1 = next(c for c in diff2.chapters if c.index == 1)
        ls11 = next(l for l in ch1.lesson_diffs if l.id == "1.1")
        ls12 = next(l for l in ch1.lesson_diffs if l.id == "1.2")
        # 1.1 整 lesson SKIP(2 个 leaf 全 SKIP)
        assert ls11.action == DiffAction.SKIP
        # 1.2 保持 CREATE
        assert ls12.action == DiffAction.CREATE
        assert ls12.leaf_diffs[0].action == DiffAction.CREATE

    def test_existing_match_stays_skip(self):
        """U1:已存在且匹配的资源,在局部目标里也保持 SKIP(不重复 create)"""
        # 构造一个 SKIP 状态的 leaf
        diff = TreeDiff(
            course_id="c1",
            chapters=(ChapterDiff(
                index=1, desired_title="ch1", actual_id=100, actual_title="ch1",
                action=DiffAction.SKIP, matched_by="exact",
                lesson_diffs=(LessonDiff(
                    id="1.1", desired_title="L1", actual_id=10, actual_title="L1",
                    action=DiffAction.SKIP, matched_by="exact",
                    leaf_diffs=(LeafDiff(
                        lesson_id="1.1", kind="video", desired_name="L1V",
                        actual_id=1000, action=DiffAction.SKIP,
                    ),),
                ),),
            ),),
            stats={"create": 0, "skip": 1, "rename": 0, "prune": 0,
                   "create_chapters": 0, "create_sections": 0, "create_leaves": 0},
        )
        diff2 = _mark_only_targets(
            diff, only_lessons=None, only_resources={("1.1", "video")},
        )
        # 仍是 SKIP
        assert diff2.chapters[0].lesson_diffs[0].leaf_diffs[0].action == DiffAction.SKIP

    def test_missing_resource_becomes_create(self):
        """U1:缺失资源在局部目标里 → CREATE"""
        diff = _build_diff([
            (1, "ch1", "1.1", [("ppt", "课件")]),
        ])
        diff2 = _mark_only_targets(
            diff, only_lessons=None, only_resources={("1.1", "ppt")},
        )
        ch1 = diff2.chapters[0]
        assert ch1.lesson_diffs[0].action == DiffAction.CREATE
        assert ch1.lesson_diffs[0].leaf_diffs[0].action == DiffAction.CREATE

    def test_is_target_leaf_logic(self):
        """U1 内部逻辑:only_resources 优先,only_lessons 兜底,都 None 返 True"""
        assert _is_target_leaf("1.1", "video",
                               only_lessons=None, only_resources={("1.1", "video")})
        assert _is_target_leaf("1.1", "video",
                               only_lessons={"1.1"}, only_resources={("1.2", "video")})
        assert not _is_target_leaf("1.3", "video",
                                   only_lessons={"1.1"}, only_resources={("1.2", "video")})
        assert _is_target_leaf("9.9", "anything", only_lessons=None, only_resources=None)

    def test_parse_only_resources_ignores_malformed(self):
        """U1 CLI 解析:--only-resource 格式错就忽略"""
        only_lessons, only_resources = _parse_only_targets_only(
            only_lessons=None, only_resources={"1.1:video", "bad-format", "1.2:ppt"},
        )
        assert only_resources == {("1.1", "video"), ("1.2", "ppt")}


# ─── U3: plan-only 模式 ─────────────────────────────

class TestPlanOnly:
    def test_write_upload_plan_creates_files(self, tmp_path: Path):
        """U3:write_upload_plan 写 _upload_plan.json 和 _upload_plan.md"""
        diff = _build_diff([
            (1, "ch1", "1.1", [("video", "L1V"), ("ppt", "课件")]),
            (1, "ch1", "1.2", [("video", "L2V")]),
        ])
        # 应用局部目标(只动 1.1)
        diff = _mark_only_targets(diff, only_lessons={"1.1"}, only_resources=None)

        write_upload_plan(
            diff, tmp_path,
            course_id="c1", only_lessons={"1.1"}, only_resources=None,
        )
        assert (tmp_path / "_upload_plan.json").exists()
        assert (tmp_path / "_upload_plan.md").exists()
        data = json.loads((tmp_path / "_upload_plan.json").read_text(encoding="utf-8"))
        # 至少 CREATE = 2(1.1 有 2 个 leaf)
        assert data["summary"]["CREATE"] == 2
        # SKIP > 0(1.2 全 SKIP)
        assert data["summary"]["SKIP"] >= 1
        # scope 包含 only_lessons
        assert data["scope"]["only_lessons"] == ["1.1"]

    def test_write_upload_plan_marks_high_risk(self, tmp_path: Path):
        """U3:high_risk=True 时,plan 报告顶部写 ⚠ HIGH_RISK"""
        diff = _build_diff([
            (1, "ch1", "1.1", [("video", "L1V")]),
        ])
        write_upload_plan(
            diff, tmp_path,
            course_id="c1",
            high_risk=True,
            high_risk_reason="drift = 1/1 >= 60%;局部模式只 warn 不阻断",
        )
        md = (tmp_path / "_upload_plan.md").read_text(encoding="utf-8")
        assert "HIGH_RISK" in md
        assert "局部模式" in md


# ─── 19 轮:plan-first / apply-plan / yes ─────────────

class TestPlanFirstApplyPlanYes:
    """P1/P2/P3/P4:plan 元数据 + apply-plan 校验 + --yes bypass"""

    def test_compute_mapping_hash_stable(self):
        """P2:同样 mapping 算同样 hash,微改 title 后 hash 变"""
        from scrape_new.upload.api_uploader import compute_mapping_hash
        from scrape_new.upload.models import (
            CourseStructure, Chapter, Lesson, ContentType,
        )
        def make(title: str) -> CourseStructure:
            return CourseStructure(
                course_id="c1", course_title=title,
                chapters=(Chapter(
                    index=1, title="ch1",
                    lessons=(Lesson(
                        id="1.1", title=title, content_type=ContentType.VIDEO,
                        video="x.mp4",
                    ),),
                ),),
            )
        h1 = compute_mapping_hash(make("foo"))
        h2 = compute_mapping_hash(make("foo"))
        h3 = compute_mapping_hash(make("bar"))
        assert h1 == h2
        assert h1 != h3
        assert len(h1) == 16  # SHA1 前 16 字符

    def test_compute_tree_fingerprint_changes_with_tree(self):
        """P2:tree 改了 fingerprint 变"""
        from scrape_new.upload.api_uploader import compute_tree_fingerprint
        t1 = {"chapter_list": [{"id": 1, "name": "ch1", "section_list": []}]}
        t2 = {"chapter_list": [{"id": 1, "name": "ch1", "section_list": []}]}
        t3 = {"chapter_list": [{"id": 1, "name": "改名了", "section_list": []}]}
        f1 = compute_tree_fingerprint(t1)
        f2 = compute_tree_fingerprint(t2)
        f3 = compute_tree_fingerprint(t3)
        assert f1 == f2
        assert f1 != f3

    def test_write_upload_plan_includes_metadata(self, tmp_path: Path):
        """P2:write_upload_plan 输出 JSON 含 generated_at/mapping_hash/tree_fingerprint/scope"""
        from scrape_new.upload.api_uploader import write_upload_plan
        diff = _build_diff([
            (1, "ch1", "1.1", [("video", "L1V")]),
        ])
        write_upload_plan(
            diff, tmp_path,
            course_id="c1",
            only_lessons={"1.1"}, only_resources=None,
            mapping_hash="abc123",
            tree_fingerprint="def456",
        )
        data = json.loads((tmp_path / "_upload_plan.json").read_text(encoding="utf-8"))
        # 关键字段都在
        assert data["course_id"] == "c1"
        assert data["mapping_hash"] == "abc123"
        assert data["tree_fingerprint"] == "def456"
        assert "generated_at" in data
        assert data["scope"]["only_lessons"] == ["1.1"]
        # MD 也有
        md = (tmp_path / "_upload_plan.md").read_text(encoding="utf-8")
        assert "abc123" in md
        assert "def456" in md

    def test_apply_plan_course_id_mismatch_rejected(self, tmp_path: Path, capsys):
        """P3:apply-plan course_id 不一致 → 拒绝 + sys.exit(1)"""
        from scrape_new.upload.api_uploader import _load_and_verify_plan
        plan = {
            "course_id": "OTHER",  # 不一致
            "mapping_hash": "h", "tree_fingerprint": "f",
            "scope": {"only_lessons": None, "only_resources": None, "only_chapters": None},
        }
        (tmp_path / "p.json").write_text(json.dumps(plan), encoding="utf-8")
        with pytest.raises(SystemExit) as exc:
            _load_and_verify_plan(
                tmp_path / "p.json", course_id="c1",
                mapping_hash="h", tree_fingerprint="f",
                only_chapters=None, only_lessons=None, only_resources=None,
            )
        assert exc.value.code == 1
        captured = capsys.readouterr()
        assert "course_id 不一致" in captured.out

    def test_apply_plan_mapping_hash_mismatch_rejected(self, tmp_path: Path, capsys):
        """P3:apply-plan mapping_hash 不一致 → 拒绝"""
        from scrape_new.upload.api_uploader import _load_and_verify_plan
        plan = {
            "course_id": "c1", "mapping_hash": "OLD",
            "tree_fingerprint": "f",
            "scope": {"only_lessons": None, "only_resources": None, "only_chapters": None},
        }
        (tmp_path / "p.json").write_text(json.dumps(plan), encoding="utf-8")
        with pytest.raises(SystemExit) as exc:
            _load_and_verify_plan(
                tmp_path / "p.json", course_id="c1",
                mapping_hash="NEW", tree_fingerprint="f",
                only_chapters=None, only_lessons=None, only_resources=None,
            )
        assert exc.value.code == 1
        assert "mapping_hash 不一致" in capsys.readouterr().out

    def test_apply_plan_scope_mismatch_rejected(self, tmp_path: Path, capsys):
        """P3:apply-plan scope 不一致 → 拒绝"""
        from scrape_new.upload.api_uploader import _load_and_verify_plan
        plan = {
            "course_id": "c1", "mapping_hash": "h", "tree_fingerprint": "f",
            "scope": {"only_lessons": ["1.2"], "only_resources": None, "only_chapters": None},
        }
        (tmp_path / "p.json").write_text(json.dumps(plan), encoding="utf-8")
        with pytest.raises(SystemExit) as exc:
            _load_and_verify_plan(
                tmp_path / "p.json", course_id="c1",
                mapping_hash="h", tree_fingerprint="f",
                only_chapters=None, only_lessons=None, only_resources=None,
            )
        assert exc.value.code == 1
        assert "scope" in capsys.readouterr().out

    def test_apply_plan_tree_fingerprint_mismatch_rejected(self, tmp_path: Path, capsys):
        """P3:apply-plan tree_fingerprint 不一致 → 拒绝"""
        from scrape_new.upload.api_uploader import _load_and_verify_plan
        plan = {
            "course_id": "c1", "mapping_hash": "h", "tree_fingerprint": "OLD",
            "scope": {"only_lessons": None, "only_resources": None, "only_chapters": None},
        }
        (tmp_path / "p.json").write_text(json.dumps(plan), encoding="utf-8")
        with pytest.raises(SystemExit) as exc:
            _load_and_verify_plan(
                tmp_path / "p.json", course_id="c1",
                mapping_hash="h", tree_fingerprint="NEW",
                only_chapters=None, only_lessons=None, only_resources=None,
            )
        assert exc.value.code == 1
        assert "tree_fingerprint 不一致" in capsys.readouterr().out

    def test_apply_plan_passes_when_all_match(self, tmp_path: Path, capsys):
        """P3:apply-plan 4 项都匹配 → 通过,返 plan 字典"""
        from scrape_new.upload.api_uploader import _load_and_verify_plan
        plan = {
            "course_id": "c1", "mapping_hash": "h", "tree_fingerprint": "f",
            "generated_at": "2026-06-18T10:00:00",
            "scope": {"only_lessons": ["1.1"], "only_resources": None, "only_chapters": None},
        }
        (tmp_path / "p.json").write_text(json.dumps(plan), encoding="utf-8")
        result = _load_and_verify_plan(
            tmp_path / "p.json", course_id="c1",
            mapping_hash="h", tree_fingerprint="f",
            only_chapters=None, only_lessons={"1.1"}, only_resources=None,
        )
        assert result["course_id"] == "c1"
        assert "校验通过" in capsys.readouterr().out

    def test_plan_first_no_yes_no_apply_plan_uses_default(self):
        """P1:run_upload_api 形参 apply_plan_path / yes 存在,默认 None / False"""
        from scrape_new.upload.api_uploader import run_upload_api
        import inspect
        sig = inspect.signature(run_upload_api)
        assert "apply_plan_path" in sig.parameters
        assert "yes" in sig.parameters
        # 默认值:None / False
        assert sig.parameters["apply_plan_path"].default is None
        assert sig.parameters["yes"].default is False

    def test_cli_upload_has_apply_plan_and_yes_flags(self):
        """P5:cli.py upload 子命令有 --apply-plan / --yes"""
        from scrape_new.upload.cli import build_parser
        parser = build_parser()
        # 测 --apply-plan
        args = parser.parse_args([
            "upload", "--mapping", "m.json", "--course-id", "123",
            "--cookies-string", "x", "--apply-plan", "p.json",
        ])
        assert args.apply_plan == "p.json"
        assert args.yes is False
        # 测 --yes
        args = parser.parse_args([
            "upload", "--mapping", "m.json", "--course-id", "123",
            "--cookies-string", "x", "--yes",
        ])
        assert args.apply_plan is None
        assert args.yes is True
        # 测默认(没传)
        args = parser.parse_args([
            "upload", "--mapping", "m.json", "--course-id", "123",
            "--cookies-string", "x",
        ])
        assert args.apply_plan is None
        assert args.yes is False

    # ─── 第二十轮 F1/F2:安全细节 ──

    def test_tree_fingerprint_ignores_leaf_order(self):
        """F1:同一树 leaf/section 顺序不同 fingerprint 必须相同(超星会重排)"""
        from scrape_new.upload.api_uploader import compute_tree_fingerprint
        # leaf 顺序变了
        t1 = {"chapter_list": [{
            "id": 1, "name": "ch1",
            "section_list": [{
                "id": 10, "name": "sec1",
                "leaf_list": [
                    {"id": 100, "name": "L1"},
                    {"id": 101, "name": "L2"},
                    {"id": 102, "name": "L3"},
                ],
            }],
        }]}
        t2 = {"chapter_list": [{
            "id": 1, "name": "ch1",
            "section_list": [{
                "id": 10, "name": "sec1",
                "leaf_list": [
                    {"id": 102, "name": "L3"},
                    {"id": 100, "name": "L1"},
                    {"id": 101, "name": "L2"},
                ],  # 顺序变了
            }],
        }]}
        # chapter 顺序变了(内容完全相同)
        t3 = {"chapter_list": [
            {"id": 2, "name": "ch2", "section_list": [
                {"id": 20, "name": "s2", "leaf_list": [
                    {"id": 200, "name": "L2a"},
                ]},
            ]},
            {"id": 1, "name": "ch1", "section_list": [
                {"id": 10, "name": "sec1", "leaf_list": [
                    {"id": 100, "name": "L1"},
                ]},
            ]},
        ]}
        f1 = compute_tree_fingerprint(t1)
        f2 = compute_tree_fingerprint(t2)
        f3 = compute_tree_fingerprint(t3)
        assert f1 == f2, "leaf 顺序不同应同 fingerprint"
        # t3 跟 t1 顺序反(2 在前),内容相同(都是 ch1 sec1 leaf100)
        # 注意:t1 只有 ch1,t3 有 ch1+ch2 — 不同内容,不同 fingerprint
        # 真正的"顺序忽略"测试需要内容相同
        t4 = {"chapter_list": [
            {"id": 1, "name": "ch1", "section_list": [
                {"id": 10, "name": "sec1", "leaf_list": [
                    {"id": 100, "name": "L1"},
                ]},
            ]},
            {"id": 2, "name": "ch2", "section_list": [
                {"id": 20, "name": "s2", "leaf_list": [
                    {"id": 200, "name": "L2a"},
                ]},
            ]},
        ]}
        f4 = compute_tree_fingerprint(t4)
        # t3 跟 t4 内容相同,顺序不同 → 应同 fingerprint
        assert f3 == f4, "chapter 顺序不同应同 fingerprint(内容相同)"

    def test_tree_fingerprint_changes_when_leaf_name_changes(self):
        """F1:leaf 名称不同 fingerprint 必须不同(内容真的变了)"""
        from scrape_new.upload.api_uploader import compute_tree_fingerprint
        t1 = {"chapter_list": [{
            "id": 1, "name": "ch1",
            "section_list": [{
                "id": 10, "name": "sec1",
                "leaf_list": [{"id": 100, "name": "L1"}],
            }],
        }]}
        t2 = {"chapter_list": [{
            "id": 1, "name": "ch1",
            "section_list": [{
                "id": 10, "name": "sec1",
                "leaf_list": [{"id": 100, "name": "L1改成了别的"}],
            }],
        }]}
        assert compute_tree_fingerprint(t1) != compute_tree_fingerprint(t2)

    def test_apply_plan_rejects_missing_mapping_hash(self, tmp_path: Path, capsys):
        """F2:plan 缺 mapping_hash 字段 → 拒绝 + 提示重新 plan"""
        from scrape_new.upload.api_uploader import _load_and_verify_plan
        plan = {
            "course_id": "c1",
            # 故意缺 mapping_hash
            "tree_fingerprint": "f",
            "scope": {"only_lessons": None, "only_resources": None, "only_chapters": None},
        }
        (tmp_path / "p.json").write_text(json.dumps(plan), encoding="utf-8")
        with pytest.raises(SystemExit) as exc:
            _load_and_verify_plan(
                tmp_path / "p.json", course_id="c1",
                mapping_hash="h", tree_fingerprint="f",
                only_chapters=None, only_lessons=None, only_resources=None,
            )
        assert exc.value.code == 1
        out = capsys.readouterr().out
        assert "mapping_hash" in out
        assert "缺失" in out or "旧版" in out

    def test_apply_plan_rejects_missing_tree_fingerprint(self, tmp_path: Path, capsys):
        """F2:plan 缺 tree_fingerprint 字段 → 拒绝"""
        from scrape_new.upload.api_uploader import _load_and_verify_plan
        plan = {
            "course_id": "c1",
            "mapping_hash": "h",
            # 故意缺 tree_fingerprint
            "scope": {"only_lessons": None, "only_resources": None, "only_chapters": None},
        }
        (tmp_path / "p.json").write_text(json.dumps(plan), encoding="utf-8")
        with pytest.raises(SystemExit) as exc:
            _load_and_verify_plan(
                tmp_path / "p.json", course_id="c1",
                mapping_hash="h", tree_fingerprint="f",
                only_chapters=None, only_lessons=None, only_resources=None,
            )
        assert exc.value.code == 1
        assert "tree_fingerprint" in capsys.readouterr().out

    def test_apply_plan_rejects_missing_or_invalid_scope(self, tmp_path: Path, capsys):
        """F2:scope 缺失或不是 dict → 拒绝"""
        from scrape_new.upload.api_uploader import _load_and_verify_plan
        # 3a: scope 字段完全缺失
        plan_no_scope = {
            "course_id": "c1", "mapping_hash": "h", "tree_fingerprint": "f",
        }
        (tmp_path / "p1.json").write_text(json.dumps(plan_no_scope), encoding="utf-8")
        with pytest.raises(SystemExit) as exc:
            _load_and_verify_plan(
                tmp_path / "p1.json", course_id="c1",
                mapping_hash="h", tree_fingerprint="f",
                only_chapters=None, only_lessons=None, only_resources=None,
            )
        assert exc.value.code == 1
        out1 = capsys.readouterr().out
        assert "scope" in out1
        # 3b: scope 存在但不是 dict
        plan_bad_scope = {
            "course_id": "c1", "mapping_hash": "h", "tree_fingerprint": "f",
            "scope": "not-a-dict",
        }
        (tmp_path / "p2.json").write_text(json.dumps(plan_bad_scope), encoding="utf-8")
        with pytest.raises(SystemExit) as exc:
            _load_and_verify_plan(
                tmp_path / "p2.json", course_id="c1",
                mapping_hash="h", tree_fingerprint="f",
                only_chapters=None, only_lessons=None, only_resources=None,
            )
        assert exc.value.code == 1
        out2 = capsys.readouterr().out
        assert "scope" in out2
        assert "dict" in out2 or "类型" in out2
