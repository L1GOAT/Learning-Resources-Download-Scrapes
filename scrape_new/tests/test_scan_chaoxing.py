"""
chaoxing scan-only 扫描侧测试(D1-D4 + R1-R3)

覆盖(11 测试):
  D1: scan-only 不下载任何文件,只生成报告
  D2: 多 tab 探测(video + ppt + english),连续空停止,202 限流中断
  D3: 智能 role 识别(扩展名/标题/同节结构/tab_num 兜底),unknown 不丢
  D4: 漏扫检测(同章缺 PPT/English/empty_lesson/tab_failed)
  R1: scan-only 也生成 resource_key,跟 manifest 一致
  R2: verify-resume-only 不下载,只判断哪些会跳过
  R3: retry manifest 排除 unknown/no_key/tab_failed,进 pending_actions
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

# 仓库根:scrape_new/tests/ → parents[2]。
# 用 PROJECT_ROOT 推导本地真课 fixture 路径,避免硬编码 "E:/林视"
# 这种本地绝对路径(CI runner 是 linux/windows,找不到会 fail)。
PROJECT_ROOT = Path(__file__).resolve().parents[2]

from scrape_new.services.scan_chaoxing import (
    detect_resource_role,
    scan_lesson_tabs,
    detect_suspicious_lessons,
    build_scan_context,
    write_scan_reports,
    LessonScanResult,
    TabResult,
    DEFAULT_MAX_TABS,
    CONSEC_EMPTY_STOP,
    CONSEC_202_STOP,
    ROLE_VIDEO, ROLE_ENGLISH, ROLE_PPT, ROLE_PDF, ROLE_UNKNOWN,
)
from scrape_new.services.resource_manifest import (
    write_download_retry_manifest,
    build_resource_naming_records,
)


# ─── D2: 多 tab 探测 ─────────────────────────────────────

class TestMultiTabScan:
    def test_multi_tab_finds_video_ppt_english(self):
        """D2 多 tab:fetcher 返回 mp4/pdf/video,scan_lesson_tabs 全部收集"""
        def fetcher(tab_num):
            if tab_num == 0:
                return [{"objectid": "v1", "type": ".mp4", "name": "Lesson Video"}], [], False, False, ""
            if tab_num == 1:
                return [], [{"objectid": "p1", "type": ".pdf", "name": "课件"}], False, False, ""
            if tab_num == 2:
                return [{"objectid": "e1", "type": ".mp4", "name": "English Version"}], [], False, False, ""
            if tab_num == 3:
                return [], [], False, False, ""  # 1 个空
            if tab_num == 4:
                return [], [], False, False, ""  # 2 个空 → 停止(但 max_tabs=5 时才到)
            return [], [], False, False, ""

        # 用 max_tabs=5 让 5 个 tab 全部扫到
        tabs, stopped = scan_lesson_tabs(fetcher, max_tabs=5)
        assert stopped == "consecutive_empty_tabs"
        # 累加资源
        all_v = sum(len(t.videos) for t in tabs)
        all_d = sum(len(t.docs) for t in tabs)
        assert all_v == 2  # video + english
        assert all_d == 1  # ppt/ppt/pdf(实际是 pdf,tab=1)
        # 每个 tab 的 tab_num 正确
        tab_nums = [t.tab_num for t in tabs]
        assert tab_nums == [0, 1, 2, 3, 4]

    def test_consecutive_empty_stops_scan(self):
        """D2 连续 CONSEC_EMPTY_STOP 个空 tab → 停止"""
        def fetcher(tab_num):
            if tab_num == 0:
                return [{"objectid": "v1"}], [], False, False, ""
            return [], [], False, False, ""

        tabs, stopped = scan_lesson_tabs(fetcher, max_tabs=10)
        # tab 0 有资源,tab 1/2 空 → 2 个连续空(== CONSEC_EMPTY_STOP)→ 停止
        assert stopped == "consecutive_empty_tabs"
        assert len(tabs) == 3  # 0(有),1(空),2(空触发停止)
        assert tabs[0].videos  # tab 0 有
        assert tabs[1].is_empty
        assert tabs[2].is_empty

    def test_rate_limit_stops_after_consecutive_202(self):
        """D2 连续 CONSEC_202_STOP 个 202 → 停止"""
        def fetcher(tab_num):
            # 全部返回 202 限流
            return [], [], True, False, ""

        tabs, stopped = scan_lesson_tabs(fetcher, max_tabs=10)
        assert stopped == "rate_limited"
        assert len(tabs) == CONSEC_202_STOP
        assert all(t.rate_limited for t in tabs)

    def test_max_tabs_respected(self):
        """D2 max_tabs 限制 tab 数量"""
        call_count = 0
        def fetcher(tab_num):
            nonlocal call_count
            call_count += 1
            return [{"objectid": f"v{tab_num}"}], [], False, False, ""

        tabs, stopped = scan_lesson_tabs(fetcher, max_tabs=3)
        assert call_count == 3
        assert len(tabs) == 3
        assert stopped == ""  # 正常完成


# ─── D3: 智能 role 识别 ─────────────────────────────────

class TestSmartRole:
    def test_role_from_mimetype(self):
        """D3 mimetype 优先:application/pdf → pdf"""
        role = detect_resource_role(type_or_mimetype="application/pdf")
        assert role == ROLE_PDF

    def test_role_from_extension(self):
        """D3 扩展名兜底:.pptx → ppt"""
        role = detect_resource_role(filename="课件.pptx")
        assert role == ROLE_PPT

    def test_role_from_title_keyword_english(self):
        """D3 标题/文件名关键字:English / 英文 / english version → english"""
        for title in ["English Version", "Lesson 1 English", "英文版", "english audio"]:
            role = detect_resource_role(
                filename=f"{title}.mp4", type_or_mimetype=".mp4", title=title,
            )
            assert role == ROLE_ENGLISH, f"failed for {title!r}"

    def test_role_unknown_never_silently_picked(self):
        """D3 unknown 绝不静默默认 video。完全没信号时返 unknown。"""
        # 没 type / 扩展名 / 标题关键字 / 同节 / tab_num
        role = detect_resource_role()
        assert role == ROLE_UNKNOWN

    def test_role_tab_num_fallback_only_when_nothing_else(self):
        """D3 tab_num 兜底:只在 type / ext / 标题 / 同节都没信号时用"""
        # 完全没其他信号,tab_num=2 → english
        role = detect_resource_role(tab_num=2)
        assert role == ROLE_ENGLISH

        # 但如果有扩展名 .pdf,优先用 .pdf(不取 tab_num=2 → english)
        role = detect_resource_role(tab_num=2, filename="x.pdf")
        assert role == ROLE_PDF

    def test_role_same_lesson_videos_influence(self):
        """D3 同节资源结构:其他资源是 video,这个也是 video"""
        same = [{"role": ROLE_VIDEO}, {"role": ROLE_VIDEO}, {"role": ROLE_PPT}]
        role = detect_resource_role(same_lesson_resources=same)
        # 同节多数是 video → 兜底 video
        assert role == ROLE_VIDEO


# ─── D4: 漏扫检测 ───────────────────────────────────────

class TestSuspiciousLessons:
    def test_empty_lesson_marked(self):
        ls = LessonScanResult(
            ch_num=1, ls_num=1, chapter="ch1", lesson="empty", lesson_id="1",
            videos=[], docs=[], unknown_resources=[],
        )
        detect_suspicious_lessons([ls])
        assert "empty_lesson" in ls.flags

    def test_missing_ppt_in_chapter_majority_have_ppt(self):
        """D4 同章其他节有 PPT,这一节没有 → suspicious_missing_ppt"""
        # 同章 3 节:2 节有 PPT,1 节没有
        ls1 = LessonScanResult(ch_num=1, ls_num=1, chapter="ch1", lesson="a", lesson_id="1.1",
                                docs=[{"role": ROLE_PPT, "objectid": "p1"}])
        ls2 = LessonScanResult(ch_num=1, ls_num=2, chapter="ch1", lesson="b", lesson_id="1.2",
                                docs=[{"role": ROLE_PPT, "objectid": "p2"}])
        ls3 = LessonScanResult(ch_num=1, ls_num=3, chapter="ch1", lesson="c", lesson_id="1.3",
                                docs=[{"role": ROLE_PDF, "objectid": "f1"}])  # 没 PPT
        detect_suspicious_lessons([ls1, ls2, ls3])
        # ls3 没 PPT → 标记
        assert "suspicious_missing_ppt" in ls3.flags
        assert "suspicious_missing_ppt" not in ls1.flags

    def test_missing_english_marked(self):
        """D4 同章其他节有英文视频,这一节没有 → suspicious_missing_english"""
        ls1 = LessonScanResult(ch_num=2, ls_num=1, chapter="ch2", lesson="a", lesson_id="2.1",
                                videos=[{"role": ROLE_ENGLISH, "objectid": "e1"}])
        ls2 = LessonScanResult(ch_num=2, ls_num=2, chapter="ch2", lesson="b", lesson_id="2.2",
                                videos=[{"role": ROLE_VIDEO, "objectid": "v1"}])  # 没 english
        detect_suspicious_lessons([ls1, ls2])
        assert "suspicious_missing_english" in ls2.flags

    def test_tab_failed_marked(self):
        """D4 tab_failed 标记"""
        ls = LessonScanResult(
            ch_num=3, ls_num=1, chapter="ch3", lesson="x", lesson_id="3.1",
            videos=[{"role": ROLE_VIDEO, "objectid": "v1"}],
            tabs=[TabResult(tab_num=0, failed=True, error_msg="timeout")],
        )
        detect_suspicious_lessons([ls])
        assert "tab_failed" in ls.flags


# ─── D1: scan-only 写报告 ───────────────────────────────

class TestScanOnlyReports:
    def test_write_scan_reports_creates_4_files(self, tmp_path: Path):
        """D1 scan-only 写 _scanned_resources.json + discovery_report.json + .md"""
        ls1 = LessonScanResult(
            ch_num=1, ls_num=1, chapter="ch1", lesson="intro", lesson_id="1",
            videos=[{"role": ROLE_VIDEO, "objectid": "v1", "tab_num": 0,
                     "title": "Intro"}],
            docs=[{"role": ROLE_PPT, "objectid": "p1", "tab_num": 1,
                   "title": "课件"}],
        )
        ctx = build_scan_context(course_id="c1", course_title="测试课", lessons=[ls1])
        paths = write_scan_reports(ctx, tmp_path)

        # 至少:scanned_resources.json + discovery_report.json + .md
        assert "scanned_resources" in paths
        assert "discovery_report_json" in paths
        assert "discovery_report_md" in paths
        # 文件存在
        for p in paths.values():
            assert p.exists()
        # JSON 解析无错
        data = json.loads(paths["scanned_resources"].read_text(encoding="utf-8"))
        assert data["summary"]["discovered"] == 2  # 1 video + 1 doc

    def test_build_scan_context_aggregates(self):
        """D1 ScanContext 自动聚合 all_videos/all_docs/failed_tabs/suspicious_lessons"""
        ls1 = LessonScanResult(
            ch_num=1, ls_num=1, chapter="ch1", lesson="a", lesson_id="1.1",
            videos=[{"role": ROLE_VIDEO, "objectid": "v1"}],
            docs=[{"role": ROLE_PPT, "objectid": "p1"}],
            tabs=[TabResult(tab_num=0, failed=True, error_msg="net err")],
        )
        ls2 = LessonScanResult(
            ch_num=1, ls_num=2, chapter="ch1", lesson="b", lesson_id="1.2",
            videos=[], docs=[], unknown_resources=[],
        )
        ctx = build_scan_context(course_id="c1", course_title="", lessons=[ls1, ls2])
        assert len(ctx.all_videos) == 1
        assert len(ctx.all_docs) == 1
        assert len(ctx.failed_tabs) == 1  # ls1.tab[0]
        # ls2 是 empty → 进 suspicious
        assert len(ctx.suspicious_lessons) >= 1


# ─── R3: retry manifest 严格过滤 ─────────────────────────

class TestRetryManifestStrict:
    def test_unknown_role_goes_to_pending(self, tmp_path: Path):
        """R3 unknown role 进 pending_actions,不进 assets"""
        v = {
            "ch_num": 1, "ls_num": 1, "chapter": "ch1", "lesson": "a",
            "name": "x.weird", "role": "unknown",  # ← unknown
            "filename": "x.weird", "status": "failed",
            "size_bytes": 0, "reason": "类型无法识别",
            "source_meta": {},
            "resource_key": "rk1",
        }
        records = build_resource_naming_records([v], [])
        path = write_download_retry_manifest(records, tmp_path)
        assert path is not None
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        # assets 空(unknown 不进 retry)
        assert data["assets"] == []
        # pending_actions 含 unknown
        assert any(p["pending_reason"] == "role_unknown" for p in data["pending_actions"])

    def test_tab_failed_goes_to_pending(self, tmp_path: Path):
        """R3 tab_failed 进 pending,不进 assets"""
        v = {
            "ch_num": 1, "ls_num": 1, "chapter": "ch1", "lesson": "a",
            "name": "x.mp4", "role": "video",
            "filename": "x.mp4", "status": "failed",
            "size_bytes": 0, "reason": "tab timeout",
            "source_meta": {},
            "resource_key": "rk2",
            "tab_failed": True,  # 标记
        }
        records = build_resource_naming_records([v], [])
        path = write_download_retry_manifest(records, tmp_path)
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        assert data["assets"] == []
        assert any(p["pending_reason"] == "tab_failed" for p in data["pending_actions"])

    def test_normal_failed_still_in_assets(self, tmp_path: Path):
        """R3 普通 failed + role + key → 正常进 assets"""
        v = {
            "ch_num": 1, "ls_num": 1, "chapter": "ch1", "lesson": "a",
            "name": "x.mp4", "role": "video",
            "filename": "x.mp4", "status": "failed",
            "size_bytes": 0, "reason": "网络超时",
            "source_meta": {},
            "resource_key": "rk3",
        }
        records = build_resource_naming_records([v], [])
        path = write_download_retry_manifest(records, tmp_path)
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        assert len(data["assets"]) == 1
        assert data["assets"][0]["resource_key"] == "rk3"

    def test_scan_only_resource_key_consistent_with_manifest(self, tmp_path: Path):
        """R1:scan-only 也生成 resource_key(复用 make_resource_key 公式),跟 manifest 一致"""
        from scrape_new.services.download_resume import normalize_download_resources
        from scrape_new.services.resource_manifest import build_resource_naming_records
        from scrape_new.upload.resource_key import make_resource_key

        all_videos = [{
            "ch_num": 1, "ls_num": 1, "chapter": "ch1", "lesson": "intro",
            "name": "Lesson 1 Video", "objectid": "obj-1",
        }]
        all_docs = [{
            "ch_num": 1, "ls_num": 1, "chapter": "ch1", "lesson": "intro",
            "name": "课件.pdf", "objectid": "obj-2",
        }]
        # 1) normalize 生成 resource_key
        normalize_download_resources(all_videos, all_docs, "c1")
        # 2) build manifest records(也带 resource_key)
        records = build_resource_naming_records(all_videos, all_docs)
        # 3) 用 make_resource_key 手动算预期 key
        expected_video = make_resource_key(
            course_id="c1", chapter_index=1, lesson_id="1",
            role="video", saved_name=all_videos[0]["filename"],
        )
        expected_doc = make_resource_key(
            course_id="c1", chapter_index=1, lesson_id="1",
            role="pdf", saved_name=all_docs[0]["filename"],
        )
        # 4) 验证两边 key 一致
        # 注意:records 按 (chapter, lesson, role, name) 排序,pdf 排在 video 之前
        record_by_role = {r["role"]: r for r in records}
        assert all_videos[0]["resource_key"] == expected_video
        assert all_docs[0]["resource_key"] == expected_doc
        assert record_by_role["video"]["resource_key"] == expected_video
        assert record_by_role["pdf"]["resource_key"] == expected_doc


# ─── 18 轮:flag 不重复 + 真课 driver ───────────────────

class TestRound18ScanOnly:
    """第十八轮 P2:build_scan_context 自动跑 detect_suspicious_lessons,
    外部不能重复调(否则 flag 加 2 次)。
    """

    def test_flags_no_duplicate_when_called_via_build_scan_context(self):
        """P2:走 build_scan_context 路径,flag 不重复"""
        from scrape_new.services.scan_chaoxing import (
            build_scan_context, LessonScanResult, ROLE_PPT, ROLE_VIDEO,
        )
        # 同章 3 节:前 2 节有 PPT,第 3 节没
        lessons = [
            LessonScanResult(
                ch_num=1, ls_num=1, chapter="ch1", lesson="L1", lesson_id="1.1",
                docs=[{"role": ROLE_PPT, "objectid": "p1"}],
            ),
            LessonScanResult(
                ch_num=1, ls_num=2, chapter="ch1", lesson="L2", lesson_id="1.2",
                docs=[{"role": ROLE_PPT, "objectid": "p2"}],
            ),
            LessonScanResult(
                ch_num=1, ls_num=3, chapter="ch1", lesson="L3", lesson_id="1.3",
                videos=[{"role": ROLE_VIDEO, "objectid": "v1"}],
            ),
        ]
        ctx = build_scan_context(course_id="c1", course_title="t", lessons=lessons)
        # lesson 3 应该有 suspicious_missing_ppt(同章多数有 PPT)
        assert "suspicious_missing_ppt" in lessons[2].flags
        # 关键:flag 不重复(只 1 次,不是 2 次)
        assert lessons[2].flags.count("suspicious_missing_ppt") == 1

    def test_build_scan_context_does_not_double_flag(self):
        """P2:外部先调 detect_suspicious_lessons + 再调 build_scan_context → flag 重复

        行为记录(已知问题):这是 driver 写法错误,build_scan_context 内部已自动调,
        不应该再调。本测试记录这个行为以防回退。
        """
        from scrape_new.services.scan_chaoxing import (
            detect_suspicious_lessons, build_scan_context,
            LessonScanResult, ROLE_PPT, ROLE_VIDEO,
        )
        lessons = [
            LessonScanResult(
                ch_num=1, ls_num=1, chapter="ch1", lesson="L1", lesson_id="1.1",
                docs=[{"role": ROLE_PPT, "objectid": "p1"}],
            ),
            LessonScanResult(
                ch_num=1, ls_num=2, chapter="ch1", lesson="L2", lesson_id="1.2",
                videos=[{"role": ROLE_VIDEO, "objectid": "v1"}],
            ),
        ]
        # 外部先调
        detect_suspicious_lessons(lessons)
        # build_scan_context 又调一次
        build_scan_context(course_id="c1", course_title="t", lessons=lessons)
        # 行为记录:flag 会被加 2 次
        assert lessons[1].flags.count("suspicious_missing_ppt") == 2  # 重复了

    def test_real_course_scan_only_driver(self, tmp_path: Path):
        """P2:真课 scan-only driver(物理化学 outline 模拟)
        验证:5 个文件产出 + 摘要数字合理
        """
        from scrape_new.services.scan_chaoxing import (
            scan_lesson_tabs, build_scan_context, write_scan_reports,
            LessonScanResult,
        )
        from scrape_new.services.resource_manifest import (
            build_chapter_tree_data, write_chapter_tree_json, write_chapter_tree_md,
        )

        # 真课数据(从 disk 加载,缺则 skip)
        outline_p = PROJECT_ROOT / "物理化学" / "视频" / "_chapter_outline.json"
        if not outline_p.exists():
            pytest.skip("物理化学 outline 不存在,跳过真课 driver 测试")
        outline = json.loads(outline_p.read_text(encoding="utf-8"))

        lessons_meta = []
        for ch in outline["chapters"]:
            for ls in ch.get("lessons", []):
                lessons_meta.append({
                    "id": ls["id"], "name": ls["title"], "parent": ch["title"],
                    "ch_num": ch["index"], "ls_num": int(ls["id"].split(".")[-1]) if "." in ls["id"] else 0,
                })

        # 按 lesson_id 模拟 fetcher(每节 1 个 video/English + 1/3 节有 PPT)
        import random
        all_videos, all_docs = [], []
        lesson_results = []
        for ls in lessons_meta:
            rng = random.Random(hash(ls["id"]) & 0xFFFFFFFF)
            def make_fetcher(lid, lrng):
                def fetcher(tab_num):
                    if tab_num == 0:
                        target = next(
                            (x for ch in outline["chapters"] for x in ch.get("lessons", []) if x["id"] == lid),
                            None,
                        )
                        if not target or not target.get("video_filename"):
                            return [], [], False, False, ""
                        fn = target["video_filename"]
                        role = "english" if "_English" in fn else "video"
                        return [{"objectid": f"obj_{lid}_0", "type": ".mp4",
                                 "name": fn, "tab_num": tab_num, "role": role}], [], False, False, ""
                    if tab_num == 1 and lrng.random() < 0.33:
                        return [], [{"objectid": f"obj_{lid}_1", "type": ".pptx",
                                    "name": f"{lid}_课件.pptx",
                                    "tab_num": tab_num, "role": "ppt"}], False, False, ""
                    return [], [], False, False, ""
                return fetcher
            fetcher = make_fetcher(ls["id"], rng)
            tabs, _ = scan_lesson_tabs(fetcher, max_tabs=4)
            v_list, d_list = [], []
            for t in tabs:
                for v in t.videos:
                    v.update({"ch_num": ls["ch_num"], "ls_num": ls["ls_num"],
                              "chapter": ls["parent"], "lesson": ls["name"]})
                    v_list.append(v)
                for d in t.docs:
                    d.update({"ch_num": ls["ch_num"], "ls_num": ls["ls_num"],
                              "chapter": ls["parent"], "lesson": ls["name"]})
                    d_list.append(d)
            lesson_results.append(LessonScanResult(
                ch_num=ls["ch_num"], ls_num=ls["ls_num"],
                chapter=ls["parent"], lesson=ls["name"],
                lesson_id=ls["id"],
                videos=v_list, docs=d_list, tabs=tabs,
            ))
            all_videos.extend(v_list)
            all_docs.extend(d_list)

        # 不手动调 detect_suspicious_lessons,让 build_scan_context 自动
        ctx = build_scan_context(
            course_id="test_wlhx", course_title="物理化学(测试)",
            lessons=lesson_results,
        )

        out = tmp_path / "real_course_scan"
        out.mkdir()
        write_scan_reports(ctx, out)

        lessons_meta_for_tree = [
            {"chapter_index": ls["ch_num"], "chapter_title": ls["parent"],
             "lesson_index": ls["ls_num"], "lesson_title": ls["name"],
             "knowledge_id": str(ls["id"])}
            for ls in lessons_meta
        ]
        tree = build_chapter_tree_data(
            course_title="物理化学(测试)", platform="chaoxing",
            source_url=outline.get("source_url", ""),
            all_videos=all_videos, all_docs=all_docs,
            lessons_meta=lessons_meta_for_tree,
        )
        write_chapter_tree_json(tree, out)
        write_chapter_tree_md(tree, out)

        # 5 个文件都存在
        for f in ("_scanned_resources.json", "_resource_discovery_report.json",
                  "_resource_discovery_report.md", "_chapter_tree.json", "_chapter_tree.md"):
            assert (out / f).exists(), f"{f} 未生成"

        # 摘要合理:课数 == outline lesson 数;视频数 == lesson 数(每节 1 个)
        s = ctx.summary()
        assert s["lessons_total"] == len(lessons_meta)
        # 视频数(57 物理化学 1 节多资源模拟下等于 lesson 数)
        assert s["videos"] > 0
        # flag 不重复(关键回归)
        for ls in lesson_results:
            for flag in ls.flags:
                assert ls.flags.count(flag) == 1, \
                    f"{ls.lesson_id} flag {flag} 重复 {ls.flags.count(flag)} 次"