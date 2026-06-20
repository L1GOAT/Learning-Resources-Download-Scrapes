"""
离线端到端验收测试 — fixture 串起 audit + wizard。

5 个测试:
  1. test_fixture_audit_generates_reports
  2. test_fixture_detects_expected_issues
  3. test_wizard_audit_plan_points_to_fixture_paths
  4. test_wizard_execute_safe_audit_step_with_mock_subprocess
  5. test_docs_examples_exist_and_are_generic

所有测试:
  - 只用本地 fixture,不真实网络
  - mock subprocess 不真实子进程
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

import pytest


FIXTURE_DIR = Path(__file__).parent / "fixtures" / "course_audit_demo"
DOCS_DIR = Path(__file__).parent.parent / "docs" / "examples"


# ─── helper ──────────────────────────────────────────

class _FakeProc:
    """模拟 subprocess.run 返回值。"""

    def __init__(self, returncode: int = 0, stdout: str = "", stderr: str = ""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _load_fixture(name: str) -> dict:
    return json.loads((FIXTURE_DIR / name).read_text(encoding="utf-8"))


def _run_audit_through_cli(tmp_path: Path):
    """直接调 resource_audit 的纯函数,模拟 audit CLI 行为(scan + mapping 合并)。"""
    from scrape_new.services.resource_audit import (
        audit_scan_completeness, audit_mapping_alignment,
        write_resource_audit_reports, CourseAuditReport,
    )

    chapter_tree = _load_fixture("_chapter_tree.json")
    manifest = _load_fixture("_resource_naming_manifest.json")
    mapping = _load_fixture("_mapping.json")
    scanned = manifest.get("records", [])

    report = audit_scan_completeness(chapter_tree, scanned)
    map_report = audit_mapping_alignment(mapping, manifest)

    # 合并 mapping audit(同 _cmd_audit 行为)
    report.global_issues.extend(map_report.global_issues)
    report.recommendations.extend(map_report.recommendations)
    existing_ids = {ls.lesson_id for ls in report.lessons}

    def _raise_risk(target, level: str) -> None:
        order = {"ok": 0, "low": 1, "medium": 2, "high": 3}
        if order[level] > order[target.risk_level]:
            target.risk_level = level

    for ls in map_report.lessons:
        if ls.lesson_id in existing_ids:
            target = next(x for x in report.lessons if x.lesson_id == ls.lesson_id)
            target.issues = list(set(target.issues + ls.issues))
            target.resources.extend(ls.resources)
            _raise_risk(target, ls.risk_level)
        else:
            report.lessons.append(ls)
    for k, v in map_report.summary.items():
        report.summary[k] = report.summary.get(k, 0) + v

    paths = write_resource_audit_reports(report, tmp_path)
    return paths, report


# ─── 1: fixture 跑通 audit,产出 3 份报告,md 含中文短语 ─────────────

class TestFixtureAuditGeneratesReports:
    def test_audit_through_fixture_writes_three_files_and_md_contains_keywords(self, tmp_path):
        paths, _report = _run_audit_through_cli(tmp_path)
        assert (tmp_path / "_resource_audit.json").exists()
        assert (tmp_path / "_resource_audit.md").exists()
        assert (tmp_path / "_resource_audit.csv").exists()

        md = paths["audit_md"].read_text(encoding="utf-8")

        # 至少出现一个核心中文短语
        phrases = ["可能漏扫", "需要人工确认", "建议补资源", "可以安全跳过"]
        assert any(p in md for p in phrases), \
            f"md 应含核心中文短语之一,实际:\n{md[:800]}"


# ─── 2: 故意制造的 issue 都被检测到 ───────────────────────────

class TestFixtureDetectsExpectedIssues:
    def test_all_intentional_issues_surface_in_report(self, tmp_path):
        _paths, report = _run_audit_through_cli(tmp_path)

        # 把 md + 全局 issues + 合并后的 lesson issues 都收集
        all_lesson_issues: set[str] = set()
        for ls in report.lessons:
            all_lesson_issues.update(ls.issues)
        global_text = " ".join(report.global_issues)

        # 2.1 空 lesson
        assert "empty_lesson" in all_lesson_issues
        # 2.2 saved_name 重复
        assert "重复" in global_text or "duplicate" in global_text.lower()
        # 2.1 missing_local_file
        assert "missing_local_file" in all_lesson_issues
        # 2.2 non_video_in_video_slot(pptx 放进 video 字段)
        assert "non_video_in_video_slot" in all_lesson_issues
        # 1.2 ppt_only_lesson_informational
        assert "ppt_only_lesson_informational" in all_lesson_issues

    def test_md_renders_high_medium_risk_sections(self, tmp_path):
        paths, _report = _run_audit_through_cli(tmp_path)
        md = paths["audit_md"].read_text(encoding="utf-8")
        # 中风险或高风险列表应出现
        assert ("⚡" in md or "中风险" in md or "高风险" in md or "MEDIUM" in md.upper())


# ─── 3: wizard audit intent plan 指向 fixture 路径 ─────────────────

class TestWizardAuditPlanPointsToFixturePaths:
    def test_wizard_plan_uses_fixture_paths(self, tmp_path):
        from scrape_new.services.workflow_planner import build_workflow_plan

        chapter_tree_path = FIXTURE_DIR / "_chapter_tree.json"
        manifest_path = FIXTURE_DIR / "_resource_naming_manifest.json"
        mapping_path = FIXTURE_DIR / "_mapping.json"

        plan = build_workflow_plan(
            intent="audit", platform="unknown", course_url="",
            output_dir=str(tmp_path), cookie_source="none",
            options={
                "chapter_tree_path": str(chapter_tree_path),
                "manifest_path": str(manifest_path),
                "mapping_path": str(mapping_path),
            },
        )

        step_ids = [s.id for s in plan.steps]
        assert "audit_scan" in step_ids, f"plan 应含 audit_scan step,实际 {step_ids}"
        assert "audit_mapping" in step_ids, f"plan 应含 audit_mapping step,实际 {step_ids}"

        # 至少有 step 的 command 引用 fixture 路径
        all_cmd = " ".join(s.command for s in plan.steps)
        assert "_chapter_tree.json" in all_cmd or "_resource_naming_manifest.json" in all_cmd
        # risk_level = safe(都不危险)
        assert plan.risk_level in ("safe", "low")


# ─── 4: --execute-step audit_scan mock subprocess,日志写入 ─────────

class TestWizardExecuteSafeAuditStepMocked:
    def _ns(self, **overrides) -> argparse.Namespace:
        base = {
            "intent": "audit",
            "platform": "unknown",
            "url": "",
            "output_dir": "./out",
            "cookie_source": "none",
            "course_id": None,
            "mapping_path": None,
            "outline_path": None,
            "videos_dir": None,
            "plan_path": None,
            "retry_list": None,
            "only_lessons": None,
            "only_resources": None,
            "reset_confirm": None,
            "include_empty_lessons": False,
            "max_tabs": 4,
            "json": False,
            "markdown": False,
            "no_color": False,
            "yes": False,
            "execute_step": None,
            "run_log": None,
        }
        base.update(overrides)
        return argparse.Namespace(**base)

    def test_execute_audit_scan_with_mocked_subprocess(self, tmp_path, monkeypatch):
        from scrape_new import cli
        from scrape_new.services.workflow_planner import build_workflow_plan

        plan = build_workflow_plan(
            intent="audit", platform="unknown", course_url="",
            output_dir=str(tmp_path), cookie_source="none",
            options={
                "chapter_tree_path": str(FIXTURE_DIR / "_chapter_tree.json"),
                "manifest_path": str(FIXTURE_DIR / "_resource_naming_manifest.json"),
                "mapping_path": str(FIXTURE_DIR / "_mapping.json"),
            },
        )
        # 强制安全属性
        for s in plan.steps:
            s.destructive = False
            s.requires_confirmation = False

        called = {}
        def fake_run(argv, **kwargs):
            called["argv"] = argv
            called["shell"] = kwargs.get("shell", False)
            return _FakeProc(returncode=0, stdout="audit ok\n", stderr="")

        log = tmp_path / "_wizard_runs.jsonl"

        monkeypatch.setattr(cli.subprocess, "run", fake_run)
        monkeypatch.setattr(
            "scrape_new.services.workflow_planner.build_workflow_plan",
            lambda **kw: plan,
        )

        rc = cli._run_wizard(self._ns(
            intent="audit", platform="unknown",
            output_dir=str(tmp_path), cookie_source="none",
            execute_step="audit_scan",
            run_log=str(log),
        ))
        assert rc == 0
        assert called["shell"] is False
        # command 拼接成字符串应含 audit 子命令
        cmd_str = " ".join(called["argv"])
        assert "scrape_new audit" in cmd_str, f"command 应含 'scrape_new audit': {cmd_str[:200]}"

        # 日志
        assert log.exists()
        recs = [json.loads(l) for l in log.read_text(encoding="utf-8").splitlines() if l]
        assert len(recs) >= 1
        rec = recs[-1]
        assert rec["step_id"] == "audit_scan"
        assert rec["status"] == "succeeded"
        assert rec["returncode"] == 0
        assert rec["intent"] == "audit"


# ─── 5: docs/examples 文件存在且内容通用,无真实数据 ─────────────

class TestDocsExamplesExistAndAreGeneric:
    """docs/examples/ 两个 md 文件存在,内容不泄漏真实课程名 / 凭据。"""

    REQUIRED_FILES = [
        DOCS_DIR / "resource_audit_demo.md",
        DOCS_DIR / "offline_e2e_workflow.md",
    ]

    FORBIDDEN_COURSE_NAMES = ["逻辑学", "物理化学", "地震灾害", "免疫", "教学媒体"]
    FORBIDDEN_SECRETS = ["sessionid=", "csrftoken=", "p_auth_token=", "vc3="]

    def test_both_files_exist(self):
        for p in self.REQUIRED_FILES:
            assert p.exists(), f"docs 缺失: {p}"
            text = p.read_text(encoding="utf-8")
            assert text.strip(), f"docs 空文件: {p}"

    def test_no_real_course_names_in_either_doc(self):
        for p in self.REQUIRED_FILES:
            text = p.read_text(encoding="utf-8")
            for name in self.FORBIDDEN_COURSE_NAMES:
                assert name not in text, f"{p} 出现真实课程名 '{name}'"

    def test_no_real_secret_fields_in_either_doc(self):
        for p in self.REQUIRED_FILES:
            text = p.read_text(encoding="utf-8")
            for secret in self.FORBIDDEN_SECRETS:
                assert secret not in text, f"{p} 出现敏感字段 '{secret}'"