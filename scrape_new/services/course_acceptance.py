"""
课程本地验收总报告 — 把一个 output_dir 下的所有产物汇总成
"这门课能不能进入下一步"的最终判定。

读取(都有则读,无则容错):
  - _chapter_tree.json
  - _resource_naming_manifest.json
  - _resource_audit.json
  - _retry_downloads.json
  - _mapping.json
  - _upload_plan.json

输出:
  - _course_acceptance.json
  - _course_acceptance.md

状态机:
  INCOMPLETE  缺核心产物 (chapter_tree / manifest)
  BLOCKED     high risk 或 大面积失败, 不应继续 upload
  REVIEW      medium risk / low_confidence / ppt-only, 需人工确认
  READY       全绿, 可进入下一步 (build_mapping 或 upload --plan-only)

设计原则:
  - 纯函数 + 纯本地 IO, 不访问任何网络
  - 文件缺失 / JSON 解析失败 都不崩, 转成 risk / missing_inputs
  - to_dict / to_json / to_markdown 全部 dataclass-friendly, GUI 可直接消费
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field, asdict
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable


# ─── 状态枚举(字符串常量, 不用 Enum 以便直接序列化) ──────

ACCEPT_READY = "READY"
ACCEPT_REVIEW = "REVIEW"
ACCEPT_BLOCKED = "BLOCKED"
ACCEPT_INCOMPLETE = "INCOMPLETE"
ALL_STATUSES = (ACCEPT_READY, ACCEPT_REVIEW, ACCEPT_BLOCKED, ACCEPT_INCOMPLETE)


# ─── 风险严重程度(避免和 resource_audit 混淆, 命名空间独立) ──────

RISK_HIGH = "high"
RISK_MEDIUM = "medium"
RISK_LOW = "low"
RISK_INFO = "info"


# ─── 风险 code 集合(accept 关心哪些) ──────

# 一旦命中任意一个 → BLOCKED
BLOCKING_AUDIT_ISSUES = frozenset({
    "missing_local_file",
    "non_video_in_video_slot",
    "duplicate_file_use",
    "duplicate_saved_name",
    "duplicate_objectid",
    "count_mismatch",  # 严重程度:出现在 _resource_audit.json 时认为 high
})

# 一旦命中任意一个 → REVIEW(非 BLOCKED)
REVIEW_AUDIT_ISSUES = frozenset({
    "low_confidence_role",
    "ppt_only_lesson_informational",
    "empty_lesson",
    "empty_chapter",
    "attachment_as_video",
    "role_conflict",
    "scan_incomplete",
    "possible_missing_resource",
    "invalid_json",
})


# ─── 风险条目 ──────────────────────────────────────────────

@dataclass
class AcceptanceRisk:
    """单条风险。"""
    level: str          # high / medium / low / info
    code: str           # 短代码, 如 "missing_local_file"
    message: str        # 人类可读描述
    source: str = ""    # 来自哪个文件 / 阶段 (audit / manifest / retry / ...)
    lesson_id: str = ""
    suggestion: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# ─── 报告主体 ──────────────────────────────────────────────

@dataclass
class CourseAcceptanceReport:
    output_dir: str
    generated_at: str
    status: str = ACCEPT_INCOMPLETE
    summary: dict[str, int] = field(default_factory=dict)
    risks: list[AcceptanceRisk] = field(default_factory=list)
    missing_inputs: list[str] = field(default_factory=list)
    recommendations: list[str] = field(default_factory=list)
    next_commands: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "output_dir": self.output_dir,
            "generated_at": self.generated_at,
            "status": self.status,
            "summary": dict(self.summary),
            "risks": [r.to_dict() for r in self.risks],
            "missing_inputs": list(self.missing_inputs),
            "recommendations": list(self.recommendations),
            "next_commands": list(self.next_commands),
            "notes": list(self.notes),
        }

    def to_json(self, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=indent)


# ─── 输入读取 ──────────────────────────────────────────────

# (文件 key, 主文件名, 是否必需)
INPUT_FILES = (
    ("chapter_tree", "_chapter_tree.json", False),
    ("manifest", "_resource_naming_manifest.json", False),
    ("audit", "_resource_audit.json", False),
    ("retry", "_retry_downloads.json", False),
    ("mapping", "_mapping.json", False),
    ("upload_plan", "_upload_plan.json", False),
)


def load_course_acceptance_inputs(output_dir: Path) -> dict[str, dict | None]:
    """读取一个 output_dir 下所有可识别的产物 JSON。

    Returns:
        dict[key, data_or_None] — key 见 INPUT_FILES 第一列。
        文件不存在 → None。
        JSON 解析失败 → 仍然返回 None(由 build_report 阶段记录为 risk)。

    不抛异常。
    """
    out: dict[str, dict | None] = {}
    for key, fname, _required in INPUT_FILES:
        p = Path(output_dir) / fname
        if not p.exists():
            out[key] = None
            continue
        try:
            out[key] = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError, UnicodeDecodeError):
            out[key] = None
    return out


# ─── 统计 helper ──────────────────────────────────────────

def _summarize_manifest(manifest: dict | None) -> dict[str, int]:
    """从 manifest records 里提取 summary。

    manifest 格式: {records: [{ch_num, ls_num, type, status, ...}, ...]}
    """
    if not manifest:
        return {
            "resources_count": 0,
            "video_count": 0,
            "document_count": 0,
            "failed_count": 0,
            "suspicious_count": 0,
        }
    records = manifest.get("records") or []
    video_exts = {"mp4", "flv", "m3u8", "avi", "mkv", "mov"}
    doc_exts = {"ppt", "pptx", "pdf", "doc", "docx", "xls", "xlsx"}
    resources = 0
    video = 0
    doc = 0
    failed = 0
    suspicious = 0
    for r in records:
        if not isinstance(r, dict):
            continue
        resources += 1
        status = (r.get("status") or "").lower()
        if status == "failed":
            failed += 1
        elif status == "suspicious":
            suspicious += 1
        # type 形如 ".mp4" 或 ".pptx" — 去前导点
        t = (r.get("type") or "").lower().lstrip(".")
        if t in video_exts:
            video += 1
        elif t in doc_exts:
            doc += 1
    return {
        "resources_count": resources,
        "video_count": video,
        "document_count": doc,
        "failed_count": failed,
        "suspicious_count": suspicious,
    }


def _summarize_audit(audit: dict | None) -> dict[str, int]:
    """从 _resource_audit.json 里提取 high/medium/low 计数。"""
    if not audit:
        return {"audit_high_count": 0, "audit_medium_count": 0, "audit_low_count": 0}
    high = medium = low = 0
    for ls in audit.get("lessons") or []:
        if not isinstance(ls, dict):
            continue
        risk = ls.get("risk_level") or "ok"
        if risk == "high":
            high += 1
        elif risk == "medium":
            medium += 1
        elif risk == "low":
            low += 1
    return {
        "audit_high_count": high,
        "audit_medium_count": medium,
        "audit_low_count": low,
    }


def _summarize_chapter_tree(tree: dict | None) -> dict[str, int]:
    if not tree:
        return {"chapters_count": 0, "lessons_count": 0}
    chapters = tree.get("chapters") or []
    lessons = sum(len(c.get("lessons") or []) for c in chapters if isinstance(c, dict))
    return {"chapters_count": len(chapters), "lessons_count": lessons}


def _retry_count(retry: dict | None) -> int:
    if not retry:
        return 0
    # 多种格式都容错
    if isinstance(retry, list):
        return len(retry)
    if isinstance(retry, dict):
        # 常见格式: {"items": [...]}, {"records": [...]} 或 {"count": N}
        for k in ("items", "records", "retry_downloads", "pending_actions"):
            v = retry.get(k)
            if isinstance(v, list):
                return len(v)
        # 兜底:数所有 list 字段的最大值
        best = 0
        for v in retry.values():
            if isinstance(v, list):
                best = max(best, len(v))
        return best
    return 0


# ─── 风险收集 ──────────────────────────────────────────────

def _collect_audit_risks(audit: dict | None, out: list[AcceptanceRisk]) -> None:
    """把 _resource_audit.json 的 lesson issues 转成 AcceptanceRisk。"""
    if not audit:
        return
    for ls in audit.get("lessons") or []:
        if not isinstance(ls, dict):
            continue
        lesson_id = ls.get("lesson_id") or ""
        lesson_title = ls.get("lesson_title") or ""
        risk_level = ls.get("risk_level") or "ok"
        for issue in ls.get("issues") or []:
            if not isinstance(issue, str):
                continue
            level = _map_issue_to_level(issue, risk_level)
            out.append(AcceptanceRisk(
                level=level,
                code=issue,
                message=f"ch{ls.get('ch_num', '?')}.{lesson_id} {lesson_title} 含 {issue}",
                source="resource_audit",
                lesson_id=lesson_id,
                suggestion=_suggestion_for_issue(issue),
            ))


def _map_issue_to_level(issue: str, lesson_risk: str) -> str:
    """把 audit issue 映射到 accept 的 4 级严重度。"""
    if issue in BLOCKING_AUDIT_ISSUES:
        # count_mismatch 看 lesson_risk: medium 也只算 medium (不直接 BLOCK)
        # 但 missing_local_file / non_video_in_video_slot / duplicate_* 必 high
        if issue == "count_mismatch":
            return RISK_MEDIUM if lesson_risk in ("medium", "low") else RISK_HIGH
        return RISK_HIGH
    if issue in REVIEW_AUDIT_ISSUES:
        # empty_lesson / empty_chapter 也看 lesson_risk
        if issue in ("empty_lesson", "empty_chapter"):
            return RISK_LOW
        return RISK_MEDIUM
    return RISK_LOW


def _suggestion_for_issue(issue: str) -> str:
    """针对每类 issue 给建议。"""
    return {
        "missing_local_file": "重新下载该文件 / 检查文件名",
        "non_video_in_video_slot": "把非视频文件移到 attachments 字段",
        "duplicate_file_use": "检查 mapping 是否重复挂载同一文件",
        "duplicate_saved_name": "检查 manifest / scan 是否产出重复 saved_name",
        "duplicate_objectid": "检查 scan 是否把同一资源挂到多节",
        "count_mismatch": "重扫该节 / 增大 --max-tabs",
        "low_confidence_role": "人工确认资源类型,必要时改名",
        "ppt_only_lesson_informational": "可保留;确认后台确实只需要 PPT",
        "empty_lesson": "重扫该节或确认后台真没资源",
        "empty_chapter": "确认整章真没资源或限流中断",
        "attachment_as_video": "把 .mp4 从 attachments 改到 video 字段",
        "role_conflict": "扩展名与标题暗示不同类型,人工确认",
        "scan_incomplete": "增大 --max-tabs 重跑 scan-only",
        "possible_missing_resource": "重扫该节 / 增大 --max-tabs",
    }.get(issue, "人工 review 该 issue")


def _collect_manifest_risks(manifest: dict | None, out: list[AcceptanceRisk]) -> None:
    """manifest 里 failed / suspicious 数量过多 → risk。"""
    if not manifest:
        return
    records = manifest.get("records") or []
    failed = sum(1 for r in records if (r.get("status") or "").lower() == "failed")
    suspicious = sum(1 for r in records if (r.get("status") or "").lower() == "suspicious")
    if failed > 0:
        out.append(AcceptanceRisk(
            level=RISK_HIGH,
            code="manifest_failed_resources",
            message=f"manifest 含 {failed} 条 status=failed 资源",
            source="manifest",
            suggestion="先跑 --retry-downloads,失败重下",
        ))
    if suspicious >= 3:
        out.append(AcceptanceRisk(
            level=RISK_MEDIUM,
            code="manifest_suspicious_resources",
            message=f"manifest 含 {suspicious} 条 status=suspicious 资源(可疑但不是失败)",
            source="manifest",
            suggestion="人工 review 大小异常的 suspicious 资源",
        ))
    elif suspicious > 0:
        out.append(AcceptanceRisk(
            level=RISK_LOW,
            code="manifest_suspicious_resources",
            message=f"manifest 含 {suspicious} 条 status=suspicious 资源",
            source="manifest",
            suggestion="人工 review suspicious 资源",
        ))


def _collect_retry_risks(retry: dict | None, out: list[AcceptanceRisk]) -> None:
    if not retry:
        return
    n = _retry_count(retry)
    if n >= 5:
        out.append(AcceptanceRisk(
            level=RISK_HIGH,
            code="retry_list_too_large",
            message=f"_retry_downloads.json 含 {n} 条重试任务",
            source="retry",
            suggestion="先跑 --retry-downloads 处理失败资源,再继续",
        ))
    elif n > 0:
        out.append(AcceptanceRisk(
            level=RISK_MEDIUM,
            code="retry_list_present",
            message=f"_retry_downloads.json 含 {n} 条重试任务",
            source="retry",
            suggestion="建议先跑 --retry-downloads",
        ))


def _collect_invalid_json_risks(
    output_dir: Path, inputs: dict[str, dict | None], out: list[AcceptanceRisk]
) -> None:
    """文件存在但解析失败 → invalid_json risk。"""
    for key, fname, _ in INPUT_FILES:
        p = Path(output_dir) / fname
        if not p.exists():
            continue
        if inputs.get(key) is None:
            # 文件存在但我们读到 None(load 阶段已捕获),说明解析失败
            out.append(AcceptanceRisk(
                level=RISK_MEDIUM,
                code="invalid_json",
                message=f"{fname} 存在但 JSON 解析失败",
                source=key,
                suggestion=f"检查 {fname} 是否为合法 JSON",
            ))


# ─── 状态判定 ──────────────────────────────────────────────

def _decide_status(report: CourseAcceptanceReport, summary: dict[str, int],
                   missing_required: list[str]) -> str:
    # 缺核心 → INCOMPLETE
    if missing_required:
        return ACCEPT_INCOMPLETE
    # high risk → BLOCKED
    high_risks = [r for r in report.risks if r.level == RISK_HIGH]
    if high_risks:
        return ACCEPT_BLOCKED
    # 大面积失败 → BLOCKED
    if summary.get("failed_count", 0) >= 3:
        return ACCEPT_BLOCKED
    # medium / low risk 但没 high → REVIEW
    if any(r.level in (RISK_MEDIUM, RISK_LOW) for r in report.risks):
        return ACCEPT_REVIEW
    return ACCEPT_READY


# ─── 报告构建 ──────────────────────────────────────────────

def build_course_acceptance_report(
    output_dir: Path | str,
    *,
    inputs: dict[str, dict | None] | None = None,
) -> CourseAcceptanceReport:
    """构造 CourseAcceptanceReport。

    Args:
        output_dir: 课程输出目录(可以不存在或为空)。
        inputs: 可选 — 预加载的 inputs(测试用)。None → 现场读。

    设计: 即使 output_dir 不存在或为空, 也返回 INCOMPLETE 报告(不抛)。
    """
    output_dir = Path(output_dir)
    report = CourseAcceptanceReport(
        output_dir=str(output_dir),
        generated_at=datetime.now().isoformat(timespec="seconds"),
    )

    # output_dir 不存在 → 整张报告就是 INCOMPLETE
    if not output_dir.exists():
        report.missing_inputs.append("(output_dir 不存在)")
        report.summary = {
            "chapters_count": 0, "lessons_count": 0,
            "resources_count": 0, "video_count": 0, "document_count": 0,
            "failed_count": 0, "suspicious_count": 0, "retry_count": 0,
            "audit_high_count": 0, "audit_medium_count": 0, "audit_low_count": 0,
            "mapping_present": 0, "upload_plan_present": 0,
        }
        report.recommendations.append("缺少关键产物:output_dir 不存在")
        report.next_commands.append(f"mkdir -p {output_dir}")
        return report

    if inputs is None:
        inputs = load_course_acceptance_inputs(output_dir)

    # 1. missing_inputs(必需文件缺失)
    required = ("chapter_tree", "manifest")  # 这俩缺一不可
    for key in required:
        if inputs.get(key) is None:
            report.missing_inputs.append(_key_to_filename(key))
    # 可选文件缺失也记一下(用于提示用户)
    for key, fname, _ in INPUT_FILES:
        if key in required:
            continue
        p = output_dir / fname
        if not p.exists():
            # 不加进 missing_inputs(会污染状态判定),只加 notes
            report.notes.append(f"未提供可选产物:{fname}")

    # 2. 收集风险
    _collect_audit_risks(inputs.get("audit"), report.risks)
    _collect_manifest_risks(inputs.get("manifest"), report.risks)
    _collect_retry_risks(inputs.get("retry"), report.risks)
    _collect_invalid_json_risks(output_dir, inputs, report.risks)

    # 3. summary
    summary = {}
    summary.update(_summarize_chapter_tree(inputs.get("chapter_tree")))
    summary.update(_summarize_manifest(inputs.get("manifest")))
    summary.update(_summarize_audit(inputs.get("audit")))
    summary["retry_count"] = _retry_count(inputs.get("retry"))
    summary["mapping_present"] = 1 if inputs.get("mapping") else 0
    summary["upload_plan_present"] = 1 if inputs.get("upload_plan") else 0
    report.summary = summary

    # 4. 状态判定
    report.status = _decide_status(report, summary, report.missing_inputs)

    # 5. recommendations + next_commands
    _build_recommendations(report, inputs)

    return report


def _key_to_filename(key: str) -> str:
    for k, fname, _ in INPUT_FILES:
        if k == key:
            return fname
    return key


def _build_recommendations(
    report: CourseAcceptanceReport,
    inputs: dict[str, dict | None],
) -> None:
    status = report.status
    summary = report.summary
    out_dir = report.output_dir

    if status == ACCEPT_INCOMPLETE:
        # 缺核心
        for fname in report.missing_inputs:
            report.recommendations.append(f"缺少关键产物:{fname}")
        # 给出建议的下一步
        report.next_commands.append(
            f"python -m scrape_new platform chaoxing \"<URL>\" {out_dir} --scan-only"
        )
        report.next_commands.append(
            f"python -m scrape_new platform chaoxing \"<URL>\" {out_dir}"
        )
        return

    if status == ACCEPT_BLOCKED:
        report.recommendations.append("不建议上传,先修复 high risk")
        high_codes = sorted({r.code for r in report.risks if r.level == RISK_HIGH})
        for code in high_codes:
            report.recommendations.append(
                f"建议先修复 high risk:{code}(见 suggestions)"
            )
        # next: audit + retry
        report.next_commands.append(
            f"python -m scrape_new audit "
            f"--chapter-tree {out_dir}/_chapter_tree.json "
            f"--manifest {out_dir}/_resource_naming_manifest.json "
            f"--output-dir {out_dir}"
        )
        if summary.get("failed_count", 0) > 0 or summary.get("retry_count", 0) > 0:
            report.next_commands.append(
                f"python -m scrape_new platform chaoxing \"<URL>\" {out_dir} --retry-downloads"
            )
        return

    if status == ACCEPT_REVIEW:
        report.recommendations.append("有 medium/low risk,建议人工确认后再上传")
        med_codes = sorted({r.code for r in report.risks if r.level == RISK_MEDIUM})
        if med_codes:
            report.recommendations.append(
                f"建议先 review medium issue:{', '.join(med_codes[:5])}"
            )
        # next: audit + 视情况 build-mapping / upload
        report.next_commands.append(
            f"cat {out_dir}/_resource_audit.md  # 看 medium risk 明细"
        )
        if not inputs.get("mapping"):
            report.next_commands.append(
                f"python -m scrape_new upload build-mapping "
                f"--videos {out_dir}/视频 --doc <outline> "
                f"--course-id <id> --output {out_dir}/_mapping.json"
            )
        if inputs.get("mapping"):
            report.next_commands.append(
                f"python -m scrape_new upload upload "
                f"--mapping {out_dir}/_mapping.json "
                f"--cookies-file <cookies.txt> --plan-only"
            )
        return

    # READY
    report.recommendations.append("可以进入下一步")
    if inputs.get("mapping"):
        report.recommendations.append(
            "已有 _mapping.json,可以 upload --plan-only"
        )
        report.next_commands.append(
            f"python -m scrape_new upload upload "
            f"--mapping {out_dir}/_mapping.json "
            f"--cookies-file <cookies.txt> --plan-only"
        )
    else:
        report.recommendations.append("还没有 _mapping.json,下一步是 build-mapping")
        report.next_commands.append(
            f"python -m scrape_new upload build-mapping "
            f"--videos {out_dir}/视频 --doc <outline> "
            f"--course-id <id> --output {out_dir}/_mapping.json"
        )


# ─── 报告写入 ──────────────────────────────────────────────

def write_course_acceptance_reports(
    report: CourseAcceptanceReport,
    output_dir: Path | str,
) -> dict[str, Path]:
    """写 _course_acceptance.json + _course_acceptance.md。"""
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    json_path = output_dir / "_course_acceptance.json"
    json_path.write_text(report.to_json(indent=2), encoding="utf-8")

    md_path = output_dir / "_course_acceptance.md"
    md_path.write_text(_render_md(report), encoding="utf-8")

    return {"json": json_path, "markdown": md_path}


def _render_md(report: CourseAcceptanceReport) -> str:
    """人类可读 Markdown。"""
    lines: list[str] = []
    lines.append("# Course Acceptance Report")
    lines.append("")
    lines.append(f"- 状态:**{report.status}**")
    lines.append(f"- 输出目录:`{report.output_dir}`")
    lines.append(f"- 生成时间:{report.generated_at}")
    lines.append("")

    # 简介一句话
    blurb = {
        ACCEPT_READY: "✅ **可以进入下一步**。",
        ACCEPT_REVIEW: "⚠️ **建议人工确认**后进入下一步。",
        ACCEPT_BLOCKED: "❌ **不建议上传**,先修复 high risk。",
        ACCEPT_INCOMPLETE: "🚧 **缺少关键产物**,无法判定。",
    }
    lines.append(blurb.get(report.status, ""))
    lines.append("")

    # 总览
    lines.append("## 总览")
    lines.append("")
    lines.append("| 指标 | 值 |")
    lines.append("|---|---|")
    for k, v in report.summary.items():
        lines.append(f"| {k} | {v} |")
    lines.append("")

    # 风险
    lines.append("## 风险")
    lines.append("")
    if not report.risks:
        lines.append("无。")
    else:
        for level in (RISK_HIGH, RISK_MEDIUM, RISK_LOW, RISK_INFO):
            group = [r for r in report.risks if r.level == level]
            if not group:
                continue
            lines.append(f"### {level.upper()} ({len(group)})")
            lines.append("")
            for r in group[:30]:
                where = f" `{r.lesson_id}`" if r.lesson_id else ""
                lines.append(f"- **{r.code}**{where} — {r.message}")
                if r.suggestion:
                    lines.append(f"  - 建议:{r.suggestion}")
            if len(group) > 30:
                lines.append(f"- …(还有 {len(group) - 30} 条,见 JSON)")
            lines.append("")
    # 风险总览放在最末,提醒"风险高 = 不建议上传"
    if any(r.level == RISK_HIGH for r in report.risks):
        lines.append("> ⚠️ 检测到 high risk,**不建议上传**。")
        lines.append("")

    # 建议
    lines.append("## 建议")
    lines.append("")
    if not report.recommendations:
        lines.append("无。")
    else:
        for r in report.recommendations:
            lines.append(f"- {r}")
    lines.append("")

    # 下一步命令
    lines.append("## 下一步命令")
    lines.append("")
    if not report.next_commands:
        lines.append("无。")
    else:
        for cmd in report.next_commands:
            lines.append(f"```bash\n{cmd}\n```")
    lines.append("")

    # 缺失输入
    lines.append("## 缺失输入")
    lines.append("")
    if not report.missing_inputs:
        lines.append("无。所有核心产物都在。")
    else:
        for m in report.missing_inputs:
            lines.append(f"- **{m}**")
        lines.append("")
        lines.append("> 缺少关键产物:本报告只能给最低置信度建议。")
    lines.append("")

    # notes(可选产物缺失)
    if report.notes:
        lines.append("## 备注")
        lines.append("")
        for n in report.notes:
            lines.append(f"- {n}")
        lines.append("")

    return "\n".join(lines)