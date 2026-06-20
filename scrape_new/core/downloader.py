"""
下载器模块

提供文件下载功能。
"""

from __future__ import annotations

import logging
import os
import time
from pathlib import Path

import requests

from ..config import Config
from ..exceptions import DownloadError
from ..models import DownloadItem, DownloadResult
from .paths import check_path_traversal, sanitize_filename, guess_ext
from .verifier import verify_download

logger = logging.getLogger(__name__)


def download_file(
    session: requests.Session,
    item: DownloadItem | str,
    output_dir: Path | str,
    config: Config | None = None,
) -> DownloadResult:
    """
    下载单个文件

    Args:
        session: requests.Session
        item: 下载项（或旧式 URL 字符串）
        output_dir: 输出目录（或旧式文件路径）
        config: 配置（None 用默认值）

    Returns:
        下载结果
    """
    # 兼容旧式调用: download_file(session, url, filepath, size_hint=..., max_retries=...)
    # 这种情况 item 是 str(URL), output_dir 是文件路径
    if config is None:
        config = Config()
    if isinstance(item, str):
        # 旧式调用: 直接下载到 output_dir 指定的文件路径
        url = item
        filepath = Path(output_dir)
        filename = filepath.name
        item = DownloadItem(url=url, filename=filename)
        output_dir = filepath.parent

    result = DownloadResult(item=item)

    try:
        # 确定输出路径
        filename = item.filename or _extract_filename(item.url)
        filename = sanitize_filename(filename)
        filepath = output_dir / filename

        # 检查路径穿越
        check_path_traversal(filepath, output_dir)

        # 检查是否已存在
        if filepath.exists():
            existing_size = filepath.stat().st_size
            if existing_size > 0:
                logger.info(f"文件已存在，跳过: {filepath} ({existing_size} bytes)")
                result.path = filepath
                result.status = "skipped"
                result.size_bytes = existing_size
                return result

        # 确保目录存在
        output_dir.mkdir(parents=True, exist_ok=True)

        # 下载文件
        success = _download_with_retry(
            session=session,
            url=item.url,
            filepath=filepath,
            config=config,
            headers=item.headers,
        )

        if not success:
            result.status = "failed"
            result.error = "下载失败"
            return result

        # 校验文件
        verify_status = verify_download(
            filepath=filepath,
            config=config,
            size_hint=item.size_hint,
        )

        result.path = filepath
        result.size_bytes = filepath.stat().st_size if filepath.exists() else 0
        result.status = verify_status

        if verify_status == "ok":
            logger.info(f"下载成功: {filepath} ({result.size_bytes} bytes)")
        else:
            logger.warning(f"下载完成但校验异常: {filepath} ({verify_status})")

    except Exception as e:
        result.status = "failed"
        result.error = str(e)
        logger.error(f"下载异常: {item.url}: {e}")

    return result


def download_many(
    session: requests.Session,
    items: list[DownloadItem],
    output_dir: Path,
    config: Config,
) -> list[DownloadResult]:
    """
    批量下载文件

    Args:
        session: requests.Session
        items: 下载项列表
        output_dir: 输出目录
        config: 配置

    Returns:
        下载结果列表
    """
    results: list[DownloadResult] = []
    consecutive_failures = 0

    for i, item in enumerate(items, 1):
        logger.info(f"下载 [{i}/{len(items)}]: {item.url}")

        result = download_file(session, item, output_dir, config)
        results.append(result)

        # 连续失败检查
        if result.status == "failed":
            consecutive_failures += 1
            if consecutive_failures >= 3:
                logger.error("连续 3 次下载失败，中断")
                break
        else:
            consecutive_failures = 0

        # 下载间隔
        if i < len(items):
            time.sleep(config.retry_delay)

    return results


def _download_with_retry(
    session: requests.Session,
    url: str,
    filepath: Path,
    config: Config,
    headers: dict[str, str] | None = None,
) -> bool:
    """
    带重试的下载

    Args:
        session: requests.Session
        url: URL
        filepath: 输出路径
        config: 配置
        headers: 额外 headers

    Returns:
        是否成功
    """
    last_error = None

    for attempt in range(config.max_retries):
        try:
            success = _do_download(
                session=session,
                url=url,
                filepath=filepath,
                config=config,
                headers=headers,
            )
            if success:
                return True
        except Exception as e:
            last_error = e
            logger.warning(f"下载失败 (尝试 {attempt + 1}/{config.max_retries}): {e}")

        if attempt < config.max_retries - 1:
            time.sleep(config.retry_delay * (attempt + 1))

    if last_error:
        logger.error(f"下载最终失败: {url}: {last_error}")
    return False


def _do_download(
    session: requests.Session,
    url: str,
    filepath: Path,
    config: Config,
    headers: dict[str, str] | None = None,
) -> bool:
    """
    执行下载

    Args:
        session: requests.Session
        url: URL
        filepath: 输出路径
        config: 配置
        headers: 额外 headers

    Returns:
        是否成功
    """
    # 使用 .part 临时文件
    part_path = filepath.with_suffix(filepath.suffix + '.part')

    try:
        # 发送请求
        req_headers = dict(session.headers)
        if headers:
            req_headers.update(headers)

        resp = session.get(
            url,
            headers=req_headers,
            timeout=config.timeout,
            stream=True,
        )
        resp.raise_for_status()

        # 检查 content-type
        content_type = resp.headers.get('Content-Type', '')
        if 'text/html' in content_type:
            # 可能是登录页或错误页
            text_sample = resp.text[:2000].lower()
            if any(keyword in text_sample for keyword in ['login', '登录', '请先登录']):
                logger.warning(f"收到登录页面: {url}")
                return False

        # 写入 .part 文件
        with open(part_path, 'wb') as f:
            for chunk in resp.iter_content(chunk_size=config.chunk_size):
                if chunk:
                    f.write(chunk)

        # 原子替换
        if filepath.exists():
            filepath.unlink()
        part_path.rename(filepath)

        return True

    except requests.exceptions.RequestException as e:
        logger.error(f"请求失败: {url}: {e}")
        # 清理 .part 文件
        if part_path.exists():
            part_path.unlink()
        return False

    except Exception as e:
        logger.error(f"下载异常: {url}: {e}")
        # 清理 .part 文件
        if part_path.exists():
            part_path.unlink()
        return False


def _extract_filename(url: str) -> str:
    """
    从 URL 提取文件名

    Args:
        url: URL

    Returns:
        文件名
    """
    from urllib.parse import urlparse, unquote

    try:
        parsed = urlparse(url)
        path = unquote(parsed.path)
        filename = path.split('/')[-1]

        # 移除查询参数
        if '?' in filename:
            filename = filename.split('?')[0]

        # 如果没有扩展名，添加默认扩展名
        if '.' not in filename:
            ext = guess_ext(url)
            filename = filename or 'download'
            if ext:
                filename += ext

        return filename or 'download'
    except Exception:
        return 'download'