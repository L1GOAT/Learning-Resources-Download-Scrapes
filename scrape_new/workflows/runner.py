"""
平台工作流统一入口

提供 run_platform_workflow() 函数，由 cli.py 调用。
不反向依赖 cli.py。
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

logger = logging.getLogger(__name__)

# 支持的平台列表
PLATFORMS = {
    "chaoxing": "超星学习通",
    "zhihuishu": "智慧树/知到",
    "xuetangx": "学堂在线",
    "icourse163": "中国大学MOOC",
}


def run_platform_workflow(
    platform: str,
    url: str,
    output_dir: Path | None = None,
    config_path: Path | None = None,
) -> int:
    """
    运行平台工作流

    Args:
        platform: 平台名称（chaoxing/zhihuishu/xuetangx/icourse163）
        url: 课程 URL
        output_dir: 输出目录
        config_path: 配置文件路径（暂未使用，预留）

    Returns:
        退出码：0=成功，1=失败
    """
    platform = platform.lower().strip()

    if platform not in PLATFORMS:
        logger.error(f"不支持的平台: {platform}，支持: {', '.join(PLATFORMS.keys())}")
        return 1

    # 构造 sys.argv 给工作流模块使用
    # 工作流模块的 main() 从 sys.argv 读取参数
    argv_backup = sys.argv[:]
    sys.argv = [f"workflows/{platform}.py", url]
    if output_dir:
        sys.argv.append(str(output_dir))

    try:
        if platform == "chaoxing":
            from .chaoxing import main as workflow_main
        elif platform == "zhihuishu":
            from .zhihuishu import main as workflow_main
        elif platform == "xuetangx":
            from .xuetangx import main as workflow_main
        elif platform == "icourse163":
            from .icourse163 import main as workflow_main
        else:
            return 1

        workflow_main()
        return 0

    except SystemExit as e:
        return e.code if isinstance(e.code, int) else 1
    except ImportError as e:
        logger.error(f"平台模块导入失败: {e}")
        return 1
    except Exception as e:
        logger.error(f"平台工作流执行失败: {e}")
        return 1
    finally:
        sys.argv = argv_backup


def list_platforms() -> list[str]:
    """列出所有支持的平台"""
    return list(PLATFORMS.keys())