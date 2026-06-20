# 网页资源扒取工具箱 (Scrape Toolkit)

**当前版本：v0.3.0**

[![CI](https://github.com/L1GOAT/Learning-Resources-Download-Scrapes/actions/workflows/ci.yml/badge.svg)](https://github.com/L1GOAT/Learning-Resources-Download-Scrapes/actions/workflows/ci.yml)


现代化重构版本，支持多种资源类型的网页下载 + 老师后台建课。

## 核心能力 / Why this tool is safer

这个工具最强的三个设计点是：**资源审计 + 计划先行的上传 + 向导模式**。

### 下载侧 — 资源智能审计

下载完成后,工具能自动审计 `_chapter_tree.json` 和 `_resource_naming_manifest.json`,生成 `_resource_audit.{json,md,csv}`:

- 漏扫检测:空节 / 空章 / count_mismatch / duplicate_objectid / duplicate_saved_name
- 错分类检测:扩展名 / MIME / 标题关键字 / tab 倾向 多源判定,带置信度与证据链
- 角色识别:`video` / `ppt` / `pdf` / `doc` / `english` / `quiz` / `note` / `image` / `attachment`
- 错配检测:`missing_local_file` / `attachment_as_video` / `non_video_in_video_slot` / `duplicate_file_use` / `ppt_only_lesson_informational`
- Markdown 报告用中文短语明确提示:`可能漏扫` / `需要人工确认` / `可以安全跳过` / `建议补资源` / `建议只重扫该节`

```bash
python -m scrape_new audit \
  --chapter-tree ./output/_chapter_tree.json \
  --manifest ./output/_resource_naming_manifest.json \
  --mapping ./output/_mapping.json \
  --output-dir ./output
```

### 上传侧 — 默认 plan-first,不直接写后台

`upload upload` 默认先生成 `_upload_plan.json` / `_upload_plan.md`,**不直接调用后台写接口**:

- `_upload_plan.json` 包含 `course_id` / `mapping_hash` / `tree_fingerprint` / `scope` / `summary` / `items`
- `apply-plan` 校验 `mapping_hash` + `tree_fingerprint` + `scope` + `course_id` 四件套,后台树一变就拒执行
- 局部修改模式( `--only-lessons` / `--only-resources` )禁止 `reset-confirm`,防"只改一处却清空重建"
- `RENAME` 默认 `pending`,不自动 `delete + create`
- reset / rename 需要显式 `--reset-confirm` / `--confirm-rename`

```bash
# 1) 先生成 plan
python -m scrape_new upload upload \
  --mapping ./output/_mapping.json \
  --cookies-file ./cookies.txt \
  --output-dir ./output

# 2) 检查 plan 没问题再 apply
python -m scrape_new upload apply-plan \
  --plan ./output/_upload_plan.json \
  --cookies-file ./cookies.txt
```

### 向导侧 — `wizard` / `assistant` 只生成计划

向 AI 助手 / GUI 暴露统一入口 `python -m scrape_new wizard`(别名 `assistant`):

- 7 个 `intent`: `download` / `scan` / `build_mapping` / `upload` / `retry` / `modify` / `audit`
- 4 种 cookie 来源:`curl` / `string` / `file` / `env`
- 默认只生成 `WorkflowPlan`,**不执行危险操作**
- `apply-plan` 步骤标记 `requires_confirmation=True` + `destructive=True`
- 输出 JSON(给 GUI / CI 消费) 或 Markdown(给 README / issue)
- 未来 GUI 可直接 `json.loads(plan.to_json())`

```bash
# 交互式向导
python -m scrape_new wizard

# 非交互(给 AI / 脚本用)
python -m scrape_new wizard --intent upload --course-id <id> --cookie-source env --json
python -m scrape_new wizard --intent audit --markdown
```

---

## 项目定位

- 课程资源整理
- 已授权资源备份
- 教学资料迁移辅助
- 老师后台内容整理辅助

## 功能特性

- **多种资源类型支持**:视频、图片、文档、表格、文章、链接、JSON/API
- **智能提取**:自动从 HTML 中提取资源链接
- **批量下载**:支持从文件读取 URL 列表批量下载
- **历史记录**:自动记录下载历史,支持去重
- **失败重试**:支持重试失败的下载任务
- **自动归档**:自动整理和重命名下载文件
- **m3u8 支持**:支持 HLS 视频下载和 AES 解密
- **阻断检测**:自动检测登录墙、验证码、付费墙
- **平台工作流**:超星、智慧树、学堂在线、中国大学MOOC 一键下载
- **资源审计**:下载后能发现漏扫 / 错分类 / 重复资源 / PPT-only / 空节
- **老师后台**:自动建课、上传视频、生成习题

## 安装

```bash
# 克隆项目
git clone <repo-url>
cd scrape_new

# 安装依赖
pip install -e .

# 或者手动安装
pip install requests beautifulsoup4
pip install pycryptodome  # 可选,用于 m3u8 AES 解密
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
# 创建 URL 列表文件 urls.txt(每行一个 URL,# 开头为注释)
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

### 统一入口(推荐)

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

### 直接调用(兼容旧用法)

```bash
python scrape_new/workflows/chaoxing.py "课程URL" [输出目录]
python scrape_new/workflows/zhihuishu.py "课程URL" [输出目录]
python scrape_new/workflows/xuetangx.py "课程URL" [输出目录]
python scrape_new/workflows/icourse163.py "课程URL" [输出目录]
```

### 超星下载侧的强点(下载后可审计)

- `--scan-only` 只扫描不下载
- `--max-tabs N` 控制扫描 tab 数(0=视频 / 1=PPT+文档 / 2=测验 / 3=笔记)
- `--resume` 跳过已下载
- `--retry-downloads` 重试失败 / 可疑文件
- `--verify-resume-only` 只验证 resume 状态
- `--include-empty-lessons` 包含空课时
- 下载产物:`_chapter_tree.json` / `_resource_naming_manifest.json` / `_review.html`

## 老师后台搭建

### 统一入口(推荐)

```bash
# 1) 生成 mapping(从 outline.json + videos 目录推断)
python -m scrape_new upload build-mapping \
  --videos ./videos \
  --doc ./outline.json \
  --course-id <id> \
  --output ./mapping.json

# 2) 验证 Cookie + 拉后台现有树
python -m scrape_new upload upload \
  --mapping ./mapping.json \
  --cookies-file ./cookies.txt \
  --verify-only

# 3) 干跑看计划(生成 _upload_plan.json,不写后台)
python -m scrape_new upload upload \
  --mapping ./mapping.json \
  --cookies-file ./cookies.txt \
  --dry-run

# 4) apply-plan(校验 mapping_hash + tree_fingerprint 后才写)
python -m scrape_new upload apply-plan \
  --plan ./output/_upload_plan.json \
  --cookies-file ./cookies.txt
```

### 增量 / 局部修改

```bash
# 只补指定 lesson(增量,不 reset 后台)
python -m scrape_new upload upload \
  --mapping ./mapping.json \
  --cookies-file ./cookies.txt \
  --only-lessons "1.1,1.2,3.5"

# 只补指定资源(PPT-only lesson 等)
python -m scrape_new upload upload \
  --mapping ./mapping.json \
  --cookies-file ./cookies.txt \
  --only-resources "1.2:ppt"

# reset / rename 需要显式确认
python -m scrape_new upload upload \
  --mapping ./mapping.json \
  --cookies-file ./cookies.txt \
  --reset-confirm <id>      # 清空后台所有章节
python -m scrape_new upload upload \
  --mapping ./mapping.json \
  --cookies-file ./cookies.txt \
  --confirm-rename          # 真正执行 RENAME(delete + create)
```

### 直接调用(兼容旧用法)

```bash
python -m scrape_new.upload build-mapping --videos ./videos --doc ./outline.json --course-id <id>
python -m scrape_new.upload upload --mapping ./mapping.json --cookies-file ./cookies.txt
```

## 资源审计

下载 / build-mapping / upload 任一阶段完成后,可独立跑审计:

```bash
# 全量(chapter-tree + manifest + mapping)
python -m scrape_new audit \
  --chapter-tree ./output/_chapter_tree.json \
  --manifest ./output/_resource_naming_manifest.json \
  --mapping ./output/_mapping.json \
  --output-dir ./output

# 只扫 mapping(挂错节 / 漏挂 / 重复)
python -m scrape_new audit \
  --mapping ./output/_mapping.json \
  --manifest ./output/_resource_naming_manifest.json \
  --output-dir ./output

# 只查漏扫
python -m scrape_new audit \
  --chapter-tree ./output/_chapter_tree.json \
  --manifest ./output/_resource_naming_manifest.json \
  --output-dir ./output
```

产出 3 份文件:

- `_resource_audit.json` — 完整结构化数据(GUI / CI 消费)
- `_resource_audit.md` — 人类可读报告(高风险节 + 问题资源明细 + 中文提示)
- `_resource_audit.csv` — 表格(每行一个资源,issue / suggestion 列)

## 向导模式

```bash
# 交互式问问题
python -m scrape_new wizard

# 直接给参数
python -m scrape_new wizard --intent download --platform chaoxing --url "<URL>" --output-dir ./course --cookie-source env

# JSON 输出(给 AI / GUI 用)
python -m scrape_new wizard --intent upload --course-id <id> --cookie-source env --json

# Markdown 输出(给 README / issue 用)
python -m scrape_new wizard --intent build_mapping --markdown
python -m scrape_new wizard --intent audit --markdown

# assistant 是 wizard 的别名
python -m scrape_new assistant --intent scan --markdown
```

`wizard` 默认**只生成计划**,不会执行任何写操作(包括 `upload apply-plan`)。

## Cookie 使用

### Cookie 格式

**cookies.txt**(Netscape 格式):
```
# Netscape HTTP Cookie File
.example.com	TRUE	/	FALSE	0	name	value
```

**cookies.json**(简单格式):
```json
{
  "name1": "value1",
  "name2": "value2"
}
```

### Cookie 加载方式

**推荐:使用 `--cookies-file`**
```bash
python -m scrape_new upload upload --mapping ./mapping.json --cookies-file ./cookies.txt
```

**推荐:使用环境变量**
```bash
export XTBZ_COOKIE="name1=value1; name2=value2"
python -m scrape_new upload upload --mapping ./mapping.json
```

**不推荐:命令行直接传 Cookie(可能进入 shell history)**
```bash
python -m scrape_new upload upload --mapping ./mapping.json --cookies-string 'name1=value1; name2=value2'
```

### Cookie 安全说明

- Cookie 是敏感凭据,请勿泄露
- 不要在日志或报告中输出完整 Cookie
- 推荐使用 `--cookies-file` 或环境变量
- 避免在命令行直接传递 Cookie 字符串

## 配置文件

创建 `config.json` 配置文件:

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

使用配置文件:
```bash
python -m scrape_new -c config.json 视频 https://example.com/video
```

## 历史记录

- 自动记录每次下载
- URL 使用 SHA256 短哈希去重
- 默认保留最近 500 条记录
- 使用 `--no-dedup` 跳过去重

## 自动归档

下载完成后自动:
- 清理非法文件名
- 重命名哈希文件名
- 按序号整理文件
- 跳过报告文件(`_report.json`, `_download_log.csv` 等)

## m3u8 支持

- 支持 master playlist 自动选择最高质量流
- 支持分片下载和合并
- 支持 AES-128 解密(需要 `pycryptodome`)
- 使用 `urllib.parse.urljoin` 正确处理相对路径

## 常见问题

### Q: 下载失败怎么办?
A: 使用 `--retry` 重试:
```bash
python -m scrape_new --retry ./output
```

### Q: 如何跳过去重?
A: 使用 `--no-dedup`:
```bash
python -m scrape_new --no-dedup 视频 https://example.com/video
```

### Q: 如何查看详细日志?
A: 使用 `-v`:
```bash
python -m scrape_new -v 视频 https://example.com/video
```

### Q: 遇到验证码/登录墙怎么办?
A: 程序会自动检测并停止,提示需要登录或验证码。请手动登录后使用 Cookie。

### Q: 新旧项目有什么区别?
A: 旧项目 `scrape/` 保留用于兼容,新项目 `scrape_new/` 是推荐使用版本,后续新功能只进入 `scrape_new/`。

### Q: 上传前怎么知道资源没漏 / 没挂错?
A: 先跑一次 `audit`:
```bash
python -m scrape_new audit --chapter-tree ./_chapter_tree.json --manifest ./_resource_naming_manifest.json --output-dir .
```
打开 `_resource_audit.md` 看高风险节。

### Q: 不想立刻执行 upload,只想看计划?
A: 用 `upload upload --dry-run` 或 `wizard --intent upload`:
```bash
python -m scrape_new wizard --intent upload --course-id <id> --cookie-source env --json
```

## 安全与合规

**重要声明:**

本工具仅用于下载用户**有权访问**的资源。本工具:

- 不绕过验证码
- 不绕过登录墙
- 不绕过付费墙
- 不破解 DRM
- 不破解加密授权
- 不绕过平台限制
- 不包含自动刷课、自动答题功能

遇到需要登录、验证码、付费的页面,本工具只会检测并提示,不会尝试绕过。

请确保:
1. 只下载您有权访问的资源
2. 遵守网站的使用条款
3. 尊重版权和知识产权

## 项目结构

```
scrape_new/
├── __init__.py             # 包初始化
├── __main__.py             # python -m 入口
├── cli.py                  # 命令行接口(含 audit / wizard 子命令)
├── app.py                  # 业务流程核心
├── config.py               # 配置管理
├── models.py               # 数据模型
├── exceptions.py           # 自定义异常
├── core/                   # 核心能力
│   ├── session.py          # Session 管理
│   ├── cookies.py          # Cookie 处理
│   ├── downloader.py       # 文件下载
│   ├── hls.py              # m3u8 处理
│   ├── verifier.py         # 文件校验
│   ├── paths.py            # 路径操作
│   ├── blockers.py         # 阻断检测
│   └── notify.py           # 完成通知
├── extractors/             # 提取器
│   ├── video.py            # 视频提取
│   ├── image.py            # 图片提取
│   ├── document.py         # 文档提取
│   ├── table.py            # 表格提取
│   ├── article.py          # 文章提取
│   ├── links.py            # 链接提取
│   └── api.py              # API 提取
├── services/               # 服务层
│   ├── history.py          # 历史记录
│   ├── reporter.py         # 报告生成
│   ├── organizer.py        # 自动归档
│   ├── batch.py            # 批量下载
│   ├── retry.py            # 失败重试
│   ├── english_detect.py   # 英文视频识别
│   ├── download_resume.py  # resume / retry-downloads
│   ├── resource_manifest.py# _resource_naming_manifest 生成
│   ├── review_html.py      # _review.html 浏览器可视审计
│   ├── scan_chaoxing.py    # 超星 cards API 扫描
│   ├── resource_audit.py   # 资源智能审计(JSON / MD / CSV)
│   └── workflow_planner.py # WorkflowPlan + 7 个 intent
├── workflows/              # 平台工作流
│   ├── runner.py           # 统一入口
│   ├── chaoxing.py         # 超星学习通
│   ├── zhihuishu.py        # 智慧树/知到
│   ├── xuetangx.py         # 学堂在线
│   └── icourse163.py       # 中国大学MOOC
├── upload/                 # 老师后台搭建
│   ├── runner.py           # 统一入口
│   ├── api_uploader.py     # 建课主流程
│   ├── outline.py          # 章节目录解析
│   ├── mapping.py          # 视频-章节映射
│   ├── exercise_docx.py    # 习题生成
│   └── exercise_upload.py  # 习题上传
├── docs/                   # 文档
│   ├── SESSION_PROTOCOL.md
│   └── COOKIE_GUIDE.md
└── tests/                  # 测试(447 个用例)
```

## 开发

```bash
# 运行测试
pytest scrape_new/tests/ -q

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