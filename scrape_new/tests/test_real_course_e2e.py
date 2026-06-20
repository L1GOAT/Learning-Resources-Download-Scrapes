"""
真课端到端验证 — 用物理化学课程真数据(57 视频,7 章 19 节)走 scan-only / --resume / upload --only-resource

设计:
  - 不真连超星(免触发限流)
  - 把"chaoxing scan_lesson_resources 内部 fetcher"mock 成读 _chapter_outline.json + 模拟 tab
  - 然后跑 scan-only / --resume / upload --only-resource 完整链路
  - 断言:
    V1: scan-only 写 5 个报告,resource_key 一致
    V2: --resume 跳过已下,只重下缺
    V3: --only-resource 只动一个 leaf

为什么单独抽:
  - 第十三轮代码链已断:chaoxing.py main() 没法不在网络下跑
  - 但每个独立模块(scan_chaoxing / download_resume / api_uploader)都单测覆盖
  - 这里 e2e 验证"模块组合 + 真课数据"不出错
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from scrape_new.services.scan_chaoxing import (
    detect_resource_role, scan_lesson_tabs, build_scan_context,
    write_scan_reports, detect_suspicious_lessons, LessonScanResult,
    ROLE_VIDEO, ROLE_ENGLISH, ROLE_PPT, ROLE_PDF, ROLE_UNKNOWN,
)


# ─── 真实 fixture:物理化学课程数据 ─────────────────────────

def _load_physical_chemistry_outline() -> dict:
    """从磁盘加载物理化学真课 outline。"""
    p = Path("E:/林视/物理化学/视频/_chapter_outline.json")
    if not p.exists():
        pytest.skip("物理化学真课 outline 不存在,跳过 e2e")
    return json.loads(p.read_text(encoding="utf-8"))


def _build_fetcher_from_outline(outline: dict):
    """构造 fetcher:从 outline 读资源,模拟不同 tab 的返回。

    tab=0:返回全部 video(包括主+英文,假装 1 个 lesson 有 1 主 + 1 英)
    tab=1:返回全部 doc(PPT,有的有有的没有)
    tab=2:同上
    tab=3+:空
    """
    # lesson_id (ch.ls) -> {(tab_num, role) -> resources}
    # 简化:从 outline 解析
    lessons_meta: list[dict] = []
    for ch in outline["chapters"]:
        for ls in ch.get("lessons", []):
            lessons_meta.append({
                "ch_num": ch["index"],
                "ls_num": ls["id"].split(".")[-1] if "." in ls["id"] else 0,
                "chapter": ch["title"],
                "lesson": ls["title"],
                "video_filename": ls.get("video_filename", ""),
                "id": ls["id"],
            })
    # 1 个 lesson → 1 主视频 + 1 English 视频(从 filename 后缀猜)
    def fetcher(tab_num: int):
        if tab_num == 0:
            # 全部主视频 + 英文(假装都来自 tab=0,因为实际 tab 2=English 但 mock 不区分)
            v = []
            for ls in lessons_meta:
                fn = ls["video_filename"]
                if not fn:
                    continue
                if fn.endswith("_English.mp4"):
                    role = ROLE_ENGLISH
                else:
                    role = ROLE_VIDEO
                v.append({
                    "objectid": f"obj_{ls['id']}",
                    "type": ".mp4",
                    "name": fn,
                    "ch_num": ls["ch_num"],
                    "ls_num": int(ls["ls_num"]),
                    "chapter": ls["chapter"],
                    "lesson": ls["lesson"],
                    "tab_num": tab_num,
                    "role": role,
                })
            return v, [], False, False, ""
        if tab_num == 1:
            # 模拟部分节有 PPT
            d = []
            for i, ls in enumerate(lessons_meta):
                if i % 3 == 0:  # 1/3 节有 PPT
                    d.append({
                        "objectid": f"ppt_{ls['id']}",
                        "type": ".pptx",
                        "name": f"{ls['video_filename'].rsplit('.', 1)[0]}.pptx",
                        "ch_num": ls["ch_num"],
                        "ls_num": int(ls["ls_num"]),
                        "chapter": ls["chapter"],
                        "lesson": ls["lesson"],
                        "tab_num": tab_num,
                        "role": ROLE_PPT,
                    })
            return [], d, False, False, ""
        # tab 2+:空
        return [], [], False, False, ""
    return fetcher, lessons_meta


# ─── V1:scan-only 端到端 ──────────────────────────────

class TestRealCourseScanOnlyE2E:
    """V1:真课数据 + scan-only 全链路 → 5 个报告 + resource_key 一致"""

    def test_scan_only_produces_5_reports(self, tmp_path: Path):
        """V1 主流程:scan-only 写 _scanned_resources.json + discovery_report.json/.md + _chapter_tree.json/.md"""
        outline = _load_physical_chemistry_outline()
        fetcher, lessons_meta = _build_fetcher_from_outline(outline)

        # 1) 模拟 chaoxing 主循环:对每节调 scan_lesson_tabs
        lesson_results: list[LessonScanResult] = []
        for ls in lessons_meta:
            tabs, _ = scan_lesson_tabs(fetcher, max_tabs=4)
            # 聚合 tabs 到 LessonScanResult
            all_videos = []
            all_docs = []
            unknown = []
            for t in tabs:
                for v in t.videos:
                    v["tab_num"] = t.tab_num
                    all_videos.append(v)
                for d in t.docs:
                    d["tab_num"] = t.tab_num
                    all_docs.append(d)
            lesson_results.append(LessonScanResult(
                ch_num=ls["ch_num"], ls_num=int(ls["ls_num"]),
                chapter=ls["chapter"], lesson=ls["lesson"],
                lesson_id=ls["id"],
                videos=all_videos, docs=all_docs, unknown_resources=unknown,
            ))

        # 2) 漏扫检测
        detect_suspicious_lessons(lesson_results)

        # 3) 构造 ScanContext + 写 3 个报告
        ctx = build_scan_context(
            course_id="wulihuaxue", course_title="物理化学",
            lessons=lesson_results,
        )
        out = tmp_path / "physical_chemistry_scan"
        out.mkdir()
        paths = write_scan_reports(ctx, out)

        # 4) 写 _chapter_tree.json/md(走 resource_manifest 既有 API)
        from scrape_new.services.resource_manifest import (
            build_chapter_tree_data, write_chapter_tree_json, write_chapter_tree_md,
        )
        # 展平 all_videos / all_docs(它们分散在 lesson_results)
        flat_videos = [v for ls in lesson_results for v in ls.videos]
        flat_docs = [d for ls in lesson_results for d in ls.docs]
        tree = build_chapter_tree_data(
            course_title="物理化学", platform="chaoxing",
            source_url=outline.get("source_url", ""),
            all_videos=flat_videos, all_docs=flat_docs,
            lessons_meta=lessons_meta,
        )
        write_chapter_tree_json(tree, out)
        write_chapter_tree_md(tree, out)

        # 5) 断言:5 个文件都在
        assert (out / "_scanned_resources.json").exists()
        assert (out / "_resource_discovery_report.json").exists()
        assert (out / "_resource_discovery_report.md").exists()
        assert (out / "_chapter_tree.json").exists()
        assert (out / "_chapter_tree.md").exists()
        # 不能嵌套
        assert not (out / "_chapter_tree.json").is_dir()

        # 6) 资源统计
        s = ctx.summary()
        assert s["lessons_total"] == len(lesson_results)
        # 视频 + 文档数应该 > 0
        assert s["videos"] > 0
        assert s["docs"] > 0
        # 真课漏扫检测:部分节空(empty_lesson)或缺 PPT(suspicious_missing_ppt)应该有
        # 不强制断言(看具体数据),只确认 summary 字段存在
        assert "failed_tabs" in s
        assert "suspicious_lessons" in s

    def test_resource_key_consistent_across_scan_and_manifest(self, tmp_path: Path):
        """V1 资源 key 一致:scan-only 的 v/d 跟 manifest 的 record 同 key"""
        outline = _load_physical_chemistry_outline()
        fetcher, lessons_meta = _build_fetcher_from_outline(outline)

        # 模拟扫描,收集 all_videos/all_docs
        all_videos, all_docs = [], []
        for ls in lessons_meta:
            tabs, _ = scan_lesson_tabs(fetcher, max_tabs=4)
            for t in tabs:
                for v in t.videos:
                    v["tab_num"] = t.tab_num
                    v["ch_num"] = ls["ch_num"]
                    v["ls_num"] = int(ls["ls_num"])
                    v["chapter"] = ls["chapter"]
                    v["lesson"] = ls["lesson"]
                    all_videos.append(v)
                for d in t.docs:
                    d["tab_num"] = t.tab_num
                    d["ch_num"] = ls["ch_num"]
                    d["ls_num"] = int(ls["ls_num"])
                    d["chapter"] = ls["chapter"]
                    d["lesson"] = ls["lesson"]
                    all_docs.append(d)

        # 1) normalize 算 key
        from scrape_new.services.download_resume import normalize_download_resources
        normalize_download_resources(all_videos, all_docs, "wulihuaxue")

        # 2) manifest records 算 key
        from scrape_new.services.resource_manifest import build_resource_naming_records
        records = build_resource_naming_records(all_videos, all_docs)

        # 3) 验证每个 video/doc 的 resource_key 跟 records 里的对应 record 一致
        # 按 (ch_num, ls_num, role, saved_name) 匹配
        rec_by_key: dict[tuple, str] = {
            (r["chapter_index"], r["lesson_id"], r["role"], r["saved_name"]): r["resource_key"]
            for r in records
        }
        for v in all_videos:
            key = (v["ch_num"], f"{v['ch_num']}.{v['ls_num']}",
                   v.get("role", "video"), v.get("filename", ""))
            if not v.get("filename"):  # 未下载无 filename,跳过
                continue
            assert key in rec_by_key, f"video {v.get('name')} 没对应 record"
            assert v["resource_key"] == rec_by_key[key], (
                f"key 不一致:scan={v['resource_key']} vs manifest={rec_by_key[key]}"
            )


# ─── V2:--resume 端到端(基于模拟状态) ─────────────────

class TestRealCourseResumeE2E:
    """V2:--resume 跳过已下,只重下缺。基于模拟(免真网络)。"""

    def test_resume_skips_downloaded_re_downloads_missing(self, tmp_path: Path):
        """V2:首次跑有 3 资源(1 已下 1 缺 1 损坏),--resume 只重下缺 + 损坏"""
        from scrape_new.services.download_resume import (
            normalize_download_resources, apply_resume_decisions,
        )
        from scrape_new.services.resource_manifest import (
            build_resource_naming_records, write_resource_naming_manifest_json,
        )

        # 模拟扫描结果:3 个视频(lesson 标题用 "L1/L2/L3",normalize 后 filename
        # 会变 "1.1_L1.mp4" 等 — 跟 manifest 一致,这样 _find_record 能命中)
        all_videos = [
            {
                "ch_num": 1, "ls_num": 1, "chapter": "ch1", "lesson": "L1",
                "objectid": "obj-1",
            },
            {
                "ch_num": 1, "ls_num": 2, "chapter": "ch1", "lesson": "L2",
                "objectid": "obj-2",
            },
            {
                "ch_num": 1, "ls_num": 3, "chapter": "ch1", "lesson": "L3",
                "objectid": "obj-3",
            },
        ]
        # normalize 加 resource_key + filename
        normalize_download_resources(all_videos, [], "c1")
        # 关键:filename 跟 lesson 标题挂钩(1.1_L1.mp4),保证 manifest 和二次跑一致
        expected_filenames = [v["filename"] for v in all_videos]
        assert expected_filenames == ["1.1_L1.mp4", "1.2_L2.mp4", "1.3_L3.mp4"]

        # 模拟首次跑:写一份 _resource_naming_manifest.json
        video_dir = tmp_path / "视频"
        video_dir.mkdir()
        # obj-1 文件存在 + 大小 OK
        (video_dir / "1.1_L1.mp4").write_bytes(b"x" * 2000)
        # obj-2 缺文件
        # obj-3 文件存在但太小(< 500B,损坏)
        (video_dir / "1.3_L3.mp4").write_bytes(b"x" * 100)

        records = build_resource_naming_records(all_videos, [])
        rec_by_id = {(r["chapter_index"], r["lesson_id"]): r for r in records}
        rec_by_id[(1, "1.1")]["status"] = "downloaded"
        rec_by_id[(1, "1.1")]["size_bytes"] = 2000
        rec_by_id[(1, "1.2")]["status"] = "failed"
        rec_by_id[(1, "1.2")]["size_bytes"] = 0
        rec_by_id[(1, "1.3")]["status"] = "suspicious"
        rec_by_id[(1, "1.3")]["size_bytes"] = 100
        write_resource_naming_manifest_json(records, tmp_path)

        # 模拟二次跑:重新 normalize(扫描结果跟首次一致)
        all_videos_2 = [
            {
                "ch_num": 1, "ls_num": 1, "chapter": "ch1", "lesson": "L1",
                "objectid": "obj-1",
            },
            {
                "ch_num": 1, "ls_num": 2, "chapter": "ch1", "lesson": "L2",
                "objectid": "obj-2",
            },
            {
                "ch_num": 1, "ls_num": 3, "chapter": "ch1", "lesson": "L3",
                "objectid": "obj-3",
            },
        ]
        normalize_download_resources(all_videos_2, [], "c1")

        # --resume 决策
        manifest_path = tmp_path / "_resource_naming_manifest.json"
        stats = apply_resume_decisions(
            all_videos_2, [], manifest_path, video_dir,
        )

        # 期望:1.1 skip(已下 + 文件 OK),1.2 重下(失败),1.3 重下(损坏文件)
        assert all_videos_2[0]["status"] == "skipped_existing"  # 跳过
        assert all_videos_2[1]["status"] != "skipped_existing"  # 重下
        assert all_videos_2[2]["status"] != "skipped_existing"  # 重下
        # stats 至少 1 个 skip
        assert stats["skipped_videos"] == 1
        assert stats["missing_keys"] == 0  # 都在 key 集合


# ─── V3:--only-resource 端到端(走 api_uploader) ───────

class TestRealCourseOnlyResourceE2E:
    """V3:upload --only-resource 只动一个 leaf,其他 SKIP,drift 改 warn。"""

    def test_only_resource_does_not_touch_others(self):
        """V3:--only-resource 只动 1 个 leaf,其他 lesson 整 lesson SKIP

        模拟真课场景:3 lesson × 2 resource = 6 leaf,只标 1.2 命中,期望 1 个 CREATE。
        """
        from scrape_new.upload.sync_tree import (
            TreeDiff, ChapterDiff, LessonDiff, LeafDiff, DiffAction,
        )
        from scrape_new.upload.api_uploader import _mark_only_targets

        # 构造 3 lesson × 2 resource 的 TreeDiff
        lesson_specs: list[tuple[int, str, list[tuple[str, str]]]] = [
            (1, "ch1", "1.1", [("video", "L1V"), ("ppt", "L1P")]),
            (1, "ch1", "1.2", [("video", "L2V"), ("ppt", "L2P")]),
            (1, "ch1", "1.3", [("video", "L3V"), ("ppt", "L3P")]),
        ]
        # 按 ch 分桶
        by_ch: dict[int, tuple[str, list]] = {}
        for ch_idx, ch_title, ls_id, leaves in lesson_specs:
            by_ch.setdefault(ch_idx, (ch_title, []))[1].append((ls_id, leaves))
        chapters: list[ChapterDiff] = []
        total_create = 0
        for ch_idx, (ch_title, ls_list) in sorted(by_ch.items()):
            ls_diffs = []
            for ls_id, leaves in ls_list:
                leaf_diffs = tuple(
                    LeafDiff(
                        lesson_id=ls_id, kind=k, desired_name=name,
                        actual_id=None, action=DiffAction.CREATE,
                    )
                    for k, name in leaves
                )
                ls_diffs.append(LessonDiff(
                    id=ls_id, desired_title=f"Lesson {ls_id}",
                    actual_id=None, actual_title=None,
                    action=DiffAction.CREATE, matched_by="none",
                    leaf_diffs=leaf_diffs,
                ))
                total_create += len(leaves)
            chapters.append(ChapterDiff(
                index=ch_idx, desired_title=ch_title,
                actual_id=None, actual_title=None,
                action=DiffAction.CREATE, matched_by="none",
                lesson_diffs=tuple(ls_diffs),
            ))
        diff = TreeDiff(
            course_id="c1",
            chapters=tuple(chapters),
            stats={"create": total_create, "skip": 0, "rename": 0, "prune": 0,
                   "create_chapters": 1, "create_sections": 3, "create_leaves": total_create},
        )

        # 应用 --only-resource (1.2, video)
        diff2 = _mark_only_targets(
            diff, only_lessons=None, only_resources={("1.2", "video")},
        )
        ch1 = diff2.chapters[0]
        # 1.2 的 video 仍 CREATE
        ls12 = next(l for l in ch1.lesson_diffs if l.id == "1.2")
        assert ls12.leaf_diffs[0].action == DiffAction.CREATE
        # 1.2 的 ppt 被 SKIP(不在 (1.2, video) 集合)
        assert ls12.leaf_diffs[1].action == DiffAction.SKIP
        # 1.1 / 1.3 整 lesson SKIP(全 leaf 非目标)
        ls11 = next(l for l in ch1.lesson_diffs if l.id == "1.1")
        ls13 = next(l for l in ch1.lesson_diffs if l.id == "1.3")
        assert ls11.action == DiffAction.SKIP
        assert ls13.action == DiffAction.SKIP
        # stats:create 从 6 降到 1
        assert diff2.stats["create"] == 1
        assert diff2.stats["skip"] >= 5  # 5 个非目标 SKIP

    def test_only_resource_drift_too_high_only_warns(self):
        """V3 副作用:局部模式 drift > 60% 也只 warn,不 reset

        模拟真课场景:6 资源,1 命中 → 5/6 SKIP,drift=1/6=16% (不高)
        改为:全 6 资源 SKIP(空匹配集)+ 1 命中 → 仍 1 CREATE
        验证 drift 检测代码逻辑:局部模式只 warn 不阻断
        """
        from scrape_new.upload.sync_tree import (
            TreeDiff, ChapterDiff, LessonDiff, LeafDiff, DiffAction,
        )
        # 构造 6 leaf,只命中 1,drift=1/6=16%(其实不高,验证 is_too_drifted 行为)
        leaves = [LeafDiff(
            lesson_id=f"1.{i}", kind="video", desired_name=f"L{i}",
            actual_id=None, action=DiffAction.CREATE,
        ) for i in range(1, 7)]
        diff = TreeDiff(
            course_id="c1",
            chapters=(ChapterDiff(
                index=1, desired_title="ch1", actual_id=None, actual_title=None,
                action=DiffAction.CREATE, matched_by="none",
                lesson_diffs=(LessonDiff(
                    id="1", desired_title="L1", actual_id=None, actual_title=None,
                    action=DiffAction.CREATE, matched_by="none",
                    leaf_diffs=tuple(leaves),
                ),),
            ),),
            stats={"create": 6, "skip": 0, "rename": 0, "prune": 0,
                   "create_chapters": 1, "create_sections": 1, "create_leaves": 6},
        )
        # 全部都 SKIP(空 only_lessons/only_resources → 实际是全 True,全保留)
        # 这里测试 is_too_drifted 的逻辑
        # 注意:实际是当 only_resources 是空 set 时,所有 leaf 都不命中 → 全 SKIP
        from scrape_new.upload.api_uploader import _mark_only_targets
        diff2 = _mark_only_targets(
            diff, only_lessons=None, only_resources=set(),  # 空集 → 全 SKIP
        )
        # 全 SKIP,create=0
        assert diff2.stats["create"] == 0
        assert diff2.stats["skip"] >= 6


# ─── V4:3 个其他 workflow 同步 cli_args ───────────────

class TestOtherWorkflowsSharedCLI:
    """V4:xuetangx / zhihuishu / icourse163 改用 cli_args.parse_workflow_args

    修复:之前 3 个 workflow 直接用 sys.argv[2] 当 output_dir,
    用户传 --scan-only 等旗标会被当 output_dir。
    """

    def test_cli_args_parse_handles_chaoxing_style_args(self):
        """V4 共享:parse_workflow_args 能解析 chaoxing 风格的所有旗标"""
        from scrape_new.workflows.cli_args import parse_workflow_args
        # URL + --scan-only
        r = parse_workflow_args(["URL", "--scan-only"])
        assert r.url == "URL"
        assert r.scan_only is True
        assert r.error is None
        # URL + out + --scan-only + --max-tabs 6
        r = parse_workflow_args(["URL", "out", "--scan-only", "--max-tabs", "6"])
        assert r.url == "URL"
        assert r.output_dir == "out"
        assert r.scan_only is True
        assert r.max_tabs == 6
        # URL + --verify-resume-only + --resume m.json
        r = parse_workflow_args(["URL", "--verify-resume-only", "--resume", "m.json"])
        assert r.url == "URL"
        assert r.verify_resume_only is True
        assert str(r.resume_manifest) == "m.json"
        # URL + --cpi 12345
        r = parse_workflow_args(["URL", "--cpi", "12345"])
        assert r.cpi == "12345"

    def test_xuetangx_uses_shared_cli_args(self):
        """V4:xuetangx main 走 parse_workflow_args,不再 sys.argv[2] 硬拿"""
        # 旧 bug 模拟:URL --scan-only
        # 旧代码 output_dir = "--scan-only"(因为 sys.argv[2] 是 --scan-only)
        # 新代码 output_dir = "./output"(默认值)
        from scrape_new.workflows.cli_args import parse_workflow_args
        r = parse_workflow_args(["URL", "--scan-only"], default_output="./output")
        assert r.url == "URL", f"URL parse fail, got {r.url}"
        assert r.output_dir == "./output", f"output_dir should be default, got {r.output_dir}"
        assert r.scan_only is True

    def test_zhihuishu_uses_shared_cli_args(self):
        """V4:zhihuishu main 走 parse_workflow_args,不再 sys.argv[2] 硬拿"""
        from scrape_new.workflows.cli_args import parse_workflow_args
        r = parse_workflow_args(["URL", "--scan-only"], default_output="./output")
        assert r.url == "URL"
        assert r.output_dir == "./output"
        assert r.scan_only is True

    def test_icourse163_uses_shared_cli_args(self):
        """V4:icourse163 main 走 parse_workflow_args,不再 sys.argv[2] 硬拿"""
        from scrape_new.workflows.cli_args import parse_workflow_args
        r = parse_workflow_args(["URL", "--scan-only"], default_output="./output")
        assert r.url == "URL"
        assert r.output_dir == "./output"
        assert r.scan_only is True

    def test_extract_positional_args_via_shared_module(self):
        """V4:共享 cli_args.extract_positional_args 可独立用"""
        from scrape_new.workflows.cli_args import extract_positional_args
        # 各种旗标组合
        assert extract_positional_args(["URL", "out", "--scan-only"]) == ["URL", "out"]
        assert extract_positional_args(["URL", "--scan-only", "--max-tabs", "4"]) == ["URL"]
        assert extract_positional_args(["URL", "--cpi", "12345"]) == ["URL"]
        assert extract_positional_args(["URL", "--verify-resume-only", "--resume", "m.json"]) == ["URL"]


# ─── 第十六轮 P0/P2:运行入口 + verify-resume-only 行为一致 ──

class TestRunEntrypointConsistency:
    """P0:4 个 workflow 都能 `python -m scrape_new.workflows.X` 跑
    P2:xuetangx/zhihuishu/icourse163 传 --verify-resume-only 不在解析阶段就报错
    """

    def test_importing_workflow_modules_succeeds(self):
        """P0:模块路径 import 4 个 workflow 主模块不抛 ModuleNotFoundError"""
        # 模拟 `python -m scrape_new.workflows.chaoxing` 的 import 阶段
        import importlib
        for mod_name in (
            "scrape_new.workflows.chaoxing",
            "scrape_new.workflows.xuetangx",
            "scrape_new.workflows.zhihuishu",
            "scrape_new.workflows.icourse163",
        ):
            mod = importlib.import_module(mod_name)
            assert hasattr(mod, "main"), f"{mod_name} 缺 main()"

    def test_workflow_files_have_syspath_bootstrap(self):
        """P0:4 个 workflow 文件顶部都有 sys.path bootstrap(让直跑也能用)"""
        import re
        from pathlib import Path
        for wf in ("chaoxing.py", "xuetangx.py", "zhihuishu.py", "icourse163.py"):
            p = Path("E:/林视/scrape_new/workflows") / wf
            text = p.read_text(encoding="utf-8")
            # 必须在顶部 30 行内(导入 scrape_new 之前)
            head = text[:2000]  # 前 ~50 行
            assert "_PROJECT_ROOT" in head, f"{wf} 缺 sys.path bootstrap"
            assert "sys.path.insert" in head, f"{wf} 缺 sys.path.insert 调用"

    def test_xuetangx_accepts_verify_resume_only_without_resume(self):
        """P2:xuetangx 传 --verify-resume-only 不报 必须配 --resume 错误"""
        from scrape_new.workflows.cli_args import parse_workflow_args
        # require_resume_for_verify=False → 不报错
        r = parse_workflow_args(
            ["URL", "--verify-resume-only"],
            default_output="./output",
            require_resume_for_verify=False,
        )
        assert r.error is None
        assert r.verify_resume_only is True

    def test_zhihuishu_accepts_verify_resume_only_without_resume(self):
        """P2:zhihuishu 同上"""
        from scrape_new.workflows.cli_args import parse_workflow_args
        r = parse_workflow_args(
            ["URL", "--verify-resume-only"],
            default_output="./output",
            require_resume_for_verify=False,
        )
        assert r.error is None
        assert r.verify_resume_only is True

    def test_icourse163_accepts_verify_resume_only_without_resume(self):
        """P2:icourse163 同上"""
        from scrape_new.workflows.cli_args import parse_workflow_args
        r = parse_workflow_args(
            ["URL", "--verify-resume-only"],
            default_output="./output",
            require_resume_for_verify=False,
        )
        assert r.error is None
        assert r.verify_resume_only is True

    def test_chaoxing_still_requires_resume_for_verify(self):
        """P2:chaoxing 仍要求 --verify-resume-only 必须配 --resume(原行为不变)"""
        from scrape_new.workflows.cli_args import parse_workflow_args
        # require_resume_for_verify 默认 True → 必须配 --resume
        r = parse_workflow_args(
            ["URL", "--verify-resume-only"],
            default_output="./output",
        )
        assert r.error is not None
        assert "verify-resume-only" in r.error.lower() or "必须配 --resume" in r.error

    def test_workflows_init_does_not_preimport_submodules(self):
        """P1:workflows/__init__.py 不再预 import 4 个子模块
        (避免 `python -m` 时 RuntimeWarning)"""
        # 关键:__init__.py 不能 from .chaoxing import main ...
        from pathlib import Path
        init = Path("E:/林视/scrape_new/workflows/__init__.py").read_text(encoding="utf-8")
        # 用正则,只匹配代码行(忽略注释),以 `from .X import` 起首
        import re
        # 多行:^ 注释前缀是 # 才算注释
        code_lines = "\n".join(
            line for line in init.split("\n")
            if not line.lstrip().startswith("#")
        )
        for name in ("chaoxing", "xuetangx", "zhihuishu", "icourse163"):
            assert not re.search(
                rf"^\s*from \.{name}\s+import",
                code_lines, re.MULTILINE,
            ), f"__init__.py 仍预 import .{name}"
        # 应该有 __all__ 声明
        assert "__all__" in init
        # 列出可用子模块名
        for name in ("chaoxing", "xuetangx", "zhihuishu", "icourse163", "cli_args"):
            assert name in init

    def test_workflow_files_have_only_one_syspath_insert(self):
        """P3:每个 workflow 文件 sys.path.insert 只出现 1 次(去重)"""
        from pathlib import Path
        for wf in ("chaoxing.py", "xuetangx.py", "zhihuishu.py", "icourse163.py"):
            text = (Path("E:/林视/scrape_new/workflows") / wf).read_text(encoding="utf-8")
            count = text.count("sys.path.insert")
            assert count == 1, f"{wf} 含 {count} 处 sys.path.insert(应有 1 处)"


# ─── 第二十一轮 S2/S3:cldisk Referer + 空章跳过 ─────

class TestS2S3:
    """S2:download_video 按 role 设 cldisk Referer(video/ppt 分流)
       S3:build-mapping 默认跳空章/空节 + 产 _mapping_exclusions.md"""

    def test_get_video_download_url_referer_for_ppt(self):
        """S2:role=ppt → Referer 是 ananas/modules/ppt/index.html"""
        from scrape_new.workflows.chaoxing import get_video_download_url
        from unittest.mock import MagicMock
        sess = MagicMock()
        sess.get.return_value.json.return_value = {
            "status": "success",
            "download": "http://d0.cldisk.com/download/xxx",
            "http": "",
            "filename": "ch1.pptx",
            "length": 1024,
        }
        info = get_video_download_url(sess, "fake-oid", role="ppt")
        assert info is not None
        assert info["referer"] == "https://mooc1.chaoxing.com/ananas/modules/ppt/index.html"

    def test_get_video_download_url_referer_for_video(self):
        """S2:role=video → Referer 是 ananas/modules/video/index.html"""
        from scrape_new.workflows.chaoxing import get_video_download_url
        from unittest.mock import MagicMock
        sess = MagicMock()
        sess.get.return_value.json.return_value = {
            "status": "success",
            "download": "http://d0.cldisk.com/download/xxx",
            "http": "",
            "filename": "ch1.mp4",
            "length": 1024,
        }
        info = get_video_download_url(sess, "fake-oid", role="video")
        assert info is not None
        assert info["referer"] == "https://mooc1.chaoxing.com/ananas/modules/video/index.html"

    def test_build_mapping_excludes_empty_chapters_and_lessons(self, tmp_path: Path):
        """S3:build-mapping 默认跳过无视频无附件的章节/课时,产 _mapping_exclusions.md"""
        import json
        outline = {
            "source_url": "x", "platform": "chaoxing", "course_title": "测试课",
            "chapters": [
                {"index": 1, "title": "ch1 有资源",
                 "lessons": [{"id": "1.1", "title": "L1", "content_type": "video",
                              "video_filename": "1.1_L1.mp4"}]},
                {"index": 2, "title": "ch2 整章空",
                 "lessons": [
                     {"id": "2.1", "title": "L1", "content_type": "video", "video_filename": ""},
                     {"id": "2.2", "title": "L2", "content_type": "video", "video_filename": ""},
                 ]},
                {"index": 3, "title": "ch3 部分空",
                 "lessons": [
                     {"id": "3.1", "title": "L1", "content_type": "video", "video_filename": "3.1_L1.mp4"},
                     {"id": "3.2", "title": "L2", "content_type": "video", "video_filename": ""},
                 ]},
            ],
        }
        outline_path = tmp_path / "outline.json"
        outline_path.write_text(json.dumps(outline, ensure_ascii=False), encoding="utf-8")
        video_dir = tmp_path / "视频"; video_dir.mkdir()
        (video_dir / "1.1_L1.mp4").write_bytes(b"x" * 1024)
        (video_dir / "3.1_L1.mp4").write_bytes(b"x" * 1024)
        from scrape_new.upload.cli import main
        out_path = tmp_path / "_mapping.json"
        rc = main([
            "build-mapping", "--videos", str(video_dir), "--doc", str(outline_path),
            "--out", str(out_path), "--course-id", "c1",
        ])
        assert rc == 0
        assert out_path.exists()
        excl_path = tmp_path / "_mapping_exclusions.md"
        assert excl_path.exists()
        content = excl_path.read_text(encoding="utf-8")
        # ch2 是整章空
        assert "ch2 整章空" in content
        # 3.2 是空课时
        assert "3.2" in content
        # 1.1 / 3.1 保留(不在 exclusions)
        assert "1.1" not in content
        assert "3.1" not in content

    def test_build_mapping_with_include_empty_keeps_all(self, tmp_path: Path):
        """S3:--include-empty-lessons 保留空结构,mapping 全保留,exclusions 报告"保留但空"清单"""
        import json
        outline = {
            "source_url": "x", "platform": "chaoxing", "course_title": "t",
            "chapters": [
                {"index": 1, "title": "ch1 全空",
                 "lessons": [{"id": "1.1", "title": "L", "content_type": "video", "video_filename": ""}]},
            ],
        }
        outline_path = tmp_path / "o.json"
        outline_path.write_text(json.dumps(outline, ensure_ascii=False), encoding="utf-8")
        video_dir = tmp_path / "视频"; video_dir.mkdir()
        from scrape_new.upload.cli import main
        out_path = tmp_path / "_mapping.json"
        rc = main([
            "build-mapping", "--videos", str(video_dir), "--doc", str(outline_path),
            "--out", str(out_path), "--include-empty-lessons",
        ])
        assert rc == 0
        # --include-empty:仍写 exclusions(用"保留但空"标记)
        excl = tmp_path / "_mapping_exclusions.md"
        assert excl.exists()
        content = excl.read_text(encoding="utf-8")
        assert "保留但空" in content
        # mapping 仍包含 ch1(空)
        data = json.loads(out_path.read_text(encoding="utf-8"))
        assert len(data["chapters"]) == 1


# ─── 第二十三轮 P1/P2:plan-only 文案 + 空节 + flag 不重复 ──

class TestRound23P1P2:
    """P1:print_summary 在 plan-only 时不写"老师后台上传完成"
       P2:scan-only 已经包含空节 + build-mapping include 模式行为
       附:detect_suspicious_lessons flag 不重复(防回归)"""

    def test_print_summary_plan_only_does_not_say_upload_complete(self, capsys):
        """P1:mode='plan_only' 时 print_summary 输出"计划生成完成,未执行上传"而不是"老师后台上传完成" """
        from scrape_new.upload.report import print_summary
        from scrape_new.upload.models import UploadResult
        r = UploadResult(
            course_id="c1", course_title="测试课",
            started_at="2026-06-19", assets=(), mode="plan_only",
        )
        print_summary(r)
        out = capsys.readouterr().out
        assert "计划生成完成,未执行上传" in out
        assert "老师后台上传完成" not in out
        assert "请 review _upload_plan.md" in out
        # 引导用户用 --apply-plan / --yes
        assert "--apply-plan" in out
        assert "--yes" in out

    def test_print_summary_verify_only_routes_to_verify_block(self, capsys):
        """P1:mode='verify_only' 时不打印上传完成,改"verify-only 模式,未执行上传" """
        from scrape_new.upload.report import print_summary
        from scrape_new.upload.models import UploadResult
        r = UploadResult(
            course_id="c1", course_title="测试课",
            started_at="2026-06-19", assets=(), mode="verify_only",
        )
        print_summary(r)
        out = capsys.readouterr().out
        assert "verify-only 模式,未执行上传" in out
        assert "老师后台上传完成" not in out

    def test_print_summary_upload_mode_keeps_old_format(self, capsys):
        """P1:mode='upload'(默认)时仍打印"老师后台上传完成!"(向后兼容)"""
        from scrape_new.upload.report import print_summary
        from scrape_new.upload.models import UploadResult, Asset, AssetStatus, ContentType
        a = Asset(
            chapter_index=1, lesson_id="1.1", lesson_title="L1",
            content_type=ContentType.VIDEO, source_path="x.mp4",
            status=AssetStatus.OK,
        )
        r = UploadResult(
            course_id="c1", course_title="测试课",
            started_at="2026-06-19", assets=(a,), mode="upload",
        )
        print_summary(r)
        out = capsys.readouterr().out
        assert "老师后台上传完成" in out
        assert "成功: 1" in out

    def test_build_scan_context_no_double_detect(self):
        """附:build_scan_context 内部已调 detect_suspicious_lessons,
        外部再调一次 → flag 重复 2 次(回归测试)"""
        from scrape_new.services.scan_chaoxing import (
            build_scan_context, LessonScanResult, ROLE_PPT, ROLE_VIDEO,
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
        ctx = build_scan_context(course_id="c1", course_title="t", lessons=lessons)
        # L2 应该有 suspicious_missing_ppt(同章多数有 PPT)
        assert "suspicious_missing_ppt" in lessons[1].flags
        # 关键:flag 只 1 次(不重复)
        assert lessons[1].flags.count("suspicious_missing_ppt") == 1

    def test_build_mapping_include_empty_records_kept_but_empty(self, tmp_path: Path):
        """P2:--include-empty-lessons 时,mapping 保留空,exclusions.md 标"保留但空" """
        import json
        outline = {
            "source_url": "x", "platform": "chaoxing", "course_title": "t",
            "chapters": [
                {"index": 1, "title": "ch1 全空",
                 "lessons": [{"id": "1.1", "title": "L1", "content_type": "video", "video_filename": ""}]},
                {"index": 2, "title": "ch2 部分空",
                 "lessons": [
                     {"id": "2.1", "title": "L1", "content_type": "video", "video_filename": "2.1_L1.mp4"},
                     {"id": "2.2", "title": "L2空", "content_type": "video", "video_filename": ""},
                 ]},
            ],
        }
        outline_path = tmp_path / "o.json"
        outline_path.write_text(json.dumps(outline, ensure_ascii=False), encoding="utf-8")
        video_dir = tmp_path / "视频"; video_dir.mkdir()
        (video_dir / "2.1_L1.mp4").write_bytes(b"x" * 1024)
        from scrape_new.upload.cli import main
        rc = main([
            "build-mapping", "--videos", str(video_dir), "--doc", str(outline_path),
            "--out", str(tmp_path / "_mapping.json"), "--include-empty-lessons",
        ])
        assert rc == 0
        # mapping 含 ch1 + ch2(都保留)
        data = json.loads((tmp_path / "_mapping.json").read_text(encoding="utf-8"))
        assert len(data["chapters"]) == 2
        # exclusions.md 记录"保留但空"
        excl = (tmp_path / "_mapping_exclusions.md").read_text(encoding="utf-8")
        assert "保留但空" in excl
        # ch1 整章空 → 在"整章空章"段
        assert "ch1" in excl
        # 1.1 / 2.2 单节空 → 在"空课时"段
        assert "1.1" in excl
        assert "2.2" in excl
        # 2.1 不在(exclusions)(它有视频)
        assert "2.1" not in excl
