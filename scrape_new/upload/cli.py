"""
命令行入口:scrape.upload.cli

子命令:
  build-mapping  解析 .doc + 视频文件夹 → _mapping.json
  upload         按 _mapping.json 在老师后台上传(API 模式,纯 requests)

设计:
  - argparse,--help 中文化
  - dry-run 模式:不打网络,只跑 mapping 或把上传计划打印出来
  - verify-only 模式:只验 cookie + 拉现有树
  - cookie 三种来源:--cookies-string > --cookies 文件 > XTBZ_COOKIE 环境变量
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from dataclasses import replace
from pathlib import Path

from .mapping import build_mapping, write_mapping
from .models import course_structure_from_dict


def setup_logging(verbose: bool = False) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    # Windows 下默认 stdout 是 GBK,强制 UTF-8 让中文/特殊符号能打印
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except (AttributeError, OSError):
        pass


def _parse_expected_counts(s: str | None) -> dict[str, int] | None:
    """解析 --expected-counts 'SingleChoice=20,MultipleChoice=20,Judgement=10'。

    支持的题型 key: SingleChoice / MultipleChoice / Judgement / FillBlank
    """
    if not s:
        return None
    out: dict[str, int] = {}
    for part in s.split(","):
        part = part.strip()
        if not part:
            continue
        if "=" not in part:
            print(f"[警告] 跳过格式不对的 expected-counts 项: {part!r}")
            continue
        k, v = part.split("=", 1)
        k = k.strip()
        try:
            out[k] = int(v.strip())
        except ValueError:
            print(f"[警告] 跳过非整数 expected-counts 值: {part!r}")
    return out or None


# ─── 子命令 1: build-mapping ─────────────────────────────────────

def cmd_build_mapping(args: argparse.Namespace) -> int:
    """解析章节文档 + 视频文件夹 → _mapping.json

    S3:默认跳过空章/空节(没视频没附件),并产 _mapping_exclusions.md
    列出被跳过的结构(用户 review 用)。要保留空章,加 --include-empty-lessons。
    """
    from dataclasses import replace as dc_replace
    videos_folder = Path(args.videos)
    doc_path = Path(args.doc)
    out_path = Path(args.out) if args.out else videos_folder / "_mapping.json"

    if not videos_folder.is_dir():
        print(f"[错误] 视频文件夹不存在: {videos_folder}")
        return 1
    if not doc_path.exists():
        print(f"[错误] 章节文档不存在: {doc_path}")
        return 1

    structure = build_mapping(
        videos_folder=videos_folder,
        doc_path=doc_path,
        course_id=args.course_id or "",
        course_title=args.course_title or "",
    )

    # S3:跳过空章/空节(没视频也没附件)→ exclusions + 过滤
    include_empty = bool(getattr(args, "include_empty_lessons", False))
    excluded_chapters: list[tuple[int, str, int]] = []  # (ch_index, title, n_lessons)
    excluded_lessons: list[tuple[int, int, str]] = []    # (ch_index, lesson_id, title)
    # include 模式下,记"保留但空"的章/节(后续 upload 时这些章/节会建但无 leaf)
    empty_kept_chapters: list[tuple[int, str, int]] = []
    empty_kept_lessons: list[tuple[int, int, str]] = []
    if not include_empty:
        new_chapters = []
        for ch in structure.chapters:
            kept = []
            for ls in ch.lessons:
                if not ls.video and not ls.attachments and not ls.quiz:
                    # 空课:无视频无附件
                    excluded_lessons.append((ch.index, ls.id, ls.title))
                else:
                    kept.append(ls)
            if not kept:
                # 整章空
                excluded_chapters.append((ch.index, ch.title, len(ch.lessons)))
            else:
                new_chapters.append(dc_replace(ch, lessons=tuple(kept)))
        if excluded_chapters or excluded_lessons:
            structure = dc_replace(structure, chapters=tuple(new_chapters))
    else:
        # include 模式:不删任何章/节,但收集"保留但空"的供 exclusions 报告
        for ch in structure.chapters:
            empty_in_ch = []
            for ls in ch.lessons:
                if not ls.video and not ls.attachments and not ls.quiz:
                    empty_kept_lessons.append((ch.index, ls.id, ls.title))
                    empty_in_ch.append(ls)
            if not ch.lessons or len(empty_in_ch) == len(ch.lessons):
                # 整章所有节都空 → 算"保留但空的章"
                empty_kept_chapters.append((ch.index, ch.title, len(ch.lessons)))

    write_mapping(structure, out_path)

    # 写 _mapping_exclusions.md(让人 review 跳过的内容)
    # include 模式也写一份"保留但空"的报告
    has_exclusions = bool(excluded_chapters or excluded_lessons)
    has_empty_kept = bool(empty_kept_chapters or empty_kept_lessons)
    if (has_exclusions and not include_empty) or (has_empty_kept and include_empty):
        exclusions_path = out_path.parent / "_mapping_exclusions.md"
        lines = ["# Mapping Exclusions(自动跳过)", ""]
        if include_empty:
            lines.append("本课程启用了 `--include-empty-lessons`:空章/空节保留进 mapping。")
            lines.append("下面列出保留但 0 资源的章/节(upload 时会建空章/空节,**可能不是你要的**)。")
        else:
            lines.append("build-mapping 默认跳过无视频无附件的章节/课时(避免后台建空章)。")
            lines.append("如果想保留空章节作大纲占位,加 `--include-empty-lessons` 重跑。")
        lines.append("")
        lessons_to_show = excluded_lessons if not include_empty else empty_kept_lessons
        chapters_to_show = excluded_chapters if not include_empty else empty_kept_chapters
        if lessons_to_show:
            tag = "(被跳过)" if not include_empty else "(保留但空)"
            lines.append(f"## 空课时 {len(lessons_to_show)} 个 {tag}")
            lines.append("")
            lines.append("| 章 | 节 ID | 节标题 |")
            lines.append("|---|---|---|")
            for ch_idx, ls_id, title in lessons_to_show:
                lines.append(f"| {ch_idx} | {ls_id} | {title} |")
            lines.append("")
        if chapters_to_show:
            tag = "(被跳过)" if not include_empty else "(保留但空)"
            lines.append(f"## 整章空章 {len(chapters_to_show)} 个 {tag}")
            lines.append("")
            lines.append("| 章 | 章标题 | 课时数 |")
            lines.append("|---|---|---|")
            for ch_idx, title, n in chapters_to_show:
                lines.append(f"| {ch_idx} | {title} | {n} |")
            lines.append("")
        exclusions_path.write_text("\n".join(lines), encoding="utf-8")
        print(f"  [exclusions] {exclusions_path}")

    # 报告
    n_chapters = len(structure.chapters)
    n_lessons = sum(len(c.lessons) for c in structure.chapters)
    n_mapped = len(structure.lessons_with_video())
    n_missing = len(structure.missing_video_lessons())
    print()
    print("=" * 60)
    print("Mapping 生成完成!")
    print(f"  课程: {structure.course_title or '(未填)'}")
    print(f"  文档: {doc_path.name}")
    print(f"  输出: {out_path}")
    print(f"  章节: {n_chapters}")
    print(f"  课时: {n_lessons}")
    print(f"  已映射视频: {n_mapped}")
    print(f"  缺视频课时: {n_missing}")
    if n_missing > 0:
        print()
        print("  ⚠ 以下课时缺视频，请先补素材:")
        for ch, ls in structure.missing_video_lessons():
            print(f"    - {ls.id} {ls.title}")
    if not include_empty and (excluded_chapters or excluded_lessons):
        print()
        print(f"  跳过的空结构: {len(excluded_chapters)} 章 + {len(excluded_lessons)} 课时")
        print(f"  详情见 _mapping_exclusions.md")
    print("=" * 60)
    return 0


# ─── 子命令 2: upload ────────────────────────────────────────────

def cmd_upload(args: argparse.Namespace) -> int:
    """按 _mapping.json 在老师后台上传(API 模式,纯 requests)"""
    mapping_path = Path(args.mapping)
    if not mapping_path.exists():
        print(f"[错误] _mapping.json 不存在: {mapping_path}")
        print(f"  请先跑 build-mapping 生成")
        return 1

    raw = mapping_path.read_text(encoding="utf-8")
    structure = course_structure_from_dict(json.loads(raw))
    if args.course_id:
        # 覆盖课程 ID
        structure = replace(structure, course_id=args.course_id)

    if not structure.course_id:
        print(f"[错误] mapping 里没有 course_id,请用 --course-id 指定")
        return 1

    # cookie 来源:优先 --cookies-string,次之 --cookies 文件;或环境变量 XTBZ_COOKIE
    cookies_string = args.cookies_string or os.environ.get("XTBZ_COOKIE")
    cookies_path = Path(args.cookies) if args.cookies else None
    if not cookies_string and not cookies_path:
        print("[错误] 必须提供 --cookies <文件> 或 --cookies-string '<原始 cookie>' 之一")
        print("       也可以设环境变量 XTBZ_COOKIE='<原始 cookie>'")
        return 1

    # 视频目录:默认 mapping 同级
    videos_folder = Path(args.videos) if args.videos else mapping_path.parent

    # only_chapters 解析:"1,2,5" → {1,2,5}
    only_chapters: set[int] | None = None
    if args.only_chapters:
        try:
            only_chapters = {int(x.strip()) for x in args.only_chapters.split(",") if x.strip()}
        except ValueError:
            print(f"[错误] --only-chapters 必须是逗号分隔的数字: {args.only_chapters}")
            return 1

    # U1:only_lessons / only_resources 解析
    only_lessons: set[str] | None = None
    if getattr(args, "only_lessons", None):
        only_lessons = {x.strip() for x in args.only_lessons.split(",") if x.strip()}
    only_resources: set[str] | None = None
    if getattr(args, "only_resources", None):
        only_resources = {x.strip() for x in args.only_resources.split(",") if x.strip()}
    plan_only = bool(getattr(args, "plan_only", False))

    # 调用 API 主流程(延迟 import,避免 cli --help 也要拉 requests)
    apply_plan_path = None
    if getattr(args, "apply_plan", None):
        apply_plan_path = Path(args.apply_plan)
    yes = bool(getattr(args, "yes", False))
    # 互斥检查
    if apply_plan_path is not None and yes:
        print("[错误] --apply-plan 和 --yes 互斥,只能选一个")
        return 1

    try:
        from .api_uploader import run_upload_api
        result = run_upload_api(
            structure=structure,
            videos_folder=videos_folder,
            cookies_path=cookies_path,
            cookies_string=cookies_string,
            output_dir=Path(args.output) if args.output else mapping_path.parent,
            dry_run=args.dry_run,
            verify_only=args.verify_only,
            only_chapters=only_chapters,
            only_lessons=only_lessons,
            only_resources=only_resources,
            prune=args.prune,
            reset_confirm=args.reset_confirm,
            confirm_rename=args.confirm_rename,
            plan_only=plan_only,
            apply_plan_path=apply_plan_path,
            yes=yes,
        )
    except SystemExit as e:
        # apply-plan 校验失败时用 sys.exit(1) — 友好转为返回 1
        return 1
    except Exception as e:
        logging.exception("上传失败")
        print(f"[错误] 上传失败: {e}")
        return 2

    from .report import print_summary
    print_summary(result)
    return 0 if result.delta() == 0 else 3


# ─── 子命令 3: retry-failed(智能重试,优化 4) ───────────────────────

def cmd_retry_failed(args: argparse.Namespace) -> int:
    """从上次 _upload_manifest.json 读 FAILED 资产,只重传这些。

    用法:
      python -m scrape.upload retry-failed \\
        --manifest ./视频/_upload_manifest.json \\
        --course-id 15932418 \\
        --cookies-string "$XTBZ_COOKIE"
    """
    from pathlib import Path
    from .report import load_manifest, save_manifest, print_summary
    from .api_uploader import run_upload_api
    from .models import AssetStatus
    from dataclasses import replace
    import json

    manifest_path = Path(args.manifest)
    if not manifest_path.exists():
        print(f"[错误] 找不到 manifest: {manifest_path}")
        return 1

    prev = load_manifest(manifest_path)
    if prev is None:
        print(f"[错误] manifest 格式不对或为空: {manifest_path}")
        return 1

    # 找 FAILED 资产
    failed_assets = [a for a in prev.assets if a.status == AssetStatus.FAILED]
    if not failed_assets:
        print("[OK] 上次全部成功,没有 FAILED 资产需要重试")
        return 0

    # 找 FAILED 涉及的章节,只重试这些
    failed_chapters = sorted({a.chapter_index for a in failed_assets})
    print(f"[重试] 上次失败 {len(failed_assets)} 个资产,涉及 {len(failed_chapters)} 个章节: {failed_chapters}")

    # 找 mapping.json(同目录或父目录)
    mapping_path = manifest_path.parent / "_mapping.json"
    if not mapping_path.exists():
        print(f"[错误] 找不到 _mapping.json: {mapping_path}")
        return 1

    raw = mapping_path.read_text(encoding="utf-8")
    structure = course_structure_from_dict(json.loads(raw))

    # Cookie 来源
    cookies_string = args.cookies_string or os.environ.get("XTBZ_COOKIE", "")
    cookies_path = Path(args.cookies) if args.cookies else None
    if not cookies_string and not cookies_path:
        print("[错误] 必须提供 --cookies 或 --cookies-string")
        return 1

    course_id = args.course_id or structure.course_id
    if not course_id:
        print("[错误] 缺少 course_id (--course-id 或 mapping 里)")
        return 1

    # 跑(only_chapters 限制到失败章节)
    output_dir = Path(args.output) if args.output else mapping_path.parent

    result = run_upload_api(
        structure=structure,
        videos_folder=output_dir,
        cookies_path=cookies_path,
        cookies_string=cookies_string,
        output_dir=output_dir,
        only_chapters=set(failed_chapters),
    )

    # 合并 manifest:成功的标记 OK,失败的保留
    new_manifest_dict = {
        a.chapter_index: a for a in result.assets
    }
    final_assets = tuple(
        new_manifest_dict.get(a.chapter_index, a) for a in prev.assets
    )
    from .models import UploadResult
    merged = UploadResult(
        course_id=prev.course_id,
        course_title=prev.course_title,
        started_at=prev.started_at,
        finished_at=result.finished_at,
        assets=final_assets,
    )
    save_manifest(merged, manifest_path)
    print(f"  ✓ manifest 已更新: {manifest_path}")
    print_summary(merged)
    return 0 if merged.delta() == 0 else 3


# ─── 子命令 4: retry-resources(增量 resume 失败资源) ────────────

def cmd_retry_resources(args: argparse.Namespace) -> int:
    """读 _retry_resources.json,只重跑这些 resource_key 对应的资源。

    流程:
      1. 读 input(--input → _retry_resources.json,只含 FAILED/SUSPICIOUS/PENDING)
      2. 提取 resource_key 集合
      3. 读 mapping(--mapping)
      4. 调 run_upload_api(..., retry_keys=resource_keys)
         → run_upload_api 算完 diff 后,_mark_retry_keys 走"只保留 retry_keys
           里的 CREATE,其他 CREATE 全部 SKIP"逻辑

    注意:这跟 `--resume _upload_manifest.json` 是相反的:
      - resume 跳过 OK(已成功的)
      - retry 只跑指定 key(要重试的)
    """
    from .report import load_retry_resources

    input_path = Path(args.input)
    if not input_path.exists():
        print(f"[错误] retry 清单不存在: {input_path}")
        return 1

    mapping_path = Path(args.mapping)
    if not mapping_path.exists():
        print(f"[错误] mapping 不存在: {mapping_path}")
        return 1

    try:
        retry_data = load_retry_resources(input_path)
    except Exception as e:
        print(f"[错误] 读 _retry_resources.json 失败: {e}")
        return 1

    course_id = args.course_id or retry_data.get("course_id", "")
    if not course_id:
        print(f"[错误] input 没 course_id,请用 --course-id 指定")
        return 1

    # 让 user 确认
    if not args.yes and not args.dry_run:
        n = retry_data.get("count", len(retry_data.get("assets", [])))
        print(f"  将重试 {n} 个失败资源(课程 {course_id})")
        try:
            ans = input("  继续? [y/N] ").strip().lower()
        except EOFError:
            ans = ""
        if ans not in ("y", "yes"):
            print("  已取消")
            return 0

    # 解析 mapping
    raw = mapping_path.read_text(encoding="utf-8")
    structure = course_structure_from_dict(json.loads(raw))
    if str(structure.course_id) != str(course_id):
        print(f"[警告] mapping course_id={structure.course_id} != {course_id}")

    # 提取 resource_key 列表,作为 retry_keys 传给 run_upload_api
    retry_keys = {
        a.get("resource_key") for a in retry_data.get("assets", [])
        if a.get("resource_key")
    }

    # 空清单:没可自动重试的资源,直接返回(不调 run_upload_api)
    # pending_actions(改名待确认)不算可重试资源,不会进 assets,所以 retry_keys 为空正常
    pending_n = len(retry_data.get("pending_actions", []))
    if not retry_keys:
        msg = (
            f"[信息] input 没有任何可自动重试的 resource_key "
            f"(assets={retry_data.get('count', 0)}, pending_actions={pending_n})"
        )
        if pending_n:
            msg += (
                "\n  有 RENAME 等待处理项需人工决定 — 见 input:pending_actions 段"
            )
        print(msg)
        return 0

    # 跑(让 _mark_retry_keys 只留 retry_keys 里的 CREATE)
    from .api_uploader import run_upload_api
    output_dir = Path(args.output) if args.output else mapping_path.parent
    videos_folder = Path(args.videos) if args.videos else mapping_path.parent
    cookies_string = args.cookies_string or os.environ.get("XTBZ_COOKIE")
    cookies_path = Path(args.cookies) if args.cookies else None
    if not cookies_string and not cookies_path:
        print("[错误] 必须提供 --cookies 或 --cookies-string")
        return 1

    try:
        result = run_upload_api(
            structure=structure,
            videos_folder=videos_folder,
            cookies_path=cookies_path,
            cookies_string=cookies_string,
            output_dir=output_dir,
            dry_run=args.dry_run,
            retry_keys=retry_keys,  # 只重跑这些 key
        )
    except Exception as e:
        print(f"[错误] 重试失败: {e}")
        return 2

    from .report import print_summary
    print_summary(result)
    return 0 if result.delta() == 0 else 3


# ─── 子命令 4: exercise ──────────────────────────────────────────

def cmd_exercise_generate(args: argparse.Namespace) -> int:
    """从 _chapter_outline.json 生成章末习题 .docx（或期末考试）"""
    from .exercise_docx import generate_exercise_docx, generate_final_exam_docx

    outline_path = Path(args.outline)
    out_dir = Path(args.output)

    if not outline_path.exists():
        print(f"[错误] outline 文件不存在: {outline_path}")
        return 1

    data = json.loads(outline_path.read_text(encoding="utf-8"))
    chapters = data.get("chapters", [])
    if not chapters:
        print("[错误] outline 中没有 chapters")
        return 1

    # 期末考试模式
    if args.final:
        course_title = data.get("course_title", "本课程")
        out_dir.mkdir(parents=True, exist_ok=True)
        fname = f"{course_title}_期末考试.docx" if course_title else "期末考试.docx"
        generate_final_exam_docx(data, out_dir / fname)
        print(f"\n期末考试生成完成 → {out_dir / fname}")
        return 0

    target_chapter = int(args.chapter) if args.chapter else None
    out_dir.mkdir(parents=True, exist_ok=True)

    generated = 0
    for ch in chapters:
        ch_index = ch.get("index", 1)
        if target_chapter and ch_index != target_chapter:
            continue
        ch_data = {
            "ch_index": ch_index,
            "ch_name": ch.get("title", f"第{ch_index}章"),
            "lessons": ch.get("lessons", []),
        }
        fname = f"第{ch_data['ch_index']}章_{ch_data['ch_name']}_习题.docx"
        generate_exercise_docx(ch_data, out_dir / fname)
        generated += 1

    print(f"\n生成完成: {generated} 套习题 → {out_dir}")
    return 0


def cmd_preflight(args: argparse.Namespace) -> int:
    """上传前体检报告(数量对比 / RENAME / 风险等级 / 缺资源)。

    dry-run:不调用任何写操作 API,只拉真实章节树 + 算 diff。
    """
    from .preflight import build_preflight, format_preflight_text, write_preflight_text

    mapping_path = Path(args.mapping)
    if not mapping_path.exists():
        print(f"[错误] mapping 不存在: {mapping_path}")
        return 1

    # 解析 mapping
    raw = mapping_path.read_text(encoding="utf-8")
    structure = course_structure_from_dict(json.loads(raw))
    if args.course_id:
        structure = replace(structure, course_id=args.course_id)

    if not structure.course_id:
        print(f"[错误] mapping 里没有 course_id,请用 --course-id 指定")
        return 1

    # Cookie 验证 + 拉真实树
    cookies_string = args.cookies_string or os.environ.get("XTBZ_COOKIE")
    cookies_path = Path(args.cookies) if args.cookies else None
    if not cookies_string and not cookies_path:
        print("[错误] 必须提供 --cookies 或 --cookies-string(或 XTBZ_COOKIE 环境变量)")
        return 1

    try:
        from .api_uploader import (
            _make_session, _build_context, verify_login, get_resource_tree,
        )
    except Exception as e:
        print(f"[错误] 导入 api_uploader 失败: {e}")
        return 1

    # --json 模式:进度日志走 stderr,stdout 只放 JSON(机器可读)
    json_mode = getattr(args, "json", False)
    log = _stderr_log if json_mode else print

    log(f"[1/3] 验证 Cookie...")
    session = _make_session(cookies_path=cookies_path, cookies_string=cookies_string)
    ctx = _build_context(session, structure.course_id)
    if not verify_login(ctx):
        return 2

    log(f"[2/3] 拉取真实章节树...")
    try:
        tree = get_resource_tree(ctx)
    except Exception as e:
        log(f"  [失败] 拉章节树失败: {e}")
        return 2

    log(f"[3/3] 计算 preflight...")
    output_dir = Path(args.output) if args.output else mapping_path.parent
    report = build_preflight(
        structure, tree,
        drift_threshold=getattr(args, "drift_threshold", 0.6),
    )

    if json_mode:
        # stdout 干净输出 JSON,stderr 走其它日志
        print(json.dumps(report.to_dict(), ensure_ascii=False, indent=2))
    else:
        print(format_preflight_text(report))

    # 总是写 _preflight_report.txt(可贴邮件/微信)
    txt_path = write_preflight_text(report, output_dir)
    log(f"\n  报告已写: {txt_path}")

    return 0 if report.risk_level.value != "HIGH" else 3


def _stderr_log(msg: str) -> None:
    """stderr 输出(供 --json 等机器可读模式用)。"""
    import sys
    print(msg, file=sys.stderr)


def cmd_exercise_upload(args: argparse.Namespace) -> int:
    """上传习题 .docx 到老师后台并挂到章节"""
    from .exercise_upload import upload_exercise
    from .api_uploader import _make_session, _build_context

    docx_path = Path(args.docx)
    if not docx_path.exists():
        print(f"[错误] 习题文件不存在: {docx_path}")
        return 1

    # cookie 来源
    cookies_string = args.cookies_string or os.environ.get("XTBZ_COOKIE")
    cookies_path = Path(args.cookies) if args.cookies else None
    if not cookies_string and not cookies_path:
        print("[错误] 必须提供 --cookies 或 --cookies-string")
        return 1

    if args.dry_run:
        print(f"[DRY-RUN] 将上传: {docx_path.name}")
        print(f"  目标课程: {args.course_id}")
        print(f"  显示名称: {args.leaf_name}")
        if args.create_chapter:
            print(f"  将先建章: '{args.create_chapter}'")
        else:
            print(f"  目标章节: {args.chapter_id}")
        print("[DRY-RUN] 不上传,退出")
        return 0

    session = _make_session(cookies_path=cookies_path, cookies_string=cookies_string)
    ctx = _build_context(session, args.course_id)

    try:
        chapter_id = int(args.chapter_id) if args.chapter_id else 0
        leaf_name = args.leaf_name

        # 如果指定了 --create-chapter,先建章
        if args.create_chapter:
            from .exercise_upload import create_final_exam_chapter
            chapter_id = create_final_exam_chapter(ctx, args.create_chapter)
            leaf_name = args.create_chapter

        if not chapter_id:
            print("[错误] 必须提供 --chapter-id 或 --create-chapter")
            return 1

        if args.final:
            # 解析 --expected-counts "SingleChoice=25,MultipleChoice=10,Judgement=15"
            exp = _parse_expected_counts(getattr(args, "expected_counts", None))
            result = upload_exercise(
                ctx, docx_path, chapter_id, leaf_name,
                score_single=2, score_multiple=2, score_judgement=2,
                expected_counts=exp,
                strict=not args.no_strict,
            )
        else:
            exp = _parse_expected_counts(getattr(args, "expected_counts", None))
            result = upload_exercise(
                ctx, docx_path, chapter_id, leaf_name,
                expected_counts=exp,
                strict=not args.no_strict,
            )
    except Exception as e:
        logging.exception("上传习题失败")
        print(f"[错误] 上传失败: {e}")
        return 2

    print(f"\n完毕: exercise_id={result.exercise_id}, leaf_id={result.leaf_id}")
    return 0


# ─── argparse ────────────────────────────────────────────────────

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m scrape.upload",
        description="学堂在线老师后台课程自动搭建工具",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  # 1. 生成 mapping 草稿（不连网络）
  python -m scrape.upload build-mapping \\
    --videos "E:/林视/免疫课程视频" \\
    --doc "E:/林视/docs/《【26春】疾病与免疫学2026春》章节目录.doc" \\
    --course-id 15932418 \\
    --course-title "疾病与免疫学2026春"

  # 2.0 只验证 Cookie + 看课程现有结构
  python -m scrape.upload upload \\
    --mapping "E:/林视/免疫课程视频/_mapping.json" \\
    --cookies-string "csrftoken=...; sessionid=...; xtbz=cloud; ..." \\
    --verify-only

  # 2.1 干跑：只打印将要建什么
  python -m scrape.upload upload \\
    --mapping "E:/林视/免疫课程视频/_mapping.json" \\
    --cookies "E:/林视/cookies_teacher.txt" \\
    --dry-run

  # 2.2 正式上传(可选 --only-chapters '1' 只跑第一章)
  python -m scrape.upload upload \\
    --mapping "E:/林视/免疫课程视频/_mapping.json" \\
    --cookies "E:/林视/cookies_teacher.txt"
        """,
    )
    p.add_argument("-v", "--verbose", action="store_true", help="打开 DEBUG 日志")
    sub = p.add_subparsers(dest="cmd", required=True)

    # build-mapping
    pm = sub.add_parser("build-mapping", help="解析章节文档 + 视频文件夹 → _mapping.json")
    pm.add_argument("--videos", required=True, help="视频文件夹路径")
    pm.add_argument("--doc", required=True, help="章节目录文档路径（.doc / .html / .md）")
    pm.add_argument("--out", help="mapping 输出路径，默认 <videos>/_mapping.json")
    pm.add_argument("--course-id", help="覆盖课程 ID（默认读 mapping）")
    pm.add_argument("--course-title", help="覆盖课程标题（默认读 mapping）")
    pm.add_argument("--include-empty-lessons", action="store_true",
                    help="S3 默认跳过无视频无附件的章节/课时(避免后台建空章)。"
                         "加此 flag 保留空结构作大纲占位。跳过的内容写到 _mapping_exclusions.md。")
    pm.set_defaults(func=cmd_build_mapping)

    # upload
    pu = sub.add_parser("upload", help="按 _mapping.json 在老师后台上传（API 模式，纯 requests）")
    pu.add_argument("--mapping", required=True, help="_mapping.json 路径")
    pu.add_argument("--videos", help="视频文件夹路径，默认 mapping 同级")
    pu.add_argument("--course-id", help="覆盖课程 ID")
    pu.add_argument("--cookies", help="教师 Cookie 文件路径 (.txt 或 .json)")
    pu.add_argument("--cookies-string", help="教师 Cookie 原始字符串(in-memory，不落盘),"
                                              "也可走 XTBZ_COOKIE 环境变量")
    pu.add_argument("--output", help="输出目录（log/manifest/report），默认 mapping 所在目录")
    pu.add_argument("--dry-run", action="store_true", help="干跑：只打印计划,不调用 API")
    pu.add_argument("--verify-only", action="store_true",
                    help="只验证 Cookie + 拉现有章节树,不写任何内容")
    pu.add_argument("--only-chapters", help="只处理这些章节,逗号分隔的 1-based index,"
                                            "如 '1,3,5'")
    pu.add_argument("--only-lessons", help="U1 局部目标:只处理这些 lesson_id(形如 '1.2,3.4'),"
                                            "其它 chapter/section/leaf 一律只读不操作")
    pu.add_argument("--only-resources", help="U1 局部目标:精确到 (lesson_id, kind),形如 '1.2:english,3.4:ppt'")
    pu.add_argument("--plan-only", action="store_true",
                    help="U3 只输出 _upload_plan.json/md,不做任何写操作。"
                         "适合上传前 review 计划。")
    pu.add_argument("--apply-plan", metavar="PLAN_JSON",
                    help="P1/P3 加载之前 plan-only 生成的 _upload_plan.json,"
                         "校验 4 项(course_id / mapping_hash / scope / tree_fingerprint)通过后才执行。"
                         "默认(无此 flag 且无 --yes)是 plan-first:自动写 plan 后停。")
    pu.add_argument("--yes", action="store_true",
                    help="P4 显式跳过 plan-first 安全闸,直接执行写 API。"
                         "注意:即使 --yes,局部模式仍禁 reset,drift > 60% 全量模式仍按旧逻辑拒绝。")
    pu.add_argument("--prune", action="store_true",
                    help="删除 mapping 中没有的多余章/节/leaf(默认不动)")
    pu.add_argument("--reset-confirm", metavar="COURSE_ID",
                    help="显式确认清空重建。必须传 course_id(等于当前课程才生效),"
                         "执行前自动备份当前真实树到 _resource_tree_backup_<ts>.json。"
                         "U2:局部模式(--only-lesson/--only-resource)下此 flag 被忽略,不会 reset。")
    pu.add_argument("--confirm-rename", action="store_true",
                    help="允许执行章名 RENAME(默认 RENAME 进入 PENDING 状态,不动原章)。"
                         "开启后,RENAME 会 delete 整章 + create 新章,"
                         "原章所有 leaf 会被清空,需谨慎。")
    pu.set_defaults(func=cmd_upload)

    # ── exercise generate ──
    pe = sub.add_parser("exercise", help="章末习题管理（generate / upload）")
    pes = pe.add_subparsers(dest="exercise_cmd", required=True)

    peg = pes.add_parser("generate", help="从 _chapter_outline.json 生成章末习题 .docx（或期末考试）")
    peg.add_argument("--outline", required=True, help="_chapter_outline.json 路径")
    peg.add_argument("--output", required=True, help="习题 .docx 输出目录")
    peg.add_argument("--chapter", help="指定章序号(1-based),默认全生成,如 '3'")
    peg.add_argument("--final", action="store_true", help="生成期末考试(覆盖全课程:单选25+多选10+判断15,每题2分,共100分)")
    peg.set_defaults(func=cmd_exercise_generate)

    peu = pes.add_parser("upload", help="上传习题 .docx 到老师后台并挂到章节")
    peu.add_argument("--docx", required=True, help="习题 .docx 文件路径")
    peu.add_argument("--course-id", required=True, help="课程实例 ID")
    peu.add_argument("--chapter-id", default="0", help="要挂载的目标章节 ID(若 --create-chapter 则忽略)")
    peu.add_argument("--create-chapter", help="先创建新章节再挂载(如 '期末考试'),返回新 chapter_id")
    peu.add_argument("--cookies", help="教师 Cookie 文件(.txt 或 .json)")
    peu.add_argument("--cookies-string", help="教师 Cookie 原始字符串(in-memory)")
    peu.add_argument("--leaf-name", default="章末测试", help="习题在章节树上的显示名")
    peu.add_argument("--final", action="store_true", help="期末考试模式(所有题型每题2分)")
    peu.add_argument("--dry-run", action="store_true", help="只打印计划,不调 API")
    peu.add_argument("--expected-counts",
                     help="期望题型分布,如 'SingleChoice=5,MultipleChoice=5,Judgement=5'。"
                          "不匹配时按 strict 策略处理")
    peu.add_argument("--no-strict", action="store_true",
                     help="关闭 strict 校验(默认严格:题数/答案不符就拒绝创建)")
    peu.set_defaults(func=cmd_exercise_upload)

    # retry-failed(优化 4)
    pr = sub.add_parser(
        "retry-failed",
        help="从上次 _upload_manifest.json 重试 FAILED 资产(自动只跑失败章节)",
    )
    pr.add_argument("--manifest", required=True, help="_upload_manifest.json 路径")
    pr.add_argument("--course-id", help="覆盖 manifest 里的 course_id")
    pr.add_argument("--cookies", help="教师 Cookie 文件路径 (.txt 或 .json)")
    pr.add_argument("--cookies-string", help="教师 Cookie 原始字符串(in-memory,优先于 --cookies)")
    pr.add_argument("--output", help="视频文件夹(默认 manifest 同级)")
    pr.set_defaults(func=cmd_retry_failed)

    # ── retry-resources(从 _retry_resources.json 读,只重跑失败的) ──
    prr = sub.add_parser(
        "retry-resources",
        help="从 _retry_resources.json 读失败资源清单,只重跑这些(增量 resume)",
    )
    prr.add_argument("--input", required=True,
                     help="_retry_resources.json 路径")
    prr.add_argument("--mapping", required=True, help="_mapping.json 路径(同课程)")
    prr.add_argument("--course-id", help="覆盖课程 ID(默认读 input)")
    prr.add_argument("--cookies", help="教师 Cookie 文件路径")
    prr.add_argument("--cookies-string", help="教师 Cookie 原始字符串")
    prr.add_argument("--videos", help="视频文件夹(默认 mapping 同级)")
    prr.add_argument("--output", help="输出目录(默认 mapping 同级)")
    prr.add_argument("--dry-run", action="store_true", help="只打印计划,不调 API")
    prr.add_argument("--yes", action="store_true",
                     help="跳过交互确认(批量脚本用)")
    prr.set_defaults(func=cmd_retry_resources)

    # ── preflight ──
    pp = sub.add_parser(
        "preflight",
        help="上传前体检(数量对比/RENAME/风险/缺资源),dry-run,不动后台",
    )
    pp.add_argument("--mapping", required=True, help="_mapping.json 路径")
    pp.add_argument("--course-id", help="覆盖课程 ID")
    pp.add_argument("--cookies", help="教师 Cookie 文件路径")
    pp.add_argument("--cookies-string", help="教师 Cookie 原始字符串(in-memory)")
    pp.add_argument("--output", help="输出目录(写 _preflight_report.txt),默认 mapping 同级")
    pp.add_argument("--drift-threshold", type=float, default=0.6,
                    help="drift 阈值,默认 0.6(>= 60% 判 HIGH)")
    pp.add_argument("--json", action="store_true",
                    help="机器可读模式:stdout 只输出 JSON,"
                         "进度日志走 stderr(便于管道 / jq 处理)")
    pp.set_defaults(func=cmd_preflight)

    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    setup_logging(args.verbose)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
