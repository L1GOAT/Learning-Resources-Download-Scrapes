"""
测试:章节前缀 + 课时多资源命名(format_chapter_title / lesson_filename / build_lesson_filenames)

覆盖:
  - CJK / 英文自动判定
  - 已有前缀识别(第N章/Chapter N/1.)
  - 多资源命名(英文视频 _English/_English_2,PPT/课件后缀)
  - sanitize 兜底(Windows 非法字符)
"""

from __future__ import annotations

import pytest

from scrape_new.upload.naming import (
    format_chapter_title,
    is_cjk_text,
    lesson_filename,
    lesson_leaf_name,
    build_lesson_filenames,
)


class TestIsCjk:
    def test_chinese_true(self):
        assert is_cjk_text("免疫学") is True

    def test_english_false(self):
        assert is_cjk_text("Foundations of Physical Chemistry") is False

    def test_mixed_true(self):
        assert is_cjk_text("Chapter 1 概述") is True

    def test_empty_false(self):
        assert is_cjk_text("") is False


class TestFormatChapterTitle:
    def test_chinese_new(self):
        assert format_chapter_title(1, "免疫学基础知识概述") == "第一章 免疫学基础知识概述"

    def test_chinese_idx_5(self):
        # idx=5 → 第五章(不是"第一章 5")
        assert format_chapter_title(5, "热力学定律") == "第五章 热力学定律"

    def test_chinese_idx_12(self):
        # idx=12 → 第十二章
        assert format_chapter_title(12, "综合应用") == "第十二章 综合应用"

    def test_chinese_idx_20(self):
        # idx=20 → 第二十章
        assert format_chapter_title(20, "总结") == "第二十章 总结"

    def test_english_new(self):
        assert (
            format_chapter_title(1, "Foundations of Physical Chemistry")
            == "Chapter 1: Foundations of Physical Chemistry"
        )

    def test_chinese_existing_prefix_unchanged(self):
        # 已带"第1章"前缀,识别后保留
        assert format_chapter_title(1, "第1章 免疫学概述") == "第1章 免疫学概述"
        assert format_chapter_title(2, "第二章 教学媒体") == "第二章 教学媒体"

    def test_english_existing_prefix_unchanged(self):
        assert (
            format_chapter_title(3, "Chapter 3. Electrochemistry")
            == "Chapter 3. Electrochemistry"
        )
        assert (
            format_chapter_title(1, "Chapter 1: Foundations")
            == "Chapter 1: Foundations"
        )

    def test_digit_prefix_chinese(self):
        # "1. xxx" 视为已有短前缀 → 强制英文模式时直接返回
        assert (
            format_chapter_title(1, "1. Foundations of Chemistry", force_language="en")
            == "1. Foundations of Chemistry"
        )

    def test_force_language_cn(self):
        # 强制中文,即使内容是英文
        assert (
            format_chapter_title(1, "Foundations", force_language="cn")
            == "第一章 Foundations"
        )

    def test_force_language_en(self):
        # 强制英文,即使内容是中文
        assert (
            format_chapter_title(2, "免疫学", force_language="en")
            == "Chapter 2: 免疫学"
        )

    def test_empty_title(self):
        # 防御:空标题也安全返回
        result = format_chapter_title(1, "")
        assert "1" in result  # 至少包含 idx

    def test_existing_prefix_chinese_with_colon(self):
        # "第1章: xxx" 识别并保留
        assert format_chapter_title(1, "第1章: 免疫学概述") == "第1章 免疫学概述"

    def test_chinese_index_arabic(self):
        # idx=10 → 第十章
        assert format_chapter_title(10, "热力学") == "第十章 热力学"


class TestLessonFilename:
    def test_video_default(self):
        # 单中文视频:无序号
        assert lesson_filename("1.1", "技术", role="video") == "1.1_技术.mp4"

    def test_video_index_2(self):
        # 多个中文视频:从 1 开始(不是从 2)
        assert (
            lesson_filename("1.1", "技术", role="video", index=2)
            == "1.1_技术_2.mp4"
        )

    def test_video_index_3(self):
        assert (
            lesson_filename("1.1", "技术", role="video", index=3)
            == "1.1_技术_3.mp4"
        )

    def test_english_single(self):
        assert (
            lesson_filename("1.1", "技术", role="english")
            == "1.1_技术_English.mp4"
        )

    def test_english_multiple(self):
        assert (
            lesson_filename("1.1", "技术", role="english", index=2)
            == "1.1_技术_English_2.mp4"
        )

    def test_ppt(self):
        assert (
            lesson_filename("1.1", "技术", role="ppt", ext="pptx")
            == "1.1_技术_PPT.pptx"
        )

    def test_pdf(self):
        assert (
            lesson_filename("1.1", "技术", role="pdf", ext="pdf")
            == "1.1_技术_课件.pdf"
        )

    def test_docx(self):
        assert (
            lesson_filename("1.1", "技术", role="docx", ext="docx")
            == "1.1_技术_讲义.docx"
        )

    def test_attachment_no_index(self):
        # attachment 单文件 → 不加 _1
        assert (
            lesson_filename("1.1", "技术", role="attachment", ext="jpg")
            == "1.1_技术_附件.jpg"
        )

    def test_attachment_index_2(self):
        # attachment 多文件 → _附件_2
        assert (
            lesson_filename("1.1", "技术", role="attachment", index=2, ext="jpg")
            == "1.1_技术_附件_2.jpg"
        )

    def test_sanitize_illegal_chars(self):
        # 含 Windows 非法字符 \ / : * ? " < > |
        out = lesson_filename("1.1", "技/术:测*试", role="video")
        # 经过 sanitize_filename,非法字符被替换
        assert "/" not in out
        assert ":" not in out
        assert "*" not in out

    def test_english_role_english_string_with_index(self):
        # role="english_2" 形式
        assert (
            lesson_filename("1.1", "技术", role="english_2")
            == "1.1_技术_English_2.mp4"
        )

    def test_unknown_role(self):
        # 未知 role 当后缀,扩展名 default "bin"(防御性默认值)
        assert (
            lesson_filename("1.1", "技术", role="zip")
            == "1.1_技术_zip.bin"
        )

    def test_empty_title(self):
        # 标题为空:仍生成 {id}_.ext
        out = lesson_filename("1.1", "", role="video")
        assert out.startswith("1.1_") and out.endswith(".mp4")


class TestBuildLessonFilenames:
    def test_video_only(self):
        result = build_lesson_filenames("1.1", "技术", primary_video="/v/1.1_技术.mp4")
        assert result == {
            "video_filename": "1.1_技术.mp4",
            "attachments": [],
        }

    def test_video_plus_one_english(self):
        result = build_lesson_filenames(
            "1.1", "技术",
            primary_video="/v/1.1_技术.mp4",
            english_videos=["/v/1.1_技术_2.mp4"],
        )
        assert result["video_filename"] == "1.1_技术.mp4"
        assert result["attachments"] == ["1.1_技术_English.mp4"]

    def test_video_plus_two_english(self):
        result = build_lesson_filenames(
            "1.1", "技术",
            primary_video="/v/1.1_技术.mp4",
            english_videos=["/v/1.1_技术_2.mp4", "/v/1.1_技术_3.mp4"],
        )
        assert result["attachments"] == [
            "1.1_技术_English.mp4",
            "1.1_技术_English_2.mp4",
        ]

    def test_video_plus_ppt(self):
        result = build_lesson_filenames(
            "1.1", "技术",
            primary_video="/v/1.1_技术.mp4",
            attachments=["/d/1.1_技术_课件.pptx"],
        )
        assert result["attachments"] == ["1.1_技术_PPT.pptx"]

    def test_full_combo(self):
        result = build_lesson_filenames(
            "1.1", "技术",
            primary_video="/v/1.1_技术.mp4",
            english_videos=["/v/1.1_技术_2.mp4"],
            attachments=[
                "/d/1.1_技术_课件.pptx",
                "/d/1.1_技术_讲义.pdf",
            ],
        )
        assert result["video_filename"] == "1.1_技术.mp4"
        assert result["attachments"] == [
            "1.1_技术_English.mp4",
            "1.1_技术_PPT.pptx",
            "1.1_技术_课件.pdf",
        ]


class TestLessonLeafName:
    def test_video(self):
        assert lesson_leaf_name("1.1", "技术") == "1.1 技术"

    def test_english(self):
        assert lesson_leaf_name("1.1", "技术", "english") == "1.1 技术 | English"

    def test_english_index_2(self):
        assert (
            lesson_leaf_name("1.1", "技术", "english", index=2)
            == "1.1 技术 | English_2"
        )

    def test_ppt(self):
        assert lesson_leaf_name("1.1", "技术", "ppt") == "1.1 技术 | PPT"
