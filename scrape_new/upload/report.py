"""
报告层：_upload_log.csv (UTF-8-BOM) + _upload_manifest.json + _upload_report.json

设计：
  - 日志是 append-only 的 csv，每行一个 asset
  - manifest 是 full state，可以 re-load 来 resume
  - report 是 final summary，遵循项目"差额必须为 0"规则

复用：
  - 沿用 scrape.core.save_report() 的 JSON 风格
  - CSV 用 utf-8-sig 编码，Excel 可直接打开
"""

from __future__ import annotations

import csv
import json
import logging
import os
import tempfile
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Iterable

from .models import (
    Asset,
    AssetStatus,
    CourseStructure,
    UploadResult,
)

logger = logging.getLogger(__name__)


# ─── CSV 日志 ────────────────────────────────────────────────────

CSV_FIELDNAMES = [
    "timestamp",
    "course_id",
    "chapter",
    "lesson_id",
    "lesson_title",
    "content_type",
    "source_path",
    "target_url",
    "status",
    "attempts",
    "bytes",
    "error",
]


def append_log_row(csv_path: Path, asset: Asset, course_id: str) -> None:
    """append 一行到 _upload_log.csv（带表头检查）"""
    csv_path = Path(csv_path)
    is_new = not csv_path.exists()
    with open(csv_path, "a", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_FIELDNAMES)
        if is_new:
            writer.writeheader()
        writer.writerow({
            "timestamp": datetime.now().isoformat(timespec="seconds"),
            "course_id": course_id,
            "chapter": asset.chapter_index,
            "lesson_id": asset.lesson_id,
            "lesson_title": asset.lesson_title,
            "content_type": asset.content_type.value,
            "source_path": asset.source_path or "",
            "target_url": asset.target_url or "",
            "status": asset.status.value,
            "attempts": asset.attempts,
            "bytes": asset.bytes_uploaded,
            "error": asset.error or "",
        })


# ─── Manifest ────────────────────────────────────────────────────

def save_manifest(
    result: UploadResult,
    out_path: Path,
) -> Path:
    """原子写 _upload_manifest.json（.tmp → os.replace）"""
    out_path = Path(out_path)
    tmp_path = out_path.with_suffix(out_path.suffix + ".tmp")
    payload = {
        "course_id": result.course_id,
        "course_title": result.course_title,
        "started_at": result.started_at,
        "finished_at": result.finished_at,
        "assets": [
            {
                **{k: v for k, v in asdict(a).items()},
                "content_type": a.content_type.value,
                "status": a.status.value,
            }
            for a in result.assets
        ],
    }
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2, default=str)
    os.replace(tmp_path, out_path)
    logger.info(f"已保存 manifest: {out_path}")
    return out_path


def load_manifest(path: Path) -> UploadResult | None:
    """从 _upload_manifest.json 恢复 UploadResult，用于 resume。

    Note: 必须读取 resource_key 字段(2026-06-17 修)。
    旧 manifest 没这个字段时,默认 ""(向后兼容),resume 不会跳过。
    """
    path = Path(path)
    if not path.exists():
        return None
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    from .models import ContentType
    assets = tuple(
        Asset(
            chapter_index=a["chapter_index"],
            lesson_id=a["lesson_id"],
            lesson_title=a["lesson_title"],
            content_type=ContentType(a["content_type"]),
            source_path=a.get("source_path"),
            target_url=a.get("target_url"),
            status=AssetStatus(a["status"]),
            attempts=a.get("attempts", 0),
            bytes_uploaded=a.get("bytes_uploaded", 0),
            error=a.get("error"),
            uploaded_at=a.get("uploaded_at"),
            # 关键:读 resource_key。旧 manifest 没这字段时返 ""(向后兼容)
            resource_key=a.get("resource_key", ""),
        )
        for a in data.get("assets", [])
    )
    return UploadResult(
        course_id=data.get("course_id", ""),
        course_title=data.get("course_title", ""),
        started_at=data.get("started_at", ""),
        finished_at=data.get("finished_at"),
        assets=assets,
    )


# ─── Report ──────────────────────────────────────────────────────

def write_report(
    result: UploadResult,
    out_path: Path,
    structure: CourseStructure | None = None,
) -> Path:
    """写 _upload_report.json (final summary)"""
    out_path = Path(out_path)
    counts = result.count_by_status()
    payload = {
        "course_id": result.course_id,
        "course_title": result.course_title,
        "started_at": result.started_at,
        "finished_at": result.finished_at,
        "discovered": sum(counts.values()),
        "succeeded": counts["ok"],
        "failed": counts["failed"],
        "skipped": counts["skipped"],
        "suspicious": counts["suspicious"],
        "pending": counts["pending"],
        "delta": result.delta(),  # 0 = success by project rule
    }
    if structure is not None:
        payload["chapters_total"] = len(structure.chapters)
        payload["lessons_total"] = sum(len(c.lessons) for c in structure.chapters)
        payload["lessons_with_video"] = len(structure.lessons_with_video())
        payload["lessons_missing_video"] = len(structure.missing_video_lessons())
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2, default=str)
    logger.info(f"已保存 report: {out_path}")
    return out_path


# ─── 打印给用户看的总结 ────────────────────────────────────────

def print_summary(result: UploadResult) -> None:
    """控制台打印结果(中文版,遵循项目 CLAUDE.md 的报告风格)

    分流:
      - mode = "plan_only" / "plan_first" / "apply_plan"(只生成 plan,没真上传)
        → "计划生成完成,未执行上传" + plan JSON 路径
      - mode = "verify_only" / "dry_run"
        → "verify-only 模式,未执行上传"
      - mode = "upload"(实际跑过写 API)
        → 原"老师后台上传完成!"(assets 决定 OK/FAIL/SKIP 数)
    """
    mode = getattr(result, "mode", "upload")

    if mode in ("plan_only", "plan_first"):
        print("=" * 60)
        print("计划生成完成,未执行上传。")
        print("  请 review _upload_plan.md 后使用:")
        print("    - --apply-plan <path>  校验后执行")
        print("    - --yes                 跳过 review 直接执行")
        print(f"  课程: {result.course_title or result.course_id}")
        print(f"  课程ID: {result.course_id}")
        if result.finished_at:
            print(f"  结束时间: {result.finished_at}")
        print("=" * 60)
        return

    if mode in ("verify_only", "dry_run"):
        print("=" * 60)
        print("verify-only 模式,未执行上传。")
        print(f"  课程: {result.course_title or result.course_id}")
        print(f"  课程ID: {result.course_id}")
        if result.finished_at:
            print(f"  结束时间: {result.finished_at}")
        print("=" * 60)
        return

    # 正常 upload 模式
    counts = result.count_by_status()
    print("=" * 60)
    print("老师后台上传完成!")
    print(f"  课程: {result.course_title or result.course_id}")
    print(f"  课程ID: {result.course_id}")
    print(f"  发现: {sum(counts.values())}")
    print(f"  成功: {counts['ok']}")
    print(f"  失败: {counts['failed']}")
    print(f"  跳过: {counts['skipped']}")
    print(f"  可疑: {counts['suspicious']}")
    print(f"  待处理: {counts['pending']}")
    delta = result.delta()
    if delta == 0:
        print("  差额: 0 ✓")
    else:
        print(f"  差额: {delta} ⚠ (待处理资产未完成)")
    if result.finished_at:
        print(f"  结束时间: {result.finished_at}")
    print("=" * 60)


# ─── 失败重试资源清单(_retry_resources.json) ───────────────

RETRYABLE_STATUSES = {AssetStatus.FAILED, AssetStatus.SUSPICIOUS}


def write_retry_resources(
    result: UploadResult,
    output_dir: Path,
) -> Path | None:
    """把 FAILED / SUSPICIOUS 资源写到 _retry_resources.json 的 `assets` 段;
    PENDING 资源(主要是 RENAME 待确认)单独写到 `pending_actions` 段。

    设计原因:
      - assets 段每条都有 resource_key,可以直接喂给 run_upload_api(retry_keys=...)
      - PENDING 资源(章节改名待确认)没 resource_key,塞进 assets 会变成空 key
        污染 retry 列表
      - pending_actions 让人工看清"还有几章改名没处理",不参与自动重试

    Returns:
        写入路径,或 None(没有需要重试的资源)
    """
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    retryable = tuple(
        a for a in result.assets if a.status in RETRYABLE_STATUSES
    )
    # PENDING(主要是 RENAME 待确认)不进 assets,单独 pending_actions
    pendings = tuple(
        a for a in result.assets
        if a.status == AssetStatus.PENDING
    )

    if not retryable and not pendings:
        return None

    path = output_dir / "_retry_resources.json"
    payload = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "course_id": result.course_id,
        "course_title": result.course_title,
        # 只有 FAILED/SUSPICIOUS 进 assets(可自动重试)
        "count": len(retryable),
        "assets": [
            {
                "resource_key": a.resource_key,
                "chapter_index": a.chapter_index,
                "lesson_id": a.lesson_id,
                "lesson_title": a.lesson_title,
                "content_type": a.content_type.value
                    if hasattr(a.content_type, "value") else str(a.content_type),
                "source_path": a.source_path,
                "status": a.status.value
                    if hasattr(a.status, "value") else str(a.status),
                "attempts": a.attempts,
                "error": a.error,
            }
            for a in retryable
        ],
        # PENDING 不进 assets(无 resource_key,自动 retry 跑不了)
        "pending_actions": [
            {
                "chapter_index": a.chapter_index,
                "lesson_id": a.lesson_id,
                "lesson_title": a.lesson_title,
                "kind": "rename_pending" if "rename_pending" in (a.error or "")
                        else "other_pending",
                "description": a.error or "",
            }
            for a in pendings
        ],
    }
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    logger.info(f"已写 _retry_resources.json: {path} ({len(retryable)} 条)")
    return path


def load_retry_resources(path: Path) -> dict[str, Any]:
    """读 _retry_resources.json,返回 {course_id, course_title, count, assets}。

    失败抛 FileNotFoundError / json.JSONDecodeError(由调用方处理)。
    """
    return json.loads(Path(path).read_text(encoding="utf-8"))
