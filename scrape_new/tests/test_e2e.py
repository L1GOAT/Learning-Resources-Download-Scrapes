"""
端到端测试

使用本地测试服务器测试完整流程。
"""

from __future__ import annotations

import json
import threading
from functools import partial
from http.server import HTTPServer, SimpleHTTPRequestHandler
from pathlib import Path

import pytest

from scrape_new.models import JobRequest
from scrape_new.config import Config

# 测试 fixtures 路径
FIXTURES_DIR = Path(__file__).parent / "fixtures" / "site"


class TestHandler(SimpleHTTPRequestHandler):
    """测试 HTTP 处理器"""

    def __init__(self, *args, directory=None, **kwargs):
        super().__init__(*args, directory=directory, **kwargs)

    def log_message(self, format, *args):
        """禁止日志输出"""
        pass

    def end_headers(self):
        """添加 CORS 头"""
        self.send_header('Access-Control-Allow-Origin', '*')
        super().end_headers()


@pytest.fixture
def test_server():
    """启动本地测试服务器"""
    handler = partial(TestHandler, directory=str(FIXTURES_DIR))
    server = HTTPServer(("127.0.0.1", 0), handler)
    port = server.server_address[1]

    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    yield f"http://127.0.0.1:{port}"

    server.shutdown()


@pytest.fixture
def config():
    """测试配置"""
    return Config(
        max_retries=1,
        retry_delay=0,
        timeout=5,
        min_video_size=10,
        min_image_size=10,
        min_document_size=10,
        check_cookie=False,
        generate_report=True,
        auto_organize=False,
    )


class TestE2EImages:
    """图片端到端测试"""

    def test_extract_images(self, test_server, config, tmp_path):
        """提取图片"""
        from scrape_new.extractors.image import ImageExtractor
        from scrape_new.core.session import create_session
        from scrape_new.models import ExtractContext

        session = create_session(config)
        extractor = ImageExtractor()

        ctx = ExtractContext(
            url=f"{test_server}/images.html",
            session=session,
            config=config,
            output_dir=tmp_path,
        )

        result = extractor.extract(ctx)
        assert len(result.items) > 0
        assert all(item.kind == "image" for item in result.items)


class TestE2EDocuments:
    """文档端到端测试"""

    def test_extract_documents(self, test_server, config, tmp_path):
        """提取文档"""
        from scrape_new.extractors.document import DocumentExtractor
        from scrape_new.core.session import create_session
        from scrape_new.models import ExtractContext

        session = create_session(config)
        extractor = DocumentExtractor()

        ctx = ExtractContext(
            url=f"{test_server}/docs.html",
            session=session,
            config=config,
            output_dir=tmp_path,
        )

        result = extractor.extract(ctx)
        assert len(result.items) > 0
        assert all(item.kind == "document" for item in result.items)


class TestE2ELinks:
    """链接端到端测试"""

    def test_extract_links(self, test_server, config, tmp_path):
        """提取链接"""
        from scrape_new.extractors.links import LinksExtractor
        from scrape_new.core.session import create_session
        from scrape_new.models import ExtractContext

        session = create_session(config)
        extractor = LinksExtractor()

        ctx = ExtractContext(
            url=f"{test_server}/links.html",
            session=session,
            config=config,
            output_dir=tmp_path,
        )

        result = extractor.extract(ctx)
        assert "links_path" in result.metadata
        assert int(result.metadata.get("links_count", 0)) > 0


class TestE2EArticle:
    """文章端到端测试"""

    def test_extract_article(self, test_server, config, tmp_path):
        """提取文章"""
        from scrape_new.extractors.article import ArticleExtractor
        from scrape_new.core.session import create_session
        from scrape_new.models import ExtractContext

        session = create_session(config)
        extractor = ArticleExtractor()

        ctx = ExtractContext(
            url=f"{test_server}/article.html",
            session=session,
            config=config,
            output_dir=tmp_path,
        )

        result = extractor.extract(ctx)
        assert "article_path" in result.metadata
        article_path = Path(result.metadata["article_path"])
        assert article_path.exists()
        content = article_path.read_text(encoding="utf-8")
        assert "Test Article Title" in content


class TestE2ETable:
    """表格端到端测试"""

    def test_extract_table(self, test_server, config, tmp_path):
        """提取表格"""
        from scrape_new.extractors.table import TableExtractor
        from scrape_new.core.session import create_session
        from scrape_new.models import ExtractContext

        session = create_session(config)
        extractor = TableExtractor()

        ctx = ExtractContext(
            url=f"{test_server}/table.html",
            session=session,
            config=config,
            output_dir=tmp_path,
        )

        result = extractor.extract(ctx)
        assert int(result.metadata.get("table_count", 0)) > 0
        assert (tmp_path / "table_001.csv").exists()


class TestE2EVideo:
    """视频端到端测试"""

    def test_extract_video(self, test_server, config, tmp_path):
        """提取视频"""
        from scrape_new.extractors.video import VideoExtractor
        from scrape_new.core.session import create_session
        from scrape_new.models import ExtractContext

        session = create_session(config)
        extractor = VideoExtractor()

        ctx = ExtractContext(
            url=f"{test_server}/index.html",
            session=session,
            config=config,
            output_dir=tmp_path,
        )

        result = extractor.extract(ctx)
        assert len(result.items) > 0
        assert all(item.kind == "video" for item in result.items)


class TestE2EAllMode:
    """All 模式端到端测试"""

    def test_all_mode(self, test_server, config, tmp_path):
        """All 模式不崩溃"""
        from scrape_new.app import run_job

        request = JobRequest(
            intent_desc="全部",
            url=f"{test_server}/index.html",
            output_dir=tmp_path,
            config_path=None,
            auto_organize=False,
            no_dedup=True,
        )

        # 使用 monkeypatch 替换 config
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr("scrape_new.app.load_config", lambda x: config)
            result = run_job(request)

        assert result.error == "" or "未找到提取器" not in result.error
        assert result.found >= 0