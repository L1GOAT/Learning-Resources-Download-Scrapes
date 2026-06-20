# Contributing

欢迎给 `scrape_new` 提 PR / issue。以下是开发与贡献约定。

## 开发环境

- **Python**:3.10+ (CI 在 3.10 / 3.11 上跑)
- **操作系统**:Windows / macOS / Linux 均可(Windows 上注意 GBK/UTF-8 编码问题)

### 安装依赖

```bash
# scrape_new/requirements.txt 在子包下,不是仓库根
pip install -r scrape_new/requirements.txt
pip install pytest
```

可选:`pip install pycryptodome`(m3u8 AES 解密)、`pip install ruff`(lint)。

### 运行测试

```bash
# 编译检查
python -m compileall -q scrape_new

# 全量测试
python -m pytest scrape_new/tests -q
```

CI 也在跑同样两条命令,本地通过 = 远端也通过(几乎)。

## 贡献规则

### 测试 / 安全

- **新功能必须加测试**(放在 `scrape_new/tests/`,命名 `test_*.py`)。
- 测试**必须**只用本地 fixture / `monkeypatch` / `mock`:
  - ❌ 不要访问真实网络课程
  - ❌ 不要下载真实视频 / 文档
  - ❌ 不要上传到任何老师后台
  - ❌ 不要写真实 cookie / URL / HAR 进测试
- fixture 必须是 **fake / demo 数据**(参考 `scrape_new/tests/fixtures/course_audit_demo/`)。

### 上传 / 执行安全

- 上传相关功能默认 **plan-first**,不允许默认直接写后台。
- `wizard --execute-step` 默认**拒绝** dangerous / requires_confirmation 的 step。
- 任何会让后台产生不可逆变更的操作,必须:
  - 显式 `--reset-confirm <id>` / `--confirm-rename`
  - 校验 `mapping_hash` + `tree_fingerprint` + `scope` + `course_id` 四件套

### 文档

- 公开 API 改动同步更新 `scrape_new/README.md` 和 `scrape_new/docs/examples/`。
- 新增 fixture 同步在 `scrape_new/docs/examples/` 写一份示例。
- `SECURITY.md` 列了禁止提交的内容,**PR 时自检**。

### Commit 风格

建议 Conventional Commits 格式:

```
feat(scrape_new): 新功能描述
fix(scrape_new): 修复描述
docs: 文档改动
test(scrape_new): 测试改动
refactor(scrape_new): 重构
chore: 杂项
```

- `<type>(scope):` 是必需的。
- 第一行 ≤ 72 字符。
- body 用 `-` 列要点,空行隔开段落。
- 中文 / 英文都可以,保持一致。

### PR 流程

1. Fork 仓库,在新分支开发。
2. 跑 `python -m compileall -q scrape_new` + `python -m pytest scrape_new/tests -q` 全过。
3. 提 PR 到 `main`,描述改动 + 测试覆盖 + 截图(如有 UI 改动)。
4. 维护者 review 后合并。

## 项目结构

```
.github/workflows/ci.yml    CI matrix(win + ubuntu × 3.10 + 3.11)
SECURITY.md                  凭据安全 + 漏洞报告
CONTRIBUTING.md              本文件
scrape_new/
├── README.md
├── requirements.txt
├── pyproject.toml
├── cli.py
├── services/                纯函数服务层
│   ├── resource_audit.py    资源智能审计
│   ├── workflow_planner.py  WorkflowPlan + 7 个 intent
│   ├── resource_manifest.py _resource_naming_manifest
│   └── ...
├── upload/                  老师后台搭课
├── workflows/               平台工作流
├── tests/                   测试
│   ├── fixtures/
│   │   └── course_audit_demo/   离线 e2e fixture(fake 数据)
│   └── test_*.py
└── docs/examples/           示例文档
```

## 提 issue

- **Bug**:复现步骤 + 期望 / 实际 + Python / OS 版本。
- **Feature request**:场景描述 + 期望行为。
- **安全问题**:**不要** 公开 issue,请走 `SECURITY.md` 中的私有渠道。

---

谢谢贡献!🎉
