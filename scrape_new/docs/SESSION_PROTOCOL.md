# 扒取会话协议

> **给 Claude 的指令**：每次用户说"我要扒 XXX"时，严格按照以下流程执行。不要跳步。

---

## 流程（6 步）

### 第 1 步：确认需求

问用户（如果信息不全的话）：
- 目标 URL 是什么？
- 要扒什么？（视频/图片/文档/表格/文章/全部）
- 需要登录吗？有没有 Cookie？

### 第 2 步：侦察

1. 用 WebFetch 或 curl 访问目标 URL，分析页面结构
2. 识别资源类型和 API 链路
3. 告诉用户发现了什么，确认要扒的内容

### 第 3 步：准备

1. 检查 `scrape/` 工具箱是否存在
2. 如果需要 Cookie，按以下流程指导用户导出（不要模糊描述，给精确步骤）
3. 准备好配置

#### Cookie 导出标准流程（超星学习通）

> **重要**：`document.cookie` 拿不到 httpOnly 关键字段（如 `vc3`、`p_auth_token`），必须用 Network 面板。

**给用户的指引（直接复制这段）：**

```
请按以下步骤导出登录凭证（必须从已登录的课程页面操作）：

1. 浏览器打开课程页面，确认能看到章节列表（不要在登录页操作）
2. 按 F12 打开开发者工具
3. 点顶部「网络」（Network）标签
4. 在筛选框输入 teacherstudy（就是课程页面网址里的关键词）
5. 按 F5 刷新页面
6. 列表里应该只剩一条，右键它 → 复制 → 复制为 cURL
7. 粘贴发给我

⚠️ 注意：
- 必须先登录再操作
- 搜 teacherstudy 是因为这条就是页面本身，一定带完整登录信息
```

**给林林（macOS 计算机小白用户）的指引：**
- 不要用上面的技术版，用 `MACOS_DEPLOY.md` 里的"写给林林的版本"
- 要求：每步只做一个动作、不用英文技术词、告诉她按钮长什么样在哪个位置
- 参考：`[[user-linlin]]`

**验证 Cookie 有效性**：拿到后立即用 Python 请求课程页面，检查是否 302 跳转到登录页。如果跳转 → Cookie 无效，让用户重新导出。

#### 超星学习通 - 章节列表提取方法

> 以下方法按成功率排序。优先用第一个，失败再依次尝试。

**方法 1（✅ 成功）：从 edit/chapters 页面提取 ExtJS 树**

```python
# 步骤：访问课程编辑页面 → 提取嵌入的 ExtJS JSON 树
url = f'https://mooc1.chaoxing.com/edit/chapters/{COURSE_ID}/{COURSE_ID}?classId={CLAZZ_ID}'
resp = SESSION.get(url, timeout=30)
html = resp.text

# 定位 JSON 数组起始位置
start = html.find('[{"expanded":true')
# 用括号匹配找到完整数组
depth = 0
for i in range(start, len(html)):
    if html[i] == '[': depth += 1
    elif html[i] == ']':
        depth -= 1
        if depth == 0:
            end = i + 1
            break
tree_data = json.loads(html[start:end])

# 递归展平树结构
def flatten(nodes, depth=0, parent=''):
    results = []
    for node in nodes:
        children = node.get('children', [])
        results.append({'id': node['id'], 'name': node['text'], 'is_leaf': not children, 'parent': parent})
        if children:
            results.extend(flatten(children, depth+1, node['text']))
    return results

lessons = [ch for ch in flatten(tree_data) if ch['is_leaf']]
```

- 前提：Cookie 有效，courseId 和 clazzid 已知
- 输出：所有章节 ID（knowledgeid）+ 章节名称 + 父子关系
- 优点：一次请求拿到完整树，速度快
- 注意：章节名可能有编码问题（GBK/UTF-8 混淆），以 `text` 字段为准

**方法 2（❌ 失败）：gas/clazz/{clazzid}/tree API**

```python
# 多种变体均返回 404
url = f'https://mooc1.chaoxing.com/gas/clazz/{CLAZZ_ID}/tree?courseId={COURSE_ID}&cpi={CPI}&ut=t'
url = f'https://mooc2-ans.chaoxing.com/mooc2-ans/gas/clazz/{CLAZZ_ID}/tree?courseId={COURSE_ID}&cpi={CPI}&ut=t'
```

- 结果：全部 404
- 原因：该 API 可能已下线或需要额外权限

**方法 3（❌ 失败）：knowledge/list API**

```python
url = f'https://mooc1.chaoxing.com/knowledge/list?courseid={COURSE_ID}&clazzid={CLAZZ_ID}&cpi={CPI}&ut=t'
url = f'https://mooc2-ans.chaoxing.com/mooc2-ans/knowledge/list?courseId={COURSE_ID}&clazzid={CLAZZ_ID}&cpi={CPI}'
```

- 结果：404

**方法 4（❌ 失败）：mycourse/studentcourse 页面**

```python
url = f'https://mooc1.chaoxing.com/mycourse/studentcourse?courseId={COURSE_ID}&clazzid={CLAZZ_ID}&cpi={CPI}'
```

- 结果：200 但页面不含章节数据（章节通过 JS 动态加载）

**方法 5（❌ 失败）：mycourse/transfer 页面**

```python
url = f'https://mooc1.chaoxing.com/mycourse/transfer?moocId={COURSE_ID}&clazzid={CLAZZ_ID}&ut=t'
```

- 结果：500 内部错误

**方法 6（❌ 失败）：tchcourse 页面提取**

```python
url = f'https://mooc2-ans.chaoxing.com/mooc2-ans/mycourse/tchcourse?courseid={COURSE_ID}&clazzid={CLAZZ_ID}&cpi={CPI}'
```

- 结果：200，页面加载成功，但章节列表通过 postMessage 机制动态加载，HTML 中不包含章节数据

**方法 7（⚠️ 备用）：knowledge/cards 逐个探测**

```python
# 如果已知第一个章节 ID，可以从 knowledge/cards 页面提取该章节的资源
# 然后通过页面中的导航链接找下一章
url = f'https://mooc1.chaoxing.com/knowledge/cards?clazzid={CLAZZ_ID}&courseid={COURSE_ID}&knowledgeid={KID}&num=0&v=20160407&ut=t&cpi={CPI}&mooc2=1'
```

- 结果：可用，但只能获取单个章节的资源，不能获取完整列表
- 用途：获取每个章节的视频 objectid 和作业 jobid

#### 超星学习通 - 视频下载流程

**推荐：直接运行专用脚本**

```bash
python3 scrape/workflows/chaoxing.py "课程URL" [输出目录]
```

脚本自动完成全部流程，不用手动拼代码。

**手动流程（仅供参考）：**

```
1. 用方法 1 获取章节树 → 得到所有 leaf 节点的 knowledgeid
2. 对每个 knowledgeid 调用 knowledge/cards 页面 → 提取 objectid
3. 调用 ananas/status/{objectid} API → 获取 download URL
4. 下载视频文件
```

**关键 API：**
```
# 获取视频信息（需要 Referer 头，否则 403）
GET https://mooc1.chaoxing.com/ananas/status/{objectid}?k=262&flag=normal&ro=0&_dc={timestamp}
Referer: https://mooc1.chaoxing.com/ananas/modules/video/index.html?v=2026-0527-1025

# 返回 JSON：
{
  "status": "success",
  "download": "http://d0.cldisk.com/download/{objectid}?at_=...&ak_=...&ad_=...",
  "http": "https://s2.cldisk.com/sv-w9/video/.../sd.mp4?at_=...&ak_=...&ad_=...",
  "filename": "视频名.mp4",
  "length": 283074853,  # 文件大小（字节）
  "duration": 396        # 时长（秒）
}
```

**注意：**
- `ananas/status` API 必须带 `Referer: https://mooc1.chaoxing.com/ananas/modules/video/index.html` 否则返回 403
- `download` 字段是 HTTP 直链，`http` 字段是 HTTPS 流媒体链接（SD 画质）
- 优先用 `download` 字段下载

#### 超星学习通 - URL 参数提取

用户给的 URL 通常长这样：
```
https://mooc2-ans.chaoxing.com/mooc2-ans/mycourse/teacherstudy?chapterId=1118457447&courseId=260739359&clazzid=139845161
```

需要提取的参数：
| 参数 | 说明 | 来源 |
|------|------|------|
| `courseId` | 课程 ID | URL 参数 |
| `clazzid` | 班级 ID | URL 参数 |
| `cpi` | 课程计划 ID | 不在 URL 中，需从 tchcourse 页面或 curl 命令中提取 |

**获取 cpi 的方法：**
1. 从用户粘贴的 curl 命令中找（如 `&cpi=50453479`）
2. 从 tchcourse 页面 URL 中找（`/mycourse/tchcourse?...&cpi=xxx`）
3. 从 knowledge/cards URL 中找（`&cpi=xxx`）
4. 如果都没有，尝试从 tchcourse 页面的 JS 变量中提取

#### 超星学习通 - 资源扫描（knowledge/cards）

每个章节的资源在 knowledge/cards 页面的 `data=` 属性中：

```python
# 从 knowledge/cards 页面提取资源
for m in re.finditer(r'data="([^"]+)"', html):
    decoded = m.group(1).replace('&quot;', '"').replace('&amp;', '&')
    d = json.loads(decoded)
    # d 包含：type, name, objectid, _jobid, mid, doublespeed
```

**资源类型判断：**
- `type` 包含 `.mp4` → 视频，用 `objectid` 调用 ananas/status API 下载
- `jobid` 以 `work-` 开头 → 在线作业（quiz），**无文件附件，不可下载**
- 其他类型 → 需要进一步判断

**去重：** 同一 objectid 可能在多个位置出现，下载前按 objectid 去重。

#### 超星学习通 - 下载后处理

**文件命名规范：**
```
{章节号:02d}_{章节名}.mp4
示例：01_计算机基础知识.mp4、09_计算机组装（1）.mp4
```
- 章节号从父章节名称提取（如"第1章 xxx" → 01）
- 文件名中的 `\/:*?"<>|` 替换为 `_`

**质量检查：**
- 视频 < 100KB = 可疑（可能是错误页面或空文件）
- 下载后文件大小与 API 返回的 `length` 字段对比，差异 > 5% = 不完整
- 0 字节文件自动删除

**下载后清理：**
1. 临时扫描脚本（scan_*.py、find_*.py、extract_*.py）→ 删除
2. 下载脚本（download_all.py）→ 移到 `scripts/archive/` 并以课程名命名
3. Cookie 文件保留（`cookies.txt`），下次可复用
4. 临时 JSON/HTML 缓存文件 → 删除

#### 超星学习通 - 常见问题

| 问题 | 原因 | 解决 |
|------|------|------|
| 302 跳转到登录页 | Cookie 过期或缺少 `vc3` | 重新从 Network 面板导出 Cookie |
| ananas/status 返回 403 | 缺少 Referer 头 | 加 `Referer: https://mooc1.chaoxing.com/ananas/modules/video/index.html` |
| 章节名显示乱码 | 编码问题（GBK/UTF-8 混淆） | 不影响下载，以 API 返回的 `filename` 为准 |
| 用户给的 Cookie 不对 | 用了 `document.cookie` 或复制了第三方请求 | 按标准流程重新导出 |
| Chrome 崩溃 | 浏览器不稳定 | 建议用户改用 Edge（快捷键相同，DevTools 一样） |
| 用户粘贴 curl 命令 | 不知道怎么单独复制 Cookie | 从 curl 的 `-b` 或 `-H 'Cookie:...'` 参数中提取 |

### 第 4 步：执行

1. 调用对应模块执行扒取
2. 实时告诉用户进度
3. 失败时自动重试，仍然失败则告诉用户

### 第 5 步：验收

1. 检查下载的文件是否完整
2. 告诉用户结果：成功/失败/跳过数量
3. 告诉用户文件保存位置
4. 文件已自动归档重命名（如 `01_影响健康的因素.mp4`）
5. 下载历史已自动记录（去重用）

### 第 6 步：更新 Workflow（必须执行！）

**每次扒取完成后，必须更新以下文件：**

#### 6a. 更新 `超星学习通视频批量下载工作流.md`（如果是超星课程）

在"执行记录"表中追加一行：
```
| 项目 | 新课程名 | ... |
| 时间 | YYYY-MM-DD | ... |
| courseId | xxx | ... |
```

#### 6b. 更新 `视频批量下载通用工作流.md`

在"已知问题与修复记录"表中追加本次发现的问题（如果有）：
```
| YYYY-MM-DD | 问题描述 | 修复方式 |
```

#### 6c. 更新 `scrape/SESSION_PROTOCOL.md`（本文件）

在下方"会话记录"中追加本次操作记录。

---

## 会话记录

> 每次操作后在下方追加记录，格式如下：

```
### YYYY-MM-DD - 目标网站名
- 意图：视频/图片/文档/表格/文章/全部
- URL：https://...
- 模块：video / image / document / table / article / links / api / all
- 结果：成功 X 个，失败 Y 个，跳过 Z 个
- 输出：./output/xxx
- 问题：（如有）
- 学到：（本次操作的新发现，下次可以改进的地方）
```

---

<!-- 以下为历史记录，每次操作后追加 -->

### 2026-06-09 - 超星学习通（计算机组装与维护）
- 意图：视频 + 文档
- URL：https://mooc2-ans.chaoxing.com/mooc2-ans/mycourse/teacherstudy?chapterId=1118457447&courseId=260739359&clazzid=139845161
- 模块：video（批量）
- 结果：视频 23/23 成功，文档 11/11（均为在线作业无附件）
- 输出：E:\林视\计算机组装与维护\视频（7.44 GB）
- 问题：无
- 学到：
  1. 章节树可从 mooc1.chaoxing.com/edit/chapters/{courseId} 页面的 ExtJS JSON 提取
  2. ananas/status API 需要 Referer 头（mooc1.chaoxing.com/ananas/modules/video/index.html）否则 403
  3. "work-" 前缀的 jobid 是在线作业（quiz），无文件附件可下载

