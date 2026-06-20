"""
上传章末习题 docx 到 next-studio.xuetangx.com 老师后台。

6 步流程(HAR next-studio.xuetangx.com.homework.har 反推):

  1. 获取元数据:user_info / library_id / resource_tree
  2. 上传 docx 到 Qiniu → 拿文件 URL
  3. async_upload_docx → 轮询解析状态 → 拿 ParsedProblem 列表(含 TemplateID)
  4. batch_import_problems → 题目入库(得 ProblemID)
  5. batch_edit_problem(×3,按题型) → 设分值/答案
  6. create_exercise → create_leaf → 挂到章节

复用 scrape.upload.api_uploader:
  - _make_session() / _build_context() / _base_headers() — 认证与 headers
  - 常量:BASE_URL / USER_AGENT / HTTP_TIMEOUT

用法:
  from scrape_new.upload.exercise_upload import upload_exercise
  result = upload_exercise(ctx, exercise_docx_path, chapter_id, leaf_name="章末测试")

设计:
  - 纯 requests,无浏览器依赖
  - 每步有独立函数,方便单独重试
  - 问题:答案嵌入题干文本(如"（B）"),需要分词法解析 docx
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import requests

from .api_uploader import (
    TeacherContext,
    _make_session,
    _build_context,
    _base_headers,
    BASE_URL,
    USER_AGENT,
    HTTP_TIMEOUT,
)

logger = logging.getLogger(__name__)

# ─── 常量 ──────────────────────────────────────────────────────

QINIU_TOKEN_URL = f"{BASE_URL}/api/open/yunpan/qiniu/token"
USER_INFO_URL = f"{BASE_URL}/api/web_lesson/user_info/"
LIBRARY_URL = (
    f"{BASE_URL}/c27/online_courseware/problem_library"
    f"/instance_corresponding_library"
)
ASYNC_UPLOAD_URL = f"{BASE_URL}/parser/v1/async_upload_docx"
UPLOAD_STATUS_URL = f"{BASE_URL}/parser/v1/upload_docx_status"
UPLOAD_RESULT_URL = f"{BASE_URL}/parser/v1/upload_docx_result"
BATCH_IMPORT_URL = (
    f"{BASE_URL}/c27/online_courseware/problem_library"
    f"/batch_import_problems"
)
BATCH_EDIT_URL = (
    f"{BASE_URL}/c27/online_courseware/problem/batch_edit_problem/"
)
CREATE_EXERCISE_URL = (
    f"{BASE_URL}/c27/online_courseware/exercise/create_exercise/"
)
CREATE_LEAF_URL = (
    f"{BASE_URL}/c27/online_courseware/instance"
    f"/resource_tree/create_leaf/"
)

# 题型映射:docx 中的题型标题 → API 所需的 ProblemType / Type 字段
TYPE_MAP = {
    "单项选择题": {"ProblemType": 1, "Type": "SingleChoice", "TypeText": "单选题"},
    "多项选择题": {"ProblemType": 2, "Type": "MultipleChoice", "TypeText": "多选题"},
    "填空题":      {"ProblemType": 4, "Type": "FillBlank",       "TypeText": "填空题"},
    "判断题":      {"ProblemType": 6, "Type": "Judgement",       "TypeText": "判断题"},
}


@dataclass
class ExerciseUploadResult:
    """上传结果,便于报告。"""
    exercise_id: str = ""
    leaf_id: str = ""
    problem_count: int = 0
    chapter_id: str = ""
    diagnostics: dict[str, Any] = field(default_factory=dict)


class ExerciseValidationError(RuntimeError):
    """docx 解析/导入结果不满足期望(如题数不符、答案字段为空)。

    携带 diagnostics 字段,调用方可以写到 _exercise_validation_<ts>.json。
    """

    def __init__(self, message: str, diagnostics: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.diagnostics = diagnostics or {}


# ─── 校验:docx 解析/导入后的题目质量 ──────────────────────────

def validate_parsed_problems(
    problems: list[dict[str, Any]],
    expected_counts: dict[str, int] | None = None,
    *,
    require_answers: bool = True,
) -> dict[str, Any]:
    """校验 docx 解析/导入后的 problems 列表。

    Args:
        problems: 解析/导入后的题目 dict 列表
        expected_counts: 期望题型分布,如 {"SingleChoice": 20, "MultipleChoice": 20, "Judgement": 10}
                        None = 不做题型/题数校验(只校验答案字段)
        require_answers: 是否要求每题有答案字段(单选/多选/判断)

    Returns:
        diagnostics 字典,字段:
          - ok: bool
          - actual_count: int
          - type_counts: {"SingleChoice": n, ...}
          - expected_counts: 同入参
          - missing_types: [] (期望有但实际没有的题型)
          - type_mismatch: {type: {"expected": n, "actual": m}}
          - questions_without_answer: [index, ...]
          - missing_question_numbers: [] (期望 1..N 但实际缺的)
    """
    actual_count = len(problems)
    type_counts: dict[str, int] = {}
    for p in problems:
        t = p.get("Type", "Unknown")
        type_counts[t] = type_counts.get(t, 0) + 1

    diagnostics: dict[str, Any] = {
        "ok": True,
        "actual_count": actual_count,
        "type_counts": type_counts,
        "expected_counts": expected_counts or {},
        "missing_types": [],
        "type_mismatch": {},
        "questions_without_answer": [],
        "missing_question_numbers": [],
    }

    # 1) 期望题型分布
    if expected_counts:
        # 缺的题型
        for t, n in expected_counts.items():
            if type_counts.get(t, 0) == 0 and n > 0:
                diagnostics["missing_types"].append(t)
        # 不匹配的题型
        for t, expected in expected_counts.items():
            actual = type_counts.get(t, 0)
            if actual != expected:
                diagnostics["type_mismatch"][t] = {"expected": expected, "actual": actual}
        # 总数不匹配
        expected_total = sum(expected_counts.values())
        if actual_count != expected_total:
            diagnostics["ok"] = False
            diagnostics["type_mismatch"]["__total__"] = {
                "expected": expected_total, "actual": actual_count,
            }

    # 2) 答案字段校验
    if require_answers:
        for i, p in enumerate(problems):
            t = p.get("Type", "")
            if t in ("SingleChoice", "MultipleChoice", "Judgement"):
                # 答案在 Body 字段、Answer 字段或 Options 里
                has_answer = bool(
                    p.get("Answer") or p.get("answer")
                    or p.get("Body") and "(正确答案" in str(p.get("Body", ""))
                )
                if not has_answer:
                    diagnostics["questions_without_answer"].append(i)
            elif t == "FillBlank":
                # 填空题用 Blanks[].Answers
                blanks = p.get("Blanks") or []
                if not blanks or not blanks[0].get("Answers"):
                    diagnostics["questions_without_answer"].append(i)
        if diagnostics["questions_without_answer"]:
            diagnostics["ok"] = False

    # 3) 题号连续性(如果 problem 有 Number 字段)
    numbers = [p.get("Number") for p in problems if p.get("Number") is not None]
    if numbers and expected_counts:
        try:
            nums_int = sorted(int(n) for n in numbers)
            expected_n = set(range(1, sum(expected_counts.values()) + 1))
            missing = sorted(expected_n - set(nums_int))
            if missing:
                diagnostics["missing_question_numbers"] = missing
                diagnostics["ok"] = False
        except (ValueError, TypeError):
            pass  # Number 字段不是数字,跳过

    return diagnostics


# ─── 步骤 1:获取元数据 ──────────────────────────────────────────

def _fetch_user_info(ctx: TeacherContext) -> dict[str, Any]:
    """获取 user_id 和 auth token(parser 接口需要)。"""
    resp = ctx.session.get(USER_INFO_URL, timeout=HTTP_TIMEOUT)
    resp.raise_for_status()
    data = resp.json()
    if not data.get("success"):
        raise RuntimeError(f"user_info 失败: {data.get('msg', 'unknown')}")
    result = data.get("data", {})
    return {
        "user_id": result.get("user_id"),
        "auth": result.get("auth", ""),
    }


def _fetch_library_id(ctx: TeacherContext) -> int:
    """获取课程的题库 library_id。"""
    resp = ctx.session.get(LIBRARY_URL, timeout=HTTP_TIMEOUT)
    resp.raise_for_status()
    data = resp.json()
    if not data.get("success") and data.get("code") != 0:
        raise RuntimeError(f"获取 library_id 失败: {data}")
    library_id = data.get("data", {}).get("library_id", 0)
    if library_id == 0:
        raise RuntimeError(f"未找到课程题库: library_id=0, resp={data}")
    return library_id


def _fetch_chapter_tree(ctx: TeacherContext) -> list[dict[str, Any]]:
    """获取课程章节树,返回 chapter_list。"""
    url = (
        f"{BASE_URL}/c27/online_courseware/instance"
        f"/resource_tree/get_resource_tree/2/{ctx.course_id}/"
    )
    resp = ctx.session.get(url, timeout=HTTP_TIMEOUT)
    resp.raise_for_status()
    data = resp.json()
    return data.get("data", {}).get("chapter_list", [])


# ─── 步骤 2:上传 docx 到 Qiniu ──────────────────────────────────

def _get_qiniu_token(ctx: TeacherContext) -> dict[str, Any]:
    """获取 Qiniu 上传 token。"""
    resp = ctx.session.get(QINIU_TOKEN_URL, timeout=HTTP_TIMEOUT)
    resp.raise_for_status()
    data = resp.json()
    if not data.get("success"):
        raise RuntimeError(f"Qiniu token 失败: {data}")
    return data.get("data", {})  # {token, domain, ...}


def _upload_to_qiniu(
    ctx: TeacherContext,
    docx_path: Path,
    qiniu_data: dict[str, Any],
) -> str:
    """上传 docx 到 Qiniu,返回文件的公开 URL。"""
    docx_path = Path(docx_path)
    token = qiniu_data.get("token", "")
    domain = qiniu_data.get("domain", "https://qn-sy.yuketang.cn")

    # 生成 key = 时间戳 + 文件名
    ts = int(time.time() * 1000)
    key = f"{ts}.{docx_path.name}"

    with open(docx_path, "rb") as f:
        file_data = f.read()

    # multipart/form-data 上传
    resp = ctx.session.post(
        "https://upload.qiniup.com/",
        files={"file": (key, file_data, "application/vnd.openxmlformats-officedocument.wordprocessingml.document")},
        data={"token": token, "key": key},
        timeout=120,
    )
    resp.raise_for_status()

    # Qiniu 直接返回 JSON
    result = resp.json()
    return f"{domain}/{result.get('key', key)}"


# ─── 步骤 3:解析 docx ──────────────────────────────────────────

def _async_upload_docx(
    ctx: TeacherContext,
    file_url: str,
    library_id: int,
    user_id: int,
    auth: str,
) -> str:
    """提交 docx 解析任务,返回 task_id。"""
    body = {
        "url": file_url,
        "library_id": library_id,
        "user_id": user_id,
        "folder_id": 0,
        "auth": auth,
    }
    headers = {"Content-Type": "application/json"}
    resp = ctx.session.post(
        ASYNC_UPLOAD_URL,
        json=body,
        headers=headers,
        timeout=HTTP_TIMEOUT,
    )
    resp.raise_for_status()
    data = resp.json()
    if not data.get("success"):
        raise RuntimeError(f"async_upload_docx 失败: {data}")
    task_id = data.get("data", {}).get("task_id", "")
    if not task_id:
        raise RuntimeError(f"async_upload_docx 未返回 task_id: {data}")
    return task_id


def _poll_parse_status(
    ctx: TeacherContext,
    task_id: str,
    user_id: int,
    auth: str,
    max_wait: int = 60,
) -> bool:
    """轮询解析状态,直到完成或超时。返回是否成功。"""
    body = {
        "task_id": task_id,
        "user_id": user_id,
        "auth": auth,
    }
    headers = {"Content-Type": "application/json"}
    deadline = time.time() + max_wait

    while time.time() < deadline:
        resp = ctx.session.post(
            UPLOAD_STATUS_URL,
            json=body,
            headers=headers,
            timeout=HTTP_TIMEOUT,
        )
        resp.raise_for_status()
        data = resp.json()
        status = data.get("data", {}).get("status", "")
        if status in ("done", "SUCCESS"):
            return True
        if status in ("failed", "FAILURE"):
            raise RuntimeError(f"docx 解析失败: {data}")
        time.sleep(2)

    raise TimeoutError(f"docx 解析超时({max_wait}s),task_id={task_id}")


def _get_parse_result(
    ctx: TeacherContext,
    task_id: str,
    user_id: int,
    auth: str,
) -> list[dict[str, Any]]:
    """获取解析结果,返回 problems 列表(含 TemplateID)。"""
    body = {
        "task_id": task_id,
        "user_id": user_id,
        "auth": auth,
    }
    headers = {"Content-Type": "application/json"}
    resp = ctx.session.post(
        UPLOAD_RESULT_URL,
        json=body,
        headers=headers,
        timeout=HTTP_TIMEOUT,
    )
    resp.raise_for_status()
    data = resp.json()
    if not data.get("success"):
        raise RuntimeError(f"获取解析结果失败: {data}")
    result = data.get("data", {})
    if not result.get("is_ok"):
        raise RuntimeError(
            f"解析异常: 成功{result.get('success_count')}, "
            f"失败{result.get('failure_count')}, "
            f"重复{result.get('duplicate_count')}"
        )
    problems = result.get("problems", [])
    if not problems:
        logger.warning("docx 解析结果为空")
    return problems


# ─── 步骤 4:题目入库 ────────────────────────────────────────────

def _batch_import(
    ctx: TeacherContext,
    library_id: int,
    template_ids: list[int],
) -> list[dict[str, Any]]:
    """批量导入题目到题库,返回含 ProblemID 的题目列表。"""
    body = {
        "library_id": library_id,
        "template_ids": template_ids,
        "strategies": [3],
        "reorder": 1,
    }
    resp = ctx.session.post(
        BATCH_IMPORT_URL,
        json=body,
        headers=_base_headers(ctx),
        timeout=HTTP_TIMEOUT,
    )
    resp.raise_for_status()
    data = resp.json()
    if not data.get("success"):
        raise RuntimeError(f"batch_import_problems 失败: {data}")
    return data.get("data", {}).get("problems", [])


# ─── 步骤 5:设置分值与答案 ──────────────────────────────────────

def _batch_edit_by_type(
    ctx: TeacherContext,
    problems: list[dict[str, Any]],
    library_id: int,
    score_single: int = 8,
    score_multiple: int = 8,
    score_judgement: int = 4,
) -> None:
    """按题型分组,分别调 batch_edit_problem 设置分值/答案/解析。

    单选题:Score=8, HalfScore=0
    多选题:Score=8, HalfScore=0, awardingRule=1
    判断题:Score=4
    """
    groups: dict[str, list[dict[str, Any]]] = {
        "SingleChoice": [],
        "MultipleChoice": [],
        "FillBlank": [],
        "Judgement": [],
    }

    for p in problems:
        ptype = p.get("Type", "")
        if ptype in groups:
            groups[ptype].append(p)
        else:
            logger.warning("未知题型 %s,跳过", ptype)

    for ptype, plist in groups.items():
        if not plist:
            continue

        # 构造 body:每题的完整 JSON(含 Score / Answer / Options 等)
        problem_list = []
        for p in plist:
            entry = dict(p)  # shallow copy
            entry["library_id"] = library_id
            entry["isContinueWithWrong"] = 0
            entry["max_retry"] = 1
            entry["difficulty"] = 1
            entry["source"] = ""
            entry["data"] = {}
            entry["FolderID"] = 0
            entry["create_template_type"] = "word_parse_docx_v2"

            if ptype == "SingleChoice":
                entry["Score"] = score_single
                entry["score"] = score_single
            elif ptype == "MultipleChoice":
                entry["Score"] = score_multiple
                entry["score"] = score_multiple
                entry["HalfScore"] = 0
                entry["awardingRule"] = 1
            elif ptype == "FillBlank":
                entry["Score"] = score_judgement  # 填空题复用判断题分值(期末考试时统一为2)
                entry["score"] = score_judgement
            elif ptype == "Judgement":
                entry["Score"] = score_judgement
                entry["score"] = score_judgement

            problem_list.append(entry)

        body = {"problem_list": problem_list}
        resp = ctx.session.post(
            BATCH_EDIT_URL,
            json=body,
            headers=_base_headers(ctx),
            timeout=HTTP_TIMEOUT,
        )
        resp.raise_for_status()
        data = resp.json()
        if not data.get("success"):
            fail = data.get("data", {}).get("fail_list", [])
            raise RuntimeError(
                f"batch_edit({ptype}) 失败: fail_list={fail}"
            )
        print(f"  [编辑] {ptype}: {len(problem_list)} 题,分值 {problem_list[0].get('Score')} 分")
        # 回写分值到原始 problems,使 _create_exercise 的 content 字段携带正确分数
        for entry, orig in zip(problem_list, plist):
            for k in ("Score", "score", "HalfScore", "awardingRule"):
                if k in entry:
                    orig[k] = entry[k]


# ─── 步骤 6:创建 exercise + 挂 leaf ─────────────────────────────

def _create_exercise(
    ctx: TeacherContext,
    problems: list[dict[str, Any]],
    name: str = "章末测试",
    score_single: int = 8,
    score_multiple: int = 8,
    score_judgement: int = 4,
) -> int:
    """创建 exercise(作业),返回 exercise_id。"""
    template_ids = [p.get("TemplateID") or p.get("template_id") for p in problems]
    problem_ids = [p.get("ProblemID") or p.get("problem_id") for p in problems]

    # 按题型注入分值到 content(create_exercise 用 content 里的分值,
    # 不走 batch_edit 的间接路径,避免 Version 乐观锁问题)
    def _with_score(p: dict[str, Any]) -> dict[str, Any]:
        ptype = p.get("Type", "")
        v: int = 0
        if ptype == "SingleChoice":
            v = score_single
        elif ptype == "MultipleChoice":
            v = score_multiple
            p.setdefault("HalfScore", 0)
            p.setdefault("awardingRule", 1)
        elif ptype == "FillBlank":
            v = score_judgement  # 共用判断题分值参数
        elif ptype == "Judgement":
            v = score_judgement
        p["Score"] = v
        p["score"] = v
        return p

    body = {
        "name": name,
        "description": "",
        "show_answer": 0,
        "show_answer_time": None,
        "exercise_id": None,
        "open_exercise_type_label": None,
        "_template_id_list": [t for t in template_ids if t],
        "problem_id_list": [pid for pid in problem_ids if pid],
        "problems": [
            {
                "template_id": p.get("TemplateID") or p.get("template_id"),
                "unionid": f"{int(time.time()*1000)}-{i:05d}",
                "actionType": "update",
                "content": p,
            }
            for i, p in enumerate(problems)
        ],
    }
    resp = ctx.session.post(
        CREATE_EXERCISE_URL,
        json=body,
        headers=_base_headers(ctx),
        timeout=HTTP_TIMEOUT,
    )
    resp.raise_for_status()
    data = resp.json()
    if not data.get("success"):
        raise RuntimeError(f"create_exercise 失败: {data}")
    exercise_id = data.get("data", {}).get("exercise_id", 0)
    if not exercise_id:
        raise RuntimeError(f"create_exercise 未返回 exercise_id: {data}")
    print(f"  [创建] exercise_id={exercise_id}")
    return exercise_id


def _create_leaf(
    ctx: TeacherContext,
    exercise_id: int,
    chapter_id: int,
    name: str = "章末测试",
    section_id: int = 0,
) -> int:
    """把 exercise 挂到课程章节树上(leaf_type=6)。返回 leaf_id。"""
    body = {
        "chapter_id": chapter_id,
        "section_id": section_id,
        "name": name,
        "leaf_type": 6,          # 6 = homework/exercise
        "leaf_type_id": exercise_id,
        "content_info": {
            "leaf_type_id": exercise_id,
            "is_score": True,
            "score_evaluation": {
                "id": 11,        # 11 = 作业
                "name": "作业",
                "score": 1,
            },
            "teaching_link": 0,
            "related_agent_info": {
                "agent_id": None,
                "agent_scene_ids": [],
                "ai_workflow_id": None,
            },
        },
    }
    resp = ctx.session.post(
        CREATE_LEAF_URL,
        json=body,
        headers=_base_headers(ctx),
        timeout=HTTP_TIMEOUT,
    )
    resp.raise_for_status()
    data = resp.json()
    if not data.get("success"):
        raise RuntimeError(f"create_leaf 失败: {data}")
    leaf_id = data.get("data", {}).get("leaf_id", 0)
    if not leaf_id:
        raise RuntimeError(f"create_leaf 未返回 leaf_id: {data}")
    print(f"  [挂载] leaf_id={leaf_id} → chapter_id={chapter_id}")
    return leaf_id


# ─── 主流程 ─────────────────────────────────────────────────────

def upload_exercise(
    ctx: TeacherContext,
    docx_path: Path,
    chapter_id: int,
    leaf_name: str = "章末测试",
    score_single: int = 8,
    score_multiple: int = 8,
    score_judgement: int = 4,
    expected_counts: dict[str, int] | None = None,
    strict: bool = True,
) -> ExerciseUploadResult:
    """上传章末习题 .docx 并挂到指定章节。

    Args:
        ctx: 认证上下文(TeacherContext)
        docx_path: 习题 .docx 文件路径
        chapter_id: 目标章节 ID(从 resource_tree 获取)
        leaf_name: 习题在章节树上的显示名称
        score_single: 单选分值,默认8
        score_multiple: 多选分值,默认8
        score_judgement: 判断/填空分值,默认4
        expected_counts: 期望题型分布,如 {"SingleChoice": 5, "MultipleChoice": 5, "Judgement": 5}。
                        None = 不做题型/题数校验(只校验答案字段)
        strict: True 时校验失败抛 ExerciseValidationError(不创建 exercise/leaf);
                False 时只打印警告继续执行

    Returns:
        ExerciseUploadResult(exercise_id, leaf_id, problem_count, chapter_id, diagnostics)
    """
    docx_path = Path(docx_path)
    if not docx_path.exists():
        raise FileNotFoundError(f"习题文件不存在: {docx_path}")

    print(f"\n{'='*60}")
    print(f"上传章末习题: {docx_path.name}")
    print(f"{'='*60}")

    # 1. 获取元数据
    print("[1/6] 获取元数据...")
    user = _fetch_user_info(ctx)
    library_id = _fetch_library_id(ctx)
    user_id = user["user_id"]
    auth = user["auth"]
    print(f"  user_id={user_id}, library_id={library_id}")

    # 2. 上传 docx 到 Qiniu
    print("[2/6] 上传 docx 到 Qiniu...")
    qiniu = _get_qiniu_token(ctx)
    file_url = _upload_to_qiniu(ctx, docx_path, qiniu)
    print(f"  file_url={file_url[:80]}...")

    # 3. 解析 docx
    print("[3/6] 解析 docx...")
    task_id = _async_upload_docx(ctx, file_url, library_id, user_id, auth)
    print(f"  task_id={task_id[:16]}...")
    _poll_parse_status(ctx, task_id, user_id, auth)
    problems = _get_parse_result(ctx, task_id, user_id, auth)
    print(f"  解析完成: {len(problems)} 题")

    # 提取 template_ids
    template_ids = [p.get("TemplateID") or p.get("template_id") for p in problems]
    template_ids = [tid for tid in template_ids if tid]
    if not template_ids:
        raise RuntimeError("解析结果中没有有效的 TemplateID")

    # 4. 批量导入题库
    print("[4/6] 批量导入题库...")
    imported = _batch_import(ctx, library_id, template_ids)
    # 用导入后的问题(含 ProblemID)替换原始问题
    if imported:
        # 合并:用 TemplateID 做 key,把 ProblemID 补到原始问题里
        tpl_to_problem: dict[int, dict[str, Any]] = {}
        for imp in imported:
            tid = imp.get("TemplateID") or imp.get("template_id")
            if tid:
                tpl_to_problem[tid] = imp
        for p in problems:
            tid = p.get("TemplateID") or p.get("template_id")
            if tid in tpl_to_problem:
                imp = tpl_to_problem[tid]
                p["ProblemID"] = imp.get("ProblemID") or imp.get("problem_id")
                p["problem_id"] = p["ProblemID"]
                # 更新 Version:导入后版本变,batch_edit 需要新版本
                # 否则服务端乐观锁静默拒绝
                if "Version" in imp:
                    p["Version"] = imp["Version"]
    print(f"  入库: {len(imported)} 题")

    # FillBlank 题解析器不给 TemplateID,但可以无 ProblemID 直接进入 create_exercise
    # 只跳过确实有 TemplateID 但入库失败的题
    has_tid = [p for p in problems if p.get("TemplateID") or p.get("template_id")]
    missing_pid = [p for p in has_tid if not p.get("ProblemID")]
    if missing_pid:
        skipped_types = {p.get("Type","?") for p in missing_pid}
        print(f"  [跳过] {len(missing_pid)} 题入库失败(无 ProblemID),类型: {skipped_types}")
        problems = [p for p in problems if p not in missing_pid]

    if not problems:
        raise RuntimeError("没有可导入的题目(全部无 ProblemID)")

    # 4.5 校验:题数 / 题型分布 / 答案字段
    print("[4.5/6] 校验解析结果...")
    diagnostics = validate_parsed_problems(
        problems, expected_counts=expected_counts, require_answers=True,
    )
    if not diagnostics["ok"]:
        msg = (
            f"docx 解析结果不通过校验: actual={diagnostics['actual_count']}, "
            f"type_mismatch={diagnostics['type_mismatch']}, "
            f"questions_without_answer={len(diagnostics['questions_without_answer'])}, "
            f"missing_q_numbers={diagnostics['missing_question_numbers']}"
        )
        if strict:
            print(f"  ✗ 校验失败: {msg}")
            # 写诊断 JSON
            diag_path = docx_path.parent / (
                f"_exercise_validation_{docx_path.stem}_"
                f"{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
            )
            diag_path.write_text(
                json.dumps(diagnostics, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            print(f"  诊断详情: {diag_path}")
            raise ExerciseValidationError(msg, diagnostics)
        else:
            print(f"  ⚠ 校验不通过但 strict=False,继续: {msg}")
    else:
        print(f"  ✓ 校验通过: {diagnostics['type_counts']}")

    # 读取填空题答案侧写(解析器不提取答案,需手动注入)
    sidecar_paths = [
        docx_path.with_suffix(".answers.json"),
        docx_path.parent / (docx_path.stem + ".answers.json"),
    ]
    for sp in sidecar_paths:
        if sp.exists():
            try:
                fill_answers = json.loads(sp.read_text(encoding="utf-8"))
                fill_idx = 0
                for p in problems:
                    if p.get("Type") == "FillBlank" and fill_idx < len(fill_answers):
                        if p.get("Blanks"):
                            p["Blanks"][0]["Answers"] = [fill_answers[fill_idx]]
                        fill_idx += 1
                if fill_idx > 0:
                    print(f"  [注入] 填空题答案: {fill_idx} 个 (from {sp.name})")
            except Exception as e:
                print(f"  [警告] 填空答案侧写读取失败: {sp.name}: {e}")
            break

    # 5. 设置分值与答案(按题型分 3 组)
    print("[5/6] 设置分值与答案...")
    _batch_edit_by_type(ctx, problems, library_id,
        score_single=score_single, score_multiple=score_multiple, score_judgement=score_judgement)

    # 6. 创建 exercise + 挂 leaf
    print("[6/6] 创建 exercise 并挂载到章节...")
    exercise_id = _create_exercise(ctx, problems, leaf_name,
        score_single=score_single, score_multiple=score_multiple, score_judgement=score_judgement)
    leaf_id = _create_leaf(ctx, exercise_id, chapter_id, leaf_name)

    return ExerciseUploadResult(
        exercise_id=str(exercise_id),
        leaf_id=str(leaf_id),
        problem_count=len(problems),
        chapter_id=str(chapter_id),
        diagnostics=diagnostics,
    )


def create_final_exam_chapter(ctx: TeacherContext, name: str = "期末考试") -> int:
    """创建期末考试章节,返回 chapter_id。

    用于期末考试上传前自动建章(har 里先 create_chapter 再 create_leaf)。
    """
    url = (
        f"{BASE_URL}/c27/online_courseware/instance"
        f"/resource_tree/create_chapter/2/{ctx.course_id}/"
    )
    body = {"name": name, "is_show": True}
    resp = ctx.session.post(url, json=body, headers=_base_headers(ctx), timeout=HTTP_TIMEOUT)
    resp.raise_for_status()
    data = resp.json()
    if not data.get("success"):
        raise RuntimeError(f"create_chapter '{name}' 失败: {data}")
    chapter_id = data.get("data", {}).get("id") or data.get("data", {}).get("chapter_id", 0)
    if not chapter_id:
        raise RuntimeError(f"create_chapter 未返回 id: {data}")
    print(f"  [建章] {name} → chapter_id={chapter_id}")
    return chapter_id


# ─── 期末考试专用流程 (HAR: /c27/api/exam/) ──────────────────

EXAM_PROBLEM_URL = f"{BASE_URL}/c27/api/exam/problem/"
EXAM_GENERATE_URL = f"{BASE_URL}/c27/api/exam/generate/"


def _exam_get_problems(
    ctx: TeacherContext,
    template_ids: list[int],
) -> list[dict[str, Any]]:
    """POST /c27/api/exam/problem/ — 用 template_id 拿到 ProblemID 等详情。"""
    body = [{"template_id": tid} for tid in template_ids]
    resp = ctx.session.post(
        EXAM_PROBLEM_URL,
        json=body,
        headers=_base_headers(ctx),
        timeout=HTTP_TIMEOUT,
    )
    resp.raise_for_status()
    data = resp.json()
    if not data.get("success"):
        raise RuntimeError(f"exam/problem 失败: {data}")
    problems = data.get("data", [])
    print(f"  [exam/problem] 获取 {len(problems)} 题详情")
    return problems


def _exam_generate(
    ctx: TeacherContext,
    problems: list[dict[str, Any]],
    title: str = "期末考试",
    total_score: int = 100,
    score_per_problem: int = 2,
) -> int:
    """POST /c27/api/exam/generate/ — 创建考试,返回 leaf_type_id。

    HAR 显示:
    - 每题 {"id": ProblemID, "score": N, "Score": N}
    - 多选题额外 {"awardingRule": 1, "HalfScore": 0}
    - update_question: true
    """
    problem_list = []
    for p in problems:
        pid = p.get("ProblemID") or p.get("problem_id")
        entry = {"id": pid, "score": score_per_problem, "Score": score_per_problem}
        # 多选题需要 awardingRule
        if p.get("Type") == "MultipleChoice":
            entry["awardingRule"] = 1
            entry["HalfScore"] = 0
        problem_list.append(entry)

    body = {
        "title": title,
        "description": "",
        "score": total_score,
        "organize_problem_method": 0,
        "has_problem_dict": False,
        "organize_paper_method": 0,
        "problems": problem_list,
        "update_question": True,
    }
    resp = ctx.session.post(
        EXAM_GENERATE_URL,
        json=body,
        headers=_base_headers(ctx),
        timeout=HTTP_TIMEOUT,
    )
    resp.raise_for_status()
    data = resp.json()
    if not data.get("success"):
        raise RuntimeError(f"exam/generate 失败: {data}")
    leaf_type_id = data.get("data", {}).get("id") or data.get("data", {}).get("leaf_type_id", 0)
    if not leaf_type_id:
        # 有些版本直接返回在顶层
        leaf_type_id = data.get("leaf_type_id", 0)
    if not leaf_type_id:
        raise RuntimeError(f"exam/generate 未返回 leaf_type_id: {data}")
    print(f"  [exam/generate] 创建考试 → leaf_type_id={leaf_type_id}")
    return leaf_type_id


def _create_exam_leaf(
    ctx: TeacherContext,
    leaf_type_id: int,
    chapter_id: int,
    name: str = "期末考试",
) -> int:
    """把考试挂到章节树(leaf_type=5, score_evaluation.id=12=考试)。"""
    url = (
        f"{BASE_URL}/c27/online_courseware/instance"
        f"/resource_tree/create_leaf/"
    )
    body = {
        "chapter_id": chapter_id,
        "section_id": 0,
        "name": name,
        "leaf_type": 5,           # 5 = 考试 (不是 6=作业)
        "leaf_type_id": leaf_type_id,
        "content_info": {
            "leaf_type_id": leaf_type_id,
            "is_score": True,
            "score_evaluation": {"id": 12, "name": "考试"},  # 12=考试 (不是11=作业)
            "teaching_link": 0,
        },
    }
    resp = ctx.session.post(
        url,
        json=body,
        headers=_base_headers(ctx),
        timeout=HTTP_TIMEOUT,
    )
    resp.raise_for_status()
    data = resp.json()
    if not data.get("success"):
        raise RuntimeError(f"create_exam_leaf 失败: {data}")
    leaf_id = data.get("data", {}).get("leaf_id") or data.get("data", {}).get("id", 0)
    if not leaf_id:
        raise RuntimeError(f"create_exam_leaf 未返回 leaf_id: {data}")
    print(f"  [建leaf] {name} → leaf_id={leaf_id}")
    return leaf_id


def upload_final_exam(
    ctx: TeacherContext,
    docx_path: Path,
    chapter_id: int,
    leaf_name: str = "期末考试",
    score_per_problem: int = 2,
    expected_counts: dict[str, int] | None = None,
    strict: bool = True,
) -> ExerciseUploadResult:
    """上传期末考试 .docx 并挂到指定章节。

    流程 (HAR 反推):
      1. 获取元数据(user_info / library_id)
      2. 上传 docx 到 Qiniu
      3. async_upload_docx → 轮询解析 → 拿 template_id 列表
      4. /c27/api/exam/problem/ → 用 template_id 拿 ProblemID
      5. /c27/api/exam/generate/ → 创建考试(含分值)
      6. create_leaf(leaf_type=5, score_evaluation.id=12) → 挂到章节

    Args:
        docx_path: 期末考试 .docx
        chapter_id: 目标章节 ID
        leaf_name: 考试显示名(默认 "期末考试")
        score_per_problem: 每题分值(默认 2)
        expected_counts: 期望题型分布,如 {"SingleChoice": 25, "MultipleChoice": 10, "Judgement": 15}。
                        None = 只校验答案字段,不校验题数。
                        **强烈建议传**(如 49/50 或答案字段缺失会 raise,不创建考试 leaf)。
        strict: True 时校验失败 raise ExerciseValidationError(不创建考试 leaf)
    """
    docx_path = Path(docx_path)
    if not docx_path.exists():
        raise FileNotFoundError(f"习题文件不存在: {docx_path}")

    print(f"\n{'='*60}")
    print(f"上传期末考试: {docx_path.name}")
    print(f"{'='*60}")

    # 1. 获取元数据
    print("[1/6] 获取元数据...")
    user = _fetch_user_info(ctx)
    library_id = _fetch_library_id(ctx)
    user_id = user["user_id"]
    auth = user["auth"]
    print(f"  user_id={user_id}, library_id={library_id}")

    # 2. 上传 docx 到 Qiniu
    print("[2/6] 上传 docx 到 Qiniu...")
    qiniu = _get_qiniu_token(ctx)
    file_url = _upload_to_qiniu(ctx, docx_path, qiniu)
    print(f"  file_url={file_url[:80]}...")

    # 3. 解析 docx
    print("[3/6] 解析 docx...")
    task_id = _async_upload_docx(ctx, file_url, library_id, user_id, auth)
    print(f"  task_id={task_id[:16]}...")
    _poll_parse_status(ctx, task_id, user_id, auth)
    parsed_problems = _get_parse_result(ctx, task_id, user_id, auth)
    print(f"  解析完成: {len(parsed_problems)} 题")

    # 4. exam/problem — 用 template_id 拿 ProblemID
    print("[4/6] 获取题目详情...")
    template_ids = [int(p.get("TemplateID") or p.get("template_id", 0)) for p in parsed_problems]
    template_ids = [t for t in template_ids if t > 0]
    if not template_ids:
        raise RuntimeError("解析结果中没有有效的 TemplateID")
    print(f"  {len(template_ids)} 个有效 template_id")
    problems = _exam_get_problems(ctx, template_ids)

    # 4.5 校验:题数 / 题型分布 / 答案字段
    print("[4.5/6] 校验解析结果...")
    diagnostics = validate_parsed_problems(
        problems, expected_counts=expected_counts, require_answers=True,
    )
    if not diagnostics["ok"]:
        msg = (
            f"期末考试 docx 解析不通过校验: actual={diagnostics['actual_count']}, "
            f"expected={expected_counts}, "
            f"type_mismatch={diagnostics['type_mismatch']}, "
            f"questions_without_answer={len(diagnostics['questions_without_answer'])}, "
            f"missing_q_numbers={diagnostics['missing_question_numbers']}"
        )
        if strict:
            print(f"  ✗ 校验失败: {msg}")
            diag_path = docx_path.parent / (
                f"_exam_validation_{docx_path.stem}_"
                f"{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
            )
            diag_path.write_text(
                json.dumps(diagnostics, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            print(f"  诊断详情: {diag_path}")
            raise ExerciseValidationError(msg, diagnostics)
        else:
            print(f"  ⚠ 校验不通过但 strict=False,继续: {msg}")
    else:
        print(f"  ✓ 校验通过: {diagnostics['type_counts']}")

    # 5. exam/generate — 创建考试
    print("[5/6] 创建考试...")
    total_score = len(problems) * score_per_problem
    leaf_type_id = _exam_generate(ctx, problems, leaf_name, total_score, score_per_problem)

    # 6. create_leaf — 挂到章节
    print("[6/6] 挂到章节树...")
    leaf_id = _create_exam_leaf(ctx, leaf_type_id, chapter_id, leaf_name)

    print(f"\n  [完成] 期末考试上传成功! leaf_id={leaf_id}, {len(problems)} 题")
    return ExerciseUploadResult(
        exercise_id=str(leaf_type_id),
        leaf_id=str(leaf_id),
        problem_count=len(problems),
        chapter_id=str(chapter_id),
        diagnostics=diagnostics,
    )

