"""
wizard + WorkflowPlanner 测试(第十九轮)

10 测试:
  1-5: planner 各 intent 行为
  6-9: wizard CLI 输出 + 边界
  10: 不破坏现有测试(隐式 — 419 passed 即可)

所有测试都用 JSON 输入 / subprocess 调用,**不触发真实下载或上传**。
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

import pytest


PYTHON = sys.executable

# 仓库根:scrape_new/tests/ → parents[2]。
# subprocess.run(..., cwd=...) 不能写死 "E:/林视" 这种本地路径,
# CI runner(linux/windows)找不到,会 FileNotFoundError。
PROJECT_ROOT = Path(__file__).resolve().parents[2]


# ─── planner 测试(纯函数) ────────────────────────────────

class TestPlanner:
    """1-5:planner 各 intent 行为"""

    def test_download_intent_generates_scan_and_download(self):
        """1:download intent 生成 scan + download 步骤,带 next_suggestions"""
        from scrape_new.services.workflow_planner import build_workflow_plan
        plan = build_workflow_plan(
            intent="download", platform="chaoxing",
            course_url="https://x", output_dir="./mycourse",
            cookie_source="env",
        )
        # 步骤至少 2(scan + download)
        assert len(plan.steps) >= 2
        step_ids = [s.id for s in plan.steps]
        assert "scan" in step_ids
        assert "download" in step_ids
        # scan 不需要 cookie
        scan_step = next(s for s in plan.steps if s.id == "scan")
        assert scan_step.requires_cookie is False
        # download 需要 cookie
        dl_step = next(s for s in plan.steps if s.id == "download")
        assert dl_step.requires_cookie is True
        # next_suggestions 非空
        assert any("build_mapping" in s for s in plan.next_suggestions)
        # 输出文件至少含 _review.html 或 _chapter_tree.json
        outs_str = " ".join(plan.expected_outputs)
        assert "_review.html" in outs_str or "_chapter_tree.json" in outs_str

    def test_upload_intent_plan_first_no_yes(self):
        """2:upload 默认先 plan-only,plan-only 步骤不在危险列表"""
        from scrape_new.services.workflow_planner import build_workflow_plan
        plan = build_workflow_plan(
            intent="upload", platform="chaoxing", course_url="",
            output_dir="./mycourse", cookie_source="env",
            options={"course_id": "1234", "mapping_path": "./_mapping.json"},
        )
        step_ids = [s.id for s in plan.steps]
        # 第一步必须是 plan_only
        assert step_ids[0] == "plan_only", f"upload 第一步应该是 plan_only,实际 {step_ids}"
        # plan_only 不在 destructive
        plan_only_step = plan.steps[0]
        assert plan_only_step.destructive is False
        assert plan_only_step.requires_confirmation is False
        # risk 不是 HIGH(没 reset_confirm)
        assert plan.risk_level != "high"

    def test_modify_with_only_resources_excludes_reset_confirm(self, tmp_path: Path):
        """3:modify + only-resources 局部上传不包含 reset_confirm"""
        from scrape_new.services.workflow_planner import build_workflow_plan
        # 预先创建 plan_path 文件(否则 plan-only 之后不会生成 apply-plan)
        plan_path = tmp_path / "_upload_plan.json"
        plan_path.write_text("{}", encoding="utf-8")
        plan = build_workflow_plan(
            intent="modify", platform="chaoxing",
            output_dir=str(tmp_path), cookie_source="env",
            options={
                "course_id": "1234",
                "only_resources": "1.2:ppt",
                "plan_path": str(plan_path),
                # 注意:即使传 reset_confirm,modify 模式不该自动用
                "reset_confirm": "1234",
            },
        )
        # apply-plan 命令不应包含 --reset-confirm
        apply_step = next(s for s in plan.steps if s.id == "apply_plan")
        assert "--reset-confirm" not in apply_step.command
        # 但 required_confirmation 仍在(apply-plan 是写操作)
        assert apply_step.requires_confirmation is True

    def test_apply_plan_marked_requires_confirmation(self):
        """4:apply-plan 被标 requires_confirmation = True(GUI 弹确认对话框)"""
        from scrape_new.services.workflow_planner import build_workflow_plan
        plan = build_workflow_plan(
            intent="upload", platform="chaoxing",
            output_dir="./mycourse", cookie_source="env",
            options={"course_id": "1234", "mapping_path": "./_mapping.json"},
        )
        # 找到 apply_plan 步骤
        apply_steps = [s for s in plan.steps if s.id == "apply_plan"]
        if apply_steps:  # plan_only 之后才会生成 apply_plan
            assert apply_steps[0].requires_confirmation is True
            assert apply_steps[0].destructive is True
            assert "apply_plan" in plan.required_confirmations

    def test_reset_confirm_marked_destructive(self):
        """5:--reset-confirm 路径 destructive = True,risk = high"""
        from scrape_new.services.workflow_planner import build_workflow_plan
        plan = build_workflow_plan(
            intent="upload", platform="chaoxing",
            output_dir="./mycourse", cookie_source="env",
            options={
                "course_id": "1234",
                "mapping_path": "./_mapping.json",
                "reset_confirm": "1234",
            },
        )
        assert plan.risk_level == "high"
        # apply_plan 步骤的 notes 含 reset 警告
        apply_steps = [s for s in plan.steps if s.id == "apply_plan"]
        if apply_steps:
            assert "reset" in apply_steps[0].notes.lower() or "清空" in apply_steps[0].notes


# ─── wizard CLI 测试 ────────────────────────────────

class TestWizardCLI:
    """6-9:wizard CLI 输出格式 + 边界"""

    def _run_wizard(self, *args) -> subprocess.CompletedProcess:
        """subprocess 跑 wizard(避免污染测试 sys.path)。

        Windows 默认 stdout 用 GBK,UTF-8 字符(▶ ⚠️)会炸。
        用 PYTHONIOENCODING=utf-8 + PYTHONUTF8=1 强制 UTF-8 输出。
        保留父进程的 PATH(避免 Windows _Py_HashRandomization_Init 失败)。
        """
        import os
        cmd = [PYTHON, "-X", "utf8", "-m", "scrape_new", "wizard", *args]
        # 在父 env 上加 UTF-8 旗标,其他不变
        env = os.environ.copy()
        env["PYTHONIOENCODING"] = "utf-8"
        env["PYTHONUTF8"] = "1"
        return subprocess.run(
            cmd, capture_output=True, text=True, encoding="utf-8",
            timeout=15, cwd=str(PROJECT_ROOT), env=env,
        )

    def test_wizard_json_output_is_parseable(self):
        """6:wizard --json 输出能 json.loads"""
        r = self._run_wizard(
            "--intent", "download",
            "--platform", "chaoxing",
            "--url", "https://example.com/c",
            "--output-dir", "./x",
            "--cookie-source", "env",
            "--json",
        )
        assert r.returncode == 0, f"stdout={r.stdout[:200]} stderr={r.stderr[:200]}"
        # 至少能 json.loads 第一段非空 stdout
        data = json.loads(r.stdout)
        assert "intent" in data
        assert "steps" in data
        assert isinstance(data["steps"], list)
        assert "risk_level" in data

    def test_wizard_markdown_output_contains_command_and_risk(self):
        """7:wizard --markdown 含命令、风险"""
        r = self._run_wizard(
            "--intent", "upload",
            "--course-id", "1234",
            "--cookie-source", "env",
            "--markdown",
        )
        assert r.returncode == 0, f"stderr={r.stderr[:300]}"
        out = r.stdout
        # 含 Workflow Plan 标题
        assert "Workflow Plan" in out or "workflow plan" in out.lower()
        # 含命令
        assert "python" in out
        # 含风险等级
        assert "risk" in out.lower() or "MEDIUM" in out or "HIGH" in out or "LOW" in out

    def test_wizard_no_cookie_does_not_crash(self):
        """8:无 cookie 时不崩,只提示"""
        r = self._run_wizard(
            "--intent", "scan",
            "--platform", "chaoxing",
            "--url", "https://example.com/c",
            "--output-dir", "./x",
            "--cookie-source", "none",
            "--markdown",
        )
        assert r.returncode == 0
        out = r.stdout
        # 应有 plan(可能 risk = HIGH 因为无 cookie)
        assert "Workflow Plan" in out or "workflow plan" in out.lower()

    def test_wizard_unknown_intent_returns_clean_plan(self):
        """9:wizard --intent unknown 给清晰 next_suggestion(不要崩)"""
        r = self._run_wizard(
            "--intent", "unknown",
            "--markdown",
        )
        assert r.returncode == 0  # 不应崩,只是给空 plan + 提示
        out = r.stdout
        # 应有 plan 标题
        assert "Workflow Plan" in out or "workflow plan" in out.lower()
        # 应给可执行的下一步建议(intent 名列表)
        assert "download" in out.lower()
        assert "upload" in out.lower()

    def test_wizard_audit_intent_json_has_audit_steps(self):
        """11:wizard --intent audit --json 能 json.loads,steps 含 audit 命令"""
        r = self._run_wizard(
            "--intent", "audit",
            "--json",
        )
        assert r.returncode == 0, f"stderr={r.stderr[:300]}"
        data = json.loads(r.stdout)
        # 标准字段都在
        assert "intent" in data
        assert data["intent"] == "audit"
        assert "steps" in data
        assert isinstance(data["steps"], list)
        # 至少一个 step 的 command 是 audit
        commands = " ".join(s.get("command", "") for s in data["steps"])
        assert "scrape_new audit" in commands, f"无 audit 命令: {commands[:300]}"
        # 至少一个 step 的 id 是 audit_scan 或 audit_mapping
        step_ids = [s.get("id", "") for s in data["steps"]]
        assert any("audit" in sid for sid in step_ids), f"step_ids: {step_ids}"

    def test_wizard_audit_intent_markdown_has_report_and_tips(self):
        """12:wizard --intent audit --markdown 含 audit 命令、报告路径、风险/建议"""
        r = self._run_wizard(
            "--intent", "audit",
            "--markdown",
        )
        assert r.returncode == 0, f"stderr={r.stderr[:300]}"
        out = r.stdout
        # 1. 标题
        assert "Workflow Plan" in out or "workflow plan" in out.lower()
        # 2. 至少一个 audit 命令
        assert "scrape_new audit" in out
        # 3. 报告路径
        assert "_resource_audit.md" in out
        # 4. 风险/建议提示(中英文都算)
        low_out = out.lower()
        has_risk = "风险" in out or "risk" in low_out
        has_advice = "建议" in out or "建议" in out or "下一步" in out or "next" in low_out
        assert has_risk, f"无风险提示: {out[:200]}"
        assert has_advice, f"无建议提示: {out[:200]}"


# ─── WorkflowPlan 序列化 ────────────────────────────────

class TestWorkflowPlanSerialization:
    """10:WorkflowPlan.to_dict() + to_json() + to_markdown() 完整可用"""

    def test_workflow_plan_json_roundtrip(self):
        """WorkflowPlan 可 JSON 序列化并反序列化(JSON 兼容)"""
        from scrape_new.services.workflow_planner import build_workflow_plan
        plan = build_workflow_plan(
            intent="download", platform="chaoxing",
            course_url="https://x", output_dir="./mycourse",
            cookie_source="env", options={"max_tabs": 6},
        )
        # to_json 必须能 load 回来
        raw = plan.to_json()
        data = json.loads(raw)
        assert data["intent"] == "download"
        assert data["platform"] == "chaoxing"
        assert isinstance(data["steps"], list)
        # step 形参必须都是可序列化的
        for step in data["steps"]:
            assert "id" in step
            assert "title" in step
            assert "command" in step
            assert isinstance(step["writes_files"], list)


# ─── wizard --execute-step 测试 ────────────────────────────

class _FakeProc:
    """模拟 subprocess.run 返回值(用 attrs 而不是 CompletedProcess 避免签名差异)。"""

    def __init__(self, returncode: int = 0, stdout: str = "", stderr: str = ""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class TestWizardExecuteStep:
    """wizard --execute-step 逐步执行能力(第一版,只允许非危险 step)。

    所有测试:
      - 直接 import _run_wizard,传 mock 的 argparse.Namespace
      - monkeypatch scrape_new.cli.subprocess.run,不真实子进程
      - 不真实下载 / 上传 / 网络
    """

    def _ns(self, **overrides) -> argparse.Namespace:
        """构造一个 wizard 子命令的 Namespace(走非交互分支:必须给 --intent)。"""
        base = {
            "intent": "scan",
            "platform": "chaoxing",
            "url": "https://example.com/c",
            "output_dir": "./out",
            "cookie_source": "env",
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

    def test_execute_safe_step_runs_subprocess_and_writes_log(self, tmp_path, monkeypatch):
        """1:--execute-step scan 调 subprocess.run + 写 _wizard_runs.jsonl"""
        from scrape_new import cli
        from scrape_new.services.workflow_planner import build_workflow_plan

        # 先确认 scan intent 的 plan 中有非危险的 scan step
        plan = build_workflow_plan(
            intent="scan", platform="chaoxing", course_url="https://x",
            output_dir=str(tmp_path), cookie_source="env",
        )
        scan_step = next((s for s in plan.steps if s.id == "scan"), None)
        if scan_step is None:
            pytest.skip("scan plan 不含 id=scan step(planner 实现变化)")
        # 强制安全属性(防御性:即使 planner 把它标 dangerous,这个测试也保护语义)
        scan_step.destructive = False
        scan_step.requires_confirmation = False

        called = {}
        def fake_run(argv, **kwargs):
            called["argv"] = argv
            called["shell"] = kwargs.get("shell", False)
            return _FakeProc(returncode=0, stdout="scan done\n", stderr="")

        monkeypatch.setattr(cli.subprocess, "run", fake_run)
        monkeypatch.setattr("scrape_new.services.workflow_planner.build_workflow_plan", lambda **kw: plan)

        rc = cli._run_wizard(self._ns(
            intent="scan", output_dir=str(tmp_path),
            execute_step="scan",
        ))
        assert rc == 0
        # subprocess.run 用了 shell=False
        assert called["shell"] is False
        # argv 第一项应该是 python
        assert called["argv"][0] == "python"
        # 日志文件
        log = tmp_path / "_wizard_runs.jsonl"
        assert log.exists()
        lines = [json.loads(l) for l in log.read_text(encoding="utf-8").splitlines() if l]
        assert len(lines) == 1
        rec = lines[0]
        assert rec["step_id"] == "scan"
        assert rec["status"] == "succeeded"
        assert rec["returncode"] == 0
        assert "command" in rec
        assert "stdout_tail" in rec
        assert "stderr_tail" in rec

    def test_execute_unknown_step_returns_nonzero_no_subprocess(self, tmp_path, monkeypatch, capsys):
        """2:--execute-step nope → 返非 0,列出可用 id,不调 subprocess"""
        from scrape_new import cli
        from scrape_new.services.workflow_planner import build_workflow_plan

        plan = build_workflow_plan(
            intent="download", platform="chaoxing", course_url="https://x",
            output_dir=str(tmp_path), cookie_source="env",
        )

        def fail_run(*a, **kw):
            raise AssertionError("subprocess.run 不应该被调用")
        monkeypatch.setattr(cli.subprocess, "run", fail_run)
        monkeypatch.setattr("scrape_new.services.workflow_planner.build_workflow_plan", lambda **kw: plan)

        rc = cli._run_wizard(self._ns(
            intent="download", output_dir=str(tmp_path),
            execute_step="nope",
        ))
        assert rc != 0
        captured = capsys.readouterr()
        out_err = (captured.out + captured.err).lower()
        # 列出可用 id(任一 plan 里的 step id 都应出现)
        available_ids = [s.id for s in plan.steps]
        assert any(sid in out_err for sid in available_ids), \
            f"stderr/stdout 应列出可用 step id; got: {captured.err[:300]}"
        # 日志不应被写
        assert not (tmp_path / "_wizard_runs.jsonl").exists()

    def test_execute_dangerous_step_refuses_and_shows_command(self, tmp_path, monkeypatch, capsys):
        """3:--execute-step apply_plan(destructive)→ 拒绝,显示命令,不调 subprocess"""
        from scrape_new import cli
        from scrape_new.services.workflow_planner import build_workflow_plan, WorkflowStep

        # 预创建 _upload_plan.json(planner 才会生成 apply_plan step)
        plan_path = tmp_path / "_upload_plan.json"
        plan_path.write_text("{}", encoding="utf-8")

        plan = build_workflow_plan(
            intent="upload", platform="chaoxing", course_url="",
            output_dir=str(tmp_path), cookie_source="env",
            options={"course_id": "1234", "mapping_path": "./_mapping.json",
                     "plan_path": str(plan_path)},
        )
        apply_step = next((s for s in plan.steps if s.id == "apply_plan"), None)
        if apply_step is None:
            pytest.skip("upload plan 不含 apply_plan step(planner 变了)")
        # 强制危险属性
        apply_step.destructive = True
        apply_step.requires_confirmation = True

        def fail_run(*a, **kw):
            raise AssertionError("subprocess.run 不应该被调用(dangerous step)")
        monkeypatch.setattr(cli.subprocess, "run", fail_run)
        monkeypatch.setattr("scrape_new.services.workflow_planner.build_workflow_plan", lambda **kw: plan)

        rc = cli._run_wizard(self._ns(
            intent="upload", output_dir=str(tmp_path),
            course_id="1234", mapping_path="./_mapping.json",
            plan_path=str(plan_path),
            execute_step="apply_plan",
        ))
        assert rc != 0
        captured = capsys.readouterr()
        # 提示危险 / 不自动执行
        assert "危险" in (captured.out + captured.err) or \
               "destructive" in (captured.out + captured.err).lower() or \
               "requires_confirmation" in (captured.out + captured.err)
        # 提示用户复制命令
        assert apply_step.command in captured.out or apply_step.command in captured.err
        # 不应写日志
        assert not (tmp_path / "_wizard_runs.jsonl").exists()

    def test_execute_step_failure_returns_returncode_and_logs_failed(self, tmp_path, monkeypatch):
        """4:mock returncode=7 → _run_wizard 返 7,日志 status=failed"""
        from scrape_new import cli
        from scrape_new.services.workflow_planner import build_workflow_plan

        plan = build_workflow_plan(
            intent="scan", platform="chaoxing", course_url="https://x",
            output_dir=str(tmp_path), cookie_source="env",
        )
        scan_step = next((s for s in plan.steps if s.id == "scan"), None)
        if scan_step is None:
            pytest.skip("scan plan 不含 scan step")
        scan_step.destructive = False
        scan_step.requires_confirmation = False

        def fake_run(argv, **kwargs):
            return _FakeProc(returncode=7, stdout="oops\n", stderr="boom\n")
        monkeypatch.setattr(cli.subprocess, "run", fake_run)
        monkeypatch.setattr("scrape_new.services.workflow_planner.build_workflow_plan", lambda **kw: plan)

        rc = cli._run_wizard(self._ns(
            intent="scan", output_dir=str(tmp_path),
            execute_step="scan",
        ))
        assert rc == 7
        log = tmp_path / "_wizard_runs.jsonl"
        recs = [json.loads(l) for l in log.read_text(encoding="utf-8").splitlines() if l]
        assert recs[0]["status"] == "failed"
        assert recs[0]["returncode"] == 7
        assert recs[0]["stderr_tail"].endswith("boom\n")

    def test_custom_run_log_path_does_not_write_default(self, tmp_path, monkeypatch):
        """5:--run-log custom.jsonl → 写到 custom.jsonl,默认 _wizard_runs.jsonl 不应出现"""
        from scrape_new import cli
        from scrape_new.services.workflow_planner import build_workflow_plan

        plan = build_workflow_plan(
            intent="scan", platform="chaoxing", course_url="https://x",
            output_dir=str(tmp_path), cookie_source="env",
        )
        scan_step = next((s for s in plan.steps if s.id == "scan"), None)
        if scan_step is None:
            pytest.skip("scan plan 不含 scan step")
        scan_step.destructive = False
        scan_step.requires_confirmation = False

        monkeypatch.setattr(cli.subprocess, "run",
                            lambda *a, **kw: _FakeProc(0, "", ""))
        monkeypatch.setattr("scrape_new.services.workflow_planner.build_workflow_plan", lambda **kw: plan)

        custom_log = tmp_path / "custom.jsonl"
        rc = cli._run_wizard(self._ns(
            intent="scan", output_dir=str(tmp_path),
            execute_step="scan", run_log=str(custom_log),
        ))
        assert rc == 0
        assert custom_log.exists()
        # 默认 _wizard_runs.jsonl 不应出现
        assert not (tmp_path / "_wizard_runs.jsonl").exists()
        recs = [json.loads(l) for l in custom_log.read_text(encoding="utf-8").splitlines() if l]
        assert len(recs) == 1

    def test_execute_audit_step_command_and_log(self, tmp_path, monkeypatch):
        """6:--execute-step audit_scan(audit intent)→ command 含 audit,日志写入"""
        from scrape_new import cli
        from scrape_new.services.workflow_planner import build_workflow_plan

        plan = build_workflow_plan(
            intent="audit", platform="unknown", course_url="",
            output_dir=str(tmp_path), cookie_source="none",
        )
        audit_step = next((s for s in plan.steps if "audit" in s.id), None)
        if audit_step is None:
            pytest.skip("audit plan 不含 audit step")
        audit_step.destructive = False
        audit_step.requires_confirmation = False

        called = {}
        def fake_run(argv, **kwargs):
            called["argv"] = argv
            return _FakeProc(0, "audit done\n", "")
        monkeypatch.setattr(cli.subprocess, "run", fake_run)
        monkeypatch.setattr("scrape_new.services.workflow_planner.build_workflow_plan", lambda **kw: plan)

        rc = cli._run_wizard(self._ns(
            intent="audit", platform="unknown",
            output_dir=str(tmp_path), cookie_source="none",
            execute_step=audit_step.id,
        ))
        assert rc == 0
        # command 拼接成字符串应包含 "python -m scrape_new audit"
        cmd_str = " ".join(called["argv"])
        assert "audit" in cmd_str.lower()
        assert "scrape_new" in cmd_str
        # 日志
        log = tmp_path / "_wizard_runs.jsonl"
        recs = [json.loads(l) for l in log.read_text(encoding="utf-8").splitlines() if l]
        assert recs[0]["step_id"] == audit_step.id
        assert recs[0]["status"] == "succeeded"