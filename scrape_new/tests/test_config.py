"""
配置测试

测试配置加载和处理。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from scrape_new.config import load_config, save_config, create_example_config
from scrape_new.models import Config


class TestConfigLoad:
    """测试配置加载"""

    def test_default_config(self):
        """默认配置"""
        config = Config()
        assert config.max_retries == 3
        assert config.timeout == 30
        assert config.check_cookie is False
        assert config.auto_organize is True

    def test_load_from_dict(self):
        """从字典加载"""
        data = {
            "max_retries": 5,
            "timeout": 60,
            "check_cookie": True,
        }
        config = Config.from_dict(data)
        assert config.max_retries == 5
        assert config.timeout == 60
        assert config.check_cookie is True

    def test_load_missing_keys_ignored(self):
        """忽略缺失的键"""
        data = {"unknown_key": "value", "max_retries": 5}
        config = Config.from_dict(data)
        assert config.max_retries == 5

    def test_load_from_file(self, tmp_path):
        """从文件加载"""
        config_file = tmp_path / "config.json"
        config_file.write_text(json.dumps({
            "max_retries": 10,
            "timeout": 120,
        }), encoding="utf-8")

        config = load_config(config_file)
        assert config.max_retries == 10
        assert config.timeout == 120

    def test_load_nonexistent_file(self):
        """不存在的文件使用默认配置"""
        config = load_config(Path("/nonexistent/config.json"))
        assert config.max_retries == 3

    def test_save_and_load(self, tmp_path):
        """保存并加载"""
        config_file = tmp_path / "config.json"
        config = Config(max_retries=7, timeout=45)

        save_config(config, config_file)
        loaded = load_config(config_file)

        assert loaded.max_retries == 7
        assert loaded.timeout == 45

    def test_create_example_config(self, tmp_path):
        """创建示例配置"""
        config_file = tmp_path / "config.example.json"
        create_example_config(config_file)

        assert config_file.exists()
        config = load_config(config_file)
        assert config.max_retries == 3


class TestConfigPaths:
    """测试配置路径"""

    def test_cookies_file_path(self, tmp_path):
        """Cookie 文件路径"""
        config = Config(cookies_file=str(tmp_path / "cookies.txt"))
        assert config.cookies_file == str(tmp_path / "cookies.txt")

    def test_history_file_path(self, tmp_path):
        """历史文件路径"""
        config = Config(history_file=str(tmp_path / "history.json"))
        assert config.history_file == str(tmp_path / "history.json")

    def test_proxy_config(self):
        """代理配置"""
        config = Config(proxy="http://proxy:8080")
        assert config.proxy == "http://proxy:8080"

    def test_headers_config(self):
        """自定义 headers"""
        config = Config(headers={"X-Custom": "value"})
        assert config.headers["X-Custom"] == "value"