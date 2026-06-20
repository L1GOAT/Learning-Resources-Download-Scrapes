"""
平台 workflow 共享的 CLI 解析(URL / output_dir / --scan-only / --max-tabs 等)

为什么单独抽:
  - 4 个 workflow (chaoxing/xuetangx/zhihuishu/icourse163) 都需要解析同一套参数
  - chaoxing.py 之前先做(支持 --scan-only / --max-tabs / --verify-resume-only / --cpi / --resume / --retry-downloads)
  - 其他 3 个 workflow 只解析 URL/output_dir,用户传 --scan-only 时会把 flag 当 output_dir
  - 现在统一用 _extract_positional_args + 旗标解析,行为跟 chaoxing 一致

设计:
  - _FLAGS_WITH_VALUE / _FLAGS_NO_VALUE 集中维护(后续加新旗标只改一处)
  - parse_workflow_args(argv) → ParsedWorkflowArgs(NamedTuple)
    - url, output_dir
    - scan_only, verify_resume_only, max_tabs
    - cpi(chaoxing 用,其他平台忽略)
    - resume_manifest, retry_only_keys(转发自 parse_resume_retry_args)
  - error 字段不为 None 表示解析失败(主流程应终止)
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import NamedTuple

# 已知的旗标集合(每个旗标占 2 个 argv 槽:旗标名 + value)
_FLAGS_WITH_VALUE = ("--resume", "--retry-downloads", "--max-tabs", "--cpi")

# 已知的旗标集合(单独占 1 个 argv 槽,后面不接 value)
_FLAGS_NO_VALUE = {
    "--scan-only",
    "--verify-resume-only",
    "--outline-only",
    "--playwright",
    "--debug",
}


def extract_positional_args(argv: list[str]) -> list[str]:
    """过滤所有 --flag + value / --flag(无 value),只留真正的 positional 参数。

    跟 chaoxing.py 早期版本同源(2026-06-17 P1 修复):
      - `python chaoxing.py URL --resume m.json` 之前会把 --resume 当 output_dir
      - 现在按 _FLAGS_WITH_VALUE / _FLAGS_NO_VALUE 过滤,剩下的全是 positional
    """
    positional: list[str] = []
    i = 0
    while i < len(argv):
        a = argv[i]
        if a in _FLAGS_NO_VALUE:
            i += 1
            continue
        if a in _FLAGS_WITH_VALUE:
            if i + 1 < len(argv):
                i += 2
                continue
            else:
                # 旗标没值 → 把旗标当 positional,留给主流程报"需要参数"
                positional.append(a)
                i += 1
                continue
        positional.append(a)
        i += 1
    return positional


class ParsedWorkflowArgs(NamedTuple):
    """CLI 解析结果。"""
    url: str
    output_dir: str
    scan_only: bool = False
    verify_resume_only: bool = False
    max_tabs: int = 4
    cpi: str | None = None
    # resume / retry 来自 parse_resume_retry_args,转发保留
    resume_manifest: Path | None = None
    retry_only_keys: set[str] | None = None
    error: str | None = None


def parse_workflow_args(
    argv: list[str],
    *,
    default_output: str = "./output",
    require_resume_for_verify: bool = True,
) -> ParsedWorkflowArgs:
    """通用 workflow CLI 解析(URL / output_dir / 各种旗标)。

    Args:
        argv: sys.argv[1:](去掉脚本名)
        default_output: 没传 output_dir 时用
        require_resume_for_verify: --verify-resume-only 是否要求配合 --resume(默认 True,跟 chaoxing 一致)
    """
    positional = extract_positional_args(argv)

    # URL / output_dir
    if not positional:
        return ParsedWorkflowArgs(
            url="", output_dir=default_output,
            error="缺少 URL",
        )
    url = positional[0]
    output_dir = positional[1] if len(positional) > 1 else default_output

    # --max-tabs N
    max_tabs = 4
    if "--max-tabs" in argv:
        idx = argv.index("--max-tabs")
        if idx + 1 >= len(argv):
            return ParsedWorkflowArgs(
                url=url, output_dir=output_dir, max_tabs=max_tabs,
                error="--max-tabs 需要数字参数",
            )
        try:
            max_tabs = int(argv[idx + 1])
            if max_tabs < 1 or max_tabs > 8:
                return ParsedWorkflowArgs(
                    url=url, output_dir=output_dir, max_tabs=max_tabs,
                    error=f"--max-tabs 需在 1..8,实际 {max_tabs}",
                )
        except ValueError:
            return ParsedWorkflowArgs(
                url=url, output_dir=output_dir, max_tabs=max_tabs,
                error="--max-tabs 后必须是整数",
            )

    # --scan-only / --verify-resume-only
    scan_only = "--scan-only" in argv
    verify_resume_only = "--verify-resume-only" in argv
    if scan_only and verify_resume_only:
        return ParsedWorkflowArgs(
            url=url, output_dir=output_dir,
            scan_only=True, verify_resume_only=True, max_tabs=max_tabs,
            error="--scan-only 和 --verify-resume-only 互斥",
        )

    # --cpi(chaoxing 用,其他 workflow 拿值但不处理)
    cpi: str | None = None
    if "--cpi" in argv:
        idx = argv.index("--cpi")
        if idx + 1 < len(argv):
            cpi = argv[idx + 1]

    # resume / retry 复用 chaoxing 的 parse_resume_retry_args
    from scrape_new.services.download_resume import parse_resume_retry_args
    parsed_rr = parse_resume_retry_args(argv)
    if parsed_rr.error is not None:
        return ParsedWorkflowArgs(
            url=url, output_dir=output_dir,
            scan_only=scan_only, verify_resume_only=verify_resume_only,
            max_tabs=max_tabs, cpi=cpi,
            error=parsed_rr.error,
        )

    # verify-resume-only 必须配 --resume
    if verify_resume_only and require_resume_for_verify and parsed_rr.resume_manifest is None:
        return ParsedWorkflowArgs(
            url=url, output_dir=output_dir,
            scan_only=scan_only, verify_resume_only=verify_resume_only,
            max_tabs=max_tabs, cpi=cpi,
            error="--verify-resume-only 必须配合 --resume",
        )

    return ParsedWorkflowArgs(
        url=url, output_dir=output_dir,
        scan_only=scan_only, verify_resume_only=verify_resume_only,
        max_tabs=max_tabs, cpi=cpi,
        resume_manifest=parsed_rr.resume_manifest,
        retry_only_keys=parsed_rr.retry_only_keys,
        error=None,
    )


def print_workflow_usage(platform: str, default_output: str = "./output") -> None:
    """打印统一用法(供各 workflow 复用)。"""
    print(f"用法: python -m scrape_new.workflows.{platform} <课程URL> [输出目录] [选项]")
    print("       (也支持直接跑文件:python scrape_new/workflows/{}.py ...)".format(platform))
    print()
    print("选项:")
    print("  --scan-only       只扫描章节和资源,不下载文件(生成 4 个报告)")
    print("  --max-tabs N      多 tab 探测数(默认 4)")
    print("  --resume <path>   从历史 _resource_naming_manifest.json 跳过已下资源")
    print("  --retry-downloads <path>  只重下 _retry_downloads.json 里的资源")
    print("  --verify-resume-only     不下载,只判断哪些会跳过(配合 --resume)")
    print("  --outline-only    只扒章节树不下载视频(chaoxing 风格)")
    print("  --playwright      用 Playwright 真点视频建立 ananas 会话(chaoxing)")
    print("  --debug           打印调试信息")
    print("  --cpi <数字>      手动指定 cpi(chaoxing 专用)")
    print()
    print(f"示例: python -m scrape_new.workflows.{platform} 'URL' {default_output}")
