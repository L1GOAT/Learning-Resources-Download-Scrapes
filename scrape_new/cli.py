"""
命令行接口

负责解析命令行参数和展示人类可读输出。
"""

from __future__ import annotations

import argparse
import sys
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