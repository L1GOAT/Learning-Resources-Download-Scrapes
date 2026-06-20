"""
测试:scrape_new.upload.resource_key + _mark_resume_keys + _mark_retry_keys

覆盖(14 测试):
  1. make_resource_key 稳定
  2. 扩展名变化(.mp4 → .mov)同 key
  3. 课程/章/节/role 不同则 key 不同
  4. 路径分隔符不参与计算
  5. 跨大小写归一
  6. normalize_saved_name
  7. _mark_resume_keys 把 OK 资源的 CREATE 转 SKIP
  8. _mark_retry_keys 只留 retry_keys 里的 CREATE
  9. resume + retry 同时给(应优先 retry)
  10. _retry_resources.json 写出 + 读回
  11. write_retry_resources 没失败时返回 None
  12. retry_keys=空时所有 CREATE 跳
  13. Asset.resource_key 字段默认 ""
  14. run_upload_api 集成:resume 跳过已成功的(端到端 mock)
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from scrape_new.upload.models import (
    Asset, AssetStatus, Chapter, ContentType, CourseStructure, Lesson, UploadResult,
)
from scrape_new.upload.naming import lesson_filename
from scrape_new.upload.report import (
    save_manifest, write_retry_resources, load_retry_resources, load_manifest,
)
from scrape_new.upload.resource_key import (
    make_resource_key, normalize_saved_name,
)
from scrape_new.upload.sync_tree import (
    compute_diff, DiffAction, LeafDiff,
)
from scrape_new.upload.api_uploader import (
    _mark_resume_keys, _mark_retry_keys,
)


# ─── 1) make_resource_key 稳定 ─────────────────────────────

class TestResourceKeyStable:
    def test_same_input_same_key(self):
        k1 = make_resource_key("15939407", 1, "1.1", "video", "1.1_技术.mp4")
        k2 = make_resource_key("15939407", 1, "1.1", "video", "1.1_技术.mp4")
        assert k1 == k2

    def test_16_char_hex(self):
        k = make_resource_key("x", 1, "1.1", "video", "1.1_x.mp4")
        assert len(k) == 16
        assert all(c in "0123456789abcdef" for c in k)

    def test_empty_inputs_dont_crash(self):
        k = make_resource_key("", 0, "", "", "")
        assert len(k) == 16  # 给个稳定值


# ─── 2) 扩展名变化同 key ─────────────────────────────────

class TestResourceKeyExtensionInvariant:
    def test_mp4_to_mov_same_key(self):
        k1 = make_resource_key("c", 1, "1.1", "video", "1.1_x.mp4")
        k2 = make_resource_key("c", 1, "1.1", "video", "1.1_x.mov")
        assert k1 == k2

    def test_mp4_to_uppercase_same_key(self):
        # 归一化:大写 → 小写
        k1 = make_resource_key("c", 1, "1.1", "video", "1.1_x.mp4")
        k2 = make_resource_key("c", 1, "1.1", "video", "1.1_X.MP4")
        assert k1 == k2


# ─── 3) 课程/章/节/role 不同则 key 不同 ────────────────────

class TestResourceKeyDiscriminator:
    def test_different_course_different_key(self):
        k1 = make_resource_key("COURSE_A", 1, "1.1", "video", "1.1_x.mp4")
        k2 = make_resource_key("COURSE_B", 1, "1.1", "video", "1.1_x.mp4")
        assert k1 != k2

    def test_different_chapter_different_key(self):
        k1 = make_resource_key("c", 1, "1.1", "video", "1.1_x.mp4")
        k2 = make_resource_key("c", 2, "1.1", "video", "1.1_x.mp4")
        assert k1 != k2

    def test_different_role_different_key(self):
        k1 = make_resource_key("c", 1, "1.1", "video", "1.1_x.mp4")
        k2 = make_resource_key("c", 1, "1.1", "english", "1.1_x.mp4")
        assert k1 != k2


# ─── 4) 路径分隔符不参与计算 ─────────────────────────────

class TestResourceKeyPathInvariant:
    def test_with_path_prefix_same_key(self):
        k1 = make_resource_key("c", 1, "1.1", "video", "1.1_x.mp4")
        k2 = make_resource_key("c", 1, "1.1", "video", "视频/1.1_x.mp4")
        k3 = make_resource_key("c", 1, "1.1", "video", r"视频\1.1_x.mp4")
        assert k1 == k2 == k3


# ─── 5) normalize_saved_name ───────────────────────────────

class TestNormalizeSavedName:
    def test_strip_path(self):
        assert normalize_saved_name("视频/1.1_x.mp4") == "1.1_x"

    def test_strip_extension(self):
        assert normalize_saved_name("1.1_x.PPTX") == "1.1_x"

    def test_strip_both(self):
        assert normalize_saved_name("video/课程/1.1_x.MP4") == "1.1_x"

    def test_empty(self):
        assert normalize_saved_name("") == ""


# ─── 6) _mark_resume_keys ──────────────────────────────────

def _make_diff_with_two_lessons():
    """构造一个 diff:2 章,每章 1 lesson,每 lesson 2 leaves。"""
    structure = CourseStructure(
        course_id="c1", course_title="t",
        chapters=(
            Chapter(index=1, title="ch1", lessons=(
                Lesson(id="1.1", title="a", content_type=ContentType.VIDEO,
                       video="1.1_a.mp4",
                       attachments=("1.1_a_English.mp4",)),
            )),
            Chapter(index=2, title="ch2", lessons=(
                Lesson(id="2.1", title="b", content_type=ContentType.VIDEO,
                       video="2.1_b.mp4",
                       attachments=("2.1_b_English.mp4",)),
            )),
        ),
    )
    diff = compute_diff(structure, {"chapter_list": []})
    return structure, diff


class TestMarkResumeKeys:
    def test_resume_marks_create_as_skip(self):
        structure, diff = _make_diff_with_two_lessons()
        # 计算 ch1 video 的 key
        video_key = make_resource_key("c1", 1, "1.1", "video", "1.1_a.mp4")
        english_key = make_resource_key("c1", 1, "1.1", "english", "1.1_a_English.mp4")

        new_diff = _mark_resume_keys(diff, {video_key, english_key})
        # ch1 整章应该所有 leaf 都被标 SKIP
        ch1 = new_diff.chapters[0]
        for ld in ch1.lesson_diffs:
            for lfd in ld.leaf_diffs:
                assert lfd.action == DiffAction.SKIP
        # ch2 整章应仍 CREATE(没在 resume set 里)
        ch2 = new_diff.chapters[1]
        for ld in ch2.lesson_diffs:
            for lfd in ld.leaf_diffs:
                assert lfd.action == DiffAction.CREATE

    def test_resume_empty_set_noop(self):
        structure, diff = _make_diff_with_two_lessons()
        new_diff = _mark_resume_keys(diff, set())
        # 没改任何东西
        for cd, ncd in zip(diff.chapters, new_diff.chapters):
            for ld, nld in zip(cd.lesson_diffs, ncd.lesson_diffs):
                for lfd, nlfd in zip(ld.leaf_diffs, nld.leaf_diffs):
                    assert lfd.action == nlfd.action

    def test_resume_updates_stats(self):
        structure, diff = _make_diff_with_two_lessons()
        before_create = diff.stats.get("create_leaves", 0)
        # ch1 的所有 leaf = 2 (video + english)
        video_key = make_resource_key("c1", 1, "1.1", "video", "1.1_a.mp4")
        english_key = make_resource_key("c1", 1, "1.1", "english", "1.1_a_English.mp4")
        new_diff = _mark_resume_keys(diff, {video_key, english_key})
        # create_leaves 应 -2(2 个被标 SKIP)
        assert new_diff.stats["create_leaves"] == before_create - 2
        assert new_diff.stats["skip"] >= 2


# ─── 7) _mark_retry_keys ─────────────────────────────────

class TestMarkRetryKeys:
    def test_retry_only_runs_target_leaves(self):
        structure, diff = _make_diff_with_two_lessons()
        # 只重试 ch1 video
        video_key = make_resource_key("c1", 1, "1.1", "video", "1.1_a.mp4")

        new_diff = _mark_retry_keys(diff, {video_key})
        # ch1 video 应仍 CREATE,ch1 english 应 SKIP
        ch1 = new_diff.chapters[0]
        for ld in ch1.lesson_diffs:
            for lfd in ld.leaf_diffs:
                if lfd.kind == "video":
                    assert lfd.action == DiffAction.CREATE
                else:
                    assert lfd.action == DiffAction.SKIP
        # ch2 全 SKIP
        ch2 = new_diff.chapters[1]
        for ld in ch2.lesson_diffs:
            for lfd in ld.leaf_diffs:
                assert lfd.action == DiffAction.SKIP

    def test_retry_empty_skips_everything(self):
        structure, diff = _make_diff_with_two_lessons()
        new_diff = _mark_retry_keys(diff, set())
        # 所有 CREATE 都被标 SKIP
        for cd in new_diff.chapters:
            for ld in cd.lesson_diffs:
                for lfd in ld.leaf_diffs:
                    assert lfd.action == DiffAction.SKIP


# ─── 8) _retry_resources.json ────────────────────────────

def _make_result_with_failures():
    return UploadResult(
        course_id="c1", course_title="t",
        started_at="2026-01-01T00:00:00",
        finished_at="2026-01-01T00:01:00",
        assets=(
            Asset(
                chapter_index=1, lesson_id="1.1", lesson_title="a",
                content_type=ContentType.VIDEO,
                source_path="1.1_a.mp4",
                status=AssetStatus.OK,
                resource_key=make_resource_key("c1", 1, "1.1", "video", "1.1_a.mp4"),
            ),
            Asset(
                chapter_index=1, lesson_id="1.1", lesson_title="a",
                content_type=ContentType.VIDEO,
                source_path="1.1_a_English.mp4",
                status=AssetStatus.FAILED,
                error="BokeCC 502",
                resource_key=make_resource_key("c1", 1, "1.1", "english", "1.1_a_English.mp4"),
            ),
            Asset(
                chapter_index=1, lesson_id="1.1", lesson_title="a",
                content_type=ContentType.ATTACHMENT,
                source_path="1.1_a_PPT.pptx",
                status=AssetStatus.SUSPICIOUS,
                error="文件过小",
                resource_key=make_resource_key("c1", 1, "1.1", "ppt", "1.1_a_PPT.pptx"),
            ),
            # PENDING(模拟 RENAME 待确认)— 应进 pending_actions,不进 assets
            Asset(
                chapter_index=2, lesson_id="-", lesson_title="老章名",
                content_type=ContentType.OTHER,
                source_path=None,
                status=AssetStatus.PENDING,
                error="rename_pending: '老章名' → '新章名',需 confirm_rename=True",
                resource_key="",  # RENAME 没 key
            ),
        ),
    )


class TestRetryResources:
    def test_write_returns_path_with_failures(self, tmp_path: Path):
        result = _make_result_with_failures()
        path = write_retry_resources(result, tmp_path)
        assert path is not None
        assert path.name == "_retry_resources.json"
        assert path.exists()

    def test_write_returns_none_when_no_failures(self, tmp_path: Path):
        result = UploadResult(
            course_id="c1", course_title="t",
            started_at="x", finished_at="y",
            assets=(
                Asset(chapter_index=1, lesson_id="1.1", lesson_title="a",
                      content_type=ContentType.VIDEO, source_path="1.1_a.mp4",
                      status=AssetStatus.OK,
                      resource_key="k1"),
            ),
        )
        path = write_retry_resources(result, tmp_path)
        assert path is None
        # 也不该写文件
        assert not (tmp_path / "_retry_resources.json").exists()

    def test_write_only_failed_suspicious_pending(self, tmp_path: Path):
        result = _make_result_with_failures()
        path = write_retry_resources(result, tmp_path)
        data = load_retry_resources(path)
        # 1 OK 被过滤,2 个失败/可疑进 assets
        assert data["count"] == 2
        statuses = {a["status"] for a in data["assets"]}
        assert statuses == {"failed", "suspicious"}
        # resource_key 都是 16 字符 hex(不是明文)
        for a in data["assets"]:
            k = a["resource_key"]
            assert len(k) == 16
            assert all(c in "0123456789abcdef" for c in k)

    def test_pending_assets_go_to_separate_field(self, tmp_path: Path):
        """PENDING(RENAME 待确认)不进 assets,进 pending_actions(无 resource_key)"""
        result = _make_result_with_failures()
        path = write_retry_resources(result, tmp_path)
        data = load_retry_resources(path)
        # assets 里只有 FAILED/SUSPICIOUS(2 个)
        assert data["count"] == 2
        # pending_actions 里 1 个 PENDING
        assert "pending_actions" in data
        assert len(data["pending_actions"]) == 1
        p = data["pending_actions"][0]
        assert p["chapter_index"] == 2
        assert p["kind"] == "rename_pending"
        assert "rename_pending" in p["description"]
        # PENDING 没有 resource_key(不污染 assets)

    def test_roundtrip(self, tmp_path: Path):
        result = _make_result_with_failures()
        path = write_retry_resources(result, tmp_path)
        data = load_retry_resources(path)
        # 写回 dict 后可再用
        for a in data["assets"]:
            assert "resource_key" in a
            assert "chapter_index" in a
            assert "lesson_id" in a
            assert "source_path" in a
        # pending_actions 也有
        assert "pending_actions" in data

    def test_no_failures_no_pendings_returns_none(self, tmp_path: Path):
        """只有 OK 时,既不写文件也不返 path"""
        result = UploadResult(
            course_id="c1", course_title="t",
            started_at="x", finished_at="y",
            assets=(
                Asset(chapter_index=1, lesson_id="1.1", lesson_title="a",
                      content_type=ContentType.VIDEO, source_path="1.1_a.mp4",
                      status=AssetStatus.OK,
                      resource_key="k1"),
            ),
        )
        path = write_retry_resources(result, tmp_path)
        assert path is None


# ─── 12) 安全:cmd_retry_resources 空清单早返 ─────────────

class TestRetryResourcesCLIEmpty:
    """cmd_retry_resources 读到 input 没可重试 resource_key 时,直接返回 0
    不调 run_upload_api,避免空流程 + 误导用户。"""

    def test_empty_retry_keys_returns_0_no_upload(self, tmp_path: Path, monkeypatch, capsys):
        from scrape_new.upload.cli import cmd_retry_resources
        from argparse import Namespace

        # 写一个 _retry_resources.json:只有 pending_actions,没 assets
        retry_data = {
            "generated_at": "2026-06-17T00:00:00",
            "course_id": "c1",
            "course_title": "t",
            "count": 0,
            "assets": [],
            "pending_actions": [
                {"chapter_index": 2, "kind": "rename_pending",
                 "description": "rename_pending: 老章名 → 新章名"},
            ],
        }
        input_path = tmp_path / "_retry_resources.json"
        input_path.write_text(json.dumps(retry_data), encoding="utf-8")

        # 写一个最小 mapping(避免 mapping 校验报错)
        mapping_data = {
            "course_id": "c1", "course_title": "t",
            "chapters": [],
        }
        mapping_path = tmp_path / "_mapping.json"
        mapping_path.write_text(json.dumps(mapping_data), encoding="utf-8")

        # mock run_upload_api,验证它不应被调
        from scrape_new.upload import cli as cli_mod
        upload_called = []
        original_upload = cli_mod.run_upload_api if hasattr(cli_mod, "run_upload_api") else None
        def fake_upload(*args, **kwargs):
            upload_called.append((args, kwargs))
            return None
        # cli.py 直接 from .api_uploader import run_upload_api,得 patch api_uploader 模块
        from scrape_new.upload import api_uploader
        monkeypatch.setattr(api_uploader, "run_upload_api", fake_upload)

        args = Namespace(
            input=str(input_path),
            mapping=str(mapping_path),
            course_id=None,
            cookies=None,
            cookies_string="x=y",
            videos=None,
            output=None,
            dry_run=False,
            yes=True,  # 跳过交互确认
        )
        rc = cmd_retry_resources(args)

        # 1. 返回 0(不报错)
        assert rc == 0
        # 2. 不调 run_upload_api(空清单早返,不应该走到上传)
        assert upload_called == [], \
            f"空 retry 清单仍调了 run_upload_api: {upload_called}"
        # 3. 终端提示
        out = capsys.readouterr().out
        assert "没有任何可自动重试" in out
        assert "rename" in out.lower() or "pending" in out.lower()


# ─── 13) 安全:pending_actions 不触发上传 ──────────────────

class TestPendingActionsNeverTriggerUpload:
    """PENDING(RENAME 待确认)没 resource_key,任何 retry/resume 路径都不该让它上传。"""

    def test_retry_with_only_pending_actions_does_nothing(self, tmp_path: Path):
        """input 仅有 pending_actions,retry_keys 为空 → run_upload_api 早返(走 P1-1 路径)"""
        from scrape_new.upload.api_uploader import run_upload_api

        # 构造 mapping:有 1 个 RENAME 命中的章
        structure = CourseStructure(
            course_id="c1", course_title="t",
            chapters=(
                Chapter(index=1, title="第一章 新标题", lessons=(
                    Lesson(id="1.1", title="a", content_type=ContentType.VIDEO,
                           video="1.1_a.mp4"),
                )),
            ),
        )

        from scrape_new.upload import api_uploader as apimod
        api_calls = []
        def boom(*args, **kwargs):
            api_calls.append("create_chapter")
            raise AssertionError("pending_actions-only 不应触发写")
        apimod.create_chapter = boom

        # input 只有 pending_actions,没 assets → retry_keys 为 set()
        result = run_upload_api(
            structure=structure,
            videos_folder=tmp_path,
            cookies_string="csrftoken=fake; xtbz=cloud; university_id=1",
            output_dir=tmp_path,
            retry_keys=set(),  # 来自 P1-2 早返
        )

        # PENDING 没 key → retry_keys 过滤后空 → 早返,啥也不做
        assert result.assets == ()
        assert result.delta() == 0
        assert api_calls == []

    def test_mark_retry_keys_ignores_pending(self, tmp_path: Path):
        """_mark_retry_keys 用 key 匹配,PENDING 没 key 自动忽略"""
        structure = CourseStructure(
            course_id="c1", course_title="t",
            chapters=(
                Chapter(index=1, title="ch1", lessons=(
                    Lesson(id="1.1", title="a", content_type=ContentType.VIDEO,
                           video="1.1_a.mp4"),
                )),
            ),
        )
        diff = compute_diff(structure, {"chapter_list": []})

        # retry_keys 只含"一个空字符串"(模拟 PENDING key 漏出)
        # _mark_retry_keys 不应把它当作有效 key
        new_diff = _mark_retry_keys(diff, {""})
        # 所有 CREATE 仍转 SKIP(没 key 匹配)
        for cd in new_diff.chapters:
            for ld in cd.lesson_diffs:
                for lfd in ld.leaf_diffs:
                    assert lfd.action == DiffAction.SKIP
        # 没 stats 异常
        assert new_diff.stats["create"] == 0


# ─── 9) Asset.resource_key 字段默认 "" ──────────────────

class TestAssetResourceKey:
    def test_default_empty(self):
        a = Asset(
            chapter_index=1, lesson_id="1.1", lesson_title="a",
            content_type=ContentType.VIDEO, source_path="1.1_a.mp4",
        )
        assert a.resource_key == ""

    def test_explicit_set(self):
        a = Asset(
            chapter_index=1, lesson_id="1.1", lesson_title="a",
            content_type=ContentType.VIDEO, source_path="1.1_a.mp4",
            resource_key="abc123def456",
        )
        assert a.resource_key == "abc123def456"


# ─── 10) load_manifest 读 resource_key(阻断 bug 修复) ─────

class TestLoadManifestPreservesResourceKey:
    """codex 复核发现阻断 bug:load_manifest 旧版没读 resource_key,
    导致 --resume 实际不生效(OK 资源 key 全是空字符串,prev_ok_keys 永远空)。

    这个测试 + 修复确保 round-trip 保留 resource_key。
    """

    def test_save_load_round_trip_preserves_resource_key(self, tmp_path: Path):
        # 1. 构造含 resource_key 的 UploadResult
        original_key = make_resource_key(
            "c1", 1, "1.1", "video", "1.1_技术.mp4",
        )
        result = UploadResult(
            course_id="c1", course_title="t",
            started_at="x", finished_at="y",
            assets=(
                Asset(
                    chapter_index=1, lesson_id="1.1", lesson_title="技术",
                    content_type=ContentType.VIDEO, source_path="1.1_技术.mp4",
                    status=AssetStatus.OK,
                    target_url="https://example.com/leaf/123",
                    attempts=1, bytes_uploaded=1024,
                    uploaded_at="2026-06-17T10:00:00",
                    resource_key=original_key,
                ),
            ),
        )
        # 2. 写 manifest
        manifest_path = tmp_path / "_upload_manifest.json"
        save_manifest(result, manifest_path)
        # 3. 读回
        loaded = load_manifest(manifest_path)
        assert loaded is not None
        # 4. 关键断言:resource_key 保留
        assert len(loaded.assets) == 1
        assert loaded.assets[0].resource_key == original_key, (
            f"resource_key 丢: 原 {original_key!r}, 读回 {loaded.assets[0].resource_key!r}"
        )

    def test_load_manifest_old_format_without_resource_key(self, tmp_path: Path):
        """向后兼容:旧 manifest 没 resource_key 字段时,默认 ""(不崩)"""
        # 模拟旧 manifest
        old_data = {
            "course_id": "c1",
            "course_title": "t",
            "started_at": "x",
            "finished_at": "y",
            "assets": [
                {
                    "chapter_index": 1, "lesson_id": "1.1",
                    "lesson_title": "a", "content_type": "video",
                    "source_path": "1.1_a.mp4",
                    "status": "ok", "attempts": 1,
                    # 没 resource_key 字段
                }
            ]
        }
        path = tmp_path / "old_manifest.json"
        path.write_text(json.dumps(old_data), encoding="utf-8")

        loaded = load_manifest(path)
        assert loaded is not None
        assert loaded.assets[0].resource_key == ""  # 兜底

    def test_resume_actually_skips_after_manifest_round_trip(self, tmp_path: Path):
        """端到端:--resume 路径能正确跳过(关键)"""
        from scrape_new.upload.models import course_structure_from_dict
        from scrape_new.upload.api_uploader import _mark_resume_keys
        from scrape_new.upload.sync_tree import compute_diff, DiffAction

        # 旧 manifest:1 OK
        ok_key = make_resource_key(
            "c1", 1, "1.1", "video", "1.1_a.mp4",
        )
        result = UploadResult(
            course_id="c1", course_title="t",
            started_at="x", finished_at="y",
            assets=(
                Asset(
                    chapter_index=1, lesson_id="1.1", lesson_title="a",
                    content_type=ContentType.VIDEO, source_path="1.1_a.mp4",
                    status=AssetStatus.OK,
                    resource_key=ok_key,
                ),
            ),
        )
        manifest = tmp_path / "_upload_manifest.json"
        save_manifest(result, manifest)

        # 读旧 manifest,提取 OK keys
        prev = load_manifest(manifest)
        prev_ok_keys = {
            a.resource_key for a in prev.assets
            if a.status == AssetStatus.OK and a.resource_key
        }
        assert ok_key in prev_ok_keys, "读回的 manifest 应含原 OK key"

        # 用 _mark_resume_keys 处理新 diff
        structure = course_structure_from_dict({
            "course_id": "c1", "course_title": "t",
            "chapters": [{
                "index": 1, "title": "ch1", "lessons": [{
                    "id": "1.1", "title": "a", "content_type": "video",
                    "video": "1.1_a.mp4", "attachments": [],
                }],
            }],
        })
        diff = compute_diff(structure, {"chapter_list": []})
        new_diff = _mark_resume_keys(diff, prev_ok_keys)
        # 1.1 的 video 应转 SKIP
        lesson_diff = new_diff.chapters[0].lesson_diffs[0]
        assert lesson_diff.leaf_diffs[0].action == DiffAction.SKIP, \
            f"--resume 后应 SKIP, 实际 {lesson_diff.leaf_diffs[0].action}"


# ─── 11) 安全:retry_keys=set() 早返(避免空跑) ──────────

class TestRetryKeysEmptyEarlyReturn:
    """retry_keys=set() 是显式"无重试目标",不应跑空流程写空 manifest。"""

    def test_retry_keys_empty_returns_early(self, tmp_path: Path, monkeypatch):
        from scrape_new.upload.api_uploader import run_upload_api

        structure = CourseStructure(
            course_id="c1", course_title="t",
            chapters=(
                Chapter(index=1, title="ch1", lessons=(
                    Lesson(id="1.1", title="a", content_type=ContentType.VIDEO,
                           video="1.1_a.mp4"),
                )),
            ),
        )

        # mock 网络层
        from scrape_new.upload import api_uploader as apimod
        api_uploader_calls = []

        def boom_create_chapter(*args, **kwargs):
            api_uploader_calls.append("create_chapter")
            raise AssertionError("retry_keys=set() 不应触发 create_chapter")

        monkeypatch.setattr(apimod, "create_chapter", boom_create_chapter)

        result = run_upload_api(
            structure=structure,
            videos_folder=tmp_path,
            cookies_string="csrftoken=fake; xtbz=cloud; university_id=1",
            output_dir=tmp_path,
            retry_keys=set(),  # ← 关键:空集
        )

        # 1. 没调任何写 API
        assert api_uploader_calls == [], \
            f"retry_keys=set() 调了写 API: {api_uploader_calls}"
        # 2. 返回的 result 是空 UploadResult(delta=0,无 assets)
        assert result.assets == ()
        assert result.delta() == 0
        # 3. 不应写 manifest(避免污染)
        assert not (tmp_path / "_upload_manifest.json").exists()
        # 4. 不应写 retry_resources
        assert not (tmp_path / "_retry_resources.json").exists()