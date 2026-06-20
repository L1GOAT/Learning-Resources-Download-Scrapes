# 通用 Cookie 导出指南

> 不管什么平台，Cookie 的获取方式都一样：从浏览器的网络请求里复制。
> 所有平台的关键 Cookie 都是 httpOnly 的，`document.cookie` 拿不到，必须用 Network 面板。

## 通用步骤（适用于任何平台）

1. 用浏览器打开课程页面，**确认已登录**（能看到章节列表）
2. 按 `F12`（macOS 按 `⌥⌘I`）打开开发者工具
3. 点「**网络**」（Network）标签
4. 在筛选框输入**关键词**（见下方表格）
5. 按 `F5`（macOS 按 `Fn+F5`）刷新页面
6. 找到**页面本身的请求**（通常是第一条，或者 URL 和地址栏一样的那条）
7. 右键 → 复制 → 复制为 cURL
8. 粘贴给我

---

## 各平台详细信息

### 超星学习通

| 项目 | 值 |
|------|---|
| 域名 | `mooc1.chaoxing.com`、`mooc2-ans.chaoxing.com` |
| 课程页 URL | `https://mooc2-ans.chaoxing.com/mooc2-ans/mycourse/teacherstudy?chapterId=...&courseId=...&clazzid=...` |
| 筛选关键词 | `teacherstudy` |
| 关键 Cookie | `vc3`（httpOnly）、`p_auth_token`（httpOnly）、`uf`、`cx_p_token`、`_uid` |
| 视频格式 | mp4 直链 |
| 下载脚本 | `python3 scrape/workflows/chaoxing.py "课程URL"` |
| 备注 | 章节树从 edit/chapters 页面提取；视频 API 需要 Referer 头 |

### 智慧树 / 知到

| 项目 | 值 |
|------|---|
| 域名 | `zhihuishu.com`、`studyh5.zhihuishu.com`、`passport.zhihuishu.com` |
| 课程页 URL | `https://studyh5.zhihuishu.com/video/h5/...` 或 `https://www.zhihuishu.com/playingCourse...` |
| 筛选关键词 | `zhihuishu` |
| 关键 Cookie | `token`（httpOnly）、`JSESSIONID`（httpOnly）、`refreshToken`、`userid` |
| 视频格式 | m3u8（可能有 AES 加密） |
| 下载脚本 | `python3 scrape/workflows/zhihuishu.py "课程URL"` |

### 学堂在线

| 项目 | 值 |
|------|---|
| 域名 | `www.xuetangx.com`、`learning.xuetangx.com`、`apps.xuetangx.com` |
| 课程页 URL | `https://www.xuetangx.com/course/{course_id}/{term_id}` |
| 筛选关键词 | `course` |
| 关键 Cookie | `sessionid`（httpOnly） |
| 视频格式 | m3u8 |
| 下载脚本 | `python3 scrape/workflows/xuetangx.py "课程URL"` |

### 中国大学MOOC

| 项目 | 值 |
|------|---|
| 域名 | `www.icourse163.org`、`mooc-api.icourse163.org` |
| 课程页 URL | `https://www.icourse163.org/learn/大学-课程名-XXXXX?tid=...` |
| 筛选关键词 | `mooc-api` |
| 关键 Cookie | `STUDY_SESS`（httpOnly）、`STUDY_INFO`、`S_INFO`、`P_INFO`、`NTES_SESS` |
| 视频格式 | m3u8（可能有 AES 加密） |
| 下载脚本 | `python3 scrape/workflows/icourse163.py "课程URL"` |
| 备注 | API 是 POST 请求，返回 JSON；视频 CDN 在 `v.stu.163.com` 或 `vcloud.163.com` |

---

## 怎么找关键词？

如果上表没有你要扒的平台，自己找关键词：

1. 看浏览器地址栏的网址，比如：`https://www.example.com/course/123`
2. 取域名中间部分：`example`
3. 在筛选框输入这个关键词

## 判断复制对了没有

你复制出来的东西应该长这样：

```
curl 'https://...' -H '...' -b '...' ...
```

里面有 `-b` 或者 `-H 'Cookie:...'` 就是对的。

如果复制出来只有几行、没有 `-b`，说明复制的不是带 Cookie 的那条请求，换一条试试。

## 常见问题

**Q：筛选后有很多条，点哪条？**
A：点第一条就行。如果第一条的 URL 和你地址栏的网址差不多，那就是对的。

**Q：复制出来的内容很长很长，正常吗？**
A：正常。越长越好，说明信息完整。

**Q：浏览器崩了怎么办？**
A：换一个浏览器（Chrome 崩了用 Edge，Edge 崩了用 Safari）重新来。

**Q：Cookie 过期了怎么办？**
A：重新做一遍上面的步骤就行，大约 7 天过期一次。

**Q：`document.cookie` 能用吗？**
A：不能。所有平台的关键 Cookie 都是 httpOnly 的，`document.cookie` 拿不到。必须用 Network 面板。
