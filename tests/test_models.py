"""models.py 单元测试"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from scrape.upload.models import (
    Asset,
    AssetStatus,
    Chapter,
    ContentType,
    CourseStructure,
    Lesson,
    MatchConfidence,
    UploadResult,
    course_structure_from_dict,
    to_json,
)


def test_lesson_defaults():
    """Lesson 默认字段值"""
    ls = Lesson(id="1.3", title="测试", content_type=ContentType.VIDEO)
    assert ls.video is None
    assert ls.attachments == ()
    assert ls.quiz is None
    assert ls.match_confidence == MatchConfidence.NONE
    print("✓ test_lesson_defaults")


def test_lesson_is_frozen():
    """Lesson 是 frozen 的，赋值会报错"""
    ls = Lesson(id="1.3", title="测试", content_type=ContentType.VIDEO)
    try:
        ls.title = "改"  # type: ignore
        assert False, "应该抛 FrozenInstanceError"
    except Exception as e:
        assert "FrozenInstanceError" in type(e).__name__ or "frozen" in str(e).lower()
    print("✓ test_lesson_is_frozen")


def test_chapter_lessons():
    """Chapter 的 lessons 是 tuple 不可变"""
    ch = Chapter(index=1, title="章1", lessons=(
        Lesson(id="1.1", title="L1", content_type=ContentType.VIDEO),
    ))
    assert len(ch.lessons) == 1
    assert ch.lessons[0].id == "1.1"
    print("✓ test_chapter_lessons")


def test_course_structure_helpers():
    """lessons_with_video 和 missing_video_lessons 正确分类"""
    ch = Chapter(index=1, title="章1", lessons=(
        Lesson(id="1.1", title="有视频", content_type=ContentType.VIDEO, video="01.mp4"),
        Lesson(id="1.2", title="缺视频", content_type=ContentType.VIDEO, video=None),
        Lesson(id="1.3", title="非视频", content_type=ContentType.TEXT),
    ))
    cs = CourseStructure(course_id="x", course_title="t", chapters=(ch,))
    with_v = cs.lessons_with_video()
    missing = cs.missing_video_lessons()
    assert len(with_v) == 1
    assert with_v[0][1].id == "1.1"
    assert len(missing) == 1
    assert missing[0][1].id == "1.2"
    print("✓ test_course_structure_helpers")


def test_asset_with_status():
    """Asset.with_status 返回新对象不改变原对象"""
    a = Asset(
        chapter_index=1, lesson_id="1.1", lesson_title="t",
        content_type=ContentType.VIDEO, source_path="x.mp4",
    )
    assert a.status == AssetStatus.PENDING
    a2 = a.with_status(AssetStatus.OK, target_url="http://x")
    assert a.status == AssetStatus.PENDING  # 原对象不变
    assert a2.status == AssetStatus.OK
    assert a2.target_url == "http://x"
    print("✓ test_asset_with_status")


def test_to_json_serializes_enums():
    """to_json 正确处理枚举"""
    ch = Chapter(index=1, title="章", lessons=(
        Lesson(id="1.1", title="t", content_type=ContentType.VIDEO,
               match_confidence=MatchConfidence.EXACT),
    ))
    cs = CourseStructure(course_id="x", course_title="t", chapters=(ch,))
    text = to_json(cs)
    assert "video" in text
    assert "exact" in text
    print("✓ test_to_json_serializes_enums")


def test_round_trip_json():
    """to_json → from_dict 往返不丢字段"""
    ch = Chapter(index=1, title="章1", lessons=(
        Lesson(id="1.1", title="t", content_type=ContentType.VIDEO, video="01.mp4",
               match_confidence=MatchConfidence.CONTAINS),
    ))
    cs = CourseStructure(course_id="x", course_title="t", chapters=(ch,),
                        source_doc="doc.docx", generated_at="2026-06-10T16:00:00")
    text = to_json(cs)
    data = json.loads(text)
    cs2 = course_structure_from_dict(data)
    assert cs2.course_id == "x"
    assert cs2.chapters[0].lessons[0].video == "01.mp4"
    assert cs2.chapters[0].lessons[0].match_confidence == MatchConfidence.CONTAINS
    print("✓ test_round_trip_json")


def test_upload_result_delta():
    """delta() 正确计算"差额必须为 0"规则"""
    a1 = Asset(chapter_index=1, lesson_id="1.1", lesson_title="t",
               content_type=ContentType.VIDEO, source_path="x.mp4",
               status=AssetStatus.OK)
    a2 = Asset(chapter_index=1, lesson_id="1.2", lesson_title="t",
               content_type=ContentType.VIDEO, source_path="x.mp4",
               status=AssetStatus.FAILED)
    a3 = Asset(chapter_index=1, lesson_id="1.3", lesson_title="t",
               content_type=ContentType.VIDEO, source_path="x.mp4",
               status=AssetStatus.SKIPPED)
    a4 = Asset(chapter_index=1, lesson_id="1.4", lesson_title="t",
               content_type=ContentType.VIDEO, source_path="x.mp4",
               status=AssetStatus.PENDING)  # 这个让 delta != 0
    r = UploadResult(course_id="x", course_title="t",
                     started_at="2026-06-10T16:00:00",
                     assets=(a1, a2, a3, a4))
    # total=4, accounted=3 (ok+failed+skipped) → delta=1
    assert r.delta() == 1
    # 全部完成时 delta=0
    r2 = UploadResult(course_id="x", course_title="t",
                      started_at="2026-06-10T16:00:00",
                      assets=(a1, a2, a3))
    assert r2.delta() == 0
    print("✓ test_upload_result_delta")


if __name__ == "__main__":
    test_lesson_defaults()
    test_lesson_is_frozen()
    test_chapter_lessons()
    test_course_structure_helpers()
    test_asset_with_status()
    test_to_json_serializes_enums()
    test_round_trip_json()
    test_upload_result_delta()
    print("\n全部通过！")
