"""
课程本地验收总报告测试(本轮)。

覆盖:
  - 状态机: INCOMPLETE / BLOCKED / REVIEW / READY
  - 输入读取: 缺失 / JSON 解析失败 容错
  - summary 计数: chapters / lessons / resources / video / doc / failed / suspicious / retry
  - recommendations / next_commands 触发
  - CLI: --json / --markdown / 默认写文件
  - wizard: --intent accept + --execute-step accept (mock subprocess)

所有测试 fake JSON / tmp_path / monkeypatch, 0 网络。
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

import pytest


# ─── helper ──────────────────────────────────────────

# 仓库根:子进程必须从仓库根启动 `python -m scrape_new`, 否则 CI 上
# "No module named scrape_new"。见 scrape_new/tests/_paths.py。
from scrape_new.tests._paths import PROJECT_ROOT


def _ns(**overrides) -> argparse.Namespace:
    """构造一个 wizard 子命令的 Namespace(走非交互分支)。"""
    base = {
        "intent": "accept",
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


def _write(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


def _chapter_tree() -> dict:
    return {
        "course_title": "Demo Course",
        "platform": "chaoxing",
        "chapters": [
            {"index": 1, "title": "ch1", "lessons": [{"id": "1.1", "title": "L1"}]},
            {"index": 2, "title": "ch2", "lessons": [{"id": "2.1", "title": "L2"}]},
        ],
    }


def _clean_manifest() -> dict:
    return {"records": [
        {"ch_num": 1, "ls_num": 1, "type": ".mp4", "name": "1.mp4",
         "saved_name": "1.1_L1.mp4", "objectid": "oid-1", "status": "downloaded"},
        {"ch_num": 2, "ls_num": 1, "type": ".pptx", "name": "1.pptx",
         "saved_name": "2.1_L2.pptx", "objectid": "oid-2", "status": "downloaded"},
    ]}


# ─── 1: INCOMPLETE 当缺核心产物 ──────────────────────────

class TestIncomplete:
    def test_incomplete_when_missing_manifest(self, tmp_path):
        """1:只有 chapter_tree,缺 manifest → INCOMPLETE"""
        from scrape_new.services.course_acceptance import build_course_acceptance_report
        _write(tmp_path / "_chapter_tree.json", _chapter_tree())
        report = build_course_acceptance_report(tmp_path)
        assert report.status == "INCOMPLETE"
        assert "_resource_naming_manifest.json" in report.missing_inputs
        # next_commands 应该给 scan-only 建议
        joined = " ".join(report.next_commands)
        assert "scan-only" in joined

    def test_incomplete_when_output_dir_does_not_exist(self, tmp_path):
        """output_dir 完全不存在也要返回 INCOMPLETE 报告(不抛)"""
        from scrape_new.services.course_acceptance import build_course_acceptance_report
        nonexistent = tmp_path / "no_such_dir"
        report = build_course_acceptance_report(nonexistent)
        assert report.status == "INCOMPLETE"
        # output_dir 不存在时 _write_acceptance_reports 仍然能 mkdir
        from scrape_new.services.course_acceptance import write_course_acceptance_reports
        paths = write_course_acceptance_reports(report, nonexistent)
        assert paths["json"].exists()
        assert paths["markdown"].exists()


# ─── 2: READY 当全绿 ──────────────────────────────────────

class TestReady:
    def test_ready_with_clean_manifest_and_tree(self, tmp_path):
        """2:有 chapter_tree + manifest,无 audit risk,无 retry → READY"""
        from scrape_new.services.course_acceptance import build_course_acceptance_report
        _write(tmp_path / "_chapter_tree.json", _chapter_tree())
        _write(tmp_path / "_resource_naming_manifest.json", _clean_manifest())
        report = build_course_acceptance_report(tmp_path)
        assert report.status == "READY"
        # summary 计数
        assert report.summary["chapters_count"] == 2
        assert report.summary["lessons_count"] == 2
        assert report.summary["resources_count"] == 2
        assert report.summary["video_count"] == 1
        assert report.summary["document_count"] == 1
        assert report.summary["failed_count"] == 0
        # 没有 audit json → audit counts 全 0
        assert report.summary["audit_high_count"] == 0
        # 没有 mapping → mapping_present 0
        assert report.summary["mapping_present"] == 0
        # recommendations 含"可以进入下一步"
        assert any("可以进入下一步" in r or "进入下一步" in r for r in report.recommendations)
        # next_commands 应建议 build-mapping(没 mapping)
        joined = " ".join(report.next_commands)
        assert "build-mapping" in joined


# ─── 3: BLOCKED 当 audit 有 high risk ─────────────────────

class TestBlocked:
    def test_blocked_when_audit_has_high_risk(self, tmp_path):
        """3:audit 含 high risk → BLOCKED"""
        from scrape_new.services.course_acceptance import build_course_acceptance_report
        _write(tmp_path / "_chapter_tree.json", _chapter_tree())
        _write(tmp_path / "_resource_naming_manifest.json", _clean_manifest())
        # 写一个含 high lesson 的 audit
        audit = {
            "course_title": "Demo", "summary": {},
            "lessons": [
                {"lesson_id": "1.1", "lesson_title": "L1", "ch_num": 1,
                 "risk_level": "high", "issues": ["missing_local_file"], "resources": []},
            ],
        }
        _write(tmp_path / "_resource_audit.json", audit)
        report = build_course_acceptance_report(tmp_path)
        assert report.status == "BLOCKED"
        assert report.summary["audit_high_count"] == 1
        # recommendations 含"不建议上传"
        assert any("不建议上传" in r for r in report.recommendations)


# ─── 4: REVIEW 当 medium 无 high ──────────────────────────

class TestReview:
    def test_review_when_audit_has_medium_no_high(self, tmp_path):
        """4:只有 medium risk,无 high → REVIEW"""
        from scrape_new.services.course_acceptance import build_course_acceptance_report
        _write(tmp_path / "_chapter_tree.json", _chapter_tree())
        _write(tmp_path / "_resource_naming_manifest.json", _clean_manifest())
        audit = {
            "course_title": "Demo", "summary": {},
            "lessons": [
                {"lesson_id": "1.1", "lesson_title": "L1", "ch_num": 1,
                 "risk_level": "medium", "issues": ["count_mismatch"], "resources": []},
            ],
        }
        _write(tmp_path / "_resource_audit.json", audit)
        report = build_course_acceptance_report(tmp_path)
        assert report.status == "REVIEW"
        assert report.summary["audit_medium_count"] == 1
        # recommendations 含"建议人工确认"
        assert any("建议人工确认" in r for r in report.recommendations)


# ─── 5: retry_count 触发推荐 ──────────────────────────────

class TestRetryTrigger:
    def test_retry_count_adds_recommendation(self, tmp_path):
        """5:_retry_downloads.json count > 0 → next_commands 含 retry-downloads"""
        from scrape_new.services.course_acceptance import build_course_acceptance_report
        _write(tmp_path / "_chapter_tree.json", _chapter_tree())
        _write(tmp_path / "_resource_naming_manifest.json", _clean_manifest())
        # 写 retry list
        _write(tmp_path / "_retry_downloads.json",
               {"items": [{"saved_name": "1.1_x.mp4"}]})
        report = build_course_acceptance_report(tmp_path)
        # retry_count = 1 → medium risk
        assert report.summary["retry_count"] == 1
        # status: medium 至少 REVIEW
        assert report.status in ("REVIEW", "BLOCKED")
        # recommendations 或 next_commands 含 retry
        joined = " ".join(report.recommendations + report.next_commands)
        assert "retry" in joined.lower() or "重试" in joined


# ─── 6: 缺 mapping → build-mapping 推荐 ─────────────────

class TestMappingRecommendation:
    def test_missing_mapping_recommends_build_mapping(self, tmp_path):
        """6:无 _mapping.json → recommendations 含 build-mapping"""
        from scrape_new.services.course_acceptance import build_course_acceptance_report
        _write(tmp_path / "_chapter_tree.json", _chapter_tree())
        _write(tmp_path / "_resource_naming_manifest.json", _clean_manifest())
        report = build_course_acceptance_report(tmp_path)
        # READY + 没 mapping
        assert report.status == "READY"
        assert report.summary["mapping_present"] == 0
        # next_commands 至少一条含 build-mapping
        assert any("build-mapping" in c for c in report.next_commands)
        # recommendations 也提一下
        assert any("mapping" in r for r in report.recommendations)

    def test_mapping_present_recommends_upload_plan_only(self, tmp_path):
        """7:有 _mapping.json → next_commands 含 upload plan-only"""
        from scrape_new.services.course_acceptance import build_course_acceptance_report
        _write(tmp_path / "_chapter_tree.json", _chapter_tree())
        _write(tmp_path / "_resource_naming_manifest.json", _clean_manifest())
        _write(tmp_path / "_mapping.json", {
            "course_id": "9999", "course_title": "Demo",
            "chapters": [{"index": 1, "title": "ch1", "lessons": []}],
        })
        report = build_course_acceptance_report(tmp_path)
        assert report.summary["mapping_present"] == 1
        # recommendations 应提到 upload plan-only
        joined = " ".join(report.recommendations + report.next_commands)
        assert "plan-only" in joined


# ─── 7: JSON 解析失败不崩 ─────────────────────────────────

class TestJsonErrorTolerance:
    def test_json_parse_error_becomes_risk_not_crash(self, tmp_path):
        """8:坏 JSON → risk invalid_json,不崩"""
        from scrape_new.services.course_acceptance import build_course_acceptance_report
        _write(tmp_path / "_chapter_tree.json", _chapter_tree())
        _write(tmp_path / "_resource_naming_manifest.json", _clean_manifest())
        # 写坏 JSON
        (tmp_path / "_resource_audit.json").write_text("{ not json", encoding="utf-8")
        report = build_course_acceptance_report(tmp_path)
        # 不崩 + 含 invalid_json risk
        assert any(r.code == "invalid_json" for r in report.risks)
        # status 还能合理判定(audit 不可用 → 没 audit risk → READY)
        assert report.status in ("READY", "REVIEW", "BLOCKED", "INCOMPLETE")


# ─── 8: 报告写入 ────────────────────────────────────────

class TestReportWrite:
    def test_write_acceptance_reports_creates_json_and_md(self, tmp_path):
        """9:write_acceptance_reports 写 2 份文件"""
        from scrape_new.services.course_acceptance import (
            build_course_acceptance_report, write_course_acceptance_reports,
        )
        _write(tmp_path / "_chapter_tree.json", _chapter_tree())
        _write(tmp_path / "_resource_naming_manifest.json", _clean_manifest())
        report = build_course_acceptance_report(tmp_path)
        paths = write_course_acceptance_reports(report, tmp_path)
        assert paths["json"].exists()
        assert paths["markdown"].exists()
        # JSON 能 load
        data = json.loads(paths["json"].read_text(encoding="utf-8"))
        assert data["status"] == report.status
        # MD 含中文短语
        md = paths["markdown"].read_text(encoding="utf-8")
        # 至少含一个核心短语
        assert any(p in md for p in [
            "缺少关键产物", "不建议上传", "建议人工确认", "可以进入下一步"
        ]), f"MD 缺少核心短语: {md[:200]}"

    def test_markdown_contains_required_chinese_phrases(self, tmp_path):
        """10:MD 含核心中文短语"""
        from scrape_new.services.course_acceptance import (
            build_course_acceptance_report, write_course_acceptance_reports,
        )
        _write(tmp_path / "_chapter_tree.json", _chapter_tree())
        _write(tmp_path / "_resource_naming_manifest.json", _clean_manifest())
        # 故意给 audit 加 high + empty_lesson 触发多种短语
        audit = {
            "course_title": "Demo", "summary": {},
            "lessons": [
                {"lesson_id": "1.1", "lesson_title": "L1", "ch_num": 1,
                 "risk_level": "high", "issues": ["missing_local_file"], "resources": []},
                {"lesson_id": "2.1", "lesson_title": "L2", "ch_num": 2,
                 "risk_level": "ok", "issues": ["empty_lesson"], "resources": []},
            ],
        }
        _write(tmp_path / "_resource_audit.json", audit)
        report = build_course_acceptance_report(tmp_path)
        paths = write_course_acceptance_reports(report, tmp_path)
        md = paths["markdown"].read_text(encoding="utf-8")
        # 状态 BLOCKED + 含 high → "不建议上传" 必须出现
        assert "不建议上传" in md
        # 含"建议先修复"
        assert "建议先修复" in md


# ─── 9: CLI 测试 ────────────────────────────────────────

class TestCLIAccept:
    def test_cli_accept_json_output_parseable(self, tmp_path):
        """11:CLI --json 输出能 json.loads"""
        _write(tmp_path / "_chapter_tree.json", _chapter_tree())
        _write(tmp_path / "_resource_naming_manifest.json", _clean_manifest())
        # 直接调 _cmd_accept
        from scrape_new import cli
        ns = argparse.Namespace(output_dir=str(tmp_path), json=True, markdown=False)
        rc = cli._cmd_accept(ns)
        # READY → 0;但 _cmd_accept 默认会写文件(只 --json 时不写)
        # 实际: --json 模式只打印, 不写
        assert rc in (0, 2)
        # 因为我们直接传 --json=True 调 _cmd_accept,但 print 已经到 stdout
        # 改用 subprocess 验证 stdout parseable
        import os
        cmd = [
            "X:/Python/Python3.10.11/python.exe", "-X", "utf8",
            "-m", "scrape_new", "accept", "--output-dir", str(tmp_path), "--json",
        ]
        env = os.environ.copy()
        env["PYTHONIOENCODING"] = "utf-8"
        env["PYTHONUTF8"] = "1"
        r = subprocess.run(cmd, capture_output=True, text=True,
                           encoding="utf-8", timeout=30, env=env,
                           cwd=str(PROJECT_ROOT))
        assert r.returncode in (0, 2), f"stderr={r.stderr[:200]}"
        data = json.loads(r.stdout)
        assert "status" in data
        assert "summary" in data
        assert "risks" in data
        assert "missing_inputs" in data
        assert "next_commands" in data

    def test_cli_accept_markdown_output_contains_status(self, tmp_path):
        """12:CLI --markdown 输出含 status"""
        import os
        _write(tmp_path / "_chapter_tree.json", _chapter_tree())
        _write(tmp_path / "_resource_naming_manifest.json", _clean_manifest())
        cmd = [
            "X:/Python/Python3.10.11/python.exe", "-X", "utf8",
            "-m", "scrape_new", "accept", "--output-dir", str(tmp_path), "--markdown",
        ]
        env = os.environ.copy()
        env["PYTHONIOENCODING"] = "utf-8"
        env["PYTHONUTF8"] = "1"
        r = subprocess.run(cmd, capture_output=True, text=True,
                           encoding="utf-8", timeout=30, env=env,
                           cwd=str(PROJECT_ROOT))
        assert r.returncode in (0, 2)
        out = r.stdout
        # 含 Course Acceptance Report 标题
        assert "Course Acceptance Report" in out
        # 含 status 标签(INCOMPLETE / READY / REVIEW / BLOCKED)
        assert any(s in out for s in ("READY", "REVIEW", "BLOCKED", "INCOMPLETE"))


# ─── 10: wizard accept intent ───────────────────────────

class TestWizardAccept:
    def test_wizard_accept_plan_has_accept_step(self):
        """13:wizard --intent accept plan 含 accept step, risk=safe"""
        from scrape_new.services.workflow_planner import build_workflow_plan
        plan = build_workflow_plan(
            intent="accept", platform="unknown", course_url="",
            output_dir="./mycourse", cookie_source="none",
        )
        step_ids = [s.id for s in plan.steps]
        assert "accept" in step_ids
        # risk = safe
        assert plan.risk_level in ("safe", "low")
        # 至少一个 step 的 command 包含 'accept'
        joined = " ".join(s.command for s in plan.steps)
        assert "scrape_new accept" in joined
        # next_suggestions 非空
        assert len(plan.next_suggestions) >= 1

    def test_wizard_execute_accept_safe_step_mocked(self, tmp_path, monkeypatch):
        """14:_run_wizard --intent accept --execute-step accept + mock subprocess"""
        from scrape_new import cli
        from scrape_new.services.workflow_planner import build_workflow_plan

        plan = build_workflow_plan(
            intent="accept", platform="unknown", course_url="",
            output_dir=str(tmp_path), cookie_source="none",
        )
        # 强制非危险
        for s in plan.steps:
            s.destructive = False
            s.requires_confirmation = False

        called = {}
        class _FakeProc:
            def __init__(self):
                self.returncode = 0
                self.stdout = "accept ok\n"
                self.stderr = ""
        def fake_run(argv, **kwargs):
            called["argv"] = argv
            called["shell"] = kwargs.get("shell", False)
            return _FakeProc()
        log = tmp_path / "_wizard_runs.jsonl"

        monkeypatch.setattr(cli.subprocess, "run", fake_run)
        monkeypatch.setattr(
            "scrape_new.services.workflow_planner.build_workflow_plan",
            lambda **kw: plan,
        )

        rc = cli._run_wizard(_ns(
            intent="accept", platform="unknown",
            output_dir=str(tmp_path), cookie_source="none",
            execute_step="accept", run_log=str(log),
        ))
        assert rc == 0
        assert called["shell"] is False
        cmd_str = " ".join(called["argv"])
        assert "scrape_new accept" in cmd_str, f"command 应含 'scrape_new accept': {cmd_str[:200]}"

        # 日志
        assert log.exists()
        recs = [json.loads(l) for l in log.read_text(encoding="utf-8").splitlines() if l]
        assert recs[0]["step_id"] == "accept"
        assert recs[0]["status"] == "succeeded"
        assert recs[0]["returncode"] == 0
        assert recs[0]["intent"] == "accept"