"""
稳定资源 key —— 增量 resume / 跨运行跳过 / 失败重试的核心。

设计目标:
  - 同一资源(课程+章+节+role+saved_name)在多次运行里生成同样的 key
  - 跨课程/跨章/跨节的 key 不冲突
  - key 是 16 字符 hex(可读 + 文件名安全)
  - 跟 resource_key 一样支持 parse 回查(用于读 _retry_resources.json 重跑)

注意:saved_name 不带扩展名(去掉 .mp4/.pptx),避免 .mp4 改名 .mov 后误判
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import Optional


# ─── 生成 key ──────────────────────────────────────────────────

def make_resource_key(
    course_id: str,
    chapter_index: int,
    lesson_id: str,
    role: str,
    saved_name: str,
) -> str:
    """生成稳定的资源 key(16 字符 hex)。

    格式:课程 id + 章 + 节 + role + saved_name(去扩展名)的 SHA1 前 16 字符。
    """
    stem = _stem(saved_name)
    parts = (
        str(course_id or "").strip(),
        str(int(chapter_index or 0)),
        str(lesson_id or "").strip(),
        str(role or "").strip(),
        stem,
    )
    raw = "|".join(parts)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def _stem(saved_name: str) -> str:
    """去掉扩展名(.mp4/.pptx 等),便于跨扩展名识别同一资源。

    Windows 路径分隔符也去掉。
    """
    if not saved_name:
        return ""
    # 去掉路径
    name = saved_name.replace("\\", "/").split("/")[-1]
    # 去掉最后一个扩展名(.tar.gz 留 .tar 这种行为不要)
    if "." in name:
        name = name.rsplit(".", 1)[0]
    return name.strip().lower()


# ─── parse ─────────────────────────────────────────────────────

@dataclass(frozen=True)
class ResourceKeyParts:
    """parse_resource_key 的反向结果(只能从 parts 还原 key,不能从 key 还原 parts)。"""
    course_id: str
    chapter_index: int
    lesson_id: str
    role: str
    saved_name_stem: str


def parse_resource_key(key: str) -> Optional[ResourceKeyParts]:
    """从 key 反查(仅作"这 key 是不是我生成的"判定,不能反推原始字段)。

    当前实现:key 是 16 字符 SHA1 截断,不可逆。这里只返回 None 表示不可解析。
    后续如果改成"明文 + 短 hash"格式,可以真反查。
    """
    if not key or not re.match(r"^[0-9a-f]{16}$", key):
        return None
    return None  # SHA1 不可逆


# ─── 辅助 ─────────────────────────────────────────────────────

def normalize_saved_name(saved_name: str) -> str:
    """归一化 saved_name:去扩展名 + 去路径 + 小写。用于跨运行比对。

    用法:即使有人把 1.1_技术.mp4 改成 1.1_技术.mov,只要 stem 一样,key 也一样。
    """
    return _stem(saved_name)
