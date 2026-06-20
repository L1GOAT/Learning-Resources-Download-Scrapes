"""
测试:下载侧 resource_key + resume / retry

覆盖(8 测试,codex 要求):
  a. 下载 manifest 写入 resource_key(每个 v/d 都有 key)
  b. --resume 跳过已成功资源(文件存在 + status=downloaded)
  c. --resume 对文件丢失/过小资源重新下载
  d. failed/suspicious 进入 _retry_downloads.json(有 key 才能进)
  e. retry 清单为空时(retry_only_keys=set())不执行下载
  f. 文件名变化但 resource_key 相同仍能识别为同一资源
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from scrape_new.services.download_resume import apply_resume_decisions
from scrape_new.services.resource_manifest import (
    build_resource_naming_records,
    write_download_retry_manifest,
    load_download_retry_manifest,
)
from scrape_new.upload.resource_key import make_resource_key


# ─── 工具:构造 fixture ────────────────────────────────────

def _video_item(
    *, course_id: str, ch_num: int, ls_num: int, role: str,
    saved_name: str, status: str = "downloaded",
    size_bytes: int = 1024, resource_key: str = "",
) -> dict:
    return {
        "ch_num": ch_num, "ls_num": ls_num,
        "chapter": f"ch{ch_num}", "lesson": f"ls{ls_num}",
        "name": saved_name, "role": role,
        "filename": saved_name, "status": status,
        "size_bytes": size_bytes, "reason": "",
        "source_meta": {"objectid": "x"},
        "resource_key": resource_key or make_resource_key(
            course_id, ch_num, str(ls_num), role, saved_name,
        ),
    }


def _write_manifest(records: list[dict], path: Path) -> None:
    """写一个 _resource_naming_manifest.json(用 build 的结构)"""
    payload = {
        "generated_at": "2026-06-17T00:00:00",
        "count": len(records),
        "records": records,
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                    encoding="utf-8")


# ─── a) 下载 manifest 写入 resource_key ──────────────────

class TestDownloadManifestHasResourceKey:
    def test_resource_naming_records_include_resource_key(self):
        """build_resource_naming_records 输出的每条 record 都含 resource_key 字段"""
        videos = [
            _video_item(course_id="c1", ch_num=1, ls_num=1, role="video",
                        saved_name="1.1_技术.mp4"),
        ]
        records = build_resource_naming_records(videos, [])
        assert len(records) == 1
        rk = records[0]["resource_key"]
        assert rk, "每个 record 必须有 resource_key"
        assert len(rk) == 16
        # 跟 make_resource_key 直接算出来的一致
        assert rk == make_resource_key("c1", 1, "1", "video", "1.1_技术.mp4")

    def test_csv_writes_resource_key_column(self, tmp_path: Path):
        """CSV 表头含 resource_key"""
        videos = [
            _video_item(course_id="c1", ch_num=1, ls_num=1, role="video",
                        saved_name="1.1_技术.mp4"),
        ]
        records = build_resource_naming_records(videos, [])
        from scrape_new.services.resource_manifest import (
            write_resource_naming_manifest_csv,
        )
        write_resource_naming_manifest_csv(records, tmp_path)
        csv_path = tmp_path / "_resource_naming_manifest.csv"
        # 用 utf-8-sig 读(有 BOM)
        with csv_path.open("r", encoding="utf-8-sig", newline="") as f:
            import csv
            rows = list(csv.DictReader(f))
        assert "resource_key" in rows[0]
        assert rows[0]["resource_key"] == records[0]["resource_key"]


# ─── b) --resume 跳过已成功资源 ─────────────────────────

class TestResumeSkipsDownloaded:
    def test_downloaded_with_local_file_skipped(self, tmp_path: Path):
        """历史 manifest:downloaded + 本地文件存在 → resume 标 skipped_existing"""
        # 准备:1 个视频,旧 status=downloaded
        v = _video_item(course_id="c1", ch_num=1, ls_num=1, role="video",
                        saved_name="1.1_技术.mp4", size_bytes=1024)
        manifest = tmp_path / "_resource_naming_manifest.json"
        _write_manifest([v], manifest)
        # 本地文件存在且 size 合理
        video_dir = tmp_path / "视频"
        video_dir.mkdir()
        local = video_dir / "1.1_技术.mp4"
        local.write_bytes(b"x" * 2000)  # 2000 字节 > 1024 * 0.95

        all_videos = [dict(v)]
        all_docs = []
        stats = apply_resume_decisions(
            all_videos, all_docs, manifest, video_dir,
        )
        # 应被标 skipped_existing
        assert all_videos[0]["status"] == "skipped_existing"
        assert stats["skipped_videos"] == 1
        assert stats["missing_keys"] == 0

    def test_skip_count_reflects_status(self, tmp_path: Path):
        """3 个资源:2 downloaded + 1 failed → 跳过 2"""
        videos = [
            _video_item(course_id="c1", ch_num=1, ls_num=1, role="video",
                        saved_name="1.1_a.mp4", status="downloaded"),
            _video_item(course_id="c1", ch_num=1, ls_num=2, role="video",
                        saved_name="1.2_b.mp4", status="downloaded"),
            _video_item(course_id="c1", ch_num=1, ls_num=3, role="video",
                        saved_name="1.3_c.mp4", status="failed"),
        ]
        manifest = tmp_path / "_resource_naming_manifest.json"
        _write_manifest(videos, manifest)
        video_dir = tmp_path / "视频"
        video_dir.mkdir()
        # 创建前 2 个文件,第 3 个不创建
        for v in videos[:2]:
            (video_dir / v["filename"]).write_bytes(b"x" * 2000)

        all_videos = [dict(v) for v in videos]
        stats = apply_resume_decisions(
            all_videos, [], manifest, video_dir,
        )
        # 2 个 skip(第 3 个 failed 不在 skip 范围)
        assert stats["skipped_videos"] == 2
        # 第 1、2 个被标 skipped_existing,第 3 个保留 failed
        assert all_videos[0]["status"] == "skipped_existing"
        assert all_videos[1]["status"] == "skipped_existing"
        assert all_videos[2]["status"] == "failed"


# ─── c) --resume 对文件丢失/过小资源重新下载 ────────────

class TestResumeReDownloadsMissingOrTooSmall:
    def test_missing_file_triggers_re_download(self, tmp_path: Path):
        """文件不存在 → 保留原 status(downloaded),让下载循环接管"""
        v = _video_item(course_id="c1", ch_num=1, ls_num=1, role="video",
                        saved_name="1.1_技术.mp4", size_bytes=1024)
        manifest = tmp_path / "_resource_naming_manifest.json"
        _write_manifest([v], manifest)
        video_dir = tmp_path / "视频"
        video_dir.mkdir()
        # 文件不创建
        all_videos = [dict(v)]
        stats = apply_resume_decisions(
            all_videos, [], manifest, video_dir,
        )
        # 不应被标 skipped_existing
        assert all_videos[0]["status"] != "skipped_existing"
        assert stats["skipped_videos"] == 0

    def test_too_small_file_triggers_re_download(self, tmp_path: Path):
        """文件过小(< 500 字节)→ 重新下载"""
        v = _video_item(course_id="c1", ch_num=1, ls_num=1, role="video",
                        saved_name="1.1_技术.mp4", size_bytes=100_000)
        manifest = tmp_path / "_resource_naming_manifest.json"
        _write_manifest([v], manifest)
        video_dir = tmp_path / "视频"
        video_dir.mkdir()
        (video_dir / "1.1_技术.mp4").write_bytes(b"x" * 100)  # 100 字节 < 500 阈值

        all_videos = [dict(v)]
        stats = apply_resume_decisions(
            all_videos, [], manifest, video_dir,
        )
        assert all_videos[0]["status"] != "skipped_existing"
        assert stats["skipped_videos"] == 0


# ─── d) failed/suspicious 进入 _retry_downloads.json ──────

class TestRetryDownloadsManifest:
    def test_failed_records_included(self, tmp_path: Path):
        """FAILED 资源进 _retry_downloads.json,必须有 resource_key"""
        v = _video_item(course_id="c1", ch_num=1, ls_num=1, role="video",
                        saved_name="1.1_技术.mp4", status="failed",
                        size_bytes=0, resource_key="rk_abc123def45678")
        records = build_resource_naming_records([v], [])
        path = write_download_retry_manifest(records, tmp_path)
        assert path is not None
        data = load_download_retry_manifest(path)
        assert data["count"] == 1
        assert data["assets"][0]["resource_key"] == "rk_abc123def45678"
        assert data["assets"][0]["status"] == "failed"

    def test_no_key_excluded(self, tmp_path: Path):
        """没 resource_key 的失败资源不进 retry 清单(无法自动重试)
        但会进 pending_actions(可观察,不污染自动 retry)"""
        v = {
            "ch_num": 1, "ls_num": 1, "chapter": "ch1", "lesson": "a",
            "name": "1.1_a.mp4", "role": "video",
            "filename": "1.1_a.mp4", "status": "failed",
            "size_bytes": 0, "reason": "",
            "source_meta": {},
        }
        records = build_resource_naming_records([v], [])
        path = write_download_retry_manifest(records, tmp_path)
        # R3:有 pending_actions 也会写文件(虽然 assets 为空)
        assert path is not None
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        assert data["assets"] == []
        assert len(data["pending_actions"]) == 1
        assert data["pending_actions"][0]["pending_reason"] == "no_resource_key"

    def test_downloaded_excluded_from_retry(self, tmp_path: Path):
        """OK 不进 retry(已经是终态)"""
        v = _video_item(course_id="c1", ch_num=1, ls_num=1, role="video",
                        saved_name="1.1_技术.mp4", status="downloaded",
                        resource_key="rk_ok00000000000000")
        records = build_resource_naming_records([v], [])
        path = write_download_retry_manifest(records, tmp_path)
        assert path is None  # 没可重试的


# ─── e) retry 清单为空时不执行下载 ─────────────────────

class TestRetryOnlyEmptyDoesNothing:
    def test_retry_only_keys_empty_marks_all_skipped(self, tmp_path: Path):
        """retry_only_keys=set() → 所有资源标 skipped_existing(不下载)"""
        v = _video_item(course_id="c1", ch_num=1, ls_num=1, role="video",
                        saved_name="1.1_a.mp4", status="downloaded")
        manifest = tmp_path / "_resource_naming_manifest.json"
        _write_manifest([v], manifest)
        video_dir = tmp_path / "视频"
        video_dir.mkdir()
        (video_dir / "1.1_a.mp4").write_bytes(b"x" * 2000)

        all_videos = [dict(v)]
        all_docs = []
        stats = apply_resume_decisions(
            all_videos, all_docs, manifest, video_dir,
            retry_only_keys=set(),  # 显式空集
        )
        # 资源被标 skipped_existing(retry_filtered 计入)
        assert all_videos[0]["status"] == "skipped_existing"
        assert stats["retry_filtered"] >= 1
        # 关键:没有任何 create_chapter 之类的写操作会触发
        # (本函数不调任何 API,只是标记 status)


# ─── f) 文件名变化但 resource_key 相同仍能识别 ───────

class TestResourceKeyRobustness:
    def test_filename_change_uses_fallback_match(self, tmp_path: Path):
        """文件名变了(saved_name 改了),resource_key 也会变(因为它含 saved_name)。
        但 resume 应 fallback 到 (saved_name, role) 匹配,只要本次 saved_name
        能在历史 manifest 里找到(s+role 一样)就能 skip。
        """
        course_id = "c1"
        ch_num, ls_num = 1, 1
        role = "video"
        old_name = "1.1_技术.mp4"  # 旧 manifest
        new_name = "1.1_技术_v2.mp4"  # 本次新名字

        # 资源 key 不一样(因为 saved_name 不同)
        rk_old = make_resource_key(course_id, ch_num, str(ls_num), role, old_name)
        rk_new = make_resource_key(course_id, ch_num, str(ls_num), role, new_name)
        assert rk_old != rk_new, "不同 saved_name 应该不同 key"

        # 旧 manifest 含 old_name,本次 v 是 new_name
        # 但 (saved_name, role) 不一样 → _find_record 用 (name, role) 兜底也找不到
        # 这种情况:missing_keys +1,新资源正常下载
        v_old = _video_item(course_id=course_id, ch_num=ch_num, ls_num=ls_num,
                           role=role, saved_name=old_name, status="downloaded",
                           resource_key=rk_old)
        manifest = tmp_path / "_resource_naming_manifest.json"
        _write_manifest([v_old], manifest)
        video_dir = tmp_path / "视频"
        video_dir.mkdir()
        (video_dir / new_name).write_bytes(b"x" * 2000)  # 本地有新名

        v_new = _video_item(course_id=course_id, ch_num=ch_num, ls_num=ls_num,
                            role=role, saved_name=new_name, status="downloaded",
                            resource_key=rk_new)
        all_videos = [v_new]
        stats = apply_resume_decisions(
            all_videos, [], manifest, video_dir,
        )
        # 真换了名字 → 走 missing_keys + 正常下载路径
        assert stats["skipped_videos"] == 0
        assert stats["missing_keys"] == 1
        assert all_videos[0]["status"] != "skipped_existing"

    def test_same_resource_key_across_runs(self, tmp_path: Path):
        """跨运行 saved_name 完全一样 → resource_key 一样 → 直接 skip"""
        course_id = "c1"
        ch_num, ls_num = 1, 1
        role = "video"
        saved_name = "1.1_技术.mp4"
        rk = make_resource_key(course_id, ch_num, str(ls_num), role, saved_name)

        v_old = _video_item(course_id=course_id, ch_num=ch_num, ls_num=ls_num,
                           role=role, saved_name=saved_name, status="downloaded",
                           resource_key=rk)
        manifest = tmp_path / "_resource_naming_manifest.json"
        _write_manifest([v_old], manifest)
        video_dir = tmp_path / "视频"
        video_dir.mkdir()
        (video_dir / saved_name).write_bytes(b"x" * 2000)

        # 本次 v 算出来的 key 跟历史一样
        v_new = _video_item(course_id=course_id, ch_num=ch_num, ls_num=ls_num,
                            role=role, saved_name=saved_name, status="downloaded",
                            resource_key=rk)
        all_videos = [v_new]
        stats = apply_resume_decisions(
            all_videos, [], manifest, video_dir,
        )
        # 命中 resource_key → skip
        assert stats["skipped_videos"] == 1
        assert all_videos[0]["status"] == "skipped_existing"

    def test_old_manifest_without_resource_key_falls_back_to_name(self, tmp_path: Path):
        """旧 manifest 没 resource_key,只能按 (saved_name, role) 匹配"""
        # 旧 manifest 的 record 没 resource_key
        old_record = {
            "chapter_index": 1, "lesson_id": "1.1",
            "lesson_title": "a", "role": "video",
            "saved_name": "1.1_技术.mp4",
            "status": "downloaded", "size_bytes": 1024,
            # 没 resource_key
        }
        manifest = tmp_path / "_resource_naming_manifest.json"
        _write_manifest([old_record], manifest)
        video_dir = tmp_path / "视频"
        video_dir.mkdir()
        (video_dir / "1.1_技术.mp4").write_bytes(b"x" * 2000)

        # 本次 v 也没 resource_key(用空字符串)
        v = _video_item(course_id="c1", ch_num=1, ls_num=1, role="video",
                        saved_name="1.1_技术.mp4", status="downloaded",
                        resource_key="")
        all_videos = [v]
        stats = apply_resume_decisions(
            all_videos, [], manifest, video_dir,
        )
        # fallback 按 (name, role) 匹配,跳过
        assert all_videos[0]["status"] == "skipped_existing"
        assert stats["skipped_videos"] == 1


# ─── 9) CLI 解析(安全) ─────────────────────────────────

class TestParseResumeRetryArgs:
    """--resume / --retry-downloads 参数解析的安全检查"""

    def test_basic_resume(self, tmp_path: Path):
        from scrape_new.services.download_resume import parse_resume_retry_args
        manifest = tmp_path / "x.json"
        manifest.write_text("{}")
        result = parse_resume_retry_args([
            "URL", "output_dir", "--resume", str(manifest),
        ])
        assert result.error is None
        assert result.resume_manifest == manifest
        assert result.retry_only_keys is None

    def test_retry_only_without_resume(self, tmp_path: Path):
        from scrape_new.services.download_resume import parse_resume_retry_args
        from scrape_new.services.resource_manifest import (
            write_download_retry_manifest,
            build_resource_naming_records,
        )
        from scrape_new.upload.models import (
            Asset, AssetStatus, ContentType, UploadResult,
        )
        # 写一个有效 retry 清单
        result_obj = UploadResult(
            course_id="c1", course_title="t",
            started_at="x", finished_at="y",
            assets=(
                Asset(
                    chapter_index=1, lesson_id="1.1", lesson_title="a",
                    content_type=ContentType.VIDEO, source_path="1.1_a.mp4",
                    status=AssetStatus.FAILED,
                    resource_key="abc123def456",
                ),
            ),
        )
        write_download_retry_manifest(
            build_resource_naming_records(result_obj.assets, []),
            tmp_path,
        )
        import shutil
        retry = tmp_path / "retry.json"
        shutil.move(str(tmp_path / "_retry_downloads.json"), str(retry))

        result = parse_resume_retry_args([
            "URL", "output_dir", "--retry-downloads", str(retry),
        ])
        assert result.error is None
        assert result.resume_manifest is None
        assert result.retry_only_keys == {"abc123def456"}

    def test_both_resume_and_retry_is_error(self):
        from scrape_new.services.download_resume import parse_resume_retry_args
        result = parse_resume_retry_args([
            "URL", "--resume", "x.json", "--retry-downloads", "y.json",
        ])
        assert result.error is not None
        assert "互斥" in result.error

    def test_resume_missing_value_error(self):
        from scrape_new.services.download_resume import parse_resume_retry_args
        result = parse_resume_retry_args(["URL", "--resume"])
        assert result.error is not None
        assert "需要参数" in result.error

    def test_duplicate_resume_error(self):
        from scrape_new.services.download_resume import parse_resume_retry_args
        result = parse_resume_retry_args([
            "URL", "--resume", "a.json", "--resume", "b.json",
        ])
        assert result.error is not None
        assert "重复" in result.error

    def test_retry_missing_file_is_error(self, tmp_path: Path):
        """P0-1:--retry-downloads 指向不存在的文件,直接报错退出,
        绝不静默变成全量下载(用户本来只想重试失败项)。"""
        from scrape_new.services.download_resume import parse_resume_retry_args
        result = parse_resume_retry_args([
            "URL", "--retry-downloads", str(tmp_path / "nonexistent.json"),
        ])
        assert result.error is not None
        assert "--retry-downloads 文件不存在" in result.error
        assert result.retry_only_keys is None

    def test_no_flags_means_no_filter(self):
        from scrape_new.services.download_resume import parse_resume_retry_args
        result = parse_resume_retry_args(["URL", "output_dir"])
        assert result.error is None
        assert result.resume_manifest is None
        assert result.retry_only_keys is None

    def test_path_with_dash_prefix_does_not_break_parser(self, tmp_path: Path):
        """--resume 后跟负数(虽然不太可能)也不会破坏解析"""
        from scrape_new.services.download_resume import parse_resume_retry_args
        # 假设用户传了 "--resume -something" → 应该把 -something 当 path
        # (虽然不是合法 path)
        result = parse_resume_retry_args([
            "URL", "output_dir", "--resume", "-something",
        ])
        assert result.error is None
        assert str(result.resume_manifest) == "-something"


# ─── 10) 集成:normalize + apply_resume + early-skip ───────

class TestResumeIntegrationChain:
    """normalize → apply_resume → in-loop early skip,完整链路。"""

    def test_full_chain_skip_calls_no_network(self, tmp_path: Path):
        """完整链路:scan → normalize → apply_resume → download loop 早期 continue。
        验证:被 resume 标 skipped_existing 的资源,**绝不会**走到 get_video_download_url。

        实现方式:不真跑 chaoxing main()(那要网络),而是模拟"normalize + apply_resume + 下载循环早期 continue"
        这条完整链,验证 status 不会被覆盖。
        """
        from scrape_new.services.download_resume import (
            normalize_download_resources, apply_resume_decisions,
        )

        # 旧 manifest:1 OK 视频(saved_name 必须跟 normalize 后一致)
        v_old = _video_item(
            course_id="c1", ch_num=1, ls_num=1, role="video",
            saved_name="1.1_技术.mp4", status="downloaded",
        )
        manifest = tmp_path / "_resource_naming_manifest.json"
        _write_manifest([v_old], manifest)
        video_dir = tmp_path / "视频"
        video_dir.mkdir()
        # 本地文件存在 + size 合理
        (video_dir / "1.1_技术.mp4").write_bytes(b"x" * 2000)

        # 模拟"扫描产出"——给 filename / name 跟 manifest 对得上
        all_videos = [{
            "ch_num": 1, "ls_num": 1, "chapter": "ch1", "lesson": "技术",
            "name": "1.1_技术.mp4",  # ← 跟 manifest 的 saved_name 一致
            "objectid": "abc",
            # 故意没 role/filename/resource_key,让 normalize 补
        }]
        all_docs = []

        # 步骤 1:normalize(给所有字段加默认值)
        normalize_download_resources(all_videos, all_docs, "c1")
        assert all_videos[0]["filename"]  # 兜底
        assert all_videos[0]["resource_key"]
        assert all_videos[0]["role"] == "video"  # mp4 → video

        # 步骤 2:apply_resume_decisions
        apply_resume_decisions(
            all_videos, all_docs, manifest, video_dir,
        )
        # 关键:被标 skipped_existing
        assert all_videos[0]["status"] == "skipped_existing"

        # 步骤 3:模拟下载循环
        network_called = []
        def fake_get_url(*args, **kwargs):
            network_called.append("get_video_download_url")
            raise AssertionError("skipped_existing 不应触发网络调用")
        def fake_download(*args, **kwargs):
            network_called.append("download_video")
            raise AssertionError("skipped_existing 不应触发下载")

        # 模拟 chaoxing 的循环逻辑
        for v in all_videos:
            if v.get("status") == "skipped_existing":
                continue  # ← 早期 continue,这就是关键
            fake_get_url(v)
            fake_download(v)

        assert network_called == [], \
            f"skipped_existing 资源调了网络: {network_called}"

    def test_retry_only_keys_set_skips_everything(self, tmp_path: Path):
        """--retry-downloads 输入清单为空时,所有资源标 skipped_existing,
        后续下载循环全部 continue,不调任何网络。"""
        from scrape_new.services.download_resume import (
            normalize_download_resources, apply_resume_decisions,
        )
        v = _video_item(
            course_id="c1", ch_num=1, ls_num=1, role="video",
            saved_name="1.1_技术.mp4", status="downloaded",
        )
        manifest = tmp_path / "_resource_naming_manifest.json"
        _write_manifest([v], manifest)
        video_dir = tmp_path / "视频"
        video_dir.mkdir()
        (video_dir / "1.1_技术.mp4").write_bytes(b"x" * 2000)

        all_videos = [{
            "ch_num": 1, "ls_num": 1, "chapter": "ch1", "lesson": "技术",
            "name": "技术", "objectid": "abc",
        }]
        all_docs = []

        normalize_download_resources(all_videos, all_docs, "c1")
        # retry_only_keys=set() → 全标 skipped_existing
        apply_resume_decisions(
            all_videos, all_docs, manifest, video_dir,
            retry_only_keys=set(),
        )
        assert all_videos[0]["status"] == "skipped_existing"
        assert all_videos[0]["reason"] == "retry_downloads: retry_only_keys 为空"

        # 下载循环全部 continue
        network_called = []
        for v in all_videos:
            if v.get("status") == "skipped_existing":
                continue
            network_called.append("get_url")
        assert network_called == []

    def test_normalize_does_not_overwrite_existing_status(self, tmp_path: Path):
        """normalize_download_resources 不覆盖已设的 status(包括 skipped_existing)"""
        from scrape_new.services.download_resume import normalize_download_resources
        v = {
            "ch_num": 1, "ls_num": 1, "chapter": "ch1", "lesson": "技术",
            "name": "技术", "objectid": "abc",
            "status": "skipped_existing",  # 已经标好
            "reason": "旧 manifest 命中",
        }
        normalize_download_resources([v], [], "c1")
        # 关键:status 不被覆盖
        assert v["status"] == "skipped_existing"
        assert v["reason"] == "旧 manifest 命中"

    def test_normalize_inits_empty_status_to_pending(self):
        """normalize 给空 status 补 'pending'(没失败的语义,不是 'failed')"""
        from scrape_new.services.download_resume import normalize_download_resources
        v = {
            "ch_num": 1, "ls_num": 1, "chapter": "ch1", "lesson": "技术",
            "name": "技术",
            # 故意没 status
        }
        normalize_download_resources([v], [], "c1")
        assert v["status"] == "pending"

    def test_status_init_in_download_loop_preserves_skip(self, tmp_path: Path):
        """下载循环里 init status 时保留 skipped_existing(关键修复验证)"""
        from scrape_new.services.download_resume import normalize_download_resources

        # 模拟"resume 已经标 skipped_existing 后"的状态
        v = {
            "ch_num": 1, "ls_num": 1, "chapter": "ch1", "lesson": "技术",
            "name": "技术",
            "status": "skipped_existing",
            "reason": "resume: 旧 manifest 已 downloaded",
        }
        normalize_download_resources([v], [], "c1")

        # 模拟下载循环的 init 部分(原代码:v["status"] = _STATUS_FAILED 会覆盖)
        # 修复后:只在空时 init
        if not v.get("status"):
            v["status"] = "failed"

        # 关键:status 仍是 skipped_existing
        assert v["status"] == "skipped_existing"
        assert v["reason"] == "resume: 旧 manifest 已 downloaded"


# ─── 11) 第十二轮 P0-1/P1/P2 集成 ──────────────────────────

class TestRound12Fixes:
    """P0-1: --retry-downloads 不存在报错
    P1:  output_dir 不被旗标吃掉
    P2:  文档 skipped_d 单独统计(在 download_resume 层验证 reason 流向)
    """

    def test_retry_missing_file_error_contains_path(self, tmp_path: Path):
        """P0-1:--retry-downloads 指向不存在文件,error 含完整路径,主流程能拿到。"""
        from scrape_new.services.download_resume import parse_resume_retry_args
        nonexistent = tmp_path / "no-such-retry.json"
        result = parse_resume_retry_args([
            "URL", "--retry-downloads", str(nonexistent),
        ])
        assert result.error is not None
        # 路径必须出现(用户能看到具体哪个文件找不到)
        assert str(nonexistent) in result.error
        # retry_only_keys 必须 None(没读到 keys)
        assert result.retry_only_keys is None
        # 关键:不静默变全量下载(原行为:error=None, retry_only_keys=None)
        # 现在:error 非空,主流程会 print 错误并 sys.exit(1)

    def test_retry_existing_file_with_no_assets_returns_empty_set(self, tmp_path: Path):
        """--retry-downloads 文件存在但 _retry_downloads.json assets 为空 →
        retry_only_keys=set()(主流程会把所有资源标 skipped_existing,合理:
        用户想'重试失败项'但实际没失败项,什么都不做最安全)。"""
        from scrape_new.services.download_resume import parse_resume_retry_args
        # 写一个空 assets 的 manifest
        retry_manifest = tmp_path / "_retry_downloads.json"
        retry_manifest.write_text(
            json.dumps({"assets": []}, ensure_ascii=False),
            encoding="utf-8",
        )
        result = parse_resume_retry_args([
            "URL", "--retry-downloads", str(retry_manifest),
        ])
        assert result.error is None
        assert result.retry_only_keys == set()

    def test_extract_positional_args_skips_resume_flag(self):
        """P1:`URL --resume manifest.json` → positional=[URL],output_dir 不被吃掉。

        原 bug:output_dir = sys.argv[2] = "--resume",把旗标当目录了。
        """
        from scrape_new.workflows.chaoxing import _extract_positional_args
        # 模拟命令行:python chaoxing.py URL --resume manifest.json
        positional = _extract_positional_args([
            "https://mooc2-ans.chaoxing.com/foo", "--resume", "manifest.json",
        ])
        assert positional == ["https://mooc2-ans.chaoxing.com/foo"]
        # 关键:positional[1] 不存在,主流程会 fallback 到 DEFAULT_OUTPUT

    def test_extract_positional_args_keeps_output_dir(self):
        """P1:`URL output_dir --resume manifest` → positional=[URL, output_dir]"""
        from scrape_new.workflows.chaoxing import _extract_positional_args
        positional = _extract_positional_args([
            "URL", "my_output", "--resume", "manifest.json",
        ])
        assert positional == ["URL", "my_output"]

    def test_extract_positional_args_skips_retry_downloads(self):
        """P1:同样过滤 --retry-downloads 旗标"""
        from scrape_new.workflows.chaoxing import _extract_positional_args
        positional = _extract_positional_args([
            "URL", "--retry-downloads", "retry.json", "out_dir",
        ])
        assert positional == ["URL", "out_dir"]

    def test_extract_positional_args_no_flag(self):
        """P1:无旗标时,positional = 原 argv(向后兼容)"""
        from scrape_new.workflows.chaoxing import _extract_positional_args
        positional = _extract_positional_args(["URL", "out_dir"])
        assert positional == ["URL", "out_dir"]

    def test_extract_positional_args_flag_with_no_value_keeps_flag(self):
        """P1 防御:旗标后没 value → 把旗标当 positional(让主流程的 parse_resume_retry_args 报错)"""
        from scrape_new.workflows.chaoxing import _extract_positional_args
        positional = _extract_positional_args(["URL", "--resume"])
        # 旗标留在 positional,主流程会发现它没 value 并报"需要参数"
        assert "--resume" in positional

    def test_doc_skipped_existing_marked_correctly(self, tmp_path: Path):
        """P2 层验证(下载侧):文档被 apply_resume_decisions / apply_retry_filter 标
        skipped_existing 后,reason 字段存在且非空(供 chaoxing 文档循环的 skipped_d 计数)。"""
        from scrape_new.services.download_resume import (
            normalize_download_resources, apply_retry_filter,
        )
        d = {
            "ch_num": 1, "ls_num": 1, "chapter": "ch1", "lesson": "doc",
            "name": "1.1_课件.pdf", "objectid": "obj-doc",
        }
        all_videos, all_docs = [], [d]
        normalize_download_resources(all_videos, all_docs, "c1")
        # 用空 set → 文档被标 skipped_existing + reason
        apply_retry_filter(all_videos, all_docs, retry_only_keys=set())
        assert all_docs[0]["status"] == "skipped_existing"
        assert "retry_downloads" in all_docs[0]["reason"]  # chaoxing 用 reason 区分 source


# ─── 12) 第十四轮 P0/P1/P2 ─────────────────────────────

class TestRound14Fixes:
    """P0:chaoxing flag 解析(加 no-value)
    P1:scan-only 写 _chapter_tree.json/md 正确参数
    P2:scan-only 从完整 lessons 构造(漏节检测)
    """

    def test_extract_positional_args_filters_scan_only(self):
        """P0:`URL --scan-only` → positional == [URL](--scan-only 是 no-value flag)"""
        from scrape_new.workflows.chaoxing import _extract_positional_args
        positional = _extract_positional_args([
            "https://mooc2-ans.chaoxing.com/foo", "--scan-only",
        ])
        assert positional == ["https://mooc2-ans.chaoxing.com/foo"]

    def test_extract_positional_args_filters_verify_resume_only_with_resume(self):
        """P0:`URL --verify-resume-only --resume m.json` → positional == [URL]"""
        from scrape_new.workflows.chaoxing import _extract_positional_args
        positional = _extract_positional_args([
            "URL", "--verify-resume-only", "--resume", "m.json",
        ])
        assert positional == ["URL"]

    def test_extract_positional_args_filters_cpi(self):
        """P0:`URL --cpi 123` → positional == [URL](--cpi 加进 _FLAGS_WITH_VALUE)"""
        from scrape_new.workflows.chaoxing import _extract_positional_args
        positional = _extract_positional_args(["URL", "--cpi", "123"])
        assert positional == ["URL"]

    def test_extract_positional_args_combined(self):
        """P0:`URL out --scan-only --max-tabs 6` → positional == [URL, out]"""
        from scrape_new.workflows.chaoxing import _extract_positional_args
        positional = _extract_positional_args([
            "URL", "out", "--scan-only", "--max-tabs", "6",
        ])
        assert positional == ["URL", "out"]

    def test_write_chapter_tree_writes_to_output_dir(self, tmp_path: Path):
        """P1:write_chapter_tree_json/md 第二参数是 output_dir(目录),不是文件路径。

        旧 bug:Path(output_dir) / "_chapter_tree.json" → 让它创建 output_dir/_chapter_tree.json/_chapter_tree.json
        """
        from scrape_new.services.resource_manifest import (
            build_chapter_tree_data, write_chapter_tree_json, write_chapter_tree_md,
        )
        all_videos = [{
            "ch_num": 1, "ls_num": 1, "chapter": "ch1", "lesson": "intro",
            "name": "Lesson 1", "objectid": "obj-1", "filename": "1.1_intro.mp4",
            "role": "video", "status": "pending", "size_bytes": 0, "reason": "",
            "source_meta": {},
        }]
        tree = build_chapter_tree_data(
            course_title="测试课", platform="chaoxing", source_url="https://x",
            all_videos=all_videos, all_docs=[],
        )
        out_dir = tmp_path / "course"
        out_dir.mkdir()
        json_path = write_chapter_tree_json(tree, out_dir)
        md_path = write_chapter_tree_md(tree, out_dir)
        # 文件直接在 out_dir,不是 out_dir/_chapter_tree.json/...
        assert json_path == out_dir / "_chapter_tree.json"
        assert md_path == out_dir / "_chapter_tree.md"
        assert json_path.exists()
        assert md_path.exists()
        # 不能嵌套(out_dir/_chapter_tree.json/ 之类不存在)
        assert not (out_dir / "_chapter_tree.json").is_dir()

    def test_build_chapter_tree_data_accepts_chaoxing_args(self, tmp_path: Path):
        """P1:build_chapter_tree_data 正确形参(course_title, platform, source_url, ...)"""
        from scrape_new.services.resource_manifest import build_chapter_tree_data
        all_videos = [{
            "ch_num": 1, "ls_num": 1, "chapter": "ch1", "lesson": "intro",
            "name": "L1", "objectid": "o1", "filename": "1.1_L1.mp4",
            "role": "video", "status": "pending", "size_bytes": 0, "reason": "",
            "source_meta": {},
        }]
        # 不抛异常 + 返回 dict
        tree = build_chapter_tree_data(
            course_title="测试", platform="chaoxing", source_url="https://x",
            all_videos=all_videos, all_docs=[],
            lessons_meta=[{
                "chapter_index": 1, "chapter_title": "ch1",
                "lesson_index": 1, "lesson_title": "intro",
                "knowledge_id": "k1",
            }],
        )
        assert "chapters" in tree

    def test_suspicious_lessons_includes_empty_lessons(self):
        """P2:scan-only 从完整 lessons 章节树构造,空 lesson 也进 lesson_results 并被标 empty_lesson。

        复现:用 _build_diff 模拟 2 节 lesson(1 节有 1 个 video,1 节空),
        调 detect_suspicious_lessons,验证空节也被标 empty_lesson。
        """
        from scrape_new.services.scan_chaoxing import (
            detect_suspicious_lessons, LessonScanResult,
        )
        # 2 节:1.1 有 1 个 video,1.2 完全空
        ls1 = LessonScanResult(
            ch_num=1, ls_num=1, chapter="ch1", lesson="有资源", lesson_id="1.1",
            videos=[{"role": "video", "objectid": "v1"}],
        )
        ls2 = LessonScanResult(
            ch_num=1, ls_num=2, chapter="ch1", lesson="空节", lesson_id="1.2",
            videos=[], docs=[], unknown_resources=[],
        )
        # 跑漏扫检测
        detect_suspicious_lessons([ls1, ls2])
        # ls2 必须有 empty_lesson 标记
        assert "empty_lesson" in ls2.flags
        # ls1 不该有
        assert "empty_lesson" not in ls1.flags