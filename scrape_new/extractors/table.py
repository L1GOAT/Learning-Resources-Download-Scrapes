"""
表格提取器

从 HTML 中提取表格数据。
"""

from __future__ import annotations

import csv
import logging
import re
from pathlib import Path
from typing import TYPE_CHECKING
from xml.etree import ElementTree

from .base import BaseExtractor
from .utils import fetch_html, extract_page_title

if TYPE_CHECKING:
    from ..models import ExtractContext, ExtractResult

logger = logging.getLogger(__name__)


class TableExtractor(BaseExtractor):
    """表格提取器"""

    intent = "table"
    keywords = ["表格", "table", "excel", "csv"]

    def supports_url(self, url: str) -> bool:
        """表格提取器不支持直链"""
        return False

    def extract(self, ctx: ExtractContext) -> ExtractResult:
        """提取表格数据"""
        from ..models import ExtractResult

        result = ExtractResult()

        try:
            html = fetch_html(ctx.session, ctx.url, ctx.config.timeout)
            if not html:
                return result

            result.title = extract_page_title(html)

            # 提取所有表格
            tables = self._extract_tables(html)

            # 保存为 CSV
            output_dir = Path(ctx.output_dir)
            output_dir.mkdir(parents=True, exist_ok=True)

            for i, table in enumerate(tables, 1):
                csv_path = output_dir / f"table_{i:03d}.csv"
                self._save_table_csv(table, csv_path)
                logger.debug(f"表格已保存: {csv_path}")

            result.metadata["table_count"] = str(len(tables))
            if tables:
                result.metadata["tables_dir"] = str(output_dir)

            logger.info(f"表格提取完成: {len(tables)} 个")

        except Exception as e:
            logger.error(f"表格提取失败: {e}")

        return result

    def _extract_tables(self, html: str) -> list[list[list[str]]]:
        """提取所有表格数据"""
        tables = []

        # 使用正则提取 table 标签
        table_pattern = r'<table[^>]*>(.*?)</table>'
        for match in re.finditer(table_pattern, html, re.IGNORECASE | re.DOTALL):
            table_html = match.group(1)
            table_data = self._parse_table(table_html)
            if table_data:
                tables.append(table_data)

        return tables

    def _parse_table(self, table_html: str) -> list[list[str]]:
        """解析单个表格"""
        rows = []

        # 提取所有行
        row_pattern = r'<tr[^>]*>(.*?)</tr>'
        for row_match in re.finditer(row_pattern, table_html, re.IGNORECASE | re.DOTALL):
            row_html = row_match.group(1)
            cells = self._parse_row(row_html)
            if cells:
                rows.append(cells)

        return rows

    def _parse_row(self, row_html: str) -> list[str]:
        """解析单行"""
        cells = []

        # 提取 th 和 td
        cell_pattern = r'<(?:th|td)[^>]*>(.*?)</(?:th|td)>'
        for match in re.finditer(cell_pattern, row_html, re.IGNORECASE | re.DOTALL):
            cell_content = match.group(1)
            # 清理 HTML 标签
            text = re.sub(r'<[^>]+>', '', cell_content)
            text = text.strip()
            # 处理空白
            text = re.sub(r'\s+', ' ', text)
            cells.append(text)

        return cells

    def _save_table_csv(self, table: list[list[str]], filepath: Path) -> None:
        """保存表格为 CSV"""
        try:
            with open(filepath, 'w', encoding='utf-8-sig', newline='') as f:
                writer = csv.writer(f)
                for row in table:
                    writer.writerow(row)
        except Exception as e:
            logger.error(f"保存表格失败: {filepath}: {e}")