"""
HLS (m3u8) 下载模块

支持 m3u8 下载、合并、AES 解密。
"""

from __future__ import annotations

import logging
import tempfile
from pathlib import Path
from urllib.parse import urljoin

import requests

from ..config import Config
from ..exceptions import DownloadError

logger = logging.getLogger(__name__)

# 最大分片数量限制
MAX_SEGMENTS = 10000

# 最大递归深度
MAX_REDIRECT_DEPTH = 5


def download_m3u8(
    session: requests.Session,
    m3u8_url: str,
    filepath: Path,
    config: Config | None = None,
) -> bool:
    """
    下载 m3u8 视频

    Args:
        session: requests.Session
        m3u8_url: m3u8 URL
        filepath: 输出文件路径
        config: 配置（None 用默认值）

    Returns:
        是否成功
    """
    if config is None:
        config = Config()
    try:
        # 解析 m3u8
        segments, encryption = _parse_m3u8(session, m3u8_url, config)

        if not segments:
            logger.error(f"m3u8 无分片: {m3u8_url}")
            return False

        if len(segments) > MAX_SEGMENTS:
            logger.error(f"分片数量超限 ({len(segments)} > {MAX_SEGMENTS})")
            return False

        logger.info(f"开始下载 m3u8: {len(segments)} 个分片")

        # 下载分片
        segment_files = _download_segments(
            session=session,
            segments=segments,
            config=config,
        )

        if not segment_files:
            logger.error("无分片下载成功")
            return False

        # 合并分片
        success = _merge_segments(
            segment_files=segment_files,
            filepath=filepath,
            encryption=encryption,
        )

        # 清理临时文件
        _cleanup_segments(segment_files)

        if success:
            logger.info(f"m3u8 下载成功: {filepath}")
        else:
            logger.error(f"m3u8 合并失败: {filepath}")
            # 清理不完整文件
            if filepath.exists():
                filepath.unlink()

        return success

    except Exception as e:
        logger.error(f"m3u8 下载异常: {e}")
        # 清理不完整文件
        if filepath.exists():
            filepath.unlink()
        return False


def _parse_m3u8(
    session: requests.Session,
    url: str,
    config: Config,
    depth: int = 0,
) -> tuple[list[str], dict]:
    """
    解析 m3u8 文件

    Args:
        session: requests.Session
        url: m3u8 URL
        config: 配置
        depth: 当前递归深度

    Returns:
        (分片 URL 列表, 加密信息)
    """
    if depth > MAX_REDIRECT_DEPTH:
        raise DownloadError(f"m3u8 递归深度超限: {depth}")

    try:
        resp = session.get(url, timeout=config.timeout)
        resp.raise_for_status()
        content = resp.text
    except Exception as e:
        raise DownloadError(f"m3u8 下载失败: {url}: {e}") from e

    lines = content.strip().split('\n')
    segments: list[str] = []
    encryption: dict = {}

    # 检查是否为 master playlist
    if any(line.strip().startswith('#EXT-X-STREAM-INF') for line in lines):
        # 选择最高质量的流
        best_url = _select_best_stream(lines, url)
        if best_url:
            return _parse_m3u8(session, best_url, config, depth + 1)

    # 解析 media playlist
    base_url = url.rsplit('/', 1)[0] + '/'
    current_key = None

    i = 0
    while i < len(lines):
        line = lines[i].strip()

        if line.startswith('#EXT-X-KEY'):
            # 解析加密信息
            key_info = _parse_key_line(line)
            if key_info.get('method') == 'AES-128':
                # 获取密钥
                key_url = urljoin(url, key_info.get('uri', ''))
                try:
                    key_resp = session.get(key_url, timeout=config.timeout)
                    key_resp.raise_for_status()
                    encryption = {
                        'method': 'AES-128',
                        'key': key_resp.content,
                        'iv': key_info.get('iv'),
                    }
                    current_key = encryption
                except Exception as e:
                    logger.warning(f"密钥获取失败: {key_url}: {e}")

        elif line.startswith('#EXTINF'):
            # 下一行是分片 URL
            if i + 1 < len(lines):
                segment_line = lines[i + 1].strip()
                if segment_line and not segment_line.startswith('#'):
                    segment_url = urljoin(url, segment_line)
                    segments.append(segment_url)
                i += 1  # 跳过分片行

        i += 1

    return segments, encryption


def _select_best_stream(lines: list[str], base_url: str) -> str | None:
    """
    选择最高质量的流

    Args:
        lines: m3u8 行
        base_url: 基础 URL

    Returns:
        流 URL
    """
    best_bandwidth = 0
    best_url = None

    i = 0
    while i < len(lines):
        line = lines[i].strip()

        if line.startswith('#EXT-X-STREAM-INF'):
            # 解析带宽
            bandwidth = 0
            for part in line.split(','):
                if 'BANDWIDTH=' in part:
                    try:
                        bandwidth = int(part.split('=')[1])
                    except ValueError:
                        pass

            # 下一行是 URL
            if i + 1 < len(lines):
                url_line = lines[i + 1].strip()
                if url_line and not url_line.startswith('#'):
                    if bandwidth > best_bandwidth:
                        best_bandwidth = bandwidth
                        best_url = urljoin(base_url, url_line)
                    i += 1  # 跳过 URL 行

        i += 1

    return best_url


def _parse_key_line(line: str) -> dict:
    """
    解析 EXT-X-KEY 行

    Args:
        line: KEY 行

    Returns:
        解析结果
    """
    result = {}
    parts = line.split('#EXT-X-KEY:')[1].split(',')

    for part in parts:
        part = part.strip()
        if '=' in part:
            key, value = part.split('=', 1)
            key = key.strip()
            value = value.strip().strip('"')

            if key == 'METHOD':
                result['method'] = value
            elif key == 'URI':
                result['uri'] = value
            elif key == 'IV':
                result['iv'] = value

    return result


def _download_segments(
    session: requests.Session,
    segments: list[str],
    config: Config,
) -> list[Path]:
    """
    下载分片

    Args:
        session: requests.Session
        segments: 分片 URL 列表
        config: 配置

    Returns:
        分片文件路径列表
    """
    temp_dir = Path(tempfile.mkdtemp(prefix='scrape_hls_'))
    segment_files: list[Path] = []

    for i, url in enumerate(segments):
        try:
            segment_path = temp_dir / f"segment_{i:06d}.ts"
            resp = session.get(url, timeout=config.timeout, stream=True)
            resp.raise_for_status()

            with open(segment_path, 'wb') as f:
                for chunk in resp.iter_content(chunk_size=config.chunk_size):
                    if chunk:
                        f.write(chunk)

            segment_files.append(segment_path)

        except Exception as e:
            logger.warning(f"分片下载失败 [{i}]: {url}: {e}")

    return segment_files


def _merge_segments(
    segment_files: list[Path],
    filepath: Path,
    encryption: dict | None = None,
) -> bool:
    """
    合并分片

    Args:
        segment_files: 分片文件列表
        filepath: 输出文件路径
        encryption: 加密信息

    Returns:
        是否成功
    """
    try:
        # 确保目录存在
        filepath.parent.mkdir(parents=True, exist_ok=True)

        # 使用 .part 临时文件
        part_path = filepath.with_suffix(filepath.suffix + '.part')

        with open(part_path, 'wb') as out_f:
            for segment_path in segment_files:
                try:
                    with open(segment_path, 'rb') as in_f:
                        data = in_f.read()

                    # AES 解密
                    if encryption and encryption.get('method') == 'AES-128':
                        data = _decrypt_segment(data, encryption)

                    out_f.write(data)
                except Exception as e:
                    logger.warning(f"分片合并失败: {segment_path}: {e}")
                    return False

        # 原子替换
        if filepath.exists():
            filepath.unlink()
        part_path.rename(filepath)

        return True

    except Exception as e:
        logger.error(f"合并失败: {e}")
        return False


def _decrypt_segment(data: bytes, encryption: dict) -> bytes:
    """
    AES-128 解密分片

    Args:
        data: 加密数据
        encryption: 加密信息

    Returns:
        解密数据
    """
    try:
        from Crypto.Cipher import AES
    except ImportError:
        logger.warning("pycryptodome 未安装，无法解密 AES-128")
        return data

    key = encryption.get('key')
    iv = encryption.get('iv')

    if not key:
        logger.warning("缺少解密密钥")
        return data

    # 处理 IV
    if iv:
        if isinstance(iv, str):
            iv = bytes.fromhex(iv.replace('0x', ''))
    else:
        iv = b'\x00' * 16

    try:
        cipher = AES.new(key, AES.MODE_CBC, iv)
        decrypted = cipher.decrypt(data)
        # 移除 PKCS7 填充
        if decrypted:
            pad_len = decrypted[-1]
            if pad_len <= 16:
                decrypted = decrypted[:-pad_len]
        return decrypted
    except Exception as e:
        logger.warning(f"AES 解密失败: {e}")
        return data


def _cleanup_segments(segment_files: list[Path]) -> None:
    """
    清理临时分片文件

    Args:
        segment_files: 分片文件列表
    """
    for path in segment_files:
        try:
            if path.exists():
                path.unlink()
        except Exception:
            pass

    # 尝试删除临时目录
    if segment_files:
        try:
            temp_dir = segment_files[0].parent
            if temp_dir.exists():
                temp_dir.rmdir()
        except Exception:
            pass