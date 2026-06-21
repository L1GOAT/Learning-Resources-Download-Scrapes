"""
命令行接口

负责解析命令行参数和展示人类可读输出。
"""

from __future__ import annotations

import argparse
import json
import shlex
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Sequence

from . import __version__
from .app import run_batch, run_job, retry_job, show_history
from .models import JobRequest


def main(argv: Sequence[str] | None = None) -> int:
    """
    主入口

    Args:
        argv: 命令行参数，None 则使用 sys.argv

    Returns:
        退出码
    """
    parser = _create_parser()
    args = parser.parse_args(argv)

    # 设置日志
    _setup_logging(args.verbose)

    try:
        if args.history:
            show_history(args.config)
            return 0

        if args.retry:
            result = retry_job(args.retry, args.config)
            _print_job_result(result)
            return 0 if not result.error else 1

        if args.test:
            return _run_test()

        if args.subcmd == "batch":
            result = run_batch(
                intent_desc=args.intent,
                urls_file=args.urls_file,
                output_dir=args.output,
                config_path=args.config,
                no_dedup=args.no_dedup,
            )
            _print_batch_result(result)
            return 0 if result.failed_urls == 0 else 1

        if args.subcmd == "platform":
            from .workflows.runner import run_platform_workflow
            return run_platform_workflow(
                platform=args.platform,
                url=args.platform_url,
                output_dir=args.platform_output,
                config_path=args.config,
            )

        if args.subcmd == "upload":
            from .upload.runner import run_upload_cli
            return run_upload_cli(args.upload_args)

        if args.subcmd in ("wizard", "assistant"):
            return _run_wizard(args)

        if args.subcmd == "audit":
            return _cmd_audit(args)

        if args.subcmd == "accept":
            return _cmd_accept(args)

        # 单个下载任务
        if not args.url:
            parser.error("请提供 URL")

        request = JobRequest(
            intent_desc=args.intent,
            url=args.url,
            output_dir=args.output,
            config_path=args.config,
            no_dedup=args.no_dedup,
        )

        result = run_job(request)
        _print_job_result(result)
        return 0 if not result.error else 1

    except KeyboardInterrupt:
        print("\n用户中断", file=sys.stderr)
        return 130
    except Exception as e:
        print(f"错误: {e}", file=sys.stderr)
        return 1


def _create_parser() -> argparse.ArgumentParser:
    """创建命令行参数解析器"""
    parser = argparse.ArgumentParser(
        prog="scrape",
        description="网页资源扒取工具箱",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  python -m scrape_new 视频 https://example.com/video
  python -m scrape_new 图片 https://example.com/gallery ./images
  python -m scrape_new 全部 https://example.com/page ./output
  python -m scrape_new batch 视频 urls.txt ./output
  python -m scrape_new platform chaoxing "https://example.com/course" ./output
  python -m scrape_new upload build-mapping --videos ./videos --doc ./outline.json
  python -m scrape_new --history
  python -m scrape_new --retry ./output/video
  python -m scrape_new --test
        """,
    )

    parser.add_argument(
        "-V", "--version",
        action="version",
        version=f"%(prog)s {__version__}",
    )

    parser.add_argument(
        "-v", "--verbose",
        action="store_true",
        help="显示详细日志",
    )

    parser.add_argument(
        "-c", "--config",
        type=Path,
        help="配置文件路径",
    )

    parser.add_argument(
        "--no-dedup",
        action="store_true",
        help="跳过去重检查，强制重新下载",
    )

    parser.add_argument(
        "--history",
        action="store_true",
        help="显示下载历史",
    )

    parser.add_argument(
        "--retry",
        type=Path,
        metavar="DIR",
        help="重试指定目录中失败的下载",
    )

    parser.add_argument(
        "--test",
        action="store_true",
        help="运行测试",
    )

    # 子命令
    subparsers = parser.add_subparsers(dest="subcmd")

    # batch 子命令
    batch_parser = subparsers.add_parser(
        "batch",
        help="批量下载",
    )
    batch_parser.add_argument(
        "intent",
        help="资源类型（视频/图片/文档/全部等）",
    )
    batch_parser.add_argument(
        "urls_file",
        type=Path,
        help="URL 列表文件",
    )
    batch_parser.add_argument(
        "output",
        type=Path,
        nargs="?",
        help="输出目录",
    )

    # platform 子命令
    platform_parser = subparsers.add_parser(
        "platform",
        help="平台工作流（超星/智慧树/学堂在线/中国大学MOOC）",
    )
    platform_parser.add_argument(
        "platform",
        help="平台名称（chaoxing/zhihuishu/xuetangx/icourse163）",
    )
    platform_parser.add_argument(
        "platform_url",
        help="课程 URL",
    )
    platform_parser.add_argument(
        "platform_output",
        type=Path,
        nargs="?",
        help="输出目录",
    )

    # upload 子命令（使用 REMAINDER 收集所有剩余参数）
    upload_parser = subparsers.add_parser(
        "upload",
        help="老师后台搭建（建课/上传/习题）",
    )
    upload_parser.add_argument(
        "upload_args",
        nargs=argparse.REMAINDER,
        help="上传命令参数",
    )

    # wizard / assistant 子命令:交互式工作流向导
    # 两种入口指向同一实现(assistant 是 wizard 的 alias,用户记哪个用哪个)
    for alias_name, alias_help in (
        ("wizard", "交互式向导(下载/扫描/建 mapping/上传)"),
        ("assistant", "wizard 的别名(同上)"),
    ):
        sp = subparsers.add_parser(
            alias_name,
            help=alias_help,
            formatter_class=argparse.RawDescriptionHelpFormatter,
            epilog="""
示例:
  # 交互模式(问问题收集参数)
  python -m scrape_new wizard

  # 纯 dry-run(只出 plan,不问问题不执行)— GUI 友好
  python -m scrape_new wizard --intent download --platform chaoxing \\
      --url "https://..." --output-dir mycourse --cookie-source env

  # JSON 输出(给 GUI / CI 消费)
  python -m scrape_new wizard --intent upload --course-id 1234 \\
      --cookie-source env --json

  # Markdown 输出(给 README / issue 写)
  python -m scrape_new wizard --intent build_mapping --markdown

设计原则:
  - 默认 plan-first:任何 upload 永远先 plan-only
  - 危险操作(destructive/requires_confirmation)显式标出
  - 不直接执行,先打印 plan,人工 review 后再确认
            """,
        )
        sp.add_argument("--intent", "-i", required=False,
                        choices=["download", "scan", "build_mapping", "upload",
                                 "retry", "modify", "audit", "accept", "unknown"],
                        help="用户意图")
        sp.add_argument("--platform", "-p", required=False,
                        choices=["chaoxing", "xuetangx", "zhihuishu", "icourse163"],
                        help="平台")
        sp.add_argument("--url", "-u", required=False, help="课程 URL")
        sp.add_argument("--output-dir", "-o", required=False, default="./output",
                        help="输出目录")
        sp.add_argument("--cookie-source", "-c", required=False,
                        choices=["curl", "string", "file", "env", "none"],
                        default="none", help="cookie 来源")
        sp.add_argument("--course-id", required=False, help="课程 ID(上传用)")
        sp.add_argument("--mapping-path", required=False, help="mapping.json 路径")
        sp.add_argument("--outline-path", required=False, help="outline 路径")
        sp.add_argument("--videos-dir", required=False, help="视频文件夹")
        sp.add_argument("--plan-path", required=False, help="_upload_plan.json 路径")
        sp.add_argument("--retry-list", required=False, help="_retry_downloads.json 路径")
        sp.add_argument("--only-lessons", required=False, help="只动这些 lesson")
        sp.add_argument("--only-resources", required=False, help="只动这些 (lesson,kind)")
        sp.add_argument("--reset-confirm", required=False, help="显式传 course_id 才能 reset")
        sp.add_argument("--include-empty-lessons", action="store_true",
                        help="build-mapping 保留空章")
        sp.add_argument("--max-tabs", type=int, default=4, help="多 tab 探测数")
        sp.add_argument("--json", action="store_true", help="输出 JSON 计划(GUI/CI)")
        sp.add_argument("--markdown", action="store_true", help="输出 Markdown 计划")
        sp.add_argument("--no-color", action="store_true", help="关闭 ANSI 颜色")
        sp.add_argument("--yes", action="store_true",
                        help="跳过二次确认(仅对非危险 step 生效,危险 step 仍需确认)")
        # 逐步执行(第一版,只允许非危险 step)
        sp.add_argument("--execute-step", required=False, default=None,
                        help="执行 plan 中指定 id 的 step(仅非危险 step,"
                             "危险 step 仍拒绝执行并提示人工复制命令)")
        sp.add_argument("--run-log", required=False, default=None,
                        help="执行日志 jsonl 路径(默认 <output_dir>/_wizard_runs.jsonl)")

    # audit 子命令:资源智能审计(本地文件读,生成 _resource_audit.{json,md,csv})
    audit_parser = subparsers.add_parser(
        "audit",
        help="资源智能审计(漏/错/配/缺)— 读本地文件,无网络",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  # 完整审计(章节树 + manifest + mapping 全给)
  python -m scrape_new audit \\
      --chapter-tree _chapter_tree.json \\
      --manifest _resource_naming_manifest.json \\
      --mapping _mapping.json \\
      --output-dir .

  # 只审计 mapping 错配
  python -m scrape_new audit --mapping _mapping.json \\
      --manifest _resource_naming_manifest.json --output-dir .

  # 只审计扫描漏扫
  python -m scrape_new audit --chapter-tree _chapter_tree.json \\
      --manifest _resource_naming_manifest.json --output-dir .

设计:
  - 无网络 / 无 cookie / 不动真实数据
  - 输出 3 份报告:json / md / csv(GUI 可直接消费)
  - 含风险等级 + 置信度 + 证据链
        """,
    )
    audit_parser.add_argument("--chapter-tree", help="_chapter_tree.json 路径")
    audit_parser.add_argument("--manifest", help="_resource_naming_manifest.json 路径")
    audit_parser.add_argument("--mapping", help="_mapping.json 路径(可选)")
    audit_parser.add_argument("--output-dir", default=".", help="报告输出目录")
    audit_parser.add_argument("--expected-tab-count", type=int, default=None,
                              help="期望的 tab 扫描数(给 scan-only 用,默认 None)")

    # accept 子命令:课程验收总报告(本地产物汇总)
    accept_parser = subparsers.add_parser(
        "accept",
        help="课程本地验收总报告(读 _chapter_tree/_manifest/_audit/_mapping 等)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  # 标准用法
  python -m scrape_new accept --output-dir ./mycourse

  # 只输出 JSON(给 GUI / CI 消费)
  python -m scrape_new accept --output-dir ./mycourse --json

  # 只输出 Markdown
  python -m scrape_new accept --output-dir ./mycourse --markdown

设计:
  - 纯本地 IO, 不访问网络 / 不需要 cookie
  - 文件缺失 / JSON 解析失败 不崩, 转成 risk
  - 状态: READY / REVIEW / BLOCKED / INCOMPLETE
  - 输出 _course_acceptance.json / _course_acceptance.md
        """,
    )
    accept_parser.add_argument("--output-dir", required=True,
                               help="课程输出目录(读 _chapter_tree.json 等本地产物)")
    accept_parser.add_argument("--json", action="store_true",
                               help="只打印 JSON 到 stdout(不写文件, 不打 Markdown)")
    accept_parser.add_argument("--markdown", action="store_true",
                               help="只打印 Markdown 到 stdout(不写文件)")

    return parser


def _setup_logging(verbose: bool) -> None:
    """设置日志"""
    import logging

    level = logging.DEBUG if verbose else logging.INFO
    format_str = "%(asctime)s [%(levelname)s] %(name)s: %(message)s" if verbose else "%(message)s"

    logging.basicConfig(
        level=level,
        format=format_str,
        datefmt="%H:%M:%S",
    )


def _print_job_result(result) -> None:
    """打印任务结果"""
    if result.error:
        print(f"❌ 错误: {result.error}")
        return

    print(f"\n{'='*50}")
    print(f"任务完成: {result.url}")
    print(f"{'='*50}")
    print(f"意图: {result.intent}")
    print(f"输出: {result.output_dir}")
    print(f"发现: {result.found}")
    print(f"下载: {result.downloaded}")
    print(f"失败: {result.failed}")
    print(f"跳过: {result.skipped}")
    print(f"可疑: {result.suspicious}")
    print(f"不完整: {result.incomplete}")
    print(f"耗时: {result.elapsed:.1f}s")

    if result.report_path:
        print(f"报告: {result.report_path}")

    if result.history_path:
        print(f"历史: {result.history_path}")

    print(f"{'='*50}\n")


def _print_batch_result(result) -> None:
    """打印批量结果"""
    print(f"\n{'='*50}")
    print(f"批量下载完成")
    print(f"{'='*50}")
    print(f"总 URL: {result.total_urls}")
    print(f"成功: {result.success_urls}")
    print(f"失败: {result.failed_urls}")
    print(f"跳过: {result.skipped_urls}")
    print(f"耗时: {result.elapsed:.1f}s")
    print(f"{'='*50}\n")


def _run_test() -> int:
    """运行测试"""
    try:
        import pytest
        return pytest.main([str(Path(__file__).parent / "tests"), "-v"])
    except ImportError:
        print("请安装 pytest: pip install pytest", file=sys.stderr)
        return 1


# ─── wizard / assistant 向导 ─────────────────────────────────────

def _run_wizard(args: argparse.Namespace) -> int:
    """wizard / assistant 主入口。

    设计原则:
      1. 交互层只负责:收集答案 → 调 build_workflow_plan → 打印/输出 → 可选执行
      2. 危险操作(destructive / requires_confirmation)绝不自动执行,必须二次确认
      3. 默认 plan-first:upload 永远先生成 plan,人工 review 后才 --apply-plan
      4. --json / --markdown 输出纯数据,GUI / CI 可消费
      5. --dry-run / 无 --intent 时进入交互模式(问问题)
    """
    # Windows subprocess 默认 GBK,UTF-8 字符(▶ ⚠️)会炸。强制 UTF-8 输出。
    import sys as _sys
    if _sys.platform.startswith("win"):
        try:
            _sys.stdout.reconfigure(encoding="utf-8")
            _sys.stderr.reconfigure(encoding="utf-8")
        except Exception:
            pass

    from .services.workflow_planner import build_workflow_plan, Intent, Platform

    # 1) 收集参数
    intent = getattr(args, "intent", None)
    platform = getattr(args, "platform", None) or Platform.UNKNOWN.value
    course_url = getattr(args, "url", "") or ""
    output_dir = getattr(args, "output_dir", "./output")
    cookie_source = getattr(args, "cookie_source", "none")
    use_json = bool(getattr(args, "json", False))
    use_markdown = bool(getattr(args, "markdown", False))
    yes_all = bool(getattr(args, "yes", False))

    # 收集 options dict
    options = {}
    for k in ("course_id", "mapping_path", "outline_path", "videos_dir",
              "plan_path", "retry_list", "only_lessons", "only_resources",
              "reset_confirm", "max_tabs"):
        v = getattr(args, k, None)
        if v is not None and v != "":
            options[k] = v
    if getattr(args, "include_empty_lessons", False):
        options["include_empty_lessons"] = True

    # 2) 交互模式(没 --intent 时):问最少的问题
    if intent is None:
        intent = _wizard_ask_intent()
        if platform == Platform.UNKNOWN.value:
            platform = _wizard_ask_platform()
        if not course_url:
            course_url = _wizard_ask("课程 URL", "")
        if intent in (Intent.DOWNLOAD.value, Intent.SCAN_ONLY.value,
                      Intent.RETRY_FAILED.value):
            if cookie_source == "none":
                cookie_source = _wizard_ask_cookie_source()
        if intent in (Intent.UPLOAD.value, Intent.MODIFY.value):
            if not options.get("course_id"):
                options["course_id"] = _wizard_ask("课程 ID", "")
        if not output_dir or output_dir == "./output":
            output_dir = _wizard_ask("输出目录", "./output")

    # 3) 调纯函数 planner(GUI 友好:不执行任何命令)
    plan = build_workflow_plan(
        intent=intent, platform=platform, course_url=course_url,
        output_dir=output_dir, cookie_source=cookie_source, options=options,
    )

    # 4) 输出格式
    if use_json:
        print(plan.to_json(indent=2))
        return 0

    # 默认 Markdown(给终端 / GUI 渲染)— 但 --no-color 关闭 ANSI
    if use_markdown:
        print(plan.to_markdown())
        return 0

    # 5) --execute-step:执行 plan 中指定 id 的 step(只允许非危险 step)
    execute_step_id = getattr(args, "execute_step", None)
    if execute_step_id:
        return _wizard_execute_step(plan, execute_step_id, output_dir,
                                    getattr(args, "run_log", None))

    # 默认人类可读:Markdown + 二次确认提示
    print(plan.to_markdown())
    print()
    if plan.required_confirmations:
        print("=" * 60)
        print("⚠️  上述 plan 含需要二次确认的危险操作:")
        for sid in plan.required_confirmations:
            print(f"   - {sid}")
        print("=" * 60)
        if yes_all:
            print("⚠️  --yes 已传,但危险 step 仍需逐个确认(GUI 应弹模态对话框)")

    # 6) 非 dry-run 时,询问是否执行非危险 step
    dangerous_ids = {s.id for s in plan.steps if s.destructive}
    safe_steps = [s for s in plan.steps if s.id not in dangerous_ids]
    if safe_steps and not use_json and not use_markdown:
        print()
        print(f"非危险步骤 {len(safe_steps)} 个,可用 --json 输出 plan")
        # 实际执行留待 GUI:CLI 模式默认只打印,需要 --execute 才调 subprocess
        # (避免 CLI 模式误执行 — 默认 dry-run)

    return 0


# ─── wizard step 执行 ─────────────────────────────────────

# subprocess 输出尾巴(防止大文件把 jsonl 写爆)
_STDOUT_TAIL_MAX = 4000


def _safe_split_command(command: str) -> list[str]:
    """把 plan.command 安全拆成 argv 列表。

    永远不用 shell=True(防注入)。
    Windows + POSIX 都用 shlex.split(posix=False) 保持一致。
    """
    try:
        return shlex.split(command, posix=False)
    except ValueError:
        # 引号配不上 → 退到按空格粗拆(够用,wizard command 都很标准)
        return command.split()


def _wizard_execute_step(
    plan,
    step_id: str,
    output_dir: str,
    run_log: str | None,
) -> int:
    """执行 plan 中指定 id 的 step(只允许非危险 step)。

    危险 / 不存在 step 直接返回非 0,不调 subprocess。
    """
    target = next((s for s in plan.steps if s.id == step_id), None)
    if target is None:
        available = [s.id for s in plan.steps]
        print(f"❌ step id '{step_id}' 不在 plan 中", file=sys.stderr)
        print(f"可用 step id: {available}", file=sys.stderr)
        return 2

    if target.destructive or target.requires_confirmation:
        print("=" * 60, file=sys.stderr)
        print(f"❌ 危险步骤不会由 wizard 自动执行: {target.id}", file=sys.stderr)
        print("=" * 60, file=sys.stderr)
        print(f"标题: {target.title}", file=sys.stderr)
        print(f"destructive={target.destructive}  "
              f"requires_confirmation={target.requires_confirmation}", file=sys.stderr)
        print(f"提示: {target.notes or '(无)'}", file=sys.stderr)
        print("请人工确认后复制执行:", file=sys.stderr)
        print(f"  {target.command}", file=sys.stderr)
        return 3

    # 打印执行前摘要
    print("=" * 60)
    print(f"▶ 执行 step: {target.id} — {target.title}")
    print(f"  command: {target.command}")
    print(f"  writes_files: {target.writes_files}")
    print(f"  network_required: {target.network_required}")
    print(f"  requires_cookie: {target.requires_cookie}")
    print("=" * 60)

    argv = _safe_split_command(target.command)
    start = time.monotonic()
    try:
        proc = subprocess.run(
            argv, shell=False, capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=None,
        )
        elapsed = time.monotonic() - start
        stdout_tail = (proc.stdout or "")[-_STDOUT_TAIL_MAX:]
        stderr_tail = (proc.stderr or "")[-_STDOUT_TAIL_MAX:]
        status = "succeeded" if proc.returncode == 0 else "failed"

        if proc.stdout:
            print(proc.stdout, end="")
        if proc.stderr:
            print(proc.stderr, end="", file=sys.stderr)

        _wizard_write_run_log(
            plan, target, proc.returncode, elapsed, status,
            stdout_tail, stderr_tail, output_dir, run_log,
        )
        if proc.returncode != 0:
            print(f"❌ step '{target.id}' 失败,returncode={proc.returncode}",
                  file=sys.stderr)
        else:
            print(f"✅ step '{target.id}' 成功,耗时 {elapsed:.2f}s")
        return proc.returncode
    except FileNotFoundError as e:
        elapsed = time.monotonic() - start
        print(f"❌ 找不到可执行文件: {e}", file=sys.stderr)
        _wizard_write_run_log(
            plan, target, 127, elapsed, "failed",
            "", str(e), output_dir, run_log,
        )
        return 127
    except Exception as e:
        elapsed = time.monotonic() - start
        print(f"❌ step '{target.id}' 异常: {e}", file=sys.stderr)
        _wizard_write_run_log(
            plan, target, 1, elapsed, "failed",
            "", str(e), output_dir, run_log,
        )
        return 1


def _wizard_write_run_log(
    plan, target, returncode: int, elapsed: float, status: str,
    stdout_tail: str, stderr_tail: str,
    output_dir: str, run_log: str | None,
) -> None:
    """把执行结果 append 到 _wizard_runs.jsonl(UTF-8)。"""
    log_path = Path(run_log) if run_log else (Path(output_dir) / "_wizard_runs.jsonl")
    log_path.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "intent": plan.intent,
        "step_id": target.id,
        "title": target.title,
        "command": target.command,
        "returncode": returncode,
        "elapsed_seconds": round(elapsed, 3),
        "status": status,
        "stdout_tail": stdout_tail,
        "stderr_tail": stderr_tail,
    }
    with open(log_path, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def _wizard_ask_intent() -> str:
    """交互模式:问用户意图。"""
    print("=" * 60)
    print("scrape_new wizard — 交互式工作流向导")
    print("=" * 60)
    print()
    print("你要做什么?")
    print("  1) download    下载课程(默认包含 scan + download)")
    print("  2) scan        只扫描课程资源,不下文件")
    print("  3) build_mapping 从 outline + 视频文件夹 → _mapping.json")
    print("  4) upload      上传到老师后台(默认 plan-first)")
    print("  5) retry       重试失败下载")
    print("  6) modify      局部上传(只动某一节/某个资源)")
    print("  7) audit       资源智能审计(漏扫 / 错分类 / 挂错节)")
    print("  8) accept      课程验收总报告(汇总 audit/manifest/mapping)")
    print()
    while True:
        choice = input("请输入数字 [1-8] 或直接输入意图: ").strip()
        mapping = {"1": "download", "2": "scan", "3": "build_mapping",
                  "4": "upload", "5": "retry", "6": "modify",
                  "7": "audit", "8": "accept"}
        if choice in mapping:
            return mapping[choice]
        if choice in ("download", "scan", "build_mapping", "upload", "retry",
                      "modify", "audit", "accept"):
            return choice
        print("  输入有误,请重试(1-8 或意图名)")


def _wizard_ask_platform() -> str:
    print()
    print("哪个平台?")
    print("  chaoxing / xuetangx / zhihuishu / icourse163")
    while True:
        p = input("平台: ").strip().lower()
        if p in ("chaoxing", "xuetangx", "zhihuishu", "icourse163"):
            return p
        print("  输入有误,请重试")


def _wizard_ask(prompt: str, default: str = "") -> str:
    """交互模式:问用户一个字符串值"""
    suffix = f" [{default}]" if default else ""
    val = input(f"{prompt}{suffix}: ").strip()
    return val or default


def _wizard_ask_cookie_source() -> str:
    print()
    print("cookie 来源?(没 cookie 也能跑 scan-only 和 build-mapping)")
    print("  curl / string / file / env / none")
    while True:
        c = input("cookie-source: ").strip().lower()
        if c in ("curl", "string", "file", "env", "none"):
            return c
        print("  输入有误")


def _cmd_audit(args: argparse.Namespace) -> int:
    """audit 子命令 — 读本地文件,产 _resource_audit.{json,md,csv}。

    设计:
      - 纯本地 IO,无网络 / 无 cookie
      - 提供 chapter-tree + manifest 走 scan audit
      - 提供 mapping 走 mapping audit
      - 两份都给:合并报告(扫描 + mapping)
    """
    from .services.resource_audit import (
        audit_scan_completeness, audit_mapping_alignment,
        write_resource_audit_reports, CourseAuditReport,
    )

    output_dir = Path(getattr(args, "output_dir", ".") or ".")
    chapter_tree_path = getattr(args, "chapter_tree", None)
    manifest_path = getattr(args, "manifest", None)
    mapping_path = getattr(args, "mapping", None)
    expected_tab_count = getattr(args, "expected_tab_count", None)

    if not (chapter_tree_path or manifest_path or mapping_path):
        print("[错误] 至少给一个:--chapter-tree / --manifest / --mapping", file=sys.stderr)
        return 1

    chapter_tree: dict = {}
    scanned: list[dict] = []
    if chapter_tree_path:
        cp = Path(chapter_tree_path)
        if not cp.exists():
            print(f"[错误] chapter-tree 不存在: {cp}", file=sys.stderr)
            return 1
        chapter_tree = json.loads(cp.read_text(encoding="utf-8"))
    if manifest_path:
        mp = Path(manifest_path)
        if not mp.exists():
            print(f"[错误] manifest 不存在: {mp}", file=sys.stderr)
            return 1
        m = json.loads(mp.read_text(encoding="utf-8"))
        scanned = m.get("records", [])

    # 1) scan audit
    if chapter_tree or scanned:
        report = audit_scan_completeness(
            chapter_tree, scanned, expected_tab_count=expected_tab_count,
        )
    else:
        report = CourseAuditReport()

    # 2) mapping audit(如有)
    if mapping_path:
        mp = Path(mapping_path)
        if mp.exists():
            mapping = json.loads(mp.read_text(encoding="utf-8"))
            manifest = {}
            if manifest_path:
                manifest = json.loads(Path(manifest_path).read_text(encoding="utf-8"))
            # manifest 或 chapter_tree 任一当作"已下载/已扫描"来源
            src_for_mapping = manifest or chapter_tree or {}
            mapping_report = audit_mapping_alignment(mapping, src_for_mapping)
            # 合并:mapping audit 加到 global_issues
            report.global_issues.extend(mapping_report.global_issues)
            report.recommendations.extend(mapping_report.recommendations)
            # 合并 lesson-level audit(同名 lesson 覆盖)
            existing_ids = {ls.lesson_id for ls in report.lessons}
            for ls in mapping_report.lessons:
                if ls.lesson_id in existing_ids:
                    # 找原 lesson,把 issues / resources 合并
                    target = next(x for x in report.lessons if x.lesson_id == ls.lesson_id)
                    target.issues = list(set(target.issues + ls.issues))
                    target.resources.extend(ls.resources)
                    _raise_risk_local(target, ls.risk_level)
                else:
                    report.lessons.append(ls)
            # 更新 summary
            for k, v in mapping_report.summary.items():
                report.summary[k] = report.summary.get(k, 0) + v
        else:
            print(f"[警告] mapping 不存在: {mp}(跳过 mapping audit)", file=sys.stderr)

    # 3) 写报告
    paths = write_resource_audit_reports(report, output_dir)
    print(f"[audit] 报告已写:")
    for k, p in paths.items():
        print(f"  - {p}")
    # 摘要输出
    s = report.summary
    print()
    print("=" * 60)
    print(f"  课: {report.course_title or '(未填)'}")
    print(f"  总览: {s}")
    print(f"  全局问题: {len(report.global_issues)}")
    print(f"  建议: {len(report.recommendations)}")
    print("=" * 60)
    return 0


def _cmd_accept(args: argparse.Namespace) -> int:
    """accept 子命令 — 读本地 output_dir, 产 _course_acceptance.{json,md}。

    设计:
      - 纯本地 IO, 不访问网络 / 不需要 cookie
      - 文件缺失 / JSON 解析失败 不崩, 转成 risk / missing_inputs
      - 状态: READY / REVIEW / BLOCKED / INCOMPLETE
      - --json / --markdown 模式: 只打印到 stdout, 不写文件
      - 默认: 写 2 份文件 + 打印摘要
    """
    from .services.course_acceptance import (
        build_course_acceptance_report,
        write_course_acceptance_reports,
    )

    output_dir = Path(getattr(args, "output_dir", None) or ".")
    use_json = bool(getattr(args, "json", False))
    use_markdown = bool(getattr(args, "markdown", False))

    if not use_json and not use_markdown:
        # 默认行为: 写文件 + 摘要
        # 但 output_dir 不存在时不写,只给报告(报告本身能容错)
        report = build_course_acceptance_report(output_dir)
        # 写文件 — 即使 output_dir 原本不存在也 mkdir
        paths = write_course_acceptance_reports(report, output_dir)
        print("[accept] 报告已写:")
        for k, p in paths.items():
            print(f"  - {p}")
        print()
        print("=" * 60)
        print(f"  状态:{report.status}")
        high = sum(1 for r in report.risks if r.level == "high")
        medium = sum(1 for r in report.risks if r.level == "medium")
        print(f"  风险:high={high}, medium={medium}, total={len(report.risks)}")
        print(f"  建议:({len(report.recommendations)})")
        for r in report.recommendations[:3]:
            print(f"    - {r}")
        print(f"  下一步命令:(前 3 条)")
        for c in report.next_commands[:3]:
            print(f"    $ {c}")
        # 状态码:INCOMPLETE / BLOCKED 返 2 表示需要用户介入
        return 2 if report.status in ("INCOMPLETE", "BLOCKED") else 0

    if use_json:
        # --json: 只打印 JSON, 不写文件
        report = build_course_acceptance_report(output_dir)
        print(report.to_json(indent=2))
        return 2 if report.status in ("INCOMPLETE", "BLOCKED") else 0

    # --markdown: 只打印 Markdown, 不写文件
    from .services.course_acceptance import _render_md  # type: ignore
    report = build_course_acceptance_report(output_dir)
    print(_render_md(report))
    return 2 if report.status in ("INCOMPLETE", "BLOCKED") else 0


def _raise_risk_local(lesson_audit, level: str) -> None:
    """只升不降(从 mapping audit 继承)"""
    order = {"ok": 0, "low": 1, "medium": 2, "high": 3}
    if order[level] > order[lesson_audit.risk_level]:
        lesson_audit.risk_level = level