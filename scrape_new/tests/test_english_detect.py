"""
测试:scrape_new.services.english_detect

覆盖(8 测试):
  1. tab_num 兜底
  2. 标题含 English 关键字
  3. 文件名含 English 关键字
  4. 同节多个 mp4 全英文
  5. 单视频兜底
  6. PPT 不判定英文
  7. classify_lesson_videos 批量
  8. classify_lesson_docs 按扩展名
"""

from __future__ import annotations

import pytest

from scrape_new.services.english_detect import (
    detect_role,
    classify_lesson_videos,
    classify_lesson_docs,
    _is_english_text,
)


class TestTabNumFallback:
    def test_tab_2_is_english(self):
        # tab_num=2 + 普通中文标题 → English(老硬编码场景)
        assert detect_role(title="技术", tab_num=2) == "english"

    def test_tab_0_is_video(self):
        assert detect_role(title="技术", tab_num=0) == "video"


class TestTitleEnglishKeyword:
    def test_english_in_title(self):
        # 标题里有 English,即使 tab=0
        assert detect_role(
            title="Thermodynamic Laws (English)",
            tab_num=0,
        ) == "english"

    def test_chinese_english_word(self):
        # "英文" 关键字
        assert detect_role(title="气体 (英文版)", tab_num=0) == "english"

    def test_no_keyword(self):
        # 纯英文标题但不含 "English"/"英文" 关键字(可能是中文课只是英文标题)
        assert detect_role(title="Thermodynamic Laws", tab_num=0) == "video"


class TestFilenameEnglishKeyword:
    def test_english_in_filename(self):
        # 文件名含 English
        assert detect_role(
            filename="1.1_English.mp4",
            tab_num=0,
        ) == "english"


class TestMultiVideoAllEnglish:
    def test_multiple_all_english(self):
        # 同节多个 mp4,全部含 English 关键字 → 当前也是 English
        vids = [
            {"name": "Gas English", "title": "Gas English"},
            {"name": "Liquid English", "title": "Liquid English"},
        ]
        assert detect_role(
            title="Gas English",
            tab_num=0,
            same_lesson_videos=vids,
        ) == "english"

    def test_multiple_mixed_english_not_enough(self):
        # 同节多个 mp4,只有部分含 English → 当前不算 English
        vids = [
            {"name": "热力学定律", "title": "热力学定律"},
            {"name": "Thermodynamic (English)", "title": "Thermodynamic (English)"},
        ]
        assert detect_role(
            title="热力学定律",
            tab_num=0,
            same_lesson_videos=vids,
        ) == "video"


class TestSingleVideoFallback:
    def test_single_video_no_keyword(self):
        # 单视频,无 English 关键字 → video
        assert detect_role(title="气体", tab_num=0) == "video"

    def test_empty_inputs(self):
        # 全空输入兜底 → video
        assert detect_role() == "video"


class TestPPTNotEnglish:
    def test_pptx_by_ext(self):
        # PPT 不论 tab/title 都是 ppt
        assert detect_role(filename="1.1_技术_PPT.pptx", title="课件") == "ppt"

    def test_pdf_by_ext(self):
        assert detect_role(filename="1.1_技术_课件.pdf", title="讲义") == "pdf"

    def test_ppt_even_with_english_in_title(self):
        # 即使标题含 English,PPTX 仍是 ppt
        assert detect_role(
            filename="课件.pptx", title="English Lecture Notes",
        ) == "ppt"


class TestClassifyLessonVideos:
    def test_batch_classification(self):
        # 模拟 chaoxing 一节课下 2 个视频
        vids = [
            {"name": "热力学定律", "tab_num": 0, "lesson": "热力学定律"},
            {"name": "Thermodynamic Laws (English)", "tab_num": 2,
             "lesson": "热力学定律"},
        ]
        result = classify_lesson_videos("热力学定律", vids)
        assert len(result) == 2
        assert result[0]["role"] == "video"
        assert result[1]["role"] == "english"

    def test_keeps_existing_role(self):
        # 已分类的不再覆写
        vids = [
            {"name": "x", "tab_num": 0, "role": "video"},  # 已 role=video
        ]
        result = classify_lesson_videos("x", vids)
        assert result[0]["role"] == "video"


class TestClassifyLessonDocs:
    def test_docs_by_ext(self):
        docs = [
            {"filename": "1.1_技术_PPT.pptx", "name": "PPT"},
            {"filename": "1.1_技术_课件.pdf", "name": "PDF"},
            {"filename": "1.1_技术_讲义.docx", "name": "DOCX"},
        ]
        classify_lesson_docs(docs)
        assert docs[0]["role"] == "ppt"
        assert docs[1]["role"] == "pdf"
        assert docs[2]["role"] == "docx"