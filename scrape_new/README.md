# 网页资源扒取工具箱 (Scrape Toolkit)

**当前版本：v0.2.0**

现代化重构版本，支持多种资源类型的网页下载工具。

## 项目定位

- 课程资源整理
- 已授权资源备份
- 教学资料迁移辅助
- 老师后台内容整理辅助

## 功能特性

- **多种资源类型支持**：视频、图片、文档、表格、文章、链接、JSON/API
- **智能提取**：自动从 HTML 中提取资源链接
- **批量下载**：支持从文件读取 URL 列表批量下载
- **历史记录**：自动记录下载历史，支持去重
- **失败重试**：支持重试失败的下载任务
- **自动归档**：自动整理和重命名下载文件
- **m3u8 支持**：支持 HLS 视频下载和 AES 解密
- **阻断检测**：自动检测登录墙、验证码、付费墙
- **平台工作流**：超星、智慧树、学堂在线、中国大学MOOC 一键下载
- **老师后台**：自动建课、上传视频、生成习题

## 安装

```bash
# 克隆项目
git clone <repo-url>
cd scrape_new

# 安装依赖
pip install -e .

# 或者手动安装
pip install requests beautifulsoup4
pip install pycryptodome  # 可选，用于 m3u8 AES 解密
```

## 基础用法

### 通用下载

```bash
# 下载视频
python -m scrape_new 视频 https://example.com/video-page

# 下载图片到指定目录
python -m scrape_new 图片 https://example.com/gallery ./images

# 下载文档
python -m scrape_new 文档 https://example.com/documents ./docs

# 下载全部资源
python -m scrape_new 全部 https://example.com/page ./output
```

### 批量下载

```bash
# 创建 URL 列表文件 urls.txt（每行一个 URL，# 开头为注释）
python -m scrape_new batch 视频 urls.txt ./output
```

### 历史与重试

```bash
# 查看历史
python -m scrape_new --history

# 重试失败
python -m scrape_new --retry ./output/video
```

### 运行测试

```bash
python -m scrape_new --test
```

## 平台工作流

### 统一入口（推荐）

```bash
# 超星学习通
python -m scrape_new platform chaoxing "https://mooc2-ans.chaoxing.com/..." ./output

# 智慧树/知到
python -m scrape_new platform zhihuishu "https://..." ./output

# 学堂在线
python -m scrape_new platform xuetangx "https://..." ./output

# 中国大学MOOC
python -m scrape_new platform icourse163 "https://..." ./output
```

### 直接调用（兼容旧用法）

```bash
python scrape_new/workflows/chaoxing.py "课程URL" [输出目录]
python scrape_new/workflows/zhihuishu.py "课程URL" [输出目录]
python scrape_new/workflows/xuetangx.py "课程URL" [输出目录]
python scrape_new/workflows/icourse163.py "课程URL" [输出目录]
```

## 老师后台搭建

### 统一入口（推荐）

```bash
# 生成映射文件
python -m scrape_new upload build-mapping \
  --videos ./videos \
  --doc ./outline.json \
  --course-id 123456 \
  --output ./mapping.json

# 上传（推荐使用 --cookies-file）
python -m scrape_new upload upload \
  --mapping ./mapping.json \
  --cookies-file ./cookies.txt

# 验证 Cookie
python -m scrape_new upload upload \
  --mapping ./mapping.json \
  --cookies-file ./cookies.txt \
  --verify-only

# 干跑看计划
python -m scrape_new upload upload \
  --mapping ./mapping.json \
  --cookies-file ./cookies.txt \
  --dry-run
```

### 直接调用（兼容旧用法）

```bash
python -m scrape_new.upload build-mapping --videos ./videos --doc ./outline.json --course-id 123456
python -m scrape_new.upload upload --mapping ./mapping.json --cookies-file ./cookies.txt
```

## Cookie 使用

### Cookie 格式

**cookies.txt**（Netscape 格式）：
```
# Netscape HTTP Cookie File
.example.com	TRUE	/	FALSE	0	name	value
```

**cookies.json**（简单格式）：
```json
{
  "name1": "value1",
  "name2": "value2"
}
```

### Cookie 加载方式

**推荐：使用 --cookies-file**
```bash
python -m scrape_new upload upload --mapping ./mapping.json --cookies-file ./cookies.txt
```

**推荐：使用环境变量**
```bash
export XTBZ_COOKIE="name1=value1; name2=value2"
python -m scrape_new upload upload --mapping ./mapping.json
```

**不推荐：命令行直接传 Cookie（可能进入 shell history）**
```bash
python -m scrape_new upload upload --mapping ./mapping.json --cookies-string 'name1=value1; name2=value2'
```

### Cookie 安全说明

- Cookie 是敏感凭据，请勿泄露
- 不要在日志或报告中输出完整 Cookie
- 推荐使用 `--cookies-file` 或环境变量
- 避免在命令行直接传递 Cookie 字符串

## 配置文件

创建 `config.json` 配置文件：

```json
{
  "headers": {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
  },
  "cookies_file": "cookies.txt",
  "check_cookie": false,
  "max_retries": 3,
  "retry_delay": 1.0,
  "timeout": 30,
  "chunk_size": 8192,
  "min_video_size": 102400,
  "min_image_size": 1024,
  "min_document_size": 512,
  "suspicious_ratio": 0.5,
  "history_max_records": 500,
  "history_file": "history.json",
  "auto_organize": true,
  "generate_report": true,
  "generate_manifest": true,
  "play_sound": true,
  "proxy": null,
  "block_on_captcha": true,
  "block_on_login": true,
  "block_on_payment": true
}
```

使用配置文件：
```bash
python -m scrape_new -c config.json 视频 https://example.com/video
```

## 历史记录

- 自动记录每次下载
- URL 使用 SHA256 短哈希去重
- 默认保留最近 500 条记录
- 使用 `--no-dedup` 跳过去重

## 自动归档

下载完成后自动：
- 清理非法文件名
- 重命名哈希文件名
- 按序号整理文件
- 跳过报告文件（`_report.json`, `_download_log.csv` 等）

## m3u8 支持

- 支持 master playlist 自动选择最高质量流
- 支持分片下载和合并
- 支持 AES-128 解密（需要 `pycryptodome`）
- 使用 `urllib.parse.urljoin` 正确处理相对路径

## 常见问题

### Q: 下载失败怎么办？
A: 使用 `--retry` 重试：
```bash
python -m scrape_new --retry ./output
```

### Q: 如何跳过去重？
A: 使用 `--no-dedup`：
```bash
python -m scrape_new --no-dedup 视频 https://example.com/video
```

### Q: 如何查看详细日志？
A: 使用 `-v`：
```bash
python -m scrape_new -v 视频 https://example.com/video
```

### Q: 遇到验证码/登录墙怎么办？
A: 程序会自动检测并停止，提示需要登录或验证码。请手动登录后使用 Cookie。

### Q: 新旧项目有什么区别？
A: 旧项目 `scrape/` 保留用于兼容，新项目 `scrape_new/` 是推荐使用版本，后续新功能只进入 `scrape_new/`。

## 安全与合规

**重要声明：**

本工具仅用于下载用户**有权访问**的资源。本工具：

- 不绕过验证码
- 不绕过登录墙
- 不绕过付费墙
- 不破解 DRM
- 不破解加密授权
- 不绕过平台限制
- 不包含自动刷课、自动答题功能

遇到需要登录、验证码、付费的页面，本工具只会检测并提示，不会尝试绕过。

请确保：
1. 只下载您有权访问的资源
2. 遵守网站的使用条款
3. 尊重版权和知识产权

## 项目结构

```
scrape_new/
├── __init__.py          # 包初始化
├── __main__.py          # python -m 入口
├── cli.py               # 命令行接口
├── app.py               # 业务流程核心
├── config.py            # 配置管理
├── models.py            # 数据模型
├── exceptions.py        # 自定义异常
├── core/                # 核心能力
│   ├── session.py       # Session 管理
│   ├── cookies.py       # Cookie 处理
│   ├── downloader.py    # 文件下载
│   ├── hls.py           # m3u8 处理
│   ├── verifier.py      # 文件校验
│   ├── paths.py         # 路径操作
│   ├── blockers.py      # 阻断检测
│   └── notify.py        # 完成通知
├── extractors/          # 提取器
│   ├── video.py         # 视频提取
│   ├── image.py         # 图片提取
│   ├── document.py      # 文档提取
│   ├── table.py         # 表格提取
│   ├── article.py       # 文章提取
│   ├── links.py         # 链接提取
│   └── api.py           # API 提取
├── services/            # 服务层
│   ├── history.py       # 历史记录
│   ├── reporter.py      # 报告生成
│   ├── organizer.py     # 自动归档
│   ├── batch.py         # 批量下载
│   └── retry.py         # 失败重试
├── workflows/           # 平台工作流
│   ├── runner.py        # 统一入口
│   ├── chaoxing.py      # 超星学习通
│   ├── zhihuishu.py     # 智慧树/知到
│   ├── xuetangx.py      # 学堂在线
│   └── icourse163.py    # 中国大学MOOC
├── upload/              # 老师后台搭建
│   ├── runner.py        # 统一入口
│   ├── api_uploader.py  # 建课主流程
│   ├── outline.py       # 章节目录解析
│   ├── mapping.py       # 视频-章节映射
│   ├── exercise_docx.py # 习题生成
│   └── exercise_upload.py # 习题上传
├── docs/                # 文档
│   ├── SESSION_PROTOCOL.md
│   └── COOKIE_GUIDE.md
└── tests/               # 测试
```

## 开发

```bash
# 运行测试
pytest scrape_new/tests/ -v

# 编译检查
python -m compileall scrape_new

# 冒烟测试
python scrape_new/scripts/smoke_test.py

# 代码检查
pip install ruff
ruff check scrape_new/
```

## 许可证

MIT License