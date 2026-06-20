"""
测试路径常量与 helper —— 把测试里散落的 Path 拼接集中到一处。

Why:
  早期测试里多处硬编码 "E:/林视" 这种本地绝对路径,CI runner(linux/windows)
  上找不到,subprocess.run(cwd=...) 立即 FileNotFoundError,workflows/*.py
  静态读文件也直接炸。修法是把"项目根"和"常见子目录"集中到本模块,
  所有测试统一从这里 import,避免未来又长出新的硬编码路径。

用法:
  from scrape_new.tests._paths import (
      PROJECT_ROOT, WORKFLOWS_DIR, FIXTURES_DIR,
      workflow_path, fixture_path,
  )

  def test_xxx():
      p = workflow_path("chaoxing.py")
      text = p.read_text(encoding="utf-8")

跨平台:所有路径都是相对仓库根的 Path,不依赖任何环境变量。
"""

from __future__ import annotations

from pathlib import Path


# ─── 核心常量 ──────────────────────────────────────────

# 仓库根:scrape_new/tests/_paths.py → parents[0]=tests, [1]=scrape_new, [2]=repo root
PROJECT_ROOT: Path = Path(__file__).resolve().parents[2]

# 公开包根:scrape_new/
SCRAPE_NEW_ROOT: Path = PROJECT_ROOT / "scrape_new"

# 常用子目录
WORKFLOWS_DIR: Path = SCRAPE_NEW_ROOT / "workflows"
TESTS_DIR: Path = SCRAPE_NEW_ROOT / "tests"
FIXTURES_DIR: Path = TESTS_DIR / "fixtures"


# ─── helper 函数 ──────────────────────────────────────

def workflow_path(name: str) -> Path:
    """返回 scrape_new/workflows/<name> 路径。

    Args:
        name: 相对于 WORKFLOWS_DIR 的文件名(如 "chaoxing.py")。

    Returns:
        绝对路径,即使文件不存在也不抛错(由调用方判断)。
    """
    if not name:
        raise ValueError("workflow_path(name): name 不能为空")
    return WORKFLOWS_DIR / name


def fixture_path(rel: str) -> Path:
    """返回 scrape_new/tests/fixtures/<rel> 路径。

    Args:
        rel: 相对于 FIXTURES_DIR 的路径,支持子目录(如
            "course_audit_demo/_chapter_tree.json")。

    Returns:
        绝对路径。
    """
    if not rel:
        raise ValueError("fixture_path(rel): rel 不能为空")
    return FIXTURES_DIR / rel


__all__ = [
    "PROJECT_ROOT",
    "SCRAPE_NEW_ROOT",
    "WORKFLOWS_DIR",
    "TESTS_DIR",
    "FIXTURES_DIR",
    "workflow_path",
    "fixture_path",
]