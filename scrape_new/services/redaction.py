"""
敏感字段脱敏 — 给所有 console / log / report 输出过一遍。

覆盖字段:
  - sessionid / csrftoken / p_auth_token / vc3 / uf / UID / _uid / cx_p_token
  - fanyamoocs / jrose / route
  - Cookie: <value>
  - XTBZ_COOKIE=<value>
  - Authorization: <value>
  - token= / access_token= / refresh_token=
  - JSESSIONID= / STUDY_SESS= / NTES_SESS=

设计:
  - 纯函数, 不读 cookie, 只做正则替换
  - 用于 driver / test / log 的脱敏层
"""

from __future__ import annotations

import re

# (pattern, replacement) — 顺序敏感, 长模式在前
_PATTERNS: list[tuple[str, str]] = [
    # Cookie: 头(整行 / 整段)
    (r"(?i)(Cookie:\s*)[^\r\n]+", r"\1[REDACTED]"),
    # Authorization: 头
    (r"(?i)(Authorization:\s*)[^\r\n]+", r"\1[REDACTED]"),
    # 标准 cookie 名=值(直到 ; 或 空白)
    (r"(?i)(sessionid=)[^;\s]+", r"\1[REDACTED]"),
    (r"(?i)(csrftoken=)[^;\s]+", r"\1[REDACTED]"),
    (r"(?i)(p_auth_token=)[^;\s]+", r"\1[REDACTED]"),
    (r"(?i)(vc3=)[^;\s]+", r"\1[REDACTED]"),
    (r"(?i)(uf=)[^;\s]+", r"\1[REDACTED]"),
    (r"(?i)(UID=)[^;\s]+", r"\1[REDACTED]"),
    (r"(?i)(_uid=)[^;\s]+", r"\1[REDACTED]"),
    (r"(?i)(cx_p_token=)[^;\s]+", r"\1[REDACTED]"),
    (r"(?i)(fanyamoocs=)[^;\s]+", r"\1[REDACTED]"),
    (r"(?i)(jrose=)[^;\s]+", r"\1[REDACTED]"),
    (r"(?i)(route=)[^;\s]+", r"\1[REDACTED]"),
    (r"(?i)(JSESSIONID=)[^;\s]+", r"\1[REDACTED]"),
    (r"(?i)(STUDY_SESS=)[^;\s]+", r"\1[REDACTED]"),
    (r"(?i)(NTES_SESS=)[^;\s]+", r"\1[REDACTED]"),
    # token= 通用(避免误伤别处, 只匹配 ...&token= 或 token= ; 等边界)
    (r"(?i)(access_token=)[^;\s&]+", r"\1[REDACTED]"),
    (r"(?i)(refresh_token=)[^;\s&]+", r"\1[REDACTED]"),
    # token= 通用: 必须紧接 ? / & / ; / 开头 (避免误伤 "tokenization" 等普通单词)
    (r"(?i)([?&;\s]|^)token=[^&\s;]+", r"\1token=[REDACTED]"),
    # XTBZ_COOKIE=<value>: 吞到空格结束(不含引号, 处理 XTBZ_COOKIE="k8s=...; vc3=..." 场景)
    (r"(?i)(XTBZ_COOKIE=)[^\s]+", r"\1[REDACTED]"),
]


def redact_sensitive(text: str | None) -> str:
    """返回脱敏后的字符串。None 输入返回 None / 空串。

    用法:
        from scrape_new.services.redaction import redact_sensitive
        print(redact_sensitive(stdout))
        log_line = redact_sensitive(raw_log_line)
    """
    if not text:
        return text or ""
    out = text
    for pat, repl in _PATTERNS:
        out = re.sub(pat, repl, out)
    return out