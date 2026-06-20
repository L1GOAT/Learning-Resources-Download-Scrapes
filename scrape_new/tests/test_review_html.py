"""
测试:scrape_new.services.review_html

覆盖(5 测试):
  1. 单文件 HTML(无外部依赖)
  2. 内联数据有 chapters + records
  3. 状态色和图标正确
  4. 搜索/筛选 DOM 元素存在
  5. 大体量渲染不崩(50 章 200 节)
"""

from __future__ import annotations

from pathlib import Path

import pytest

from scrape_new.services.resource_manifest import (
    build_chapter_tree_data, build_resource_naming_records,
)
from scrape_new.services.review_html import build_review_html


def _sample():
    return (
        [
            {"ch_num": 1, "ls_num": 1, "chapter": "第一章", "lesson": "技术",
             "name": "Thermodynamic Laws", "role": "video",
             "filename": "1.1_技术.mp4", "status": "downloaded",
             "size_bytes": 123_456_789, "source_meta": {}},
            {"ch_num": 1, "ls_num": 1, "chapter": "第一章", "lesson": "技术",
             "name": "Thermodynamic Laws (English)", "role": "english",
             "filename": "1.1_技术_English.mp4", "status": "downloaded",
             "size_bytes": 98_765_432, "source_meta": {}},
        ],
        [
            {"ch_num": 1, "ls_num": 1, "chapter": "第一章", "lesson": "技术",
             "name": "课件.pptx", "role": "ppt",
             "filename": "1.1_技术_PPT.pptx", "status": "failed",
             "size_bytes": 0, "reason": "无下载链接", "source_meta": {}},
        ],
    )


class TestReviewHTML:
    def test_single_file_no_external_deps(self, tmp_path: Path):
        videos, docs = _sample()
        tree = build_chapter_tree_data("测试课", "p", "u", videos, docs)
        records = build_resource_naming_records(videos, docs)
        path = build_review_html(tree, records, tmp_path)
        assert path.exists()
        content = path.read_text(encoding="utf-8")
        # 必须是单文件,内联 CSS+JS
        assert "<!DOCTYPE html>" in content
        assert "<style>" in content
        assert "<script" in content
        # 没有外部 CDN 链接
        assert "cdn." not in content.lower()
        assert "googleapis" not in content.lower()

    def test_data_inlined(self, tmp_path: Path):
        videos, docs = _sample()
        tree = build_chapter_tree_data("测试课", "chaoxing", "u", videos, docs)
        records = build_resource_naming_records(videos, docs)
        path = build_review_html(tree, records, tmp_path)
        content = path.read_text(encoding="utf-8")
        # 数据 JSON 内联
        assert "__REVIEW_DATA__" in content
        # 含课程名 + chapter 名
        assert "测试课" in content
        assert "第一章" in content
        assert "技术" in content
        # 含文件名
        assert "1.1_技术.mp4" in content
        assert "1.1_技术_English.mp4" in content

    def test_status_colors_and_icons(self, tmp_path: Path):
        videos, docs = _sample()
        tree = build_chapter_tree_data("测试课", "p", "u", videos, docs)
        records = build_resource_naming_records(videos, docs)
        path = build_review_html(tree, records, tmp_path)
        content = path.read_text(encoding="utf-8")
        # 4 种状态色都在 CSS
        for color in ("#10b981", "#9ca3af", "#ef4444", "#f59e0b"):
            assert color in content, f"missing status color {color}"
        # 状态徽章
        for status in ("downloaded", "skipped_existing", "failed", "suspicious"):
            assert status in content
        # 标签
        assert "已下载" in content
        assert "失败" in content

    def test_toolbar_dom(self, tmp_path: Path):
        videos, docs = _sample()
        tree = build_chapter_tree_data("测试课", "p", "u", videos, docs)
        records = build_resource_naming_records(videos, docs)
        path = build_review_html(tree, records, tmp_path)
        content = path.read_text(encoding="utf-8")
        # 搜索框
        assert 'id="search"' in content
        assert 'type="search"' in content
        # 4 个筛选按钮
        assert 'data-filter="all"' in content
        assert 'data-filter="failed"' in content
        assert 'data-filter="suspicious"' in content
        assert 'data-filter="missing_english"' in content
        assert 'data-filter="missing_ppt"' in content
        # 侧边栏容器
        assert 'id="sidebar"' in content
        assert 'id="content"' in content

    def test_large_render(self, tmp_path: Path):
        # 50 章 × 4 节 = 200 节,每节 3 leaf → 600 leaf,渲染不崩
        videos = []
        for ch in range(1, 51):
            for ls in range(1, 5):
                ls_id = f"{ch}.{ls}"
                videos.append({
                    "ch_num": ch, "ls_num": ls,
                    "chapter": f"第{ch}章", "lesson": f"第{ls}节",
                    "name": f"video-{ls_id}", "role": "video",
                    "filename": f"{ls_id}_video.mp4",
                    "status": "downloaded", "size_bytes": 100,
                    "source_meta": {},
                })
        tree = build_chapter_tree_data("大课", "chaoxing", "u", videos, [])
        records = build_resource_naming_records(videos, [])
        path = build_review_html(tree, records, tmp_path)
        size_kb = path.stat().st_size / 1024
        # 期望 < 500 KB(单文件能开)
        assert size_kb < 500, f"HTML too large: {size_kb:.1f} KB"
        # 必须包含所有章
        content = path.read_text(encoding="utf-8")
        assert "第1章" in content
        assert "第50章" in content