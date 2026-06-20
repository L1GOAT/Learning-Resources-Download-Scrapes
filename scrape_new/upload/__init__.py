"""
老师后台课程自动搭建工具(next-studio.xuetangx.com)

子模块:
  models       - 不可变数据模型(Chapter / Lesson / Asset / CourseStructure / UploadResult)
  mapping      - 子流程 A:解析 .doc / _chapter_outline.json → _mapping.json
  outline      - _chapter_outline.json 统一格式(workflow 写 + mapping 读)
  api_uploader - 子流程 B:纯 requests 调老师后台 API,4 步建课
  cli          - argparse 入口,默认走 api_uploader 路径
  report       - 日志/清单/报告(CSV + JSON)

设计原则:
  - 复用 scrape.core(load_cookies)和 scrape.organizer(sanitize / get_file_index)
  - 子流程 A(mapping)和子流程 B(api_uploader)解耦,中间靠 _mapping.json 通信
  - 不可变数据 + 显式 status 字段,避免隐藏副作用
  - Cookie 默认 in-memory,不落盘(用 --cookies-string 或 XTBZ_COOKIE 环境变量)

入口:
  python -m scrape.upload build-mapping --videos ... --doc ...
  python -m scrape.upload upload --mapping ... --cookies-string ... [--verify-only|--dry-run]
"""
from .models import (
    Chapter,
    Lesson,
    Asset,
    CourseStructure,
    UploadResult,
    ContentType,
    AssetStatus,
    MatchConfidence,
)
from .naming import (
    format_chapter_title,
    lesson_filename,
    lesson_leaf_name,
    is_cjk_text,
    build_lesson_filenames,
)
from .sync_tree import (
    compute_diff,
    write_backup_snapshot,
    TreeDiff,
    ChapterDiff,
    LessonDiff,
    LeafDiff,
    DiffAction,
)

__all__ = [
    # models
    "Chapter",
    "Lesson",
    "Asset",
    "CourseStructure",
    "UploadResult",
    "ContentType",
    "AssetStatus",
    "MatchConfidence",
    # naming
    "format_chapter_title",
    "lesson_filename",
    "lesson_leaf_name",
    "is_cjk_text",
    "build_lesson_filenames",
    # sync_tree
    "compute_diff",
    "write_backup_snapshot",
    "TreeDiff",
    "ChapterDiff",
    "LessonDiff",
    "LeafDiff",
    "DiffAction",
]
