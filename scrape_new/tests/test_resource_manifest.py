"""
测试:scrape_new.services.resource_manifest

覆盖(8 测试):
  1. 单视频 lesson → JSON 输出
  2. 同一 lesson 多资源(主+英文+PPT)聚合到 resources
  3. skipped_existing 写入 size_bytes 和 reason
  4. failed 资源也进入 JSON/CSV(不静默丢)
  5. CSV 是 UTF-8-BOM(Excel 直接打开)
  6. Markdown 包含章节名、lesson id、saved_name、status
  7. source_meta 保留 objectid/knowledge_id/tab_num
  8. 相对路径是 视频/... / 文档/...,不是绝对路径
"""

from __future__ import annotations

import csv
import json
from pathlib import Path

import pytest

from scrape_new.services.resource_manifest import (
    ALL_STATUSES,
    STATUS_DOWNLOADED,
    STATUS_FAILED,
    STATUS_SKIPPED_EXISTING,
    STATUS_SUSPICIOUS,
    build_chapter_tree_data,
    build_resource_naming_records,
    make_resource_record_fields,
    write_chapter_tree_json,
    write_chapter_tree_md,
    write_download_resource_manifests,
    write_resource_naming_manifest_csv,
    write_resource_naming_manifest_json,
)


def _sample_videos():
    return [
        {
            "ch_num": 1, "ls_num": 1, "chapter": "第一章 概述", "lesson": "技术",
            "name": "Thermodynamic Laws", "role": "video",
            "filename": "1.1_技术.mp4", "status": STATUS_DOWNLOADED,
            "size_bytes": 123_456_789, "reason": "",
            "source_meta": {"objectid": "abc123", "knowledge_id": "k-1",
                            "tab_num": 0, "url": "https://dl.example.com/v.mp4"},
        },
        {
            "ch_num": 1, "ls_num": 1, "chapter": "第一章 概述", "lesson": "技术",
            "name": "Thermodynamic Laws (EN)", "role": "english",
            "filename": "1.1_技术_English.mp4", "status": STATUS_DOWNLOADED,
            "size_bytes": 98_765_432, "reason": "",
            "source_meta": {"objectid": "def456", "knowledge_id": "k-1",
                            "tab_num": 2, "url": "https://dl.example.com/v-en.mp4"},
        },
    ]


def _sample_docs():
    return [
        {
            "ch_num": 1, "ls_num": 1, "chapter": "第一章 概述", "lesson": "技术",
            "name": "课件.pptx", "role": "ppt", "filename": "1.1_技术_PPT.pptx",
            "status": STATUS_SKIPPED_EXISTING, "size_bytes": 2_000_000,
            "reason": "file already exists",
            "source_meta": {"objectid": "ghi789", "knowledge_id": "k-1"},
        },
        {
            "ch_num": 1, "ls_num": 2, "chapter": "第一章 概述", "lesson": "媒体",
            "name": "媒体课件.pdf", "role": "pdf",
            "filename": "1.2_媒体_课件.pdf",
            "status": STATUS_FAILED, "size_bytes": 0,
            "reason": "无下载链接",
            "source_meta": {"objectid": "jkl012", "knowledge_id": "k-2"},
        },
    ]


# ─── 1) 单视频 lesson 输出 JSON ─────────────────────────────

class TestSingleVideoJSON:
    def test_single_video_single_lesson(self):
        videos = [_sample_videos()[0]]  # 只有主视频
        tree = build_chapter_tree_data(
            "测试课", "chaoxing", "https://example.com", videos, [],
        )
        assert tree["course_title"] == "测试课"
        assert tree["platform"] == "chaoxing"
        assert tree["source_url"] == "https://example.com"
        assert tree["generated_at"]  # ISO8601 非空
        assert len(tree["chapters"]) == 1
        ch = tree["chapters"][0]
        assert ch["index"] == 1
        assert ch["title"] == "第一章 概述"
        assert len(ch["lessons"]) == 1
        ls = ch["lessons"][0]
        assert ls["id"] == "1.1"
        assert ls["title"] == "技术"
        assert len(ls["resources"]) == 1
        r = ls["resources"][0]
        assert r["role"] == "video"
        assert r["saved_name"] == "1.1_技术.mp4"
        assert r["relative_path"] == "视频/1.1_技术.mp4"
        assert r["status"] == STATUS_DOWNLOADED
        assert r["size_bytes"] == 123_456_789
        assert r["source_meta"]["objectid"] == "abc123"

    def test_json_serializable(self):
        # JSON 序列化无异常
        tree = build_chapter_tree_data("x", "p", "u", _sample_videos(), _sample_docs())
        text = json.dumps(tree, ensure_ascii=False)
        assert "第一章" in text


# ─── 2) 同一 lesson 多资源聚合 ───────────────────────────

class TestLessonAggregatesMultipleResources:
    def test_one_lesson_has_video_english_ppt(self):
        videos = _sample_videos()
        docs = [_sample_docs()[0]]  # PPT 在 1.1
        tree = build_chapter_tree_data("x", "chaoxing", "u", videos, docs)
        ls11 = tree["chapters"][0]["lessons"][0]
        assert ls11["id"] == "1.1"
        # 1.1 应有 3 个资源:video + english + ppt
        assert len(ls11["resources"]) == 3
        roles = [r["role"] for r in ls11["resources"]]
        assert "video" in roles
        assert "english" in roles
        assert "ppt" in roles
        # saved_name 全部唯一
        saved = [r["saved_name"] for r in ls11["resources"]]
        assert len(set(saved)) == 3

    def test_records_sort_by_chapter_lesson_role(self):
        records = build_resource_naming_records(_sample_videos(), _sample_docs())
        # 4 个 record,排序:1.1(3) + 1.2(1)
        assert len(records) == 4
        ids = [r["lesson_id"] for r in records]
        assert ids == ["1.1", "1.1", "1.1", "1.2"]


# ─── 3) skipped_existing 写入 size_bytes 和 reason ────────────

class TestSkippedExisting:
    def test_skipped_existing_size_and_reason(self):
        # 模拟磁盘上真有 2MB 文件
        records = build_resource_naming_records(
            [], [_sample_docs()[0]],  # 只有 PPT,skipped_existing
        )
        assert len(records) == 1
        r = records[0]
        assert r["status"] == STATUS_SKIPPED_EXISTING
        assert r["size_bytes"] == 2_000_000
        assert r["reason"] == "file already exists"
        assert r["saved_name"] == "1.1_技术_PPT.pptx"
        assert r["relative_path"] == "文档/1.1_技术_PPT.pptx"


# ─── 4) failed 资源也进 JSON/CSV ──────────────────────────

class TestFailedIncluded:
    def test_failed_record_in_json_and_csv(self, tmp_path: Path):
        videos = []
        docs = [_sample_docs()[1]]  # failed 的 PDF
        # 写 JSON
        records = build_resource_naming_records(videos, docs)
        write_resource_naming_manifest_json(
            records, tmp_path,
            meta={"course_title": "x", "platform": "chaoxing"},
        )
        json_path = tmp_path / "_resource_naming_manifest.json"
        data = json.loads(json_path.read_text(encoding="utf-8"))
        assert data["count"] == 1
        assert data["records"][0]["status"] == STATUS_FAILED
        assert data["records"][0]["reason"] == "无下载链接"
        # 写 CSV
        write_resource_naming_manifest_csv(records, tmp_path)
        csv_path = tmp_path / "_resource_naming_manifest.csv"
        with csv_path.open("r", encoding="utf-8-sig", newline="") as f:
            rows = list(csv.DictReader(f))
        assert len(rows) == 1
        assert rows[0]["status"] == STATUS_FAILED
        assert rows[0]["reason"] == "无下载链接"

    def test_all_statuses_constants(self):
        # 验证状态常量完整
        assert STATUS_DOWNLOADED in ALL_STATUSES
        assert STATUS_SKIPPED_EXISTING in ALL_STATUSES
        assert STATUS_FAILED in ALL_STATUSES
        assert STATUS_SUSPICIOUS in ALL_STATUSES
        assert len(ALL_STATUSES) == 4


# ─── 5) CSV 是 UTF-8-BOM ──────────────────────────────────

class TestCSVEncoding:
    def test_csv_has_utf8_bom(self, tmp_path: Path):
        records = build_resource_naming_records(_sample_videos(), _sample_docs())
        write_resource_naming_manifest_csv(records, tmp_path)
        csv_path = tmp_path / "_resource_naming_manifest.csv"
        raw = csv_path.read_bytes()
        # UTF-8 BOM = b'\xef\xbb\xbf'
        assert raw[:3] == b"\xef\xbb\xbf", f"缺 UTF-8-BOM,头 = {raw[:10]!r}"
        # 解码后含中文
        text = raw.decode("utf-8-sig")
        assert "第一章" in text
        assert "1.1_技术.mp4" in text


# ─── 6) Markdown 包含章节名、lesson id、saved_name、status ──

class TestMarkdownContent:
    def test_markdown_contains_required_fields(self, tmp_path: Path):
        videos = _sample_videos()
        docs = _sample_docs()
        tree = build_chapter_tree_data("测试课", "chaoxing", "u", videos, docs)
        write_chapter_tree_md(tree, tmp_path)
        md = (tmp_path / "_chapter_tree.md").read_text(encoding="utf-8")
        # 课程名
        assert "# 测试课" in md
        # 章节名
        assert "第一章 概述" in md
        # lesson id
        assert "1.1" in md
        assert "1.2" in md
        # saved_name
        assert "1.1_技术.mp4" in md
        assert "1.1_技术_English.mp4" in md
        assert "1.1_技术_PPT.pptx" in md
        # status
        assert "downloaded" in md
        assert "skipped_existing" in md
        assert "failed" in md
        # role
        assert "[video]" in md
        assert "[english]" in md
        assert "[ppt]" in md
        # 相对路径
        assert "视频/" in md
        assert "文档/" in md

    def test_markdown_human_readable_format(self, tmp_path: Path):
        # 验证层级缩进
        videos = [_sample_videos()[0]]
        tree = build_chapter_tree_data("课", "p", "u", videos, [])
        write_chapter_tree_md(tree, tmp_path)
        md = (tmp_path / "_chapter_tree.md").read_text(encoding="utf-8")
        # 章节是 "- ",lesson 是 "  - ",resource 是 "    - "
        assert "- 第一章 概述" in md
        assert "  - 1.1 技术" in md
        assert "    - [video] 视频/1.1_技术.mp4" in md


# ─── 7) source_meta 保留 objectid/knowledge_id/tab_num ──

class TestSourceMeta:
    def test_source_meta_fields_preserved(self):
        videos = _sample_videos()
        tree = build_chapter_tree_data("x", "p", "u", videos, [])
        for r in tree["chapters"][0]["lessons"][0]["resources"]:
            sm = r["source_meta"]
            assert sm["objectid"] in ("abc123", "def456")
            assert sm["knowledge_id"] == "k-1"
            assert sm["tab_num"] in (0, 2)
            assert sm["url"].startswith("https://")

    def test_source_meta_omits_irrelevant_keys(self):
        # 平台特定乱字段应被过滤,避免泄漏无关数据
        item = {
            "ch_num": 1, "ls_num": 1, "name": "x", "filename": "1.1_x.mp4",
            "role": "video", "status": STATUS_DOWNLOADED,
            "size_bytes": 100,
            "source_meta": {
                "objectid": "ok", "knowledge_id": "ok",
                "tab_num": 0, "url": "ok",
                "platform_secret_token": "should_be_filtered",  # 平台特有,应过滤
                "internal_user_id": 999,  # 也应过滤
            },
        }
        records = build_resource_naming_records([item], [])
        sm = records[0]["source_meta"]
        assert "objectid" in sm
        assert "knowledge_id" in sm
        assert "tab_num" in sm
        assert "url" in sm
        # 无关键不进入
        assert "platform_secret_token" not in sm
        assert "internal_user_id" not in sm

    def test_make_resource_record_fields_helper(self):
        # helper 函数:workflow 用来统一生成 record
        rec = make_resource_record_fields(
            ch_num=1, ls_num=1, chapter="ch1", lesson="ls1",
            name="课件.pptx", role="ppt", filename="1.1_ls1_PPT.pptx",
            status=STATUS_DOWNLOADED, size_bytes=1024,
            source_meta={"objectid": "x"},
            kind_dir="doc",  # 关键:文档类必须传 doc,否则默认走视频路径
        )
        assert rec["relative_path"] == "文档/1.1_ls1_PPT.pptx"
        assert rec["extension"] == "pptx"
        assert rec["size_bytes"] == 1024
        assert rec["source_meta"]["objectid"] == "x"


# ─── 8) 相对路径是 视频/... / 文档/...,不是绝对路径 ────

class TestRelativePaths:
    def test_relative_paths_no_absolute(self):
        videos = _sample_videos()
        docs = _sample_docs()
        records = build_resource_naming_records(videos, docs)
        for r in records:
            assert r["relative_path"], f"{r['role']} 必须有 relative_path"
            assert not r["relative_path"].startswith("/"), \
                f"不应以 / 开头(绝对): {r['relative_path']}"
            assert ":\\" not in r["relative_path"], \
                f"不应含 Windows 盘符: {r['relative_path']}"
            # 视频类 → 视频/...
            if r["role"] in ("video", "english"):
                assert r["relative_path"].startswith("视频/"), \
                    f"视频类路径应是 视频/...: {r['relative_path']}"
            # 附件类(ppt/pdf/docx/attachment)→ 文档/...
            elif r["role"] in ("ppt", "pdf", "docx", "doc", "attachment"):
                assert r["relative_path"].startswith("文档/"), \
                    f"附件类路径应是 文档/...: {r['relative_path']}"

    def test_one_shot_write_all_four(self, tmp_path: Path):
        # 一站式接口:5 个文件全部产出(codex 复核要求)
        videos = _sample_videos()
        docs = _sample_docs()
        paths = write_download_resource_manifests(
            "测试课", "chaoxing", "https://example.com",
            videos, docs, tmp_path,
        )
        # 5 个文件存在
        for filename in (
            "_chapter_tree.json",
            "_chapter_tree.md",
            "_resource_naming_manifest.json",
            "_resource_naming_manifest.csv",
            "_review.html",
        ):
            assert (tmp_path / filename).exists(), f"缺 {filename}"
        # 5 个返回值都对
        for k in ("chapter_tree_json", "chapter_tree_md",
                  "manifest_json", "manifest_csv", "review_html"):
            assert k in paths, f"paths 缺键 {k}"
            assert paths[k].exists(), f"{k} 文件不存在"
        # _review.html 应内联章节数据
        html = paths["review_html"].read_text(encoding="utf-8")
        assert "测试课" in html
        assert "_REVIEW_DATA__" in html  # JS 数据容器

    def test_lessons_meta_keeps_empty_lessons(self, tmp_path: Path):
        # 即使没视频,lessons_meta 里有的 lesson 也要保留 entry
        tree = build_chapter_tree_data(
            "x", "p", "u", [], [],
            lessons_meta=[
                {"ch_num": 1, "ls_num": 1, "chapter": "第一章", "lesson": "无资源节"},
            ],
        )
        assert len(tree["chapters"][0]["lessons"]) == 1
        assert tree["chapters"][0]["lessons"][0]["resources"] == []


# ─── 9) 章节 / lesson 排序 ──────────────────────────────────

class TestSorting:
    def test_chapters_sorted_by_index(self):
        # 章节按 index 升序,即使数据乱序
        from scrape_new.services.resource_manifest import build_chapter_tree_data
        # 构造乱序 chapters 数据
        from scrape_new.services.resource_manifest import (
            build_chapter_tree_data as fn,
        )
        # ch2 排在 ch1 前
        videos = [
            {"ch_num": 2, "ls_num": 1, "name": "x", "filename": "2.1_x.mp4",
             "role": "video", "status": STATUS_DOWNLOADED, "size_bytes": 100,
             "lesson": "ls", "chapter": "第二章"},
            {"ch_num": 1, "ls_num": 1, "name": "y", "filename": "1.1_y.mp4",
             "role": "video", "status": STATUS_DOWNLOADED, "size_bytes": 100,
             "lesson": "ls", "chapter": "第一章"},
        ]
        tree = fn("x", "p", "u", videos, [])
        assert [ch["index"] for ch in tree["chapters"]] == [1, 2]

    def test_lessons_sorted_by_id_within_chapter(self):
        videos = [
            {"ch_num": 1, "ls_num": 3, "name": "x", "filename": "1.3_x.mp4",
             "role": "video", "status": STATUS_DOWNLOADED, "size_bytes": 100,
             "lesson": "3rd", "chapter": "第一章"},
            {"ch_num": 1, "ls_num": 1, "name": "y", "filename": "1.1_y.mp4",
             "role": "video", "status": STATUS_DOWNLOADED, "size_bytes": 100,
             "lesson": "1st", "chapter": "第一章"},
        ]
        tree = build_chapter_tree_data("x", "p", "u", videos, [])
        ls_ids = [ls["id"] for ls in tree["chapters"][0]["lessons"]]
        assert ls_ids == ["1.1", "1.3"]


# ─── 10) 防御:缺字段 / 异常 status 兜底 ─────────────────

class TestDefensive:
    def test_missing_status_inferred_from_size(self):
        item = {
            "ch_num": 1, "ls_num": 1, "name": "x", "filename": "1.1_x.mp4",
            "role": "video", "size_bytes": 1024,
            # 没 status
        }
        records = build_resource_naming_records([item], [])
        # size > 0 → downloaded 兜底
        assert records[0]["status"] == STATUS_DOWNLOADED

    def test_empty_inputs(self, tmp_path: Path):
        # 空输入不崩
        paths = write_download_resource_manifests(
            "x", "p", "u", [], [], tmp_path,
        )
        for p in paths.values():
            assert p.exists()
        # 读 manifest JSON,count=0
        data = json.loads(
            (tmp_path / "_resource_naming_manifest.json").read_text(encoding="utf-8")
        )
        assert data["count"] == 0

    def test_review_html_failure_does_not_break_others(self, tmp_path: Path, monkeypatch):
        """Codex 复核要求:review_html 自身崩了,前面 4 个 manifest 仍要写出来"""
        from scrape_new.services import review_html as rh

        def boom(*args, **kwargs):
            raise RuntimeError("review_html simulated crash")

        # review_html 是 resource_manifest 里局部 import,patch 要打到源模块
        monkeypatch.setattr(rh, "build_review_html", boom)

        videos = _sample_videos()
        docs = _sample_docs()
        # 不应抛异常
        paths = write_download_resource_manifests(
            "测试课", "chaoxing", "https://example.com",
            videos, docs, tmp_path,
        )
        # 4 个 manifest 仍写出
        assert (tmp_path / "_chapter_tree.json").exists()
        assert (tmp_path / "_chapter_tree.md").exists()
        assert (tmp_path / "_resource_naming_manifest.json").exists()
        assert (tmp_path / "_resource_naming_manifest.csv").exists()
        # review_html 缺(因为 mock 崩了),但前面 4 个返回值都在
        assert "review_html" not in paths
        for k in ("chapter_tree_json", "chapter_tree_md",
                  "manifest_json", "manifest_csv"):
            assert k in paths
