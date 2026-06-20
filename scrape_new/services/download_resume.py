"""
下载侧 resume — 读历史 manifest 决定哪些资源跳过,哪些重下。

设计:
  - 优先按 resource_key 匹配(稳定,跨运行一致)
  - 兜底按 saved_name + role 匹配(老 manifest 没 key)
  - downloaded + 文件存在 + size 合理 → 跳过(resume 不重下)
  - skipped_existing 也算跳过(幂等)
  - failed / suspicious / 文件丢失 / 文件过小 → 重下

为什么单独抽模块:
  - chaoxing 离线测试难(整段资源扫描),用纯函数 + 字典最易测
  - xuetangx / zhihuishu / icourse163 后续可复用
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


# 跳过阈值(字节):文件过小视为"不完整",仍需重下
# chaoxing 现有阈值是 100KB(视频)/ 500B(文档),resume 用 500B 兜底
MIN_VALID_SIZE = 500


def _is_file_ok(local_path: Path, expected_size: int = 0) -> bool:
    """本地文件存在 + size 满足阈值 + (如有 expected_size)>= 95% API 大小。

    Returns:
        True = 文件 OK,resume 跳过
        False = 文件丢失 / 过小 / 不完整
    """
    if not local_path.exists():
        return False
    actual = local_path.stat().st_size
    if actual < MIN_VALID_SIZE:
        return False
    if expected_size > 0 and actual < expected_size * 0.95:
        return False
    return True


def apply_retry_filter(
    all_videos: list[dict[str, Any]],
    all_docs: list[dict[str, Any]],
    retry_only_keys: set[str],
) -> dict[str, int]:
    """只允许 key 命中的资源继续下载,其余标 skipped_existing。

    独立于 apply_resume_decisions — 不需要 --resume 也不需要 manifest_path,
    专门为 `--retry-downloads` 路径设计。

    行为:
      - retry_only_keys 非空:遍历所有 v/d,key 不在集合或没 key 的标 skipped_existing
      - key 命中:不动(走正常下载循环,后由文件存在/下载成功/失败决定最终 status)
      - 不区分 status(已 OK 的也保留 — 用户可能想"重下这些",虽然 retry 通常给 failed)

    Returns:
        统计 {"kept_videos": N, "kept_docs": M, "filtered_videos": R, "filtered_docs": F}
    """
    kept_v = kept_d = filt_v = filt_d = 0
    # 空集 → 给"无重试目标"专用 reason,提示用户
    empty_msg = "retry_downloads: retry_only_keys 为空" if not retry_only_keys else None
    for v in all_videos:
        rk = v.get("resource_key", "")
        if rk and rk in retry_only_keys:
            kept_v += 1
            continue
        # 不在集合或没 key → 标 skipped_existing(原因说明)
        v["status"] = "skipped_existing"
        if empty_msg:
            v["reason"] = empty_msg
        elif not rk:
            v["reason"] = "retry_downloads: 资源无 resource_key(无法匹配)"
        else:
            v["reason"] = "retry_downloads: 不在 retry_keys 集合"
        filt_v += 1
    for d in all_docs:
        rk = d.get("resource_key", "")
        if rk and rk in retry_only_keys:
            kept_d += 1
            continue
        d["status"] = "skipped_existing"
        if empty_msg:
            d["reason"] = empty_msg
        elif not rk:
            d["reason"] = "retry_downloads: 资源无 resource_key(无法匹配)"
        else:
            d["reason"] = "retry_downloads: 不在 retry_keys 集合"
        filt_d += 1
    if filt_v or filt_d:
        logger.info(
            f"retry filter 保留 {kept_v} 视频 + {kept_d} 文档,"
            f"过滤 {filt_v} 视频 + {filt_d} 文档"
        )
    return {
        "kept_videos": kept_v,
        "kept_docs": kept_d,
        "filtered_videos": filt_v,
        "filtered_docs": filt_d,
    }


def normalize_download_resources(
    all_videos: list[dict[str, Any]],
    all_docs: list[dict[str, Any]],
    course_id: str,
) -> None:
    """为每个 video/doc 统一初始化核心字段(role / filename / resource_key / source_meta)。

    设计目的:
      - 下载循环、resume 决策、retry 决策都依赖这些字段一致
      - 在所有决策(apply_resume_decisions / retry_only_keys)之前调用一次
      - 幂等:重复调用结果相同(只补缺失字段,不覆盖已有)

    关键:此函数生成的 filename / role / resource_key 必须**和 chaoxing 下载循环完全一致**,
    否则 resume/retry 拿 normalize 的 key 跟旧 manifest 的 key 对不上。
    视频/文档命名规范从 chaoxing 同步到此处(lesson_filename + detect_role)。

    status 字段特殊处理:
      - 只有 status 为空("" 或 None)时才初始化为 "pending"
      - 已有的 status(包括 "skipped_existing")保留 — 防止下载循环把它覆盖回 "failed"
      - 这是关键修复:之前下载循环无条件 `v["status"] = _STATUS_FAILED`,
        会把 resume 设的 skipped_existing 抹掉,导致流程"看起来跳过"但
        实际调了 get_video_download_url + 拿下载链接 + 文件存在检查
        (文件存在 → 再标 skipped_existing)但调用链路已经污染

    side effect:原地修改 all_videos / all_docs。
    """
    logger = logging.getLogger(__name__)
    from scrape_new.upload.resource_key import make_resource_key
    from scrape_new.upload.naming import lesson_filename
    from scrape_new.services.english_detect import detect_role

    # ── 视频:按 (ch_num, ls_num) 分桶,role + 序号 + filename + resource_key ──
    # 这一段必须**严格复刻** chaoxing 下载循环的命名逻辑
    # (ch_num, ls_num) → list of v
    videos_by_lesson: dict[tuple[int, int], list[dict[str, Any]]] = {}
    for v in all_videos:
        key = (int(v.get("ch_num") or 0), int(v.get("ls_num") or 0))
        videos_by_lesson.setdefault(key, []).append(v)

    # (ch_num, ls_num, role) → seq count
    seq_counter: dict[tuple[int, int, str], int] = {}
    # 先稳定排序(同节多视频),保证 seq 计算可重复
    for key in sorted(videos_by_lesson.keys()):
        vids = videos_by_lesson[key]
        # chaoxing 用 enumerate 顺序;为稳定按 (tab_num, name) 排
        vids_sorted = sorted(
            vids,
            key=lambda x: (int(x.get("tab_num") or 0), str(x.get("name") or "")),
        )
        for v in vids_sorted:
            ch_num, ls_num = key
            tab_num = int(v.get("tab_num") or 0)
            # 1) role:沿用下载循环的 detect_role(同节+tab_num)
            if not v.get("role"):
                same_lesson = videos_by_lesson.get((ch_num, ls_num), [])
                v["role"] = detect_role(
                    filename="",  # 此时 filename 还没生成
                    title=v.get("lesson", ""),
                    tab_num=tab_num,
                    same_lesson_videos=same_lesson,
                )
            role = v["role"]
            # 2) 序号(同节同 role 内)
            seq_key = (ch_num, ls_num, role)
            seq_counter[seq_key] = seq_counter.get(seq_key, 0) + 1
            seq = seq_counter[seq_key]
            lesson_id = f"{ch_num}.{ls_num}"
            # 3) filename:沿用 lesson_filename 规则
            # chaoxing 主视频/英文都按 role 调 lesson_filename
            index = seq if seq > 1 else None
            ext = "mp4"
            filename = lesson_filename(
                lesson_id, v.get("lesson", ""), role=role, index=index, ext=ext,
            )
            v["filename"] = filename
            v["size_bytes"] = v.get("size_bytes") or 0
            v["reason"] = v.get("reason") or ""
            # 4) source_meta 兜底
            if v.get("source_meta") is None:
                v["source_meta"] = {
                    "objectid": v.get("objectid", ""),
                    "knowledge_id": v.get("lesson_id") or v.get("id", ""),
                    "tab_num": tab_num,
                }
            # 5) resource_key:用最终 filename
            if not v.get("resource_key"):
                try:
                    v["resource_key"] = make_resource_key(
                        course_id=str(course_id),
                        chapter_index=ch_num,
                        lesson_id=str(ls_num),
                        role=role,
                        saved_name=filename,
                    )
                except Exception as _e:
                    logger.debug(f"video resource_key 失败: {_e}")
                    v["resource_key"] = ""
            # 6) status 兜底(只在空时初始化)
            if not v.get("status"):
                v["status"] = "pending"

    # ── 文档:按扩展名判断 role + filename + resource_key ──
    # 同一节内多文档 role 序号:同扩展名才递增
    doc_seq: dict[tuple[int, int, str], int] = {}
    for d in all_docs:
        ch_num = int(d.get("ch_num") or 0)
        ls_num = int(d.get("ls_num") or 0)
        if not d.get("filename"):
            d["filename"] = d.get("name", "")
        if not d.get("role"):
            ext = Path(d.get("filename", "")).suffix.lstrip(".").lower()
            d["role"] = {
                "pptx": "ppt", "ppt": "ppt",
                "pdf": "pdf",
                "docx": "docx", "doc": "doc",
                "mp4": "video", "flv": "video",
            }.get(ext, "attachment")
        role = d["role"]
        ext = Path(d["filename"]).suffix.lstrip(".").lower() or "pdf"
        # 同一节内多文档:1 个 → 角色后缀;多个 → "_附件_N" 追加
        seq_key = (ch_num, ls_num, role)
        doc_seq[seq_key] = doc_seq.get(seq_key, 0) + 1
        seq = doc_seq[seq_key]
        lesson_id = f"{ch_num}.{ls_num}"
        index = seq if seq > 1 else None
        filename = lesson_filename(
            lesson_id, d.get("lesson", ""), role=role, index=index, ext=ext,
        )
        d["filename"] = filename
        d["size_bytes"] = d.get("size_bytes") or 0
        d["reason"] = d.get("reason") or ""
        if d.get("source_meta") is None:
            d["source_meta"] = {
                "objectid": d.get("objectid", ""),
                "knowledge_id": d.get("lesson_id") or d.get("id", ""),
            }
        if not d.get("resource_key"):
            try:
                d["resource_key"] = make_resource_key(
                    course_id=str(course_id),
                    chapter_index=ch_num,
                    lesson_id=str(ls_num),
                    role=role,
                    saved_name=filename,
                )
            except Exception as _e:
                logger.debug(f"doc resource_key 失败: {_e}")
                d["resource_key"] = ""
        if not d.get("status"):
            d["status"] = "pending"


def _build_index(
    records: list[dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    """把 records 按 resource_key 优先 / (saved_name, role) 兜底 建索引。"""
    by_key: dict[str, dict[str, Any]] = {}
    by_name_role: dict[tuple[str, str], dict[str, Any]] = {}
    for r in records:
        rk = r.get("resource_key") or ""
        if rk:
            by_key[rk] = r
        name = r.get("saved_name", "")
        role = r.get("role", "")
        if name and role:
            by_name_role[(name, role)] = r
    return {"by_key": by_key, "by_name_role": by_name_role}


def _find_record(
    index: dict[str, dict[str, Any]],
    *,
    resource_key: str,
    saved_name: str,
    role: str,
) -> dict[str, Any] | None:
    """优先按 resource_key 找,找不到再按 (saved_name, role) 兜底。"""
    rk = resource_key
    if rk and rk in index["by_key"]:
        return index["by_key"][rk]
    if saved_name and role:
        rec = index["by_name_role"].get((saved_name, role))
        if rec is not None:
            return rec
    return None


def apply_resume_decisions(
    all_videos: list[dict[str, Any]],
    all_docs: list[dict[str, Any]],
    manifest_path: Path,
    videos_folder: Path,
    docs_folder: Path | None = None,
    *,
    retry_only_keys: set[str] | None = None,
) -> dict[str, int]:
    """根据历史 manifest 标记可跳过的资源。

    输入:all_videos / all_docs(待下载的列表)+ 旧 manifest + 视频/文档目录。
    输出:统计 {"skipped_videos": N, "skipped_docs": M, "missing_keys": K, "retry_filtered": R}

    副作用:把"可跳过"的资源在 all_videos / all_docs 里标记 status="skipped_existing"
            (不真跳过,留给下载循环用 status 判定 — 跟现在的 skipped_existing 处理一致)

    设计:不真删资源,只标记 status。这样 chaoxing 下载循环的"已存在"分支会接管,
    避免改扫描逻辑。

    retry_only_keys:
        已废弃 — 推荐用 apply_retry_filter 独立处理(不依赖 resume manifest)。
        保留参数仅为向后兼容:
          - retry_only_keys is None:走纯 resume 流程(按旧 manifest 跳过 OK 资源)
          - retry_only_keys 是 set(含空集):在 resume 之前先做 retry 过滤;
            不在集合(空集时全不命中)→ 标 skipped_existing + reason,
            命中 → 走正常 resume 判断(命中且文件 OK 才会再标 skipped_existing)
    """
    # ── 向后兼容:retry_only_keys 是 set 时,先做 retry 过滤 ──
    if retry_only_keys is not None:
        fstats = apply_retry_filter(all_videos, all_docs, retry_only_keys)
        # 空集时:全标 skipped_existing,直接返回(没人能再被 resume 命中)
        if not retry_only_keys:
            return {
                "skipped_videos": 0,  # resume 这步没贡献
                "skipped_docs": 0,
                "missing_keys": 0,
                "retry_filtered": fstats["filtered_videos"] + fstats["filtered_docs"],
            }
        # 非空集:标过 skipped_existing 的资源不应再被 resume 重新覆盖
        # —— 但 apply_retry_filter 标记 skipped_existing 的资源(不在集合)
        # 本来 resume 流程也不会再处理(resume 不会改它的 status)
        # 所以可以直接 fallthrough

    if not manifest_path.exists():
        logger.warning(f"resume manifest 不存在: {manifest_path},全部重下")
        return {"skipped_videos": 0, "skipped_docs": 0,
                "missing_keys": 0, "retry_filtered": 0}

    raw = manifest_path.read_text(encoding="utf-8")
    import json
    data = json.loads(raw)
    records = data.get("records", [])
    if not records:
        return {"skipped_videos": 0, "skipped_docs": 0,
                "missing_keys": 0, "retry_filtered": 0}

    index = _build_index(records)

    docs_folder = docs_folder or videos_folder
    skipped_v = 0
    skipped_d = 0
    missing_keys = 0
    retry_filtered = 0

    for v in all_videos:
        saved = v.get("filename", "")
        role = v.get("role", "video")
        rec = _find_record(index, resource_key=v.get("resource_key", ""),
                           saved_name=saved, role=role)
        if rec is None:
            missing_keys += 1
            continue
        # 旧的 status 必须是 downloaded 或 skipped_existing 才跳过
        prev_status = rec.get("status", "")
        if prev_status not in ("downloaded", "skipped_existing"):
            continue  # 旧的失败/可疑 → 必须重下
        # 本地文件检查
        local = videos_folder / saved
        prev_size = int(rec.get("size_bytes") or 0)
        if not _is_file_ok(local, prev_size):
            continue  # 文件丢失/过小/不完整 → 重下
        # OK,标记跳过
        v["status"] = "skipped_existing"
        v["reason"] = f"resume: 旧 manifest 已 {prev_status} 且文件存在"
        v["size_bytes"] = local.stat().st_size
        skipped_v += 1

    for d in all_docs:
        saved = d.get("filename", "")
        role = d.get("role", "attachment")
        rec = _find_record(index, resource_key=d.get("resource_key", ""),
                           saved_name=saved, role=role)
        if rec is None:
            missing_keys += 1
            continue
        prev_status = rec.get("status", "")
        if prev_status not in ("downloaded", "skipped_existing"):
            continue
        local = docs_folder / saved
        prev_size = int(rec.get("size_bytes") or 0)
        if not _is_file_ok(local, prev_size):
            continue
        d["status"] = "skipped_existing"
        d["reason"] = f"resume: 旧 manifest 已 {prev_status} 且文件存在"
        d["size_bytes"] = local.stat().st_size
        skipped_d += 1

    # (--retry-downloads 模式已抽出到独立函数 apply_retry_filter,
    #  此处不再处理。调用方先调 apply_retry_filter,再调 apply_resume_decisions)

    if skipped_v or skipped_d or retry_filtered:
        logger.info(
            f"resume 跳过 {skipped_v} 视频 + {skipped_d} 文档,"
            f"retry 过滤 {retry_filtered},missing_keys={missing_keys}"
        )
    return {
        "skipped_videos": skipped_v,
        "skipped_docs": skipped_d,
        "missing_keys": missing_keys,
        "retry_filtered": retry_filtered,
    }


# ─── CLI 参数解析(安全) ─────────────────────────────────────

from typing import NamedTuple


class ParsedResumeArgs(NamedTuple):
    """CLI 解析结果。"""
    resume_manifest: Path | None
    retry_only_keys: set[str] | None  # None=不过滤,set=只跑这些(空集=全标 skipped)
    error: str | None


def parse_resume_retry_args(argv: list[str]) -> ParsedResumeArgs:
    """安全解析 --resume <path> / --retry-downloads <path>(基于 sys.argv)。

    关键安全点:
      - 用 "--resume" 标记识别,跳过它的 value,不混进 positional
      - 重复 --resume / --retry-downloads → 报错
      - 互斥检查:--resume 和 --retry-downloads 不能同时给(语义冲突)

    Args:
        argv: sys.argv 列表(不含 argv[0])

    Returns:
        ParsedResumeArgs,error 不为 None 表示解析失败(应终止流程)
    """
    if "--resume" in argv and "--retry-downloads" in argv:
        return ParsedResumeArgs(
            resume_manifest=None, retry_only_keys=None,
            error="--resume 和 --retry-downloads 不能同时给(互斥)",
        )

    resume_manifest: Path | None = None
    retry_path: Path | None = None
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "--resume":
            if i + 1 >= len(argv):
                return ParsedResumeArgs(
                    resume_manifest=None, retry_only_keys=None,
                    error="--resume 需要参数",
                )
            if resume_manifest is not None:
                return ParsedResumeArgs(
                    resume_manifest=None, retry_only_keys=None,
                    error="--resume 重复出现",
                )
            resume_manifest = Path(argv[i + 1])
            i += 2
            continue
        if a == "--retry-downloads":
            if i + 1 >= len(argv):
                return ParsedResumeArgs(
                    resume_manifest=None, retry_only_keys=None,
                    error="--retry-downloads 需要参数",
                )
            if retry_path is not None:
                return ParsedResumeArgs(
                    resume_manifest=None, retry_only_keys=None,
                    error="--retry-downloads 重复出现",
                )
            retry_path = Path(argv[i + 1])
            i += 2
            continue
        i += 1

    # 解析 --retry-downloads 的 keys(只解析,不校验文件存在,留给主流程)
    retry_only_keys: set[str] | None = None
    if retry_path is not None:
        if not retry_path.exists():
            # P0-1:不能静默变全量下载。明确报错,主流程应退出(退出码 1)
            return ParsedResumeArgs(
                resume_manifest=resume_manifest,
                retry_only_keys=None,
                error=f"--retry-downloads 文件不存在: {retry_path}",
            )
        try:
            from scrape_new.services.resource_manifest import load_download_retry_manifest
            data = load_download_retry_manifest(retry_path)
            retry_only_keys = {
                a.get("resource_key") for a in data.get("assets", [])
                if a.get("resource_key")
            }
        except Exception as e:
            return ParsedResumeArgs(
                resume_manifest=resume_manifest, retry_only_keys=None,
                error=f"读 --retry-downloads 失败: {e}",
            )

    return ParsedResumeArgs(
        resume_manifest=resume_manifest,
        retry_only_keys=retry_only_keys,
        error=None,
    )
