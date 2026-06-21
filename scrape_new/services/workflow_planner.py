"""
WorkflowPlanner — 把 scrape_new 的"工具"形态升级成"工作流"形态。

设计目标:
  - 纯函数:不依赖 input()、不读 stdin、跟 GUI 兼容
  - 可 JSON 序列化:dataclass + dict,GUI 能直接渲染
  - intent-based:用户说"下载课程" / "扫描" / "建 mapping" / "上传" 等高层意图,
    planner 内部组装成具体 steps(含 CLI 命令、文件、风险、确认)
  - 默认 plan-first:任何上传 intent 默认先 plan-only,鼓励人工 review
  - 危险操作显式标记 destructive + requires_confirmation

跟未来 GUI 的对接:
  - GUI 调用 build_workflow_plan(intent, **kwargs) → 拿 WorkflowPlan
  - 每 step 是一个按钮(可点击 / 不可点击由 destructive + requires_cookie 决定)
  - expected_outputs 是 GUI 表格的"完成后能看到什么"
  - next_suggestions 是 GUI 右侧的"下一步"列表
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field, asdict
from enum import Enum
from pathlib import Path
from typing import Any, Optional


class Intent(str, Enum):
    """高层用户意图。"""
    DOWNLOAD = "download"          # 下载课程(默认包含 scan + download)
    SCAN_ONLY = "scan"             # 只扫描,不下文件
    BUILD_MAPPING = "build_mapping"  # 从 outline + 视频文件夹 → mapping
    UPLOAD = "upload"              # 上传到老师后台
    RETRY_FAILED = "retry"         # 重试上次失败资源
    MODIFY = "modify"              # 局部修改(只动某一节/某个资源)
    AUDIT = "audit"                # 资源智能审计(不下载不上传,只生成报告)
    ACCEPT = "accept"              # 课程本地验收总报告(汇总 audit/manifest/mapping 等)
    UNKNOWN = "unknown"


class Platform(str, Enum):
    CHAOXING = "chaoxing"
    XUETANGX = "xuetangx"
    ZHUIHUISHU = "zhihuishu"
    ICOURSE163 = "icourse163"
    UNKNOWN = "unknown"


class CookieSource(str, Enum):
    CURL = "curl"                  # 用户粘 curl
    STRING = "string"              # 直接 cookie 字符串
    FILE = "file"                  # cookies.txt 文件
    ENV = "env"                    # XTBZ_COOKIE 环境变量
    NONE = "none"                  # 无 cookie(只 scan-only / 离线任务)


class RiskLevel(str, Enum):
    SAFE = "safe"                  # 纯只读
    LOW = "low"                    # 写本地文件
    MEDIUM = "medium"              # 写 teacher 后台(create chapter / section / leaf)
    HIGH = "high"                  # delete / reset / 批量覆盖


class StepStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    SKIPPED = "skipped"


@dataclass
class WorkflowStep:
    """一个具体步骤(GUI 渲染成"按钮"或"卡片")"""
    id: str                                    # 唯一 id, "scan", "download", "apply_plan"...
    title: str                                  # 人类可读标题
    command: str                                # 完整 CLI 命令
    writes_files: list[str] = field(default_factory=list)
    network_required: bool = False
    requires_cookie: bool = False
    destructive: bool = False
    requires_confirmation: bool = False         # 危险操作 — 跑前必须二次确认
    notes: str = ""                             # 给用户的提示(如"会清空课程")

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class WorkflowPlan:
    """一个完整工作流(GUI 渲染成"卡片列表")"""
    intent: str                                  # Intent value
    platform: str                                # Platform value
    course_url: str = ""
    output_dir: str = ""
    cookie_source: str = CookieSource.NONE.value
    steps: list[WorkflowStep] = field(default_factory=list)
    risk_level: str = RiskLevel.SAFE.value
    required_confirmations: list[str] = field(default_factory=list)  # 步骤 id 列表
    expected_outputs: list[str] = field(default_factory=list)       # 文件路径(相对 output_dir)
    next_suggestions: list[str] = field(default_factory=list)        # 推荐下一步

    def to_dict(self) -> dict[str, Any]:
        return {
            "intent": self.intent,
            "platform": self.platform,
            "course_url": self.course_url,
            "output_dir": self.output_dir,
            "cookie_source": self.cookie_source,
            "steps": [s.to_dict() for s in self.steps],
            "risk_level": self.risk_level,
            "required_confirmations": self.required_confirmations,
            "expected_outputs": self.expected_outputs,
            "next_suggestions": self.next_suggestions,
        }

    def to_json(self, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=indent)

    def to_markdown(self) -> str:
        """人类可读的 Markdown 报告(给 terminal / GUI 展示)"""
        lines: list[str] = []
        lines.append(f"# Workflow Plan — {self.intent}")
        lines.append("")
        lines.append(f"- 平台: {self.platform}")
        if self.course_url:
            lines.append(f"- URL: `{self.course_url}`")
        if self.output_dir:
            lines.append(f"- 输出: `{self.output_dir}`")
        lines.append(f"- Cookie 来源: {self.cookie_source}")
        lines.append(f"- 风险等级: **{self.risk_level}**")
        lines.append("")
        if self.required_confirmations:
            lines.append("## 需要二次确认")
            for sid in self.required_confirmations:
                lines.append(f"- `{sid}`")
            lines.append("")
        lines.append(f"## 步骤({len(self.steps)})")
        for i, s in enumerate(self.steps, 1):
            badge = "⚠️" if s.destructive else "▶"
            confirm = " **(需确认)**" if s.requires_confirmation else ""
            lines.append(f"### {i}. {badge} {s.title}{confirm}")
            lines.append(f"`{s.command}`")
            meta = []
            if s.network_required:
                meta.append("网络")
            if s.requires_cookie:
                meta.append("需 cookie")
            if s.destructive:
                meta.append("⚠️ 破坏性")
            if s.writes_files:
                meta.append("写: " + ", ".join(s.writes_files))
            if meta:
                lines.append("- " + " | ".join(meta))
            if s.notes:
                lines.append(f"- 注: {s.notes}")
            lines.append("")
        if self.expected_outputs:
            lines.append("## 预期产出")
            for o in self.expected_outputs:
                lines.append(f"- `{o}`")
            lines.append("")
        if self.next_suggestions:
            lines.append("## 下一步建议")
            for n in self.next_suggestions:
                lines.append(f"- {n}")
            lines.append("")
        return "\n".join(lines)


# ─── 常量:intent → 步骤模板 ─────────────────────────────────────

_OUTPUT_DEFAULT = "./output"


def _cmd_scan(platform: str, url: str, output_dir: str, max_tabs: int = 4) -> str:
    """构造 scan-only 命令(无 cookie,只读 GET)"""
    return (
        f"python -m scrape_new.workflows.{platform} "
        f'"{url}" "{output_dir}" --scan-only --max-tabs {max_tabs}'
    )


def _cmd_download(platform: str, url: str, output_dir: str, max_tabs: int = 4) -> str:
    return (
        f"python -m scrape_new.workflows.{platform} "
        f'"{url}" "{output_dir}" --max-tabs {max_tabs}'
    )


def _cmd_scan_with_cookie(platform: str, url: str, output_dir: str, cookie_arg: str, max_tabs: int = 4) -> str:
    """有 cookie 的 scan(可拿更全的章节树 — 因为有 csrftoken 走真后端)"""
    return (
        f"python -m scrape_new.workflows.{platform} "
        f'"{url}" "{output_dir}" --scan-only --max-tabs {max_tabs} '
        f"{cookie_arg}"
    )


def _cmd_build_mapping(videos_dir: str, outline_path: str, course_id: str,
                       out_path: str = "", include_empty: bool = False) -> str:
    out_flag = f'--out "{out_path}"' if out_path else ""
    inc_flag = " --include-empty-lessons" if include_empty else ""
    return (
        f'python -m scrape_new.upload build-mapping '
        f'--videos "{videos_dir}" --doc "{outline_path}" '
        f'--course-id {course_id} {out_flag}{inc_flag}'
    )


def _cmd_upload_plan_only(mapping_path: str, course_id: str, cookie_arg: str,
                          only_lessons: Optional[str] = None,
                          only_resources: Optional[str] = None) -> str:
    only_l = f" --only-lessons {only_lessons}" if only_lessons else ""
    only_r = f" --only-resources {only_lessons or only_resources}" if (only_lessons or only_resources) else ""
    only_part = only_l or only_r or ""
    return (
        f"python -m scrape_new.upload upload "
        f'--mapping "{mapping_path}" --course-id {course_id} '
        f"--plan-only {cookie_arg}{only_part}"
    )


def _cmd_upload_apply_plan(mapping_path: str, course_id: str, plan_path: str,
                           cookie_arg: str, reset_confirm: Optional[str] = None) -> str:
    reset = f" --reset-confirm {reset_confirm}" if reset_confirm else ""
    return (
        f"python -m scrape_new.upload upload "
        f'--mapping "{mapping_path}" --course-id {course_id} '
        f'--apply-plan "{plan_path}" {cookie_arg}{reset}'
    )


def _cmd_retry_downloads(platform: str, url: str, output_dir: str, retry_list: str, cookie_arg: str) -> str:
    return (
        f"python -m scrape_new.workflows.{platform} "
        f'"{url}" "{output_dir}" --retry-downloads "{retry_list}" {cookie_arg}'
    )


def _cookie_arg(cookie_source: str) -> str:
    """把 CookieSource 转成 CLI 旗标片段。None 表示无 cookie。"""
    if cookie_source == CookieSource.ENV.value:
        return ""  # 默认从 XTBZ_COOKIE 环境变量读
    if cookie_source == CookieSource.FILE.value:
        return "--cookies cookies.txt"
    if cookie_source == CookieSource.STRING.value:
        return "--cookies-string '<paste here>'"
    return ""  # CURL / NONE:用户粘 curl 整串 / 离线


# ─── 核心:build_workflow_plan ──────────────────────────────────

def build_workflow_plan(
    intent: str,
    platform: str = Platform.UNKNOWN.value,
    course_url: str = "",
    output_dir: str = _OUTPUT_DEFAULT,
    cookie_source: str = CookieSource.NONE.value,
    options: Optional[dict[str, Any]] = None,
) -> WorkflowPlan:
    """根据 intent 构造完整 WorkflowPlan(纯函数,GUI 友好)。

    Args:
        intent: Intent value(download / scan / build_mapping / upload / retry / modify / unknown)
        platform: Platform value
        course_url: 课程 URL
        output_dir: 输出目录
        cookie_source: CookieSource value
        options: 额外选项 dict,支持:
            - course_id: str(上传时必填)
            - mapping_path: str(上传/mapping 时)
            - outline_path: str(build_mapping 时)
            - videos_dir: str(build_mapping 时)
            - plan_path: str(apply-plan 时)
            - retry_list: str(--retry-downloads 时)
            - only_lessons: str(局部上传)
            - only_resources: str(局部上传)
            - reset_confirm: str(显式清空)

    Returns:
        WorkflowPlan(JSON 可序列化)

    设计原则:
      - 默认 plan-first:上传时第一个 step 是 plan-only,不是 --yes
      - 危险操作(destructive/requires_confirmation)显式标记
      - 每个 plan 都有 next_suggestions,方便 GUI 链式推荐
    """
    options = options or {}
    plan = WorkflowPlan(
        intent=intent, platform=platform,
        course_url=course_url, output_dir=output_dir,
        cookie_source=cookie_source,
    )
    cookie = _cookie_arg(cookie_source)
    needs_cookie = cookie_source != CookieSource.NONE.value

    if intent == Intent.DOWNLOAD.value:
        _plan_download(plan, platform, course_url, output_dir, cookie, needs_cookie, options)
    elif intent == Intent.SCAN_ONLY.value:
        _plan_scan(plan, platform, course_url, output_dir, cookie, needs_cookie, options)
    elif intent == Intent.BUILD_MAPPING.value:
        _plan_build_mapping(plan, output_dir, options)
    elif intent == Intent.UPLOAD.value:
        _plan_upload(plan, output_dir, cookie, options)
    elif intent == Intent.RETRY_FAILED.value:
        _plan_retry(plan, platform, course_url, output_dir, cookie, options)
    elif intent == Intent.MODIFY.value:
        _plan_modify(plan, output_dir, cookie, options)
    elif intent == Intent.AUDIT.value:
        _plan_audit(plan, output_dir, options)
    elif intent == Intent.ACCEPT.value:
        _plan_accept(plan, output_dir, options)
    else:
        # UNKNOWN — 给一个空 plan + 友好 next_suggestion
        plan.next_suggestions = [
            "请选择意图:download / scan / build_mapping / upload / retry / modify",
        ]

    # 全局风险:无 cookie 时所有写后台 step 升到 HIGH
    if not needs_cookie and plan.risk_level in (RiskLevel.MEDIUM.value, RiskLevel.HIGH.value):
        plan.risk_level = RiskLevel.HIGH.value
        for s in plan.steps:
            if s.network_required and not s.requires_cookie:
                s.notes = (s.notes + " | 缺 cookie,可能只能 fallback 跑").strip(" |")

    return plan


def _plan_download(plan, platform, course_url, output_dir, cookie, needs_cookie, options):
    """下载 = scan + 真下载 + 审计"""
    if platform == Platform.UNKNOWN.value:
        plan.risk_level = RiskLevel.HIGH.value
        plan.next_suggestions = ["请先选择平台(chaoxing/xuetangx/zhihuishu/icourse163)"]
        return

    max_tabs = options.get("max_tabs", 4)
    # Step 1: scan-only(无 cookie 也能跑,只 GET cards API)
    plan.steps.append(WorkflowStep(
        id="scan",
        title="扫描课程资源(产出 _chapter_tree + _resource_discovery_report)",
        command=_cmd_scan(platform, course_url, output_dir, max_tabs),
        writes_files=[
            f"{output_dir}/_scanned_resources.json",
            f"{output_dir}/_resource_discovery_report.json",
            f"{output_dir}/_resource_discovery_report.md",
            f"{output_dir}/_chapter_tree.json",
            f"{output_dir}/_chapter_tree.md",
        ],
        network_required=True,
        requires_cookie=False,
        destructive=False,
        notes="可独立跑(无 cookie 也行);检查 _resource_discovery_report.md 看漏扫",
    ))
    # Step 2: 真下载
    if needs_cookie:
        plan.steps.append(WorkflowStep(
            id="download",
            title=f"下载 {platform} 资源(需 cookie)",
            command=_cmd_scan_with_cookie(platform, course_url, output_dir, cookie, max_tabs)
            if cookie else _cmd_download(platform, course_url, output_dir, max_tabs),
            writes_files=[
                f"{output_dir}/视频/...",
                f"{output_dir}/文档/...",
                f"{output_dir}/_resource_naming_manifest.json",
            ],
            network_required=True,
            requires_cookie=True,
            destructive=False,
            notes="无 cookie 时可手动加 XTBZ_COOKIE 环境变量或 --cookies-string",
        ))
    else:
        plan.steps.append(WorkflowStep(
            id="download",
            title=f"下载 {platform} 资源(需 cookie,当前未设)",
            command=_cmd_download(platform, course_url, output_dir, max_tabs),
            writes_files=[f"{output_dir}/视频/..."],
            network_required=True,
            requires_cookie=True,
            destructive=False,
            notes="⚠️ 必须先提供 cookie — 见 cookie_source 选项",
        ))

    plan.expected_outputs = [
        f"{output_dir}/_chapter_tree.json",
        f"{output_dir}/_resource_naming_manifest.json",
        f"{output_dir}/_review.html",
        f"{output_dir}/_retry_downloads.json(可能,只在有失败资源时)",
    ]
    plan.next_suggestions = [
        "查看 _resource_discovery_report.md 找漏扫节(empty_lesson / suspicious_missing_ppt)",
        "打开 _review.html 看章节树+资源状态",
        "下一步:build_mapping(用 _chapter_tree.json 100% 对齐 mapping)",
        "建议:扫描完后跑一次 resource_audit 检查角色识别 / 漏扫 / 错配",
    ]
    plan.risk_level = RiskLevel.LOW.value


def _plan_scan(plan, platform, course_url, output_dir, cookie, needs_cookie, options):
    if platform == Platform.UNKNOWN.value:
        plan.next_suggestions = ["请先选择平台"]
        return
    max_tabs = options.get("max_tabs", 4)
    plan.steps.append(WorkflowStep(
        id="scan",
        title=f"扫描 {platform} 课程资源(只读,不下文件)",
        command=_cmd_scan(platform, course_url, output_dir, max_tabs),
        writes_files=[
            f"{output_dir}/_scanned_resources.json",
            f"{output_dir}/_resource_discovery_report.md",
        ],
        network_required=True,
        requires_cookie=False,
        destructive=False,
        notes="安全:只读 GET 多次(每节 1-4 个请求,会触发限流)",
    ))
    plan.expected_outputs = [f"{output_dir}/_resource_discovery_report.md"]
    plan.next_suggestions = [
        "看 _resource_discovery_report.md 找可疑节",
        "如确认资源齐全,下一步:download(需要 cookie)",
    ]
    plan.risk_level = RiskLevel.SAFE.value


def _plan_build_mapping(plan, output_dir, options):
    """build-mapping:从 outline + 视频文件夹 → _mapping.json"""
    videos_dir = options.get("videos_dir", f"{output_dir}/视频")
    outline_path = options.get("outline_path", f"{output_dir}/_chapter_tree.json")
    course_id = options.get("course_id", "<COURSE_ID>")
    out_path = options.get("out_path", f"{output_dir}/_mapping.json")
    include_empty = options.get("include_empty_lessons", False)
    if not outline_path:
        plan.next_suggestions = ["需要提供 --doc(outline / _chapter_tree.json 路径)"]
        plan.risk_level = RiskLevel.HIGH.value
        return
    plan.steps.append(WorkflowStep(
        id="build_mapping",
        title="build-mapping(从 outline + 视频 → _mapping.json)",
        command=_cmd_build_mapping(videos_dir, outline_path, course_id, out_path, include_empty),
        writes_files=[out_path, f"{Path(out_path).parent}/_mapping_exclusions.md"],
        network_required=False,
        requires_cookie=False,
        destructive=False,
        notes="默认跳过空章/空节;加 --include-empty-lessons 保留",
    ))
    plan.expected_outputs = [out_path]
    plan.next_suggestions = [
        "检查 _mapping.json 的章节/课时/视频数对得上",
        "检查 _mapping_exclusions.md 了解跳过的内容",
        "下一步:upload --plan-only(默认 plan-first 安全闸)",
    ]
    plan.risk_level = RiskLevel.SAFE.value


def _plan_upload(plan, output_dir, cookie, options):
    """upload 永远先 plan-only(plan-first 安全闸),apply-plan 是危险的下一步"""
    mapping_path = options.get("mapping_path", f"{output_dir}/_mapping.json")
    course_id = options.get("course_id", "<COURSE_ID>")
    plan_path = options.get("plan_path", f"{output_dir}/_upload_plan.json")
    only_lessons = options.get("only_lessons")
    only_resources = options.get("only_resources")

    if not course_id or course_id == "<COURSE_ID>":
        plan.next_suggestions = ["必须先 build-mapping 并提供 --course-id"]
        plan.risk_level = RiskLevel.HIGH.value
        return

    # Step 1: plan-only(默认,安全)
    plan.steps.append(WorkflowStep(
        id="plan_only",
        title="upload --plan-only(写 _upload_plan.json/md,不做任何写 API)",
        command=_cmd_upload_plan_only(mapping_path, course_id, cookie,
                                     only_lessons, only_resources),
        writes_files=[plan_path, f"{Path(plan_path).with_suffix('.md')}"],
        network_required=True,
        requires_cookie=True,
        destructive=False,
        notes="✅ 安全:只 GET 后台章节树 + 写本地 plan;不会动后台",
    ))

    # Step 2: 人工 review _upload_plan.md 后,可选 apply-plan(危险)
    if plan_path and Path(plan_path).exists():
        apply_cmd = _cmd_upload_apply_plan(
            mapping_path, course_id, plan_path, cookie,
            reset_confirm=options.get("reset_confirm"),
        )
        plan.steps.append(WorkflowStep(
            id="apply_plan",
            title="upload --apply-plan(校验后写后台)",
            command=apply_cmd,
            writes_files=[f"{output_dir}/_upload_manifest.json"],
            network_required=True,
            requires_cookie=True,
            destructive=True,  # 真建章/节/leaf
            requires_confirmation=True,  # 必须二次确认
            notes="⚠️ 会真建章/节/leaf;--reset-confirm 会先清空课程(更危险)",
        ))
        plan.required_confirmations.append("apply_plan")

    # 如果传了 --reset-confirm,标更危险
    if options.get("reset_confirm"):
        for s in plan.steps:
            if s.id == "apply_plan":
                s.notes += " | ⚠️⚠️ 加了 --reset-confirm,会先调 reset_course_tree 清空课程"

    plan.expected_outputs = [plan_path, f"{Path(plan_path).with_suffix('.md')}"]
    plan.next_suggestions = [
        "人工 review _upload_plan.md(看 CREATE / SKIP / HIGH_RISK)",
        "看 plan 里 tree_source (real/fallback) 确认后台树真拉到了",
        "满意后跑 apply-plan(必须二次确认)",
    ]
    plan.risk_level = RiskLevel.MEDIUM.value
    if options.get("reset_confirm"):
        plan.risk_level = RiskLevel.HIGH.value


def _plan_retry(plan, platform, course_url, output_dir, cookie, options):
    """retry-downloads:重试 _retry_downloads.json 里的资源"""
    if platform == Platform.UNKNOWN.value:
        plan.next_suggestions = ["请先选择平台"]
        return
    retry_list = options.get("retry_list", f"{output_dir}/_retry_downloads.json")
    if not Path(retry_list).exists():
        plan.next_suggestions = [f"找不到 {retry_list};先跑一次 download 失败才会生成"]
        plan.risk_level = RiskLevel.HIGH.value
        return
    plan.steps.append(WorkflowStep(
        id="retry_downloads",
        title=f"重试 {platform} 失败资源(--retry-downloads)",
        command=_cmd_retry_downloads(platform, course_url, output_dir, retry_list, cookie),
        writes_files=[f"{output_dir}/_retry_downloads.json(更新)"],
        network_required=True,
        requires_cookie=True,
        destructive=False,
        notes="重试 _retry_downloads.json 里的 resource_key 集合,跳过其它",
    ))
    plan.expected_outputs = [f"{output_dir}/_resource_naming_manifest.json(更新)"]
    plan.next_suggestions = [
        "重试完检查 _resource_naming_manifest.json 的 status 字段",
        "仍失败的:查 _retry_downloads.json 看原因(限流 / 登录过期 / 资源已删)",
    ]
    plan.risk_level = RiskLevel.LOW.value


def _plan_modify(plan, output_dir, cookie, options):
    """modify 局部修改(只动某一节/某资源)"""
    mapping_path = options.get("mapping_path", f"{output_dir}/_mapping.json")
    course_id = options.get("course_id", "<COURSE_ID>")
    only_lessons = options.get("only_lessons")
    only_resources = options.get("only_resources")

    if not (only_lessons or only_resources):
        plan.next_suggestions = ["modify 必须提供 --only-lessons 或 --only-resources"]
        plan.risk_level = RiskLevel.HIGH.value
        return

    if only_resources and not only_lessons and not str(only_resources).count(":"):
        plan.next_suggestions = ["--only-resources 格式必须是 'lesson:kind',如 '1.2:ppt'"]
        plan.risk_level = RiskLevel.HIGH.value
        return

    plan_path = f"{output_dir}/_upload_plan.json"
    # Step 1: 永远先 plan-only
    plan.steps.append(WorkflowStep(
        id="plan_only",
        title=f"局部 upload --plan-only(只算目标 leaf,其它 SKIP)",
        command=_cmd_upload_plan_only(mapping_path, course_id, cookie, only_lessons, only_resources),
        writes_files=[plan_path, f"{Path(plan_path).with_suffix('.md')}"],
        network_required=True,
        requires_cookie=True,
        destructive=False,
        notes="默认先 plan-only,confirm 目标 leaf 正确再 apply",
    ))
    # Step 2: apply-plan
    if Path(plan_path).exists():
        apply_cmd = _cmd_upload_apply_plan(
            mapping_path, course_id, plan_path, cookie,
            reset_confirm=None,  # modify 永远不传 reset_confirm(局部不允许 reset)
        )
        plan.steps.append(WorkflowStep(
            id="apply_plan",
            title="apply-plan(局部上传,不动其它 chapter/section/leaf)",
            command=apply_cmd,
            writes_files=[f"{output_dir}/_upload_manifest.json"],
            network_required=True,
            requires_cookie=True,
            destructive=True,
            requires_confirmation=True,
            notes="⚠️ modify 模式禁止 --reset-confirm(局部不会清空)",
        ))
        plan.required_confirmations.append("apply_plan")

    # 验证:only-resources 局部不应包含 reset_confirm
    if options.get("reset_confirm"):
        plan.next_suggestions.append(
            "⚠️ 警告:modify 模式不建议加 --reset-confirm(局部不应清空)"
        )

    plan.expected_outputs = [plan_path]
    plan.next_suggestions = [
        "review _upload_plan.md 的 items(应该只有目标的 leaf)",
        "确认后 apply-plan",
        "建议先跑 resource_audit 检查 mapping 与本地文件对齐(可能漏挂 / 挂错节)",
    ]
    plan.risk_level = RiskLevel.MEDIUM.value


def _plan_audit(plan, output_dir, options):
    """资源智能审计:扫描产物 + mapping 检查 4 类问题(漏/错/配/缺)。

    不下载 / 不上传,只读本地文件生成 _resource_audit.{json,md,csv}。
    """
    chapter_tree_path = options.get("chapter_tree_path") or f"{output_dir}/_chapter_tree.json"
    manifest_path = options.get("manifest_path") or f"{output_dir}/_resource_naming_manifest.json"
    mapping_path = options.get("mapping_path") or f"{output_dir}/_mapping.json"

    plan.steps.append(WorkflowStep(
        id="audit_scan",
        title="audit_scan_completeness(漏扫 + 错分类 + count_mismatch)",
        command=(
            f'python -m scrape_new audit --chapter-tree "{chapter_tree_path}" '
            f'--manifest "{manifest_path}" --output-dir "{output_dir}"'
        ),
        writes_files=[
            f"{output_dir}/_resource_audit.json",
            f"{output_dir}/_resource_audit.md",
            f"{output_dir}/_resource_audit.csv",
        ],
        network_required=False,
        requires_cookie=False,
        destructive=False,
        notes="✅ 安全:纯本地读文件,生成 3 份报告(人读 MD + JSON + CSV)",
    ))
    plan.steps.append(WorkflowStep(
        id="audit_mapping",
        title="audit_mapping_alignment(挂错节 / 漏挂 / 重复 / 附件放错字段)",
        command=(
            f'python -m scrape_new audit --mapping "{mapping_path}" '
            f'--manifest "{manifest_path}" --output-dir "{output_dir}"'
        ),
        writes_files=[
            f"{output_dir}/_resource_audit.json(追加)",
        ],
        network_required=False,
        requires_cookie=False,
        destructive=False,
        notes="走 mapping audit:检查 video 字段是不是非视频扩展名、附件是不是 video 等",
    ))

    plan.expected_outputs = [
        f"{output_dir}/_resource_audit.md",
        f"{output_dir}/_resource_audit.json",
        f"{output_dir}/_resource_audit.csv",
    ]
    plan.next_suggestions = [
        "打开 _resource_audit.md 看高风险节(可能要补资源 / 修正 mapping)",
        "CSV 可给 GUI 直接渲染成表格(高风险节标红)",
        "audit 标记 low_confidence_role 的资源:需要人工确认角色",
    ]
    plan.risk_level = RiskLevel.SAFE.value


def _plan_accept(plan, output_dir, options):
    """课程本地验收总报告:把 output_dir 下所有本地产物汇总,给"能不能进下一步"判定。

    不下载 / 不上传,只读本地 _chapter_tree/_manifest/_audit/_mapping/_upload_plan/_retry
    写 _course_acceptance.{json,md},状态:READY / REVIEW / BLOCKED / INCOMPLETE。
    """
    plan.steps.append(WorkflowStep(
        id="accept",
        title="accept(课程本地验收总报告)",
        command=(
            f'python -m scrape_new accept --output-dir "{output_dir}"'
        ),
        writes_files=[
            f"{output_dir}/_course_acceptance.json",
            f"{output_dir}/_course_acceptance.md",
        ],
        network_required=False,
        requires_cookie=False,
        destructive=False,
        notes=(
            "纯本地验收报告,汇总 audit/manifest/mapping/upload_plan;"
            "状态:READY / REVIEW / BLOCKED / INCOMPLETE"
        ),
    ))

    plan.expected_outputs = [
        f"{output_dir}/_course_acceptance.md",
        f"{output_dir}/_course_acceptance.json",
    ]
    plan.next_suggestions = [
        "打开 _course_acceptance.md 看总状态 + 风险分组 + 下一步命令",
        "BLOCKED: 先修 high risk(missing_local_file / duplicate_* / non_video_in_video_slot)",
        "REVIEW: 人工确认 medium risk 后, 跑 build-mapping / upload --plan-only",
        "READY: 直接进入 build-mapping 或 upload --plan-only",
    ]
    plan.risk_level = RiskLevel.SAFE.value
