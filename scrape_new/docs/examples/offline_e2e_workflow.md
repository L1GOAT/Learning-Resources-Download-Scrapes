# 离线端到端工作流 — 用 fixture 理解工具链

> 本文档是给 GitHub 用户的"自检指南"：用一份**离线 fixture**把工具链的关键环节
> 串起来跑一遍，验证工具在本地能完整产出报告 + 安全执行。

## 涉及能力

1. `audit` 子命令 — 资源智能审计
2. `wizard` / `assistant` — 工作流向导
3. `--execute-step` — 只执行非危险 step（带 `_wizard_runs.jsonl` 日志）

## Fixture 内容

`scrape_new/tests/fixtures/course_audit_demo/` 下有 3 个 JSON：

| 文件 | 模拟的产物 | 说明 |
|---|---|---|
| `_chapter_tree.json` | scan 产出的章节树 | 2 章 4 节 |
| `_resource_naming_manifest.json` | download 产出的 manifest | 5 条资源，含故意制造的 duplicate |
| `_mapping.json` | build-mapping 产出的结构 | 故意制造 missing / ppt-only / video-slot 错配 |

所有数据是**占位**，没有任何真实课程名 / URL / cookie / 凭据。

## 端到端步骤

### 1. 跑 audit（纯本地，无网络）

```bash
python -m scrape_new audit \
  --chapter-tree scrape_new/tests/fixtures/course_audit_demo/_chapter_tree.json \
  --manifest scrape_new/tests/fixtures/course_audit_demo/_resource_naming_manifest.json \
  --mapping scrape_new/tests/fixtures/course_audit_demo/_mapping.json \
  --output-dir ./demo_output
```

会写三份文件到 `./demo_output/`：

- `_resource_audit.json` — 完整结构化数据
- `_resource_audit.md` — 人类可读报告
- `_resource_audit.csv` — 表格（每行一个资源）

不会触发任何真实下载 / 上传 / 网络请求。

### 2. 看 wizard plan（不执行任何命令）

```bash
python -m scrape_new wizard --intent audit --markdown
```

输出 WorkflowPlan，两个 step：`audit_scan` 和 `audit_mapping`，
都是非危险 step（risk_level=safe）。

### 3. 让 wizard 只执行 audit_scan 这一步

```bash
python -m scrape_new wizard \
  --intent audit \
  --output-dir ./demo_output \
  --execute-step audit_scan \
  --run-log ./demo_output/_wizard_runs.jsonl
```

执行规则：

- 找不到 step → 返非 0 + 列出可用 step id
- step 是 dangerous / requires_confirmation → 拒绝执行，提示复制命令
- step 是非危险 → `subprocess.run(shell=False)` 跑，写日志
- 日志追加到 `./demo_output/_wizard_runs.jsonl`

### 4. 看执行日志

```bash
cat ./demo_output/_wizard_runs.jsonl
```

每行一条 JSON：

```json
{
  "generated_at": "2026-06-20T15:00:00",
  "intent": "audit",
  "step_id": "audit_scan",
  "title": "...",
  "command": "python -m scrape_new audit --chapter-tree ...",
  "returncode": 0,
  "elapsed_seconds": 1.234,
  "status": "succeeded",
  "stdout_tail": "...",
  "stderr_tail": "..."
}
```

## 验收清单

跑完上面 4 步后，检查：

- [x] `./demo_output/_resource_audit.{json,md,csv}` 都存在
- [x] Markdown 包含 `可能漏扫` / `需要人工确认` / `建议补资源` / `可以安全跳过` 之一
- [x] Markdown 包含 `ppt_only_lesson_informational` / `non_video_in_video_slot` 等 issue
- [x] `./demo_output/_wizard_runs.jsonl` 至少有一行
- [x] `_wizard_runs.jsonl` 第一行 `status` = `succeeded`

## 不做的事

本端到端流程**不会**：

- ❌ 访问任何真实课程平台
- ❌ 下载任何真实视频 / 文档 / 课件
- ❌ 上传到任何老师后台
- ❌ 触发真实 cookie / HAR / 凭据加载
- ❌ 跑 `--execute-step apply_plan` 这种危险 step（wizard 会拒绝）

## 不在仓库里的产物

跑命令生成的 `./demo_output/` 是临时目录，`.gitignore` 已挡：

- `_resource_audit.json` / `_resource_audit.md` / `_resource_audit.csv`
- `_wizard_runs.jsonl`
- `demo_output/`

只提交 fixture JSON 和这份示例文档。