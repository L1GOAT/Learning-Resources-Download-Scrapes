# scrape_new 用户指南

> 5 分钟从 0 跑通 scan / audit / wizard / plan-first 完整链路。
>
> 不用真实课程、Cookie、URL — 全部基于本地 fixture。

## 1. 这是什么工具

`scrape_new` 是从公开课程平台扒资源 + 搭到老师后台的工具箱。它有三个最关键的设计：

| 设计 | 含义 |
|---|---|
| **资源审计** | 下载/建课前能发现漏扫、错分类、PPT-only、重复资源、附件错挂 |
| **plan-first** | 上传默认只生成 `_upload_plan.json`，**不直接写后台**；apply-plan 校验后台树四件套 |
| **wizard / assistant** | 统一入口，按"意图"出计划，危险 step 永远拒绝自动执行 |

完整架构概览见 [`README.md`](../README.md)。本文档聚焦**怎么用**。

## 2. 5 分钟离线 demo

仓库自带一份假课 fixture，跑它能从 0 体验完 audit + wizard 完整链路：

```bash
git clone https://github.com/L1GOAT/Learning-Resources-Download-Scrapes.git
cd Learning-Resources-Download-Scrapes
pip install -r scrape_new/requirements.txt
pip install pytest

# 1) 跑资源审计(纯本地,无网络)
python -m scrape_new audit \
  --chapter-tree scrape_new/tests/fixtures/course_audit_demo/_chapter_tree.json \
  --manifest scrape_new/tests/fixtures/course_audit_demo/_resource_naming_manifest.json \
  --mapping scrape_new/tests/fixtures/course_audit_demo/_mapping.json \
  --output-dir ./demo_output

# 2) 看产物
ls demo_output/                       # 3 份文件
cat demo_output/_resource_audit.md    # 人类可读报告
```

第 1 步会在 `./demo_output/` 写出：

- `_resource_audit.json` — 结构化数据(GUI / CI 可直接消费)
- `_resource_audit.md` — 人类可读报告(中文短语提示:可能漏扫/需要人工确认/可以安全跳过/建议补资源/建议只重扫该节)
- `_resource_audit.csv` — 表格(Excel 可开)

Fixture 故意制造了 7 类常见错配,详情见 [`docs/examples/resource_audit_demo.md`](examples/resource_audit_demo.md)。

接下来用 wizard 走一遍计划 + 执行:

```bash
# 3) 看 wizard 生成的 WorkflowPlan(不执行任何命令)
python -m scrape_new wizard --intent audit --markdown

# 4) 让 wizard 实际执行 audit_scan 这一步
python -m scrape_new wizard \
  --intent audit \
  --output-dir ./demo_output \
  --execute-step audit_scan \
  --run-log ./demo_output/_wizard_runs.jsonl

# 5) 看执行日志
cat ./demo_output/_wizard_runs.jsonl
```

第 4 步会调用 `python -m scrape_new audit ...` 子命令、捕获 stdout/stderr、追加到 `_wizard_runs.jsonl`。每行一条 JSON,字段:

- `step_id` / `title` / `command` / `returncode` / `elapsed_seconds` / `status` / `stdout_tail` / `stderr_tail`

如果想让 wizard 跑**完整 4 步流程**(scan → mapping alignment 等),只需把第 4 步的 `--execute-step audit_scan` 改成 `--execute-step audit_mapping` 即可。

## 3. 真实课程推荐流程

下面假设你**已经获得**课程平台授权。流程**严格 plan-first**,任何危险操作都需要人工 review。

### 3.1 扫描(scan-only)

```bash
# 超星课程:只扫资源,不下载任何文件
python -m scrape_new platform chaoxing "<课程 URL>" ./mycourse --scan-only
```

会生成:

- `_chapter_tree.json` / `_chapter_tree.md` — 章节结构
- `_resource_naming_manifest.json` — 资源清单(videos / pptx / pdf / docx)
- `_review.html` — 浏览器可开的资源总览(双击即可)
- `_retry_downloads.json` — 失败/可疑文件清单

### 3.2 下载

```bash
# 全量下载(基于 _resource_naming_manifest.json)
python -m scrape_new platform chaoxing "<课程 URL>" ./mycourse

# 增量(只下缺失的)
python -m scrape_new platform chaoxing "<课程 URL>" ./mycourse --resume

# 只重试失败/可疑
python -m scrape_new platform chaoxing "<课程 URL>" ./mycourse --retry-downloads
```

下载产物在 `./mycourse/视频/` 和 `./mycourse/文档/` 下。

### 3.3 审计(audit)

```bash
python -m scrape_new audit \
  --chapter-tree ./mycourse/_chapter_tree.json \
  --manifest ./mycourse/_resource_naming_manifest.json \
  --output-dir ./mycourse
```

打开 `./mycourse/_resource_audit.md` 看高风险节。**这是上传前最重要的一步**。

### 3.4 建 mapping(build-mapping)

```bash
# 推荐:扒视频时自动产生 _chapter_outline.json,直接读,零人工核对
python -m scrape_new upload build-mapping \
  --videos ./mycourse/视频 \
  --doc ./mycourse/_chapter_outline.json \
  --course-id <id> \
  --output ./mycourse/_mapping.json

# 旧流程(老师给的 .doc 启发式匹配,可能误匹配)
python -m scrape_new upload build-mapping \
  --videos ./mycourse/视频 \
  --doc <章节目录.doc> \
  --course-id <id>
```

### 3.5 上传(plan-first)

```bash
# 1) 干跑,生成 _upload_plan.json(不写后台)
python -m scrape_new upload upload \
  --mapping ./mycourse/_mapping.json \
  --cookies-file ./cookies_teacher.txt \
  --dry-run \
  --output-dir ./mycourse

# 2) 人工 review _upload_plan.json / _upload_plan.md
cat ./mycourse/_upload_plan.md

# 3) apply-plan(校验 mapping_hash + tree_fingerprint + scope + course_id 后才写)
python -m scrape_new upload apply-plan \
  --plan ./mycourse/_upload_plan.json \
  --cookies-file ./cookies_teacher.txt
```

**绝对不要**跳过第 2 步。`_upload_plan.md` 会列出每个 chapter / lesson / asset 的改动,人工确认无误再 apply。

## 4. 输出文件说明

下载/建课链路中产出的关键文件:

| 文件 | 阶段 | 用途 |
|---|---|---|
| `_chapter_tree.json` | scan | 章节结构(纯数据) |
| `_chapter_tree.md` | scan | 章节结构(人读) |
| `_resource_naming_manifest.json` | download | 资源清单 + objectid + 哈希文件名 |
| `_resource_naming_manifest.csv` | download | 表格 |
| `_review.html` | download | 浏览器可视总览(双击即可) |
| `_retry_downloads.json` | download | 失败/可疑文件清单,`--retry-downloads` 读它 |
| `_resource_audit.json/md/csv` | audit | 资源智能审计(漏扫/错分类/挂错节) |
| `_mapping.json` | build-mapping | 视频→课时映射 |
| `_upload_plan.json/md` | upload dry-run | 上传计划(必须人工 review) |
| `_upload_log.csv` | upload | 上传日志(UTF-8-BOM,Excel 可开) |
| `_upload_manifest.json` | upload | 增量 resume 元数据(每个 asset 后写一次) |
| `_upload_report.json` | upload | 最终统计报告 |
| `_wizard_runs.jsonl` | wizard | wizard 逐步执行日志(jsonl append) |

> 所有带下划线前缀的文件(`_xxx.json` / `_xxx.md` / `_xxx.csv` / `_xxx.html` / `_xxx.jsonl`)都被 `.gitignore` 默认忽略,不会误提交到 GitHub。

## 5. 安全原则

### 不提交这些到 GitHub

- 任何平台的真实 Cookie / token / sessionid / csrftoken / p_auth_token / vc3
- `cookies.txt` / `*.har` / `*.curl`
- 真实课程的视频 / PPT / docx / 文档
- 真实课程的 manifest / mapping / upload plan / download log
- 任何 `.env` / `.mcp.json` / 私有 API key

`SECURITY.md` 列了完整清单。误提交后的清理步骤详见同一文件。

### 工具自带的"防呆"设计

| 设计 | 含义 |
|---|---|
| `upload upload` 默认 plan-first | 不直接写后台,先生成 plan |
| `apply-plan` 校验 4 件套 | mapping_hash + tree_fingerprint + scope + course_id,任一不一致就拒绝执行 |
| `--only-lessons` / `--only-resources` 局部模式 | 禁止自动 `--reset-confirm`,防"只改一处却清空重建" |
| `RENAME` 默认 `pending` | 不会自动 delete + create,需要 `--confirm-rename` 显式确认 |
| `wizard --execute-step` 默认拒绝 | `destructive=True` 或 `requires_confirmation=True` 的 step 永远不自动执行,只打印命令让你复制 |

### Cookie 加载方式(推荐顺序)

1. 环境变量 `XTBZ_COOKIE`(**不落盘,推荐**)
2. `--cookies-file cookies.txt`(落盘,**别和学生 Cookie 混**)
3. `--cookies-string '<原始 cookie>'`(in-memory,不落盘)

**绝不**用 `curl ... | bash` 这种方式注入 Cookie 到 shell history。

## 6. 常见问题

### Q: scan 发现空节怎么办?

打开 `_resource_audit.md`,如果某节 issue 是 `empty_lesson` + `possible_missing_resource`,说明后台真有资源但 scan 没扫到。常见原因:

- `--max-tabs` 设太小(默认 4,够用;但有些课 tab 数 > 4)
- 限流中断(超星 `mooc1.chaoxing.com` 触发限流后 30 分钟冷却)
- 课时类型不在默认 tab 列表(老课可能用 iframe + flash)

**解决**:先 `--scan-only` 重新跑一次,或者把 `--max-tabs` 调到 6-8。

### Q: PPT-only 是错误吗?

**不是**。`ppt_only_lesson_informational` 是 informational issue,不算 high risk。说明这一节**只有 PPT 课件,没视频** — 完全合法,后台照常建 leaf。

但要确认两件事:

1. 后台确实只需要 PPT(有些老师希望有讲解视频)
2. PPT 文件确实在 manifest 里(避免漏挂)

### Q: audit 提示 `low_confidence_role` 怎么办?

`low_confidence_role` = 资源类型判不准(confidence < 0.65)。常见情况:

- 文件名很普通(只叫 `lesson1.mp4` 没扩展名)
- 标题和扩展名暗示不同类型(比如 mp4 文件但标题带"课件")
- 罕见扩展名(`.xyz`)

**解决**:人工 review `role` / `confidence` 字段,在 `_mapping.json` 里手动调正,或者改文件名让它更明确。

### Q: apply-plan 被拒绝怎么办?

`apply-plan` 校验 4 件套,任一不匹配就拒绝。最常见原因:

- `course_id` 不一致(用了别的课程的 plan)
- `mapping_hash` 不一致(改过 `_mapping.json` 但忘了重生成 plan)
- `tree_fingerprint` 不一致(后台章节树被别的人改过)

**解决**:重跑 `upload --dry-run` 重新生成 plan,然后再 apply。

### Q: CI 和本地测试怎么跑?

```bash
# 本地
python -m compileall -q scrape_new
python -m pytest scrape_new/tests -q
```

GitHub Actions 在 push / PR 到 main 时自动跑(4 matrix jobs:windows + ubuntu × 3.10 + 3.11)。CI 状态见仓库顶部 badge。

### Q: 我在 Windows 上跑,日志里出现 GBK 解码错误?

工具默认 UTF-8 输出。在 Windows cmd / PowerShell 跑前先设:

```powershell
$env:PYTHONIOENCODING = "utf-8"
$env:PYTHONUTF8 = "1"
```

或在 wizard 调用的命令里加 `-X utf8`(本仓库测试已加)。

## 7. 下一步

- 想看示例报告: [`docs/examples/resource_audit_demo.md`](examples/resource_audit_demo.md)
- 想跑端到端 demo: [`docs/examples/offline_e2e_workflow.md`](examples/offline_e2e_workflow.md)
- 想看 Cookie 怎么导出: [`docs/COOKIE_GUIDE.md`](COOKIE_GUIDE.md)
- 想看旧版扫描协议: [`docs/SESSION_PROTOCOL.md`](SESSION_PROTOCOL.md)
- 想参与开发: [`CONTRIBUTING.md`](../../CONTRIBUTING.md)
- 想报告安全问题: [`SECURITY.md`](../../SECURITY.md)

---

最后更新:与 `v0.1.0` 同步。
