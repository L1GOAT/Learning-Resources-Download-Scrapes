"""
命令行入口

支持 python -m scrape 方式调用。
"""

from .cli import main

if __name__ == "__main__":
    raise SystemExit(main())