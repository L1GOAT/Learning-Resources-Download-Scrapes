# 资源审计示例报告 — Demo Course

> 这是一份**示例**报告，由 `scrape_new/tests/fixtures/course_audit_demo/` 的 fixture
> 经 `scrape_new.services.resource_audit.write_resource_audit_reports()` 生成。
>
> 用途：让 GitHub 用户**一眼看清**工具能抓出哪些常见错配，无需自己造课。
>
> Fixture 设计：
> - 1.1 Welcome — 正常 mp4 + pptx
> - 1.2 Slides Only — PPT-only 合法 lesson
> - 2.1 Missing Resource — 空 lesson + video 字段引用不存在的文件
> - 2.2 Duplicate Demo — video 字段放 .pptx、attachments 放 .mp4、文件被两个 lesson 复用

## 总览

| 指标 | 值 |
|---|---|
| total_lessons | 4 |
| lessons_with_resources | 3 |
| empty_lessons | 1 |
| empty_chapters | 0 |
| resources_audited | 5 |

## 风险分布

- **MEDIUM**: 2 节（ch2.1、ch2.2）
- **LOW**: 1 节（ch2.1 空 lesson 本身）
- **OK**: 2 节（ch1.1、ch1.2）

## ⚡ 中风险节

- ch2.1 Missing Resource — empty_lesson, missing_local_file, attachment_as_video
- ch2.2 Duplicate Demo — missing_local_file, non_video_in_video_slot, attachment_as_video

## 全局问题

- saved_name `2.2_Duplicate.mp4` 重复出现 2 次
- ch2.1 attachment 字段放了视频文件 `2.2_Duplicate.mp4`
- ch2.2 attachment 字段放了视频文件 `2.2_Duplicate.mp4`
- 文件 `2.2_Duplicate.mp4` 被 2 个 lesson 引用：`['2.2.1', '2.2.2']`

## 建议

- 2 个 lesson 引用了不存在的本地文件，需重新下载
- 检测到附件字段放了视频文件，建议改成 video 字段

## 问题资源明细

### ch2.1 Missing Resource

- lesson issues: empty_lesson, missing_local_file, attachment_as_video
- `2.1_Missing_Never_Downloaded.mp4` role=`unknown` conf=0.5 status=`missing`
  - issues: missing_local_file
  - 建议: 建议重新下载该文件 / 检查文件名
  - 证据: filename=2.1_Missing_Never_Downloaded.mp4
- `2.2_Duplicate.mp4` role=`unknown` conf=0.5 status=`suspicious`
  - issues: non_video_in_video_slot / attachment_as_video
  - 建议: 扩展名不是视频，可能挂错字段，建议改成 attachment

### ch2.2 Duplicate Demo

- lesson issues: missing_local_file, non_video_in_video_slot, attachment_as_video
- `2.2_Slides_Put_In_Video_Slot.pptx` role=`unknown` conf=0.5 status=`suspicious`
  - issues: non_video_in_video_slot
  - 建议: 扩展名不是视频，可能挂错字段，建议改成 attachment

## 中文短语提示对照

工具实际输出的 Markdown 报告里，会用以下中文短语分类提示：

| 短语 | 含义 |
|---|---|
| 可能漏扫 | scan 阶段没扫到的资源，建议重扫 |
| 需要人工确认 | role 置信度低，工具拿不准资源类型 |
| 可以安全跳过 | 本节课没这个资源，可忽略 |
| 建议补资源 | scan 已扫到但下载失败，需要补 |
| 建议只重扫该节 | 单节可疑，只重扫这节 |

## 怎么自己跑

```bash
python -m scrape_new audit \
  --chapter-tree scrape_new/tests/fixtures/course_audit_demo/_chapter_tree.json \
  --manifest scrape_new/tests/fixtures/course_audit_demo/_resource_naming_manifest.json \
  --mapping scrape_new/tests/fixtures/course_audit_demo/_mapping.json \
  --output-dir ./demo_output
```

会在 `./demo_output/` 写出：

- `_resource_audit.json` — 完整结构化数据（GUI / CI 消费）
- `_resource_audit.md` — 人类可读报告（这份文件的**生成版**）
- `_resource_audit.csv` — 表格（Excel 可开）

> **注意**：本示例完全使用本地 fixture，**不会访问真实网络、不会下载真实资源、
> 不会上传到任何后台**。所有课程名、文件路径都是占位符。