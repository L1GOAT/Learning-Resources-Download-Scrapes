"""
提取器测试

测试各种提取器的功能。
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from scrape_new.extractors.registry import ExtractorRegistry
from scrape_new.extractors.video import VideoExtractor
from scrape_new.extractors.image import ImageExtractor
from scrape_new.extractors.document import DocumentExtractor
from scrape_new.extractors.links import LinksExtractor
from scrape_new.extractors.article import ArticleExtractor
from scrape_new.extractors.table import TableExtractor
from scrape_new.extractors.api import ApiExtractor
from scrape_new.extractors.utils import (
    extract_page_title,
    url_basename,
    unique_keep_order,
)


class TestRegistry:
    """测试注册表"""

    def test_detect_video(self):
        """检测视频意图"""
        registry = ExtractorRegistry()
        extractor = VideoExtractor()
        registry.register(extractor)

        assert registry.detect_intent("视频") == "video"
        assert registry.detect_intent("video") == "video"
        assert registry.detect_intent("mp4") == "video"

    def test_detect_image(self):
        """检测图片意图"""
        registry = ExtractorRegistry()
        extractor = ImageExtractor()
        registry.register(extractor)

        assert registry.detect_intent("图片") == "image"
        assert registry.detect_intent("image") == "image"
        assert registry.detect_intent("photo") == "image"

    def test_detect_all(self):
        """检测全部意图"""
        registry = ExtractorRegistry()
        assert registry.detect_intent("全部") == "all"
        assert registry.detect_intent("all") == "all"

    def test_default_video(self):
        """默认视频"""
        registry = ExtractorRegistry()
        assert registry.detect_intent("") == "video"
        assert registry.detect_intent("unknown") == "video"


class TestUtils:
    """测试工具函数"""

    def test_extract_page_title(self):
        """提取页面标题"""
        html = '<html><head><title>Test Title</title></head><body></body></html>'
        assert extract_page_title(html) == "Test Title"

    def test_extract_page_title_empty(self):
        """空标题"""
        html = '<html><head></head><body></body></html>'
        assert extract_page_title(html) == ""

    def test_url_basename(self):
        """URL 文件名"""
        assert url_basename("http://example.com/video.mp4") == "video"
        assert url_basename("http://example.com/path/file.txt") == "file"
        assert url_basename("http://example.com/") == "file"

    def test_unique_keep_order(self):
        """去重保持顺序"""
        assert unique_keep_order([1, 2, 3, 2, 1]) == [1, 2, 3]
        assert unique_keep_order(['a', 'b', 'a', 'c']) == ['a', 'b', 'c']


class TestVideoExtractor:
    """测试视频提取器"""

    def test_supports_url(self):
        """支持 URL 判断"""
        extractor = VideoExtractor()
        assert extractor.supports_url("http://example.com/video.mp4") is True
        assert extractor.supports_url("http://example.com/video.m3u8") is True
        assert extractor.supports_url("http://example.com/video.webm") is True
        assert extractor.supports_url("http://example.com/image.jpg") is False

    def test_extract_from_video_tag(self):
        """从 video 标签提取"""
        extractor = VideoExtractor()
        html = '<video src="video.mp4"></video>'
        urls = extractor._extract_from_video_tag(html, "http://example.com")
        assert len(urls) == 1
        assert "video.mp4" in urls[0]

    def test_extract_from_source_tag(self):
        """从 source 标签提取"""
        extractor = VideoExtractor()
        html = '<video><source src="video.mp4" type="video/mp4"></video>'
        urls = extractor._extract_from_source_tag(html, "http://example.com")
        assert len(urls) == 1
        assert "video.mp4" in urls[0]

    def test_extract_from_script(self):
        """从 script 提取 m3u8"""
        extractor = VideoExtractor()
        html = '<script>var url = "http://example.com/video.m3u8";</script>'
        urls = extractor._extract_from_scripts(html, "http://example.com")
        assert len(urls) == 1
        assert "video.m3u8" in urls[0]


class TestImageExtractor:
    """测试图片提取器"""

    def test_supports_url(self):
        """支持 URL 判断"""
        extractor = ImageExtractor()
        assert extractor.supports_url("http://example.com/image.jpg") is True
        assert extractor.supports_url("http://example.com/image.png") is True
        assert extractor.supports_url("http://example.com/image.gif") is True
        assert extractor.supports_url("http://example.com/video.mp4") is False

    def test_extract_img_src(self):
        """从 img src 提取"""
        extractor = ImageExtractor()
        html = '<img src="image.jpg" alt="test">'
        urls = extractor._extract_from_img_src(html, "http://example.com")
        assert len(urls) == 1
        assert "image.jpg" in urls[0]

    def test_extract_data_src(self):
        """从 data-src 提取"""
        extractor = ImageExtractor()
        html = '<img data-src="image.jpg" src="placeholder.gif">'
        urls = extractor._extract_from_data_src(html, "http://example.com")
        assert len(urls) == 1
        assert "image.jpg" in urls[0]

    def test_extract_srcset(self):
        """从 srcset 提取"""
        extractor = ImageExtractor()
        html = '<img srcset="image-1x.jpg 1x, image-2x.jpg 2x">'
        urls = extractor._extract_from_srcset(html, "http://example.com")
        assert len(urls) == 2


class TestDocumentExtractor:
    """测试文档提取器"""

    def test_supports_url(self):
        """支持 URL 判断"""
        extractor = DocumentExtractor()
        assert extractor.supports_url("http://example.com/doc.pdf") is True
        assert extractor.supports_url("http://example.com/doc.docx") is True
        assert extractor.supports_url("http://example.com/archive.zip") is True
        assert extractor.supports_url("http://example.com/video.mp4") is False

    def test_extract_pdf_link(self):
        """提取 PDF 链接"""
        extractor = DocumentExtractor()
        html = '<a href="document.pdf">Download PDF</a>'
        urls = extractor._extract_from_links(html, "http://example.com")
        assert len(urls) == 1
        assert "document.pdf" in urls[0]


class TestLinksExtractor:
    """测试链接提取器"""

    def test_extract_all_links(self):
        """提取所有链接"""
        extractor = LinksExtractor()
        html = '''
        <a href="http://example.com/1">Link 1</a>
        <a href="http://example.com/2">Link 2</a>
        <a href="/relative">Relative</a>
        '''
        links = extractor._extract_links(html, "http://example.com")
        assert len(links) == 3
        assert "http://example.com/1" in links
        assert "http://example.com/2" in links
        assert "http://example.com/relative" in links

    def test_skip_javascript(self):
        """跳过 javascript 链接"""
        extractor = LinksExtractor()
        html = '''
        <a href="javascript:void(0)">JS Link</a>
        <a href="http://example.com/real">Real Link</a>
        '''
        links = extractor._extract_links(html, "http://example.com")
        assert len(links) == 1
        assert "http://example.com/real" in links


class TestArticleExtractor:
    """测试文章提取器"""

    def test_extract_title(self):
        """提取标题"""
        extractor = ArticleExtractor()
        html = '<html><head><title>Article Title</title></head><body><p>Content</p></body></html>'
        title = extract_page_title(html)
        assert title == "Article Title"

    def test_clean_content(self):
        """清理内容"""
        extractor = ArticleExtractor()
        html = '<p>Hello</p><p>World</p>'
        text = extractor._clean_content(html)
        assert "Hello" in text
        assert "World" in text
        assert "<p>" not in text


class TestTableExtractor:
    """测试表格提取器"""

    def test_parse_table(self):
        """解析表格"""
        extractor = TableExtractor()
        html = '''
        <table>
            <tr><th>Name</th><th>Age</th></tr>
            <tr><td>Alice</td><td>30</td></tr>
            <tr><td>Bob</td><td>25</td></tr>
        </table>
        '''
        tables = extractor._extract_tables(html)
        assert len(tables) == 1
        assert len(tables[0]) == 3  # 3 rows
        assert tables[0][0] == ["Name", "Age"]
        assert tables[0][1] == ["Alice", "30"]


class TestAllMode:
    """测试 all 模式"""

    def test_all_mode_does_not_crash(self):
        """all 模式不会崩溃"""
        from scrape_new.extractors import registry

        # 确保 all 意图可以被识别
        intent = registry.detect_intent("全部")
        assert intent == "all"

        # 确保所有提取器都已注册
        for sub_intent in ["video", "image", "document", "table", "article", "links"]:
            extractor = registry.get(sub_intent)
            assert extractor is not None, f"提取器未注册: {sub_intent}"