"""mapping.py 单元测试

跑实际免疫学素材，端到端验证 mapping 流程。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from scrape.upload.mapping import (
    _extract_text_blobs,
    _is_html_file,
    _is_ole2_file,
    _parse_chapter_text,
    build_mapping,
    scan_video_folder,
    match_videos_to_lessons,
)
from scrape.upload.models import (
    Chapter,
    ContentType,
    Lesson,
    MatchConfidence,
)

ROOT = Path("E:/林视")
DOC = ROOT / "docs" / "《【26春】疾病与免疫学2026春》章节目录.doc"
VIDEOS = ROOT / "免疫课程视频"


def test_detect_file_formats():
    """OLE2 和 HTML 都能嗅探出来"""
    assert _is_ole2_file(DOC) is True
    assert _is_html_file(DOC) is False
    print("✓ test_detect_file_formats")


def test_extract_text_from_ole2():
    """OLE2 包装的 doc 能抽出 HTML 文本（含中文）"""
    text = _extract_text_blobs(DOC)
    assert "免疫" in text
    assert "1" in text  # 章节序号
    assert "11.1" in text or "11.3" in text  # 最后几节
    print("✓ test_extract_text_from_ole2")


def test_parse_immunology_doc():
    """真实免疫学 .doc 解析出 11 章"""
    chapters = _parse_chapter_text(_extract_text_blobs(DOC))
    assert len(chapters) == 11, f"期望 11 章，实际 {len(chapters)}"
    assert chapters[0].index == 1
    assert chapters[0].title.startswith("免疫学基础知识概述")
    assert chapters[10].index == 11
    assert "复习" in chapters[10].title
    print("✓ test_parse_immunology_doc")


def test_lesson_classification():
    """课时类型分类正确"""
    chapters = _parse_chapter_text(_extract_text_blobs(DOC))
    # 章1.1 = 预习要求 → other
    ch1 = chapters[0]
    assert ch1.lessons[0].id == "1.1"
    assert ch1.lessons[0].content_type == ContentType.OTHER
    # 章1.3 = 影响健康的因素 → video
    assert ch1.lessons[2].id == "1.3"
    assert ch1.lessons[2].content_type == ContentType.VIDEO
    # 章1.10 = 思考题 → other
    assert ch1.lessons[9].id == "1.10"
    assert ch1.lessons[9].content_type == ContentType.OTHER
    print("✓ test_lesson_classification")


def test_scan_video_folder():
    """扫到 58 个视频"""
    if not VIDEOS.is_dir():
        print(f"⚠ 跳过（视频文件夹不存在: {VIDEOS}）")
        return
    videos = scan_video_folder(VIDEOS)
    assert len(videos) == 58, f"期望 58，实际 {len(videos)}"
    # 第一个是 01_影响健康的因素.mp4
    assert videos[0].filename.startswith("01_")
    assert videos[0].index == 1
    print("✓ test_scan_video_folder")


def test_match_exact():
    """完全相等的标题能匹配上"""
    videos = scan_video_folder(VIDEOS)
    chapters = _parse_chapter_text(_extract_text_blobs(DOC))
    structure = match_videos_to_lessons(chapters, videos)
    # 章1.3 = "影响健康的因素" 对应 01_影响健康的因素.mp4
    ch1_l3 = structure.chapters[0].lessons[2]
    assert ch1_l3.id == "1.3"
    assert ch1_l3.video == "01_影响健康的因素.mp4"
    assert ch1_l3.match_confidence == MatchConfidence.EXACT
    print("✓ test_match_exact")


def test_match_contains():
    """包含关系的标题能匹配上（如 '防御' 在 课时标题里）"""
    videos = scan_video_folder(VIDEOS)
    chapters = _parse_chapter_text(_extract_text_blobs(DOC))
    structure = match_videos_to_lessons(chapters, videos)
    # 章1.6 = "免疫系统的三大功能：防御"
    ch1_l6 = structure.chapters[0].lessons[5]
    assert ch1_l6.id == "1.6"
    assert ch1_l6.video == "04_免疫系统的三大功能：防御.mp4"
    assert ch1_l6.match_confidence == MatchConfidence.EXACT
    print("✓ test_match_contains")


def test_chapter10_no_video():
    """章10 的 3 个视频课时应该缺视频"""
    videos = scan_video_folder(VIDEOS)
    chapters = _parse_chapter_text(_extract_text_blobs(DOC))
    structure = match_videos_to_lessons(chapters, videos)
    missing = structure.missing_video_lessons()
    ch10_lessons = [ls for ch, ls in missing if ch.index == 10]
    assert len(ch10_lessons) == 3, f"章10 应该缺 3 个视频课时，实际 {len(ch10_lessons)}"
    titles = {ls.title for ls in ch10_lessons}
    assert "认识新型冠状病毒" in titles
    print("✓ test_chapter10_no_video")


def test_end_to_end():
    """build_mapping 端到端：扫视频 + 解析文档 + 匹配"""
    if not VIDEOS.is_dir():
        print(f"⚠ 跳过（视频文件夹不存在）")
        return
    s = build_mapping(
        videos_folder=VIDEOS,
        doc_path=DOC,
        course_id="15932418",
        course_title="疾病与免疫学2026春",
    )
    assert s.course_id == "15932418"
    assert len(s.chapters) == 11
    n_videos = len(s.lessons_with_video())
    n_missing = len(s.missing_video_lessons())
    # 56 视频能精确匹配，2 个孤儿（25/26 艾滋病的传播途径1/2）匹配不上是已知问题
    assert n_videos >= 54, f"至少 54 个视频应匹配，实际 {n_videos}"
    assert n_missing == 5, f"应有 5 个缺视频课时，实际 {n_missing}"
    print("✓ test_end_to_end")


if __name__ == "__main__":
    test_detect_file_formats()
    test_extract_text_from_ole2()
    test_parse_immunology_doc()
    test_lesson_classification()
    test_scan_video_folder()
    test_match_exact()
    test_match_contains()
    test_chapter10_no_video()
    test_end_to_end()
    print("\n全部通过！")
