"""
冒烟测试脚本

快速验证项目基本功能。不访问公网，不真实下载。
"""

import subprocess
import sys
from pathlib import Path

# 确保项目根目录在 sys.path 中
_project_root = Path(__file__).parent.parent.parent
if str(_project_root) not in sys.path:
    sys.path.insert(0, str(_project_root))


def run_command(cmd: list[str], description: str) -> bool:
    """运行命令并检查结果"""
    print(f"\n{'='*50}")
    print(f"测试: {description}")
    print(f"命令: {' '.join(cmd)}")
    print('='*50)

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        if result.returncode == 0:
            print("[OK] 成功")
            if result.stdout:
                print(result.stdout[:500])
            return True
        else:
            print(f"[FAIL] 失败 (退出码: {result.returncode})")
            if result.stderr:
                print(result.stderr[:500])
            return False
    except subprocess.TimeoutExpired:
        print("[FAIL] 超时")
        return False
    except Exception as e:
        print(f"[FAIL] 异常: {e}")
        return False


def check_imports() -> bool:
    """检查核心模块是否可导入"""
    print(f"\n{'='*50}")
    print("测试: 模块导入检查")
    print('='*50)

    modules = [
        "scrape_new",
        "scrape_new.core",
        "scrape_new.core.session",
        "scrape_new.core.cookies",
        "scrape_new.core.downloader",
        "scrape_new.core.hls",
        "scrape_new.core.verifier",
        "scrape_new.core.paths",
        "scrape_new.core.blockers",
        "scrape_new.extractors",
        "scrape_new.extractors.video",
        "scrape_new.extractors.image",
        "scrape_new.extractors.document",
        "scrape_new.services",
        "scrape_new.services.history",
        "scrape_new.services.reporter",
        "scrape_new.services.organizer",
        "scrape_new.workflows",
        "scrape_new.workflows.runner",
        "scrape_new.upload",
        "scrape_new.upload.runner",
    ]

    failed = []
    for mod in modules:
        try:
            __import__(mod)
            print(f"  [OK] {mod}")
        except ImportError as e:
            print(f"  [FAIL] {mod}: {e}")
            failed.append(mod)

    if failed:
        print(f"\n[FAIL] {len(failed)} 个模块导入失败")
        return False
    print(f"\n[OK] 全部 {len(modules)} 个模块导入成功")
    return True


def check_config() -> bool:
    """检查配置文件是否可加载"""
    print(f"\n{'='*50}")
    print("测试: 配置文件加载")
    print('='*50)

    try:
        from scrape_new.config import load_config
        from pathlib import Path

        config_path = Path(__file__).parent.parent / "config.example.json"
        if not config_path.exists():
            print(f"[FAIL] 配置文件不存在: {config_path}")
            return False

        config = load_config(config_path)
        print(f"[OK] 配置加载成功: max_retries={config.max_retries}, timeout={config.timeout}")
        return True
    except Exception as e:
        print(f"[FAIL] 配置加载失败: {e}")
        return False


def main():
    """运行冒烟测试"""
    print("网页资源扒取工具箱 v0.2.0 - 冒烟测试")
    print("="*50)

    results = []

    # 1. 模块导入检查
    results.append(check_imports())

    # 2. 配置文件检查
    results.append(check_config())

    # 3. 编译检查
    results.append(run_command(
        [sys.executable, "-m", "compileall", "scrape_new", "-q"],
        "编译检查"
    ))

    # 4. 运行测试
    results.append(run_command(
        [sys.executable, "-m", "pytest", "scrape_new/tests/", "-q", "--tb=no"],
        "单元测试"
    ))

    # 5. CLI 帮助
    results.append(run_command(
        [sys.executable, "-m", "scrape_new", "--help"],
        "CLI 帮助"
    ))

    # 6. CLI 版本
    results.append(run_command(
        [sys.executable, "-m", "scrape_new", "--version"],
        "CLI 版本"
    ))

    # 7. platform 子命令帮助
    results.append(run_command(
        [sys.executable, "-m", "scrape_new", "platform", "--help"],
        "platform 子命令帮助"
    ))

    # 8. upload 子命令帮助
    results.append(run_command(
        [sys.executable, "-m", "scrape_new", "upload", "--help"],
        "upload 子命令帮助"
    ))

    # 汇总结果
    print("\n" + "="*50)
    print("测试汇总")
    print("="*50)
    passed = sum(results)
    total = len(results)
    print(f"通过: {passed}/{total}")

    if passed == total:
        print("\n[OK] 所有冒烟测试通过！")
        return 0
    else:
        print("\n[FAIL] 部分测试失败")
        return 1


if __name__ == "__main__":
    sys.exit(main())