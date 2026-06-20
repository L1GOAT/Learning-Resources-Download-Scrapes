"""
chaoxing 多 tab 扫描(可独立测,纯函数 + dict 结构)。

为什么单独抽:
  - chaoxing.py 里的 _fetch_cards_tab + scan_lesson_resources 依赖网络 + Playwright
  - 想测"多 tab 探测"、"限流中断"、"智能 role 识别"必须 mock 网络
  - 把扫描逻辑收成纯函数,接受 mock 的 fetcher,测试简单

设计:
  - ScanContext:一次扫描的总上下文(course/chapter/lesson,all_videos/all_docs)
  - TabResult:一个 tab 的扫描结果(videos / docs / rate_limited / failed)
  - 连续空 tab 策略:连续 CONSEC_EMPTY_STOP 个空 tab 就停(避免无效请求)
  - 限流策略:连续 CONSEC_202_STOP 个 202 就停(避免被风控)
  - 智能 role:detect_resource_role() — 扩展名/标题/同节结构/tab_num 多级 fallback
  - 漏扫检测:detect_suspicious_lessons() — 同章其他节有 X,这一节没有 → 标记

输出可序列化:ScanContext.to_dict() 喂给 JSON / Markdown 报告
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable

# 连续空 tab 几次后停止扫描(避免无效请求浪费限流配额)
CONSEC_EMPTY_STOP = 2
# 连续 202 限流几次后停止(避免被风控封号)
CONSEC_202_STOP = 3
# 默认 tab 探测范围(0..MAX_TABS-1),CLI 可调
DEFAULT_MAX_TABS = 4


# 资源类型(智能 role)
ROLE_VIDEO = "video"
ROLE_ENGLISH = "english"
ROLE_PPT = "ppt"
ROLE_PDF = "pdf"
ROLE_DOCX = "docx"
ROLE_DOC = "doc"
ROLE_ATTACHMENT = "attachment"
ROLE_QUIZ = "quiz"
ROLE_NOTE = "note"
ROLE_UNKNOWN = "unknown"

ALL_ROLES = (
    ROLE_VIDEO, ROLE_ENGLISH, ROLE_PPT, ROLE_PDF,
    ROLE_DOCX, ROLE_DOC, ROLE_ATTACHMENT, ROLE_QUIZ, ROLE_NOTE, ROLE_UNKNOWN,
)

# 扩展名 → role(优先匹配)
_EXT_TO_ROLE: dict[str, str] = {
    "mp4": ROLE_VIDEO, "flv": ROLE_VIDEO, "avi": ROLE_VIDEO,
    "mkv": ROLE_VIDEO, "mov": ROLE_VIDEO, "m3u8": ROLE_VIDEO,
    "pptx": ROLE_PPT, "ppt": ROLE_PPT,
    "pdf": ROLE_PDF,
    "docx": ROLE_DOCX, "doc": ROLE_DOC,
    "jpg": ROLE_ATTACHMENT, "jpeg": ROLE_ATTACHMENT, "png": ROLE_ATTACHMENT,
    "gif": ROLE_ATTACHMENT, "bmp": ROLE_ATTACHMENT,
    "zip": ROLE_ATTACHMENT, "rar": ROLE_ATTACHMENT,
    "xls": ROLE_ATTACHMENT, "xlsx": ROLE_ATTACHMENT,
}

# type/mimetype 字段 → role(某些平台接口直接给 mimetype)
_TYPE_TO_ROLE: dict[str, str] = {
    "video/mp4": ROLE_VIDEO, "video/flv": ROLE_VIDEO, "video/x-flv": ROLE_VIDEO,
    "application/vnd.ms-powerpoint": ROLE_PPT,
    "application/vnd.openxmlformats-officedocument.presentationml.presentation": ROLE_PPT,
    "application/pdf": ROLE_PDF,
    "application/msword": ROLE_DOC,
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ROLE_DOCX,
}

# 标题/文件名关键字 → role(命中后 override 扩展名判定)
_TITLE_KEYWORDS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"(?i)english|英文|en[\s_]?version"), ROLE_ENGLISH),
    (re.compile(r"(?i)\bppt\b|课件|演示稿|slides?"), ROLE_PPT),
    (re.compile(r"(?i)pdf|讲义|教案"), ROLE_PDF),
    (re.compile(r"(?i)docx?|word|讲义|教案"), ROLE_DOCX),
    (re.compile(r"(?i)quiz|测验|测试|exam"), ROLE_QUIZ),
    (re.compile(r"(?i)note|笔记|备注"), ROLE_NOTE),
]

# tab_num 兜底(只在其他信号都没命中时使用)
_TAB_FALLBACK: dict[int, str] = {
    0: ROLE_VIDEO,
    1: ROLE_PPT,
    2: ROLE_ENGLISH,
    # num=3+ 由资源类型/扩展名决定,无明确语义
}


@dataclass
class TabResult:
    """一个 tab 的扫描结果(纯数据,可序列化)。"""
    tab_num: int
    videos: list[dict[str, Any]] = field(default_factory=list)
    docs: list[dict[str, Any]] = field(default_factory=list)
    rate_limited: bool = False
    failed: bool = False
    error_msg: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "tab_num": self.tab_num,
            "videos": self.videos,
            "docs": self.docs,
            "rate_limited": self.rate_limited,
            "failed": self.failed,
            "error": self.error_msg,
            "counts": {"videos": len(self.videos), "docs": len(self.docs)},
        }

    @property
    def is_empty(self) -> bool:
        return not self.videos and not self.docs and not self.rate_limited and not self.failed


@dataclass
class LessonScanResult:
    """一个 lesson 的扫描结果(多 tab 聚合 + 漏扫标记)。"""
    ch_num: int
    ls_num: int
    chapter: str
    lesson: str
    lesson_id: str
    tabs: list[TabResult] = field(default_factory=list)
    videos: list[dict[str, Any]] = field(default_factory=list)
    docs: list[dict[str, Any]] = field(default_factory=list)
    unknown_resources: list[dict[str, Any]] = field(default_factory=list)
    flags: list[str] = field(default_factory=list)  # suspicious_missing_ppt 等

    def to_dict(self) -> dict[str, Any]:
        return {
            "ch_num": self.ch_num,
            "ls_num": self.l_num if hasattr(self, "l_num") else self.ls_num,
            "chapter": self.chapter,
            "lesson": self.lesson,
            "lesson_id": self.lesson_id,
            "tabs": [t.to_dict() for t in self.tabs],
            "videos": self.videos,
            "docs": self.docs,
            "unknown_resources": self.unknown_resources,
            "flags": self.flags,
            "counts": {
                "videos": len(self.videos),
                "docs": len(self.docs),
                "unknown": len(self.unknown_resources),
                "tabs_scanned": len(self.tabs),
                "tabs_failed": sum(1 for t in self.tabs if t.failed),
                "tabs_rate_limited": sum(1 for t in self.tabs if t.rate_limited),
            },
        }


@dataclass
class ScanContext:
    """一次完整扫描的上下文(全部 lesson + 全局 summary + 异常标记)。"""
    course_id: str
    course_title: str = ""
    lessons: list[LessonScanResult] = field(default_factory=list)
    all_videos: list[dict[str, Any]] = field(default_factory=list)
    all_docs: list[dict[str, Any]] = field(default_factory=list)
    all_unknown: list[dict[str, Any]] = field(default_factory=list)
    failed_tabs: list[dict[str, Any]] = field(default_factory=list)
    suspicious_lessons: list[dict[str, Any]] = field(default_factory=list)
    stopped_reason: str = ""  # 限流 / 连续空 / 异常

    def to_dict(self) -> dict[str, Any]:
        return {
            "course_id": self.course_id,
            "course_title": self.course_title,
            "summary": self.summary(),
            "lessons": [ls.to_dict() for ls in self.lessons],
            "failed_tabs": self.failed_tabs,
            "suspicious_lessons": self.suspicious_lessons,
            "stopped_reason": self.stopped_reason,
        }

    def summary(self) -> dict[str, Any]:
        return {
            "discovered": len(self.all_videos) + len(self.all_docs) + len(self.all_unknown),
            "downloadable": len(self.all_videos) + len(self.all_docs),
            "videos": len(self.all_videos),
            "docs": len(self.all_docs),
            "unknown": len(self.all_unknown),
            "failed_tabs": len(self.failed_tabs),
            "suspicious_lessons": len(self.suspicious_lessons),
            "lessons_total": len(self.lessons),
        }


# ─── 智能 role 识别 ─────────────────────────────────────

def detect_resource_role(
    *,
    type_or_mimetype: str = "",
    filename: str = "",
    title: str = "",
    tab_num: int | None = None,
    same_lesson_resources: list[dict[str, Any]] | None = None,
) -> str:
    """智能判定资源 role。

    优先级(命中即返回,后跳过):
      1. type/mimetype 字段(_TYPE_TO_ROLE 命中)
      2. 文件扩展名(_EXT_TO_ROLE 命中)
      3. 标题/文件名关键字(_TITLE_KEYWORDS 命中) — 特别能识别 English / quiz
      4. 同节资源结构(其他资源都是 video → 这个可能是 video)
      5. tab_num 兜底(_TAB_FALLBACK 命中)
      6. 仍不确定 → ROLE_UNKNOWN(不丢,进入报告)

    重要:unknown 必须返回 "unknown",**绝不**默认 "video" 或 "attachment"。
    漏报风险:默认 video 会让真 unknown 资源走下载循环(浪费请求)
    """
    # 1. type/mimetype(精确)
    if type_or_mimetype:
        t = type_or_mimetype.lower()
        if t in _TYPE_TO_ROLE:
            return _TYPE_TO_ROLE[t]

    # 2. 文件扩展名(优先于 type 后缀,避免 ".mp4" 误判英文版)
    if filename:
        import os
        ext = os.path.splitext(filename)[1].lstrip(".").lower()
        if ext in _EXT_TO_ROLE:
            base_role = _EXT_TO_ROLE[ext]
            # 但如果有 English 关键字 → 优先 english(override 扩展名)
            # 复用 english_detect 的关键字(避免正则两处不一致)
            from .english_detect import _is_english_text
            if _is_english_text(filename) or _is_english_text(title):
                return ROLE_ENGLISH
            return base_role

    # 3. 标题/文件名关键字(English / PPT / quiz / note 等)
    text = f"{title} {filename}".strip()
    if text:
        for pattern, role in _TITLE_KEYWORDS:
            if pattern.search(text):
                return role

    # 4. type/mimetype 后缀扩展名兜底(没 filename 时用,例如只给 ".mp4")
    if type_or_mimetype:
        t = type_or_mimetype.lower()
        for ext, role in _EXT_TO_ROLE.items():
            if t.endswith(f".{ext}"):
                return role
        if t.startswith("video/"):
            return ROLE_VIDEO
        if t.startswith("image/"):
            return ROLE_ATTACHMENT
        if t.startswith("audio/"):
            return ROLE_VIDEO

    # 5. 同节资源结构(看其他资源 role 是什么,跟多数)
    if same_lesson_resources:
        roles = [r.get("role") for r in same_lesson_resources if r.get("role")]
        if roles:
            most_common = Counter(roles).most_common(1)[0][0]
            if most_common in (ROLE_VIDEO, ROLE_PPT, ROLE_PDF):
                return most_common

    # 6. tab_num 兜底
    if tab_num is not None and tab_num in _TAB_FALLBACK:
        return _TAB_FALLBACK[tab_num]

    # 7. unknown — 不丢
    return ROLE_UNKNOWN


# ─── 多 tab 扫描(纯函数) ─────────────────────────────

# Tab fetcher 类型:接受 tab_num,返回 (videos, docs, rate_limited, failed, error_msg)
TabFetcher = Callable[[int], tuple[list[dict[str, Any]], list[dict[str, Any]], bool, bool, str]]


def scan_lesson_tabs(
    fetcher: TabFetcher,
    *,
    max_tabs: int = DEFAULT_MAX_TABS,
) -> tuple[list[TabResult], str]:
    """对单节课做多 tab 扫描。

    行为:
      - 从 tab_num=0 开始,逐个递增到 max_tabs-1
      - 连续 CONSEC_EMPTY_STOP 个空 tab → 停(写"consecutive_empty_tabs"到 stopped_reason)
      - 连续 CONSEC_202_STOP 个 202 限流 → 停(写"rate_limited"到 stopped_reason)
      - 单个 tab failed → 记到 TabResult.failed,继续
      - 单个 tab 限流 → 记到 TabResult.rate_limited,继续(但 consec counter 触发后停)

    Args:
        fetcher: 单个 tab 拉取函数,签名为 (tab_num) -> (videos, docs, rate_limited, failed, error_msg)
        max_tabs: 最大 tab 探测数(默认 4)

    Returns:
        (tabs, stopped_reason)
    """
    tabs: list[TabResult] = []
    consec_empty = 0
    consec_202 = 0

    for tab_num in range(max_tabs):
        try:
            videos, docs, rl, failed, err = fetcher(tab_num)
        except Exception as e:
            videos, docs = [], []
            rl, failed, err = False, True, f"{type(e).__name__}: {e}"

        tr = TabResult(
            tab_num=tab_num,
            videos=videos,
            docs=docs,
            rate_limited=rl,
            failed=failed,
            error_msg=err,
        )
        tabs.append(tr)

        if rl:
            consec_202 += 1
            consec_empty = 0
            if consec_202 >= CONSEC_202_STOP:
                return tabs, "rate_limited"
        elif failed:
            consec_202 = 0
            consec_empty = 0
            # failed tab 继续(单点错误不应阻塞其他 tab)
        elif tr.is_empty:
            consec_empty += 1
            consec_202 = 0
            if consec_empty >= CONSEC_EMPTY_STOP:
                return tabs, "consecutive_empty_tabs"
        else:
            consec_empty = 0
            consec_202 = 0

    return tabs, ""


# ─── 漏扫检测 ─────────────────────────────────────────

def detect_suspicious_lessons(lessons: list[LessonScanResult]) -> None:
    """原地修改每个 lesson 的 flags,标记漏扫。

    标记规则:
      - empty_lesson:本节 0 资源(videos + docs + unknown 都没有)
      - suspicious_missing_ppt:同章其他节大多数有 PPT,这一节没有
      - suspicious_missing_english:同章其他节大多数有英文视频,这一节没有
      - tab_failed:本节有 tab failed
    """
    # 按 chapter 分桶
    by_chapter: dict[int, list[LessonScanResult]] = {}
    for ls in lessons:
        by_chapter.setdefault(ls.ch_num, []).append(ls)

    for ch_num, chapter_lessons in by_chapter.items():
        # 统计同章其他节的资源类型
        other_ppt = 0
        other_english = 0
        other_total = 0
        for ls in chapter_lessons:
            if any(r.get("role") == ROLE_PPT for r in ls.docs):
                other_ppt += 1
            if any(r.get("role") == ROLE_ENGLISH for r in ls.videos):
                other_english += 1
            other_total += 1

        # 阈值:同章 >= 50% 节有 → 算"大多数"
        # 注意:other_total 自身算上(我们要看"本节"和"其他节"对比)
        # 但 other_total 包含全部 chapter_lessons,本节也得有资源才被统计
        # 简化:只要同章 2 节以上,且有 1 节有 X,就算 majority(对小章友好)
        ppt_majority = other_ppt >= max(1, len(chapter_lessons) // 2)
        english_majority = other_english >= max(1, len(chapter_lessons) // 2)

        for ls in chapter_lessons:
            has_resource = bool(ls.videos or ls.docs or ls.unknown_resources)
            if not has_resource:
                ls.flags.append("empty_lesson")

            has_ppt = any(r.get("role") == ROLE_PPT for r in ls.docs)
            has_english = any(r.get("role") == ROLE_ENGLISH for r in ls.videos)

            if ppt_majority and not has_ppt:
                ls.flags.append("suspicious_missing_ppt")
            if english_majority and not has_english:
                ls.flags.append("suspicious_missing_english")

            if any(t.failed for t in ls.tabs):
                ls.flags.append("tab_failed")


# ─── 报告生成 ─────────────────────────────────────────

def build_scan_context(
    *,
    course_id: str,
    course_title: str,
    lessons: list[LessonScanResult],
    stopped_reason: str = "",
) -> ScanContext:
    """从 lessons 列表构造 ScanContext,自动填充 all_videos / all_docs / failed_tabs / suspicious_lessons。"""
    # 漏扫检测(由 build_scan_context 内部统一调用,避免外部 + 内部双调导致 flag 重复)
    detect_suspicious_lessons(lessons)
    ctx = ScanContext(
        course_id=course_id,
        course_title=course_title,
        lessons=lessons,
        stopped_reason=stopped_reason,
    )
    for ls in lessons:
        ctx.all_videos.extend(ls.videos)
        ctx.all_docs.extend(ls.docs)
        ctx.all_unknown.extend(ls.unknown_resources)
        for t in ls.tabs:
            if t.failed:
                ctx.failed_tabs.append({
                    "ch_num": ls.ch_num, "ls_num": ls.ls_num,
                    "lesson": ls.lesson, "tab_num": t.tab_num,
                    "error": t.error_msg,
                })
        if ls.flags:
            ctx.suspicious_lessons.append({
                "ch_num": ls.ch_num, "ls_num": ls.ls_num,
                "lesson": ls.lesson, "flags": list(ls.flags),
            })
    return ctx


def write_scan_reports(ctx: ScanContext, output_dir: Path) -> dict[str, Path]:
    """写 4 个报告文件到 output_dir,返回路径字典。

    - _scanned_resources.json:全部 lesson 的扫描原始数据(每节 tabs/videos/docs/flags)
    - _resource_discovery_report.json:summary + failed_tabs + suspicious_lessons
    - _resource_discovery_report.md:人类可读报告
    - _chapter_tree.json/md:从 ctx 重建章节目录(已在 resource_manifest 实现,
      这里复用,确保和 scan 一致)
    """
    import json
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    paths: dict[str, Path] = {}

    # 1) _scanned_resources.json
    p1 = output_dir / "_scanned_resources.json"
    p1.write_text(
        json.dumps(ctx.to_dict(), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    paths["scanned_resources"] = p1

    # 2) _resource_discovery_report.json(只含 summary + 异常,不重复 lesson 详情)
    p2 = output_dir / "_resource_discovery_report.json"
    p2.write_text(
        json.dumps({
            "course_id": ctx.course_id,
            "course_title": ctx.course_title,
            "summary": ctx.summary(),
            "failed_tabs": ctx.failed_tabs,
            "suspicious_lessons": ctx.suspicious_lessons,
            "stopped_reason": ctx.stopped_reason,
        }, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    paths["discovery_report_json"] = p2

    # 3) _resource_discovery_report.md
    p3 = output_dir / "_resource_discovery_report.md"
    p3.write_text(_render_discovery_md(ctx), encoding="utf-8")
    paths["discovery_report_md"] = p3

    return paths


def _render_discovery_md(ctx: ScanContext) -> str:
    s = ctx.summary()
    lines: list[str] = []
    lines.append(f"# 资源发现报告 — {ctx.course_title or ctx.course_id}")
    lines.append("")
    lines.append(f"- 课程 ID:`{ctx.course_id}`")
    lines.append(f"- 课程标题:{ctx.course_title or '(未提供)'}")
    lines.append(f"- 扫描停止原因:`{ctx.stopped_reason or '正常完成'}`")
    lines.append("")
    lines.append("## 摘要")
    lines.append("")
    lines.append(f"- 发现资源总数:**{s['discovered']}** "
                 f"(视频 {s['videos']} + 文档 {s['docs']} + 未知 {s['unknown']})")
    lines.append(f"- 可下载资源:{s['downloadable']}")
    lines.append(f"- 失败 Tab 数:{s['failed_tabs']}")
    lines.append(f"- 可疑节数:{s['suspicious_lessons']}")
    lines.append(f"- 扫描节数:{s['lessons_total']}")
    lines.append("")

    if ctx.failed_tabs:
        lines.append("## 失败 Tab")
        lines.append("")
        lines.append("| 章 | 节 | 节标题 | Tab | 错误 |")
        lines.append("|---|---|---|---|---|")
        for ft in ctx.failed_tabs:
            lines.append(f"| {ft['ch_num']} | {ft['ls_num']} | {ft['lesson']} | "
                         f"{ft['tab_num']} | `{ft['error']}` |")
        lines.append("")

    if ctx.suspicious_lessons:
        lines.append("## 可疑节(漏扫)")
        lines.append("")
        lines.append("| 章 | 节 | 节标题 | 标记 |")
        lines.append("|---|---|---|---|")
        for sl in ctx.suspicious_lessons:
            flags = ", ".join(sl["flags"])
            lines.append(f"| {sl['ch_num']} | {sl['ls_num']} | {sl['lesson']} | {flags} |")
        lines.append("")

    lines.append("## 每节资源详情")
    lines.append("")
    for ls in ctx.lessons:
        lines.append(f"### 第 {ls.ch_num} 章 第 {ls.ls_num} 节 — {ls.lesson}")
        lines.append("")
        if ls.flags:
            lines.append(f"**标记**: {', '.join(ls.flags)}")
            lines.append("")
        if ls.videos:
            lines.append("**视频**:")
            for v in ls.videos:
                role = v.get("role", ROLE_UNKNOWN)
                tab = v.get("tab_num", "?")
                title = v.get("title") or v.get("name", "")
                lines.append(f"- `tab={tab}` [{role}] {title} "
                             f"(`{v.get('objectid','')}`)")
            lines.append("")
        if ls.docs:
            lines.append("**文档**:")
            for d in ls.docs:
                role = d.get("role", ROLE_UNKNOWN)
                tab = d.get("tab_num", "?")
                title = d.get("title") or d.get("name", "")
                lines.append(f"- `tab={tab}` [{role}] {title} "
                             f"(`{d.get('objectid','')}`)")
            lines.append("")
        if ls.unknown_resources:
            lines.append("**未知资源**(需人工分类):")
            for u in ls.unknown_resources:
                tab = u.get("tab_num", "?")
                title = u.get("title") or u.get("name", "")
                lines.append(f"- `tab={tab}` [unknown] {title} "
                             f"(`{u.get('objectid','')}`)")
            lines.append("")
        if not (ls.videos or ls.docs or ls.unknown_resources):
            lines.append("_(无资源)_")
            lines.append("")

    return "\n".join(lines)
