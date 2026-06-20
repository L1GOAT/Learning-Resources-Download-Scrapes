"""
下载侧资源审计模块 — 下载完成后输出 4 个文件,方便人工核对每个
章节/课时/视频/PPT 的最终命名 + 状态。

输出 4 个文件(写在视频输出目录,即 video_dir 的同级):
  _chapter_tree.json                 — 章节树 JSON(每个 lesson 带 resources 列表)
  _chapter_tree.md                   — 章节树 Markdown(人看)
  _resource_naming_manifest.json     — 资源命名清单 JSON(每条一行)
  _resource_naming_manifest.csv      — 资源命名清单 CSV(UTF-8-BOM,Excel 可开)

设计:
  - 协议统一:workflow 把 all_videos / all_docs 字典按下面约定填字段,
    调 build_chapter_tree_data(...) 就生成完整 dict,再调写文件的 4 个函数之一。
  - 不破坏 _chapter_outline.json:后者是 upload 侧用的"章节目录桥接",
    这里输出的是"下载后审计"——给用户眼看,跟 upload 走完全不同的路径。
  - 状态统一:downloaded / skipped_existing / failed / suspicious
  - 命名映射:original_name / saved_name / relative_path 三个字段,
    同 lesson 多视频自动 _2/_3 时,saved_name 必须落具体最终名(不只存规则)。
"""

from __future__ import annotations

import csv
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


# ─── 状态常量 ─────────────────────────────────────────────────────

STATUS_DOWNLOADED = "downloaded"
STATUS_SKIPPED_EXISTING = "skipped_existing"
STATUS_FAILED = "failed"
STATUS_SUSPICIOUS = "suspicious"

ALL_STATUSES = {
    STATUS_DOWNLOADED,
    STATUS_SKIPPED_EXISTING,
    STATUS_FAILED,
    STATUS_SUSPICIOUS,
}


# ─── 内部:role 推断 ─────────────────────────────────────────────

def _role_from_ext(ext: str) -> str:
    """扩展名 → role(附件类)"""
    e = (ext or "").lower().lstrip(".")
    return {
        "pptx": "ppt", "ppt": "ppt",
        "pdf": "pdf",
        "docx": "docx", "doc": "doc",
        "mp4": "video", "flv": "video",
        "avi": "video", "mkv": "video", "mov": "video",
    }.get(e, "attachment")


# ─── build_chapter_tree_data ────────────────────────────────────

def build_chapter_tree_data(
    course_title: str,
    platform: str,
    source_url: str,
    all_videos: list[dict[str, Any]],
    all_docs: list[dict[str, Any]],
    *,
    lessons_meta: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """从 workflow 已收集的 all_videos / all_docs 构造章节树 dict。

    每个 video / doc 字典应该至少有这些字段(workflow 填好):
      - ch_num: int                章序号
      - ls_num: int                课时序号(本课时内 1-based)
      - chapter: str               章名(原始)
      - lesson: str                节名(原始)
      - name: str                  平台原始文件名(英文 / 中文)
      - filename: str | None       保存文件名(workflow 算好的,None = 未下载)
      - role: str                  "video" / "english" / "ppt" / "pdf" / ...
      - status: str                downloaded / skipped_existing / failed / suspicious
      - size_bytes: int            文件大小(0 = 未知)
      - reason: str                状态原因
      - source_meta: dict          objectid / knowledge_id / tab_num / url

    lessons_meta: 可选,如果 workflow 知道 lesson 的额外信息(原始标题、URL 等),
                  提供后写入 lesson dict,即使这节课没有任何资源也能保留 entry。
                  每项: {ch_num, ls_num, chapter, lesson, knowledge_id?, url?}

    返回 dict(JSON 可序列化):
      {
        "course_title": ...,
        "platform": ...,
        "source_url": ...,
        "generated_at": ISO8601,
        "chapters": [
          {"index": 1, "title": "...", "lessons": [
            {"id": "1.1", "title": "...", "resources": [
              {"role", "original_name", "saved_name", "relative_path",
               "status", "size_bytes", "reason", "source_meta"}
            ]}
          ]}
        ]
      }
    """
    # 按 (ch_num, ls_num) 分桶
    bucket: dict[tuple[int, int], dict[str, Any]] = {}

    def _ensure_lesson(ch_num: int, ls_num: int, chapter: str, lesson: str) -> dict[str, Any]:
        key = (ch_num, ls_num)
        if key not in bucket:
            bucket[key] = {
                "id": f"{ch_num}.{ls_num}",
                "title": lesson,
                "_chapter_raw": chapter,
                "resources": [],
            }
        elif chapter and not bucket[key].get("_chapter_raw"):
            bucket[key]["_chapter_raw"] = chapter
        return bucket[key]

    for v in all_videos:
        ch_num = int(v.get("ch_num") or 0)
        ls_num = int(v.get("ls_num") or 0)
        ls = _ensure_lesson(
            ch_num, ls_num,
            v.get("chapter", ""), v.get("lesson", ""),
        )
        ls["resources"].append(_build_resource_record(v, kind_dir="video"))

    for d in all_docs:
        ch_num = int(d.get("ch_num") or 0)
        ls_num = int(d.get("ls_num") or 0)
        ls = _ensure_lesson(
            ch_num, ls_num,
            d.get("chapter", ""), d.get("lesson", ""),
        )
        ls["resources"].append(_build_resource_record(d, kind_dir="doc"))

    # 即使没有资源的 lesson 也要从 lessons_meta 留个 entry
    for lm in lessons_meta or []:
        ch_num = int(lm.get("ch_num") or 0)
        ls_num = int(lm.get("ls_num") or 0)
        _ensure_lesson(
            ch_num, ls_num,
            lm.get("chapter", ""), lm.get("lesson", ""),
        )

    # 按 (ch_num, ls_num) 分组到章
    chapters_map: dict[int, dict[str, Any]] = {}
    for (ch_num, ls_num), ls_data in bucket.items():
        ch_raw = ls_data.pop("_chapter_raw", "")
        ch = chapters_map.setdefault(ch_num, {
            "index": ch_num,
            "title": ch_raw or f"第{ch_num}章",
            "lessons": [],
        })
        # 如果 lesson 没有 chapter 上下文,从 lessons_meta 借
        if not ch["title"] or ch["title"].startswith("第") and ch_raw:
            ch["title"] = ch_raw
        ch["lessons"].append(ls_data)

    chapters = []
    for ch_num in sorted(chapters_map.keys()):
        ch = chapters_map[ch_num]
        # lesson 内按 (ch_num, ls_num) 排序
        ch["lessons"].sort(key=lambda x: int(x["id"].split(".")[1]))
        chapters.append(ch)

    return {
        "course_title": course_title or "",
        "platform": platform or "",
        "source_url": source_url or "",
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "chapters": chapters,
    }


def _build_resource_record(item: dict[str, Any], *, kind_dir: str) -> dict[str, Any]:
    """把单个 video/doc dict 包装成 resource record。"""
    role = item.get("role") or _role_from_ext(
        Path(str(item.get("filename", ""))).suffix or item.get("type", "")
    )
    saved_name = item.get("filename") or item.get("saved_name") or ""
    # 相对路径:video -> "视频/<name>" / doc -> "文档/<name>"
    rel_dir = "视频" if kind_dir == "video" else "文档"
    rel_path = f"{rel_dir}/{saved_name}" if saved_name else ""
    ext = Path(saved_name).suffix.lstrip(".").lower() if saved_name else ""

    status = item.get("status") or ""
    if status not in ALL_STATUSES:
        # 兜底:有 size_bytes 且 > 0 → downloaded,否则 failed
        if int(item.get("size_bytes") or 0) > 0:
            status = STATUS_DOWNLOADED
        else:
            status = STATUS_FAILED

    return {
        "role": role,
        "original_name": item.get("name") or item.get("original_name") or "",
        "saved_name": saved_name,
        "relative_path": rel_path,
        "extension": ext,
        "status": status,
        "size_bytes": int(item.get("size_bytes") or 0),
        "reason": item.get("reason") or "",
        "source_meta": _normalize_source_meta(item.get("source_meta")),
    }


def _normalize_source_meta(meta: Any) -> dict[str, Any]:
    """过滤 source_meta,只保留平台无关的有用字段。"""
    if not isinstance(meta, dict):
        return {}
    out: dict[str, Any] = {}
    # 这些字段对所有平台都有意义;其他(平台特定扩展字段)直接丢弃避免泄漏
    for key in ("objectid", "knowledge_id", "tab_num", "url", "mid", "jobid"):
        if key in meta and meta[key] not in (None, ""):
            out[key] = meta[key]
    return out


# ─── write_chapter_tree_json ─────────────────────────────────────

def write_chapter_tree_json(tree: dict[str, Any], output_dir: Path) -> Path:
    """写 _chapter_tree.json(完整结构)"""
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / "_chapter_tree.json"
    path.write_text(
        json.dumps(tree, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    logger.info(f"已写章节树 JSON: {path}")
    return path


# ─── write_chapter_tree_md ───────────────────────────────────────

def write_chapter_tree_md(tree: dict[str, Any], output_dir: Path) -> Path:
    """写 _chapter_tree.md(人类阅读版)。

    格式:
      # 课程名
      - 第一章 xxx
        - 1.1 小节名
          - [video] 视频/1.1_xxx.mp4 (downloaded, 123 MB)
          - [english] 视频/1.1_xxx_English.mp4 (downloaded, 98 MB)
          - [ppt] 文档/1.1_xxx_PPT.pptx (skipped_existing, 2 MB)
    """
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    lines: list[str] = []
    title = tree.get("course_title") or "课程"
    lines.append(f"# {title}")
    if tree.get("platform"):
        lines.append("")
        lines.append(f"> 平台:{tree['platform']} | 生成时间:{tree.get('generated_at','')}")
    if tree.get("source_url"):
        lines.append(f"> 来源:[链接]({tree['source_url']})")

    for ch in tree.get("chapters", []):
        lines.append("")
        lines.append(f"- {ch['title']}")
        for ls in ch.get("lessons", []):
            ls_id = ls.get("id", "")
            ls_title = ls.get("title", "")
            lines.append(f"  - {ls_id} {ls_title}".rstrip())
            for r in ls.get("resources", []):
                role = r.get("role", "")
                saved = r.get("saved_name") or "(未保存)"
                rel = r.get("relative_path") or ""
                status = r.get("status", "")
                size = int(r.get("size_bytes") or 0)
                size_str = _format_size(size)
                reason = r.get("reason") or ""
                reason_part = f", {reason}" if reason else ""
                lines.append(
                    f"    - [{role}] {rel or saved} ({status}, {size_str}{reason_part})"
                )

    path = output_dir / "_chapter_tree.md"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    logger.info(f"已写章节树 Markdown: {path}")
    return path


def _format_size(size_bytes: int) -> str:
    if size_bytes <= 0:
        return "0 B"
    if size_bytes < 1024:
        return f"{size_bytes} B"
    if size_bytes < 1024 * 1024:
        return f"{size_bytes / 1024:.1f} KB"
    if size_bytes < 1024 * 1024 * 1024:
        return f"{size_bytes / (1024 * 1024):.1f} MB"
    return f"{size_bytes / (1024 * 1024 * 1024):.2f} GB"


# ─── write_resource_naming_manifest (JSON / CSV) ────────────────

def build_resource_naming_records(
    all_videos: list[Any],
    all_docs: list[Any],
) -> list[dict[str, Any]]:
    """从 all_videos / all_docs 构造扁平 record 列表(每条对应一个资源)。

    接受 Asset(dataclass)和 dict 两种 item 类型 — workflow 给 dict,代码里测试给 Asset。

    Record 字段:
      chapter_index, chapter_title, lesson_id, lesson_title,
      role, original_name, saved_name, relative_path, extension,
      status, size_bytes, reason, source_meta, resource_key
    """
    records: list[dict[str, Any]] = []

    def _as_dict(item: Any) -> dict[str, Any]:
        if isinstance(item, dict):
            return item
        from dataclasses import asdict
        return asdict(item)

    def _to_record(item: Any, kind_dir: str) -> dict[str, Any]:
        d = _as_dict(item)
        role = d.get("role") or _role_from_ext(
            Path(str(d.get("filename", ""))).suffix or d.get("type", "")
        )
        saved_name = d.get("filename") or d.get("saved_name") or ""
        rel_dir = "视频" if kind_dir == "video" else "文档"
        rel_path = f"{rel_dir}/{saved_name}" if saved_name else ""
        ext = Path(saved_name).suffix.lstrip(".").lower() if saved_name else ""
        status = d.get("status") or ""
        if status not in ALL_STATUSES:
            if int(d.get("size_bytes") or 0) > 0:
                status = STATUS_DOWNLOADED
            else:
                status = STATUS_FAILED
        # Asset 用 chapter_index / lesson_index;dict 用 ch_num / ls_num
        ch_idx = d.get("chapter_index", d.get("ch_num", 0))
        ls_idx = d.get("lesson_index", d.get("ls_num", 0))
        return {
            "chapter_index": int(ch_idx or 0),
            "chapter_title": d.get("chapter_title") or d.get("chapter", "") or "",
            "lesson_id": d.get("lesson_id") or f"{int(ch_idx or 0)}.{int(ls_idx or 0)}",
            "lesson_title": d.get("lesson_title") or d.get("lesson", "") or "",
            "role": role,
            "original_name": d.get("original_name") or d.get("name", "") or "",
            "saved_name": saved_name,
            "relative_path": rel_path,
            "extension": ext,
            "status": status,
            "size_bytes": int(d.get("size_bytes") or 0),
            "reason": d.get("reason") or "",
            "source_meta": _normalize_source_meta(d.get("source_meta")),
            "resource_key": d.get("resource_key", ""),
            # 透传 tab_failed(下载侧 flag,影响 retry 过滤)
            "tab_failed": bool(d.get("tab_failed", False)),
        }

    for v in all_videos:
        records.append(_to_record(v, kind_dir="video"))
    for d in all_docs:
        records.append(_to_record(d, kind_dir="doc"))

    # 按 (chapter_index, lesson_id, role, original_name) 稳定排序
    records.sort(key=lambda r: (
        r["chapter_index"],
        r["lesson_id"],
        r["role"],
        r["original_name"],
    ))
    return records


def write_resource_naming_manifest_json(
    records: list[dict[str, Any]],
    output_dir: Path,
    *,
    meta: dict[str, Any] | None = None,
) -> Path:
    """写 _resource_naming_manifest.json。meta 可放 course_title / platform 等。"""
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    payload: dict[str, Any] = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "count": len(records),
        "records": records,
    }
    if meta:
        payload["meta"] = meta
    path = output_dir / "_resource_naming_manifest.json"
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    logger.info(f"已写命名清单 JSON: {path} ({len(records)} 条)")
    return path


def write_resource_naming_manifest_csv(
    records: list[dict[str, Any]],
    output_dir: Path,
) -> Path:
    """写 _resource_naming_manifest.csv(UTF-8-BOM,Excel 直接打开)。"""
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / "_resource_naming_manifest.csv"

    # 字段顺序固定;source_meta 在 CSV 里展平成 objectid/knowledge_id/tab_num/url
    fieldnames = [
        "chapter_index", "chapter_title",
        "lesson_id", "lesson_title",
        "role", "original_name", "saved_name",
        "relative_path", "extension",
        "status", "size_bytes", "reason",
        "resource_key",
        "source_meta_objectid", "source_meta_knowledge_id",
        "source_meta_tab_num", "source_meta_url",
    ]
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        for r in records:
            row = dict(r)
            sm = row.pop("source_meta", {}) or {}
            row["source_meta_objectid"] = sm.get("objectid", "")
            row["source_meta_knowledge_id"] = sm.get("knowledge_id", "")
            row["source_meta_tab_num"] = sm.get("tab_num", "")
            row["source_meta_url"] = sm.get("url", "")
            writer.writerow(row)
    logger.info(f"已写命名清单 CSV: {path} ({len(records)} 条)")
    return path


# ─── 一站式入口 ─────────────────────────────────────────────────

def write_download_resource_manifests(
    course_title: str,
    platform: str,
    source_url: str,
    all_videos: list[dict[str, Any]],
    all_docs: list[dict[str, Any]],
    output_dir: Path,
    *,
    lessons_meta: list[dict[str, Any]] | None = None,
) -> dict[str, Path]:
    """一站式写 5 个文件:章节树 (json+md) + 命名清单 (json+csv) + 验课 HTML。

    返回 {chapter_tree_json, chapter_tree_md, manifest_json, manifest_csv, review_html}。

    设计:即使中途抛异常,前面已写的 manifest 不影响(独立函数)。
    如果需要 try/finally 保护整个下载流程,workflow 端自己包。

    Note: review_html 是只读视图,失败(例如 review_html 自身 bug)不影响
    前面 4 个 manifest;反之亦然。各函数独立 try/except 由调用方控制。
    """
    output_dir = Path(output_dir)
    tree = build_chapter_tree_data(
        course_title=course_title,
        platform=platform,
        source_url=source_url,
        all_videos=all_videos,
        all_docs=all_docs,
        lessons_meta=lessons_meta,
    )
    records = build_resource_naming_records(all_videos, all_docs)

    paths = {
        "chapter_tree_json": write_chapter_tree_json(tree, output_dir),
        "chapter_tree_md": write_chapter_tree_md(tree, output_dir),
        "manifest_json": write_resource_naming_manifest_json(
            records, output_dir,
            meta={"course_title": course_title, "platform": platform,
                  "source_url": source_url},
        ),
        "manifest_csv": write_resource_naming_manifest_csv(records, output_dir),
    }

    # 验课 HTML(独立 try/except,失败不影响上面 4 个)
    try:
        # 局部 import 避免循环依赖(review_html 可能未来再依赖 resource_manifest)
        from .review_html import build_review_html
        paths["review_html"] = build_review_html(
            tree, records, output_dir,
            title=course_title,
        )
    except Exception as e:
        # 不让 review_html 失败阻断主流程
        logger.warning(f"写 _review.html 失败(前面 4 个已写好,继续): {e}")

    # _retry_downloads.json(下载侧失败重试清单,跟 upload 侧独立)
    try:
        retry_path = write_download_retry_manifest(records, output_dir)
        if retry_path:
            paths["retry_downloads"] = retry_path
    except Exception as e:
        logger.warning(f"写 _retry_downloads.json 失败(继续): {e}")

    return paths


# ─── 下载侧失败重试清单(_retry_downloads.json) ────────────

DOWNLOAD_RETRYABLE_STATUSES = {"failed", "suspicious"}


def write_download_retry_manifest(
    records: list[dict[str, Any]],
    output_dir: Path,
) -> Path | None:
    """写 _retry_downloads.json(下载侧失败重试清单)。

    严格过滤(R3):
      - 只含 FAILED / SUSPICIOUS 记录
      - 必须有 resource_key(没 key 自动 retry 跑不了)
      - role != "unknown"(unknown 不是"下载失败",是"识别不了",应进 pending_actions)
      - 不含 tab_failed 标记(整 tab 失败可能是限流,重下也无效,需人工看)

    剩余的"被过滤"记录进 `pending_actions` 字段(单独可观察,不污染自动 retry):
      - role=unknown(资源类型识别不出来,需人工分类)
      - tab_failed(tab 整体失败,可能是限流或登录过期,重下无效)
      - 没 resource_key(无法匹配)
    """
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    retryable: list[dict[str, Any]] = []
    pending: list[dict[str, Any]] = []
    for r in records:
        if r.get("status") not in DOWNLOAD_RETRYABLE_STATUSES:
            continue
        # 1) 必须有 resource_key
        if not r.get("resource_key"):
            pending.append({**r, "_pending_reason": "no_resource_key"})
            continue
        # 2) role=unknown 拒绝(无法重下,因为不知道 URL 怎么拼)
        if r.get("role") == "unknown":
            pending.append({**r, "_pending_reason": "role_unknown"})
            continue
        # 3) tab_failed 拒绝
        if r.get("tab_failed"):
            pending.append({**r, "_pending_reason": "tab_failed"})
            continue
        retryable.append(r)

    if not retryable and not pending:
        return None
    path = output_dir / "_retry_downloads.json"
    payload = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "count": len(retryable),
        "assets": [
            {
                "resource_key": r.get("resource_key", ""),
                "chapter_index": r.get("chapter_index", 0),
                "lesson_id": r.get("lesson_id", ""),
                "lesson_title": r.get("lesson_title", ""),
                "role": r.get("role", ""),
                "saved_name": r.get("saved_name", ""),
                "relative_path": r.get("relative_path", ""),
                "status": r.get("status", ""),
                "size_bytes": r.get("size_bytes", 0),
                "reason": r.get("reason", ""),
            }
            for r in retryable
        ],
        "pending_actions": [
            {
                "resource_key": r.get("resource_key", ""),
                "chapter_index": r.get("chapter_index", 0),
                "lesson_id": r.get("lesson_id", ""),
                "lesson_title": r.get("lesson_title", ""),
                "role": r.get("role", ""),
                "saved_name": r.get("saved_name", ""),
                "status": r.get("status", ""),
                "reason": r.get("reason", ""),
                "pending_reason": r.get("_pending_reason", ""),
            }
            for r in pending
        ],
    }
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    logger.info(
        f"已写 _retry_downloads.json: {path} "
        f"(可重试 {len(retryable)} 条,待人工处理 {len(pending)} 条)"
    )
    return path


def load_download_retry_manifest(path: Path) -> dict[str, Any]:
    """读 _retry_downloads.json,返回 {count, assets}。"""
    return json.loads(Path(path).read_text(encoding="utf-8"))


# ─── 内部协议:workflow 填 video / doc 字段的辅助 ──────────────────

def make_resource_record_fields(
    *,
    ch_num: int,
    ls_num: int,
    chapter: str,
    lesson: str,
    name: str,
    role: str,
    filename: str | None,
    status: str,
    size_bytes: int = 0,
    reason: str = "",
    source_meta: dict[str, Any] | None = None,
    kind_dir: str = "video",
) -> dict[str, Any]:
    """给 workflow 一个 helper,统一生成符合协议的 video/doc dict。

    这样 workflow 不必手算 relative_path / extension / size_bytes / status,
    只关心下载本身的结果。
    """
    saved_name = filename or ""
    rel_dir = "视频" if kind_dir == "video" else "文档"
    rel_path = f"{rel_dir}/{saved_name}" if saved_name else ""
    ext = Path(saved_name).suffix.lstrip(".").lower() if saved_name else ""
    if status not in ALL_STATUSES:
        status = STATUS_DOWNLOADED if size_bytes > 0 else STATUS_FAILED
    return {
        "ch_num": ch_num,
        "ls_num": ls_num,
        "chapter": chapter,
        "lesson": lesson,
        "name": name,
        "role": role,
        "filename": filename,
        "saved_name": filename,
        "relative_path": rel_path,
        "extension": ext,
        "status": status,
        "size_bytes": size_bytes,
        "reason": reason,
        "source_meta": source_meta or {},
    }