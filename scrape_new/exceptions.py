"""
自定义异常

定义所有自定义异常类型，便于结构化错误处理。
"""


class ScrapeError(Exception):
    """基础异常"""
    pass


class ConfigError(ScrapeError):
    """配置错误"""
    pass


class CookieError(ScrapeError):
    """Cookie 相关错误"""
    pass


class DownloadError(ScrapeError):
    """下载错误"""
    pass


class ExtractorError(ScrapeError):
    """提取器错误"""
    pass


class ValidationError(ScrapeError):
    """验证错误"""
    pass


class BlockerError(ScrapeError):
    """阻断条件错误（验证码、登录、付费墙等）"""
    pass


class HistoryError(ScrapeError):
    """历史记录错误"""
    pass


class OrganizerError(ScrapeError):
    """归档错误"""
    pass