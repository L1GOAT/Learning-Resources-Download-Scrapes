"""
平台特定工作流

提供各平台课程视频一键下载功能。

设计:
  - 不在包初始化时 import 4 个 workflow(避免 `python -m scrape_new.workflows.X` 触发 RuntimeWarning)
  - 用户需要时显式 import,或在 main() 入口里按需加载
  - __all__ 列出可用的子模块名(IDE 提示用)

为什么懒加载:
  - `python -m scrape_new.workflows.chaoxing` 会先 import 整个 `workflows` 包,
    再 import chaoxing 子模块 — 如果 __init__.py 已经 `from .chaoxing import main`,
    chaoxing 在 sys.modules 里就有两份"残留",Python 报 RuntimeWarning
  - 懒加载让 __init__.py 只声明 __all__,不实际 import
"""

__all__ = [
    "chaoxing",
    "zhihuishu",
    "xuetangx",
    "icourse163",
    "cli_args",
]