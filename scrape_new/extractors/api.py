"""
API 提取器

从 JSON API 获取数据。
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import TYPE_CHECKING

from .base import BaseExtractor

if TYPE_CHECKING:
    from ..models import ExtractContext, ExtractResult

logger = logging.getLogger(__name__)


class ApiExtractor(BaseExtractor):
    """API 提取器"""

    intent = "api"
    keywords = ["api", "json", "接口"]

    def supports_url(self, url: str) -> bool:
        """判断是否可能是 API URL"""
        url_lower = url.lower()
        # 常见 API 特征
        api_patterns = ['/api/', '/v1/', '/v2/', '.json', '?format=json']
        return any(p in url_lower for p in api_patterns)

    def extract(self, ctx: ExtractContext) -> ExtractResult:
        """提取 API 数据"""
        from ..models import ExtractResult

        result = ExtractResult()

        try:
            # 获取参数
            paginate = ctx.extra.get("paginate", "false").lower() == "true"
            items_key = ctx.extra.get("items_key", "data")
            max_pages = int(ctx.extra.get("max_pages", "20"))

            output_dir = Path(ctx.output_dir)
            output_dir.mkdir(parents=True, exist_ok=True)

            if paginate:
                data = self._fetch_paginated(ctx, items_key, max_pages)
            else:
                data = self._fetch_single(ctx)

            if data is not None:
                # 保存 JSON
                json_path = output_dir / "api_data.json"
                with open(json_path, 'w', encoding='utf-8') as f:
                    json.dump(data, f, indent=2, ensure_ascii=False)

                result.metadata["json_path"] = str(json_path)

                # 统计数量
                if isinstance(data, list):
                    result.metadata["data_count"] = str(len(data))
                elif isinstance(data, dict) and items_key in data:
                    result.metadata["data_count"] = str(len(data[items_key]))
                else:
                    result.metadata["data_count"] = "1"

                logger.info(f"API 提取完成: {json_path}")
            else:
                logger.warning("API 提取失败: 无法获取数据")

        except Exception as e:
            logger.error(f"API 提取失败: {e}")

        return result

    def _fetch_single(self, ctx: ExtractContext) -> Any:
        """获取单次请求"""
        try:
            resp = ctx.session.get(ctx.url, timeout=ctx.config.timeout)
            resp.raise_for_status()

            # 检查 Content-Type
            content_type = resp.headers.get('Content-Type', '')
            if 'json' not in content_type and 'javascript' not in content_type:
                logger.warning(f"非 JSON 响应: {content_type}")
                # 尝试解析
                try:
                    return resp.json()
                except Exception:
                    return None

            return resp.json()

        except Exception as e:
            logger.error(f"API 请求失败: {e}")
            return None

    def _fetch_paginated(self, ctx: ExtractContext, items_key: str, max_pages: int) -> list:
        """获取分页数据"""
        all_items = []

        for page in range(1, max_pages + 1):
            try:
                # 构建分页 URL
                url = self._build_page_url(ctx.url, page)
                logger.debug(f"获取第 {page} 页: {url}")

                resp = ctx.session.get(url, timeout=ctx.config.timeout)
                resp.raise_for_status()

                data = resp.json()

                # 提取数据
                if isinstance(data, list):
                    items = data
                elif isinstance(data, dict):
                    items = data.get(items_key, [])
                else:
                    break

                if not items:
                    break

                all_items.extend(items)
                logger.debug(f"第 {page} 页: {len(items)} 条")

            except Exception as e:
                logger.error(f"第 {page} 页失败: {e}")
                break

        return all_items

    def _build_page_url(self, base_url: str, page: int) -> str:
        """构建分页 URL"""
        from urllib.parse import urlparse, parse_qs, urlencode, urlunparse

        parsed = urlparse(base_url)
        params = parse_qs(parsed.query)

        # 常见分页参数
        page_params = ['page', 'p', 'pageNum', 'page_num', 'offset']
        for param in page_params:
            if param in params:
                params[param] = [str(page)]
                break
        else:
            # 默认使用 page
            params['page'] = [str(page)]

        # 重建 URL
        query = urlencode(params, doseq=True)
        return urlunparse(parsed._replace(query=query))