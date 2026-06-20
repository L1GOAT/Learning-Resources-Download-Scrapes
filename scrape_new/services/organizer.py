"""
自动归档模块

文件重命名、分类整理、课程视频按章节重命名。
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

from ..core.paths import sanitize_filename
from ..models import JobResult

logger = logging.getLogger(__name__)

# 内部文件前缀，归档时跳过
_INTERNAL_PREFIXES = ("_",)

# 报告文件名，归档时跳过
_REPORT_FILES = {
    "_report.json",
    "_download_log.csv",
    "_source_manifest.json",
    "_download_history.json",
}

# 哈希文件名模式
_HASH_PATTERN = re.compile(r'^[0-9a-f]{32}(\.[a-z0-9]+)?$', re.IGNORECASE)
_HASH_PATTERN_40 = re.compile(r'^[0-9a-f]{40}(\.[a-z0-9]+)?$', re.IGNORECASE)


def is_hash_name(name: str) -> bool:
    """
    判断是否为哈希文件名

    Args:
        name: 文件名

    Returns:
        是否为哈希文件名
    """
    return bool(_HASH_PATTERN.match(name) or _HASH_PATTERN_40.match(name))


def get_file_index(filename: str) -> int | None:
    """
    提取文件名前缀序号

    Args:
        filename: 文件名

    Returns:
        序号，无序号返回 None
    """
    match = re.match(r'^(\d{3})_', filename)
    if match:
        return int(match.group(1))
    return None


def _should_skip(filename: str) -> bool:
    """判断是否跳过该文件"""
    if filename in _REPORT_FILES:
        return True
    for prefix in _INTERNAL_PREFIXES:
        if filename.startswith(prefix):
            return True
    return False


def _handle_duplicate(target: Path) -> Path:
    """
    处理重名文件

    Args:
        target: 目标路径

    Returns:
        不重名的路径
    """
    if not target.exists():
        return target

    stem = target.stem
    suffix = target.suffix
    parent = target.parent

    counter = 1
    while True:
        new_name = f"{stem}_重复{counter}{suffix}"
        new_path = parent / new_name
        if not new_path.exists():
            return new_path
        counter += 1


def rename_course_videos(
    video_dir: Path,
    chapter_list: list[dict],
    course_name: str = "课程",
) -> dict:
    """
    课程视频按章节重命名

    Args:
        video_dir: 视频目录
        chapter_list: 章节目录列表
        course_name: 课程名称

    Returns:
        归档报告
    """
    report = {"renamed": 0, "warnings": [], "errors": []}

    if not video_dir.exists():
        report["errors"].append(f"目录不存在: {video_dir}")
        return report

    # 收集视频文件
    video_files = sorted([
        f for f in video_dir.iterdir()
        if f.is_file() and not _should_skip(f.name)
    ])

    if not video_files:
        report["warnings"].append("无视频文件")
        return report

    # 按章节重命名
    for i, chapter in enumerate(chapter_list, 1):
        chapter_name = chapter.get("name", f"第{i}章")
        videos = chapter.get("videos", [])

        for j, video_info in enumerate(videos, 1):
            video_name = video_info.get("name", "")
            video_index = video_info.get("index", 0)

            # 查找匹配的文件
            matched_file = None
            for f in video_files:
                file_index = get_file_index(f.name)
                if file_index == video_index:
                    matched_file = f
                    break

            if not matched_file:
                report["warnings"].append(f"未找到视频: {video_name}")
                continue

            # 生成新文件名
            new_name = f"{i:02d}_{j:02d}_{sanitize_filename(chapter_name)}_{sanitize_filename(video_name)}{matched_file.suffix}"
            new_path = video_dir / new_name

            # 处理重名
            new_path = _handle_duplicate(new_path)

            try:
                matched_file.rename(new_path)
                report["renamed"] += 1
                logger.debug(f"重命名: {matched_file.name} -> {new_path.name}")
            except Exception as e:
                report["errors"].append(f"重命名失败: {matched_file.name}: {e}")

    return report


def rename_files_clearly(
    file_dir: Path,
    name_list: list[dict] | None = None,
    prefix: str = "",
) -> dict:
    """
    清晰重命名文件

    Args:
        file_dir: 文件目录
        name_list: 名称列表 [{"index": 1, "name": "xxx"}, ...]
        prefix: 文件名前缀

    Returns:
        归档报告
    """
    report = {"renamed": 0, "warnings": [], "errors": []}

    if not file_dir.exists():
        report["errors"].append(f"目录不存在: {file_dir}")
        return report

    # 收集文件
    files = sorted([
        f for f in file_dir.iterdir()
        if f.is_file() and not _should_skip(f.name)
    ])

    if not files:
        report["warnings"].append("无文件")
        return report

    # 如果有名称列表，按名称重命名
    if name_list:
        for item in name_list:
            index = item.get("index", 0)
            name = item.get("name", "")

            # 查找匹配的文件
            matched_file = None
            for f in files:
                file_index = get_file_index(f.name)
                if file_index == index:
                    matched_file = f
                    break

            if not matched_file:
                report["warnings"].append(f"未找到文件: index={index}")
                continue

            # 生成新文件名
            new_name = f"{index:03d}_{sanitize_filename(name)}{matched_file.suffix}"
            if prefix:
                new_name = f"{prefix}_{new_name}"
            new_path = file_dir / new_name

            # 处理重名
            new_path = _handle_duplicate(new_path)

            try:
                matched_file.rename(new_path)
                report["renamed"] += 1
            except Exception as e:
                report["errors"].append(f"重命名失败: {matched_file.name}: {e}")
    else:
        # 按序号重命名
        for i, f in enumerate(files, 1):
            if is_hash_name(f.stem):
                new_name = f"{i:03d}_file{f.suffix}"
                if prefix:
                    new_name = f"{prefix}_{new_name}"
                new_path = file_dir / new_name

                # 处理重名
                new_path = _handle_duplicate(new_path)

                try:
                    f.rename(new_path)
                    report["renamed"] += 1
                except Exception as e:
                    report["errors"].append(f"重命名失败: {f.name}: {e}")

    return report


def organize_files(
    source_dir: Path,
    output_dir: Path,
    course_name: str,
    file_type: str = "video",
) -> dict:
    """
    整理文件到分类目录

    Args:
        source_dir: 源目录
        output_dir: 输出目录
        course_name: 课程名称
        file_type: 文件类型

    Returns:
        归档报告
    """
    report = {"moved": 0, "warnings": [], "errors": []}

    if not source_dir.exists():
        report["errors"].append(f"源目录不存在: {source_dir}")
        return report

    # 创建输出目录
    type_dir = output_dir / file_type
    type_dir.mkdir(parents=True, exist_ok=True)

    # 移动文件
    for f in source_dir.iterdir():
        if not f.is_file():
            continue
        if _should_skip(f.name):
            continue

        target = type_dir / f.name
        target = _handle_duplicate(target)

        try:
            f.rename(target)
            report["moved"] += 1
        except Exception as e:
            report["errors"].append(f"移动失败: {f.name}: {e}")

    return report


def organize_course(
    download_dir: Path,
    course_name: str,
    chapter_list: list[dict] | None = None,
) -> dict:
    """
    整理课程下载

    Args:
        download_dir: 下载目录
        course_name: 课程名称
        chapter_list: 章节目录

    Returns:
        归档报告
    """
    report = {"chapters": {}, "warnings": [], "errors": []}

    if not download_dir.exists():
        report["errors"].append(f"目录不存在: {download_dir}")
        return report

    # 如果有章节目录，按章节重命名
    if chapter_list:
        video_report = rename_course_videos(download_dir, chapter_list, course_name)
        report["chapters"]["video"] = video_report
    else:
        # 简单重命名
        rename_report = rename_files_clearly(download_dir, prefix=course_name)
        report["chapters"]["files"] = rename_report

    return report


def auto_organize_job(
    result: JobResult,
    course_name: str = "",
    chapter_list: list[dict] | None = None,
) -> dict:
    """
    自动归档任务结果

    Args:
        result: 任务结果
        course_name: 课程名称
        chapter_list: 章节目录

    Returns:
        归档报告
    """
    report = {"warnings": [], "errors": []}

    if not result.output_dir:
        report["warnings"].append("无输出目录")
        return report

    output_dir = Path(result.output_dir)

    # 根据意图选择归档策略
    if result.intent == "video" and chapter_list:
        course_report = organize_course(output_dir, course_name, chapter_list)
        report.update(course_report)
    else:
        rename_report = rename_files_clearly(output_dir)
        report.update(rename_report)

    return report