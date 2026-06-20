"""
测试:exercise_upload.validate_parsed_problems + CLI 解析

覆盖:
  - 题数 / 题型分布校验
  - 答案字段校验(单选/多选/判断/填空)
  - 题号连续性
  - strict 模式下 ExerciseValidationError 抛出(在 upload_exercise / upload_final_exam 中)
  - CLI --expected-counts 解析
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from scrape_new.upload.cli import _parse_expected_counts
from scrape_new.upload.exercise_upload import (
    validate_parsed_problems,
    ExerciseValidationError,
)


# ─── 校验函数单元测试 ────────────────────────────────────────────

def _make_problem(p_type: str, has_answer: bool = True, num: int | None = None) -> dict:
    p: dict = {"Type": p_type}
    if num is not None:
        p["Number"] = num
    if p_type in ("SingleChoice", "MultipleChoice", "Judgement"):
        if has_answer:
            p["Answer"] = "A" if p_type == "SingleChoice" else "AB"
    elif p_type == "FillBlank":
        if has_answer:
            p["Blanks"] = [{"Answers": ["answer"]}]
    return p


class TestValidateTypeCounts:
    def test_match_returns_ok(self):
        problems = (
            [_make_problem("SingleChoice", num=i) for i in range(1, 6)]
            + [_make_problem("MultipleChoice", num=i) for i in range(6, 11)]
            + [_make_problem("Judgement", num=i) for i in range(11, 16)]
        )
        diag = validate_parsed_problems(
            problems,
            expected_counts={"SingleChoice": 5, "MultipleChoice": 5, "Judgement": 5},
        )
        assert diag["ok"] is True
        assert diag["type_counts"]["SingleChoice"] == 5
        assert diag["actual_count"] == 15

    def test_total_mismatch_fails(self):
        problems = [_make_problem("SingleChoice", num=i) for i in range(1, 6)]  # 5 题
        diag = validate_parsed_problems(
            problems,
            expected_counts={"SingleChoice": 5, "MultipleChoice": 5, "Judgement": 5},
        )
        assert diag["ok"] is False
        assert "__total__" in diag["type_mismatch"]

    def test_partial_match_fails(self):
        problems = (
            [_make_problem("SingleChoice", num=i) for i in range(1, 4)]  # 3 题
            + [_make_problem("MultipleChoice", num=i) for i in range(4, 7)]  # 3 题
            # Judgement 缺
        )
        diag = validate_parsed_problems(
            problems,
            expected_counts={"SingleChoice": 5, "MultipleChoice": 5, "Judgement": 5},
        )
        assert diag["ok"] is False
        assert "Judgement" in diag["missing_types"]
        assert diag["type_mismatch"]["Judgement"]["expected"] == 5
        assert diag["type_mismatch"]["Judgement"]["actual"] == 0

    def test_no_expected_counts_skips_type_check(self):
        # expected_counts=None → 只校验答案字段
        problems = [_make_problem("SingleChoice", has_answer=False)]
        diag = validate_parsed_problems(problems, expected_counts=None)
        # 没答案字段 → ok=False
        assert diag["ok"] is False
        # 但 type_mismatch 不应有内容
        assert diag["type_mismatch"] == {}


class TestValidateAnswers:
    def test_questions_without_answer_detected(self):
        problems = [
            _make_problem("SingleChoice", has_answer=True, num=1),
            _make_problem("SingleChoice", has_answer=False, num=2),
            _make_problem("Judgement", has_answer=False, num=3),
        ]
        diag = validate_parsed_problems(problems, expected_counts=None)
        assert diag["ok"] is False
        assert 1 in diag["questions_without_answer"]
        assert 2 in diag["questions_without_answer"]

    def test_fillblank_answer_check(self):
        problems = [
            _make_problem("FillBlank", has_answer=True, num=1),
            _make_problem("FillBlank", has_answer=False, num=2),
        ]
        diag = validate_parsed_problems(problems, expected_counts=None)
        assert diag["ok"] is False
        assert 1 in diag["questions_without_answer"]

    def test_require_answers_false_skips_check(self):
        problems = [_make_problem("SingleChoice", has_answer=False)]
        diag = validate_parsed_problems(
            problems, expected_counts=None, require_answers=False,
        )
        assert diag["ok"] is True
        assert diag["questions_without_answer"] == []


class TestValidateQuestionNumbers:
    def test_missing_question_numbers(self):
        problems = (
            [_make_problem("SingleChoice", num=i) for i in [1, 2, 3, 5]]  # 缺 4
        )
        diag = validate_parsed_problems(
            problems, expected_counts={"SingleChoice": 5},
        )
        assert diag["ok"] is False
        assert 4 in diag["missing_question_numbers"]

    def test_continuous_numbers_ok(self):
        problems = [_make_problem("SingleChoice", num=i) for i in range(1, 6)]
        diag = validate_parsed_problems(
            problems, expected_counts={"SingleChoice": 5},
        )
        assert diag["ok"] is True
        assert diag["missing_question_numbers"] == []


class TestExerciseValidationError:
    def test_error_carries_diagnostics(self):
        e = ExerciseValidationError(
            "test fail",
            diagnostics={"ok": False, "actual_count": 49},
        )
        assert "test fail" in str(e)
        assert e.diagnostics["actual_count"] == 49


# ─── CLI 解析测试 ──────────────────────────────────────────────

class TestParseExpectedCounts:
    def test_basic(self):
        result = _parse_expected_counts("SingleChoice=20,MultipleChoice=20,Judgement=10")
        assert result == {
            "SingleChoice": 20,
            "MultipleChoice": 20,
            "Judgement": 10,
        }

    def test_with_spaces(self):
        result = _parse_expected_counts("SingleChoice = 5 , MultipleChoice = 5")
        assert result == {"SingleChoice": 5, "MultipleChoice": 5}

    def test_none_returns_none(self):
        assert _parse_expected_counts(None) is None

    def test_empty_string_returns_none(self):
        assert _parse_expected_counts("") is None

    def test_invalid_part_skipped(self):
        # 单个 part 没 = 或非整数 → 跳过
        result = _parse_expected_counts("SingleChoice=5,invalid,MultipleChoice=abc")
        assert result == {"SingleChoice": 5}


# ─── 集成:upload_exercise / upload_final_exam 在 strict 模式抛 ──

class TestStrictValidationInUpload:
    """直接 mock HTTP,只验证 strict 模式抛 ExerciseValidationError,
    不调真实 API。"""

    def test_upload_exercise_strict_raises(self, tmp_path: Path):
        # 构造一个虚假的 docx 文件(实际上传会被 mock)
        fake_docx = tmp_path / "fake.docx"
        fake_docx.write_text("dummy")

        # 构造一个 problems 列表(题数不符)— 含 TemplateID 让 batch_import 不抛
        problems = [
            {
                "Type": "SingleChoice", "Number": i, "Answer": "A",
                "ProblemID": 100 + i, "TemplateID": 500 + i,
            }
            for i in range(1, 11)
        ]

        ctx = MagicMock()
        # 让所有内部调用短路,直接返回我们的 problems
        with patch("scrape_new.upload.exercise_upload._fetch_user_info", return_value={"user_id": 1, "auth": "x"}), \
             patch("scrape_new.upload.exercise_upload._fetch_library_id", return_value=1), \
             patch("scrape_new.upload.exercise_upload._upload_to_qiniu", return_value="https://example.com/fake.docx"), \
             patch("scrape_new.upload.exercise_upload._async_upload_docx", return_value="task-id"), \
             patch("scrape_new.upload.exercise_upload._poll_parse_status", return_value=True), \
             patch("scrape_new.upload.exercise_upload._get_parse_result", return_value=problems), \
             patch("scrape_new.upload.exercise_upload._batch_import", return_value=[]):

            from scrape_new.upload.exercise_upload import upload_exercise

            with pytest.raises(ExerciseValidationError) as excinfo:
                upload_exercise(
                    ctx, fake_docx, chapter_id=1,
                    expected_counts={"SingleChoice": 5, "MultipleChoice": 5, "Judgement": 5},
                    strict=True,
                )
            diag = excinfo.value.diagnostics
            assert diag["ok"] is False
            # 诊断 JSON 文件应被写出
            diag_files = list(tmp_path.glob("_exercise_validation_*.json"))
            assert len(diag_files) == 1
            data = json.loads(diag_files[0].read_text(encoding="utf-8"))
            assert data["ok"] is False

    def test_upload_exercise_strict_false_continues(self, tmp_path: Path):
        """strict=False 时校验失败不抛,继续执行。

        注:这里只验证 strict 路径,后续调用 _batch_edit / _create_exercise 也会被 mock。
        """
        fake_docx = tmp_path / "fake.docx"
        fake_docx.write_text("dummy")
        # 20 题(全部 Judgement),但 expected 是 5+5+5
        problems = [
            {
                "Type": "Judgement", "Number": i, "Answer": "T",
                "ProblemID": 200 + i, "TemplateID": 600 + i,
            }
            for i in range(1, 21)
        ]
        ctx = MagicMock()
        with patch("scrape_new.upload.exercise_upload._fetch_user_info", return_value={"user_id": 1, "auth": "x"}), \
             patch("scrape_new.upload.exercise_upload._fetch_library_id", return_value=1), \
             patch("scrape_new.upload.exercise_upload._upload_to_qiniu", return_value="https://example.com/fake.docx"), \
             patch("scrape_new.upload.exercise_upload._async_upload_docx", return_value="task-id"), \
             patch("scrape_new.upload.exercise_upload._poll_parse_status", return_value=True), \
             patch("scrape_new.upload.exercise_upload._get_parse_result", return_value=problems), \
             patch("scrape_new.upload.exercise_upload._batch_import", return_value=[]), \
             patch("scrape_new.upload.exercise_upload._batch_edit_by_type"), \
             patch("scrape_new.upload.exercise_upload._create_exercise", return_value=999), \
             patch("scrape_new.upload.exercise_upload._create_leaf", return_value=888):
            from scrape_new.upload.exercise_upload import upload_exercise
            # strict=False → 不抛,继续
            result = upload_exercise(
                ctx, fake_docx, chapter_id=1,
                expected_counts={"SingleChoice": 5, "MultipleChoice": 5, "Judgement": 5},
                strict=False,
            )
            assert result.exercise_id == "999"
            assert result.leaf_id == "888"
