# Security Policy

## 请勿提交的内容

本仓库采用白名单式 `.gitignore`,默认**只放行** `scrape_new/` 和 `tests/`。
**绝对不要**提交以下内容(它们通常包含真实凭据或受版权保护的材料):

- 真实 **Cookie**(任何平台的 `sessionid` / `csrftoken` / `p_auth_token` / `vc3` 等)
- 真实 **HAR** / **curl** 抓包文件
- `cookies.txt` / `cookies_new.txt` / `*.cookie*.txt`
- 真实课程 **视频** / **PPT** / **docx** / **PDF** / 图片
- 真实课程的 **manifest** / **mapping** / **upload plan** / **download log**
- 任何 `.env` / `.mcp.json` / 私有 API key / token

请用以下命令自检(只是建议,不在 CI 跑):

```bash
git grep -n "sessionid=\|csrftoken=\|p_auth_token=\|vc3=\|COOKIE_STRING"
```

仓库内的 fixture(`scrape_new/tests/fixtures/course_audit_demo/`) 是**占位数据**,
课程名 / 文件名 / objectid 全部是假的,可以直接提交。

## 误提交了 secret 怎么办

1. **立即让凭据失效**:退出该平台登录,重新导出 Cookie(让旧 token 立刻不可用)。
2. **从 git 历史清理**:用 `git filter-repo` / BFG 把含 secret 的 commit 重写。
   ```bash
   # 备份后,使用 git filter-repo 重写历史
   pip install git-filter-repo
   git filter-repo --path-glob 'cookies.txt' --invert-paths
   git push origin --force
   ```
3. **重新生成 / 导出** 新的 Cookie 或 token。
4. **通知维护者**:在私有 issue 描述情况(详见下方"报告漏洞")。

即使删除文件,旧的 commit 仍会保留在历史里 — **必须** 用上面方式重写历史。

## 本工具的使用边界

本工具仅用于下载用户**有权访问**的资源:

- 不绕过验证码 / 登录墙 / 付费墙
- 不破解 DRM / 加密授权
- 不包含自动刷课 / 自动答题

请确保只下载您有权访问的内容,遵守平台使用条款和版权法。

## 报告漏洞 / 安全问题

- **请不要在公开 issue 粘贴 secret / cookie / 真实 token**。
- 推荐用 GitHub 的 [私有漏洞报告](https://github.com/L1GOAT/Learning-Resources-Download-Scrapes/security/advisories/new)
  (如果仓库启用了 Security Advisories)。
- 若 Security Advisories 未启用,可通过仓库主页面找到维护者联系方式私下沟通。

回复窗口:非商业项目,见 PR / issue 中的维护者说明。

## 凭据安全建议

- 推荐用 `--cookies-file` 或环境变量传递 Cookie,**避免**写到 shell history。
- 推荐用 `git secrets` / `gitleaks` 等工具在本地 commit 前扫一遍。
- 仓库根 `.gitignore` 已挡掉 `*.env` / `cookies*.txt` / `*.har`,但请勿因为这个就放松警惕。

---

最后更新:与 `v0.3.0` 同步。
