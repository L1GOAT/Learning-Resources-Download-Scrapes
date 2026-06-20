"""
HTML 验课页 — 把 _resource_naming_manifest.json / _chapter_tree.json 渲染成
单文件 HTML(内联 CSS+JS,不开 CDN),方便人工浏览验课。

特性:
  - 左:章节树(可折叠)
  - 右:当前章节的资源列表
  - 状态颜色:成功绿 / 跳过灰 / 失败红 / 可疑黄
  - 顶部:搜索框 + 一键筛选(失败/可疑/缺英文/缺PPT)
  - 单文件,双击即可在浏览器打开

用法:
  from scrape_new.services.review_html import build_review_html
  build_review_html(tree, records, output_dir=Path("./课程目录"))
  # → ./课程目录/_review.html
"""

from __future__ import annotations

import html
import json
import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


# 状态 → 颜色
_STATUS_COLORS = {
    "downloaded": "#10b981",        # 绿
    "skipped_existing": "#9ca3af",  # 灰
    "failed": "#ef4444",            # 红
    "suspicious": "#f59e0b",        # 黄
}
_STATUS_ICONS = {
    "downloaded": "✓",
    "skipped_existing": "↻",
    "failed": "✗",
    "suspicious": "?",
}


def build_review_html(
    tree: dict[str, Any],
    records: list[dict[str, Any]],
    output_dir: Path,
    *,
    title: str | None = None,
) -> Path:
    """写 _review.html(单文件 HTML,内联 CSS+JS)。

    Args:
        tree: 来自 build_chapter_tree_data(...) 的 dict
        records: 来自 build_resource_naming_records(...) 的列表
        output_dir: 输出目录
        title: 页面标题(默认用 tree.course_title)
    """
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / "_review.html"
    html_content = _render_html(
        tree=tree,
        records=records,
        page_title=title or tree.get("course_title") or "课程验课",
    )
    path.write_text(html_content, encoding="utf-8")
    logger.info(f"已写验课页: {path}")
    return path


def _render_html(
    *,
    tree: dict[str, Any],
    records: list[dict[str, Any]],
    page_title: str,
) -> str:
    """渲染完整 HTML 字符串(单文件 + 内联 CSS/JS,无外部依赖)。"""
    # 把 records 按 (chapter_index, lesson_id) 分桶,O(1) 查询
    by_lesson: dict[tuple[int, str], list[dict[str, Any]]] = {}
    for r in records:
        key = (r.get("chapter_index", 0), r.get("lesson_id", ""))
        by_lesson.setdefault(key, []).append(r)

    # 统计数据
    stats: dict[str, int] = {}
    for r in records:
        s = r.get("status", "unknown")
        stats[s] = stats.get(s, 0) + 1

    course = tree.get("course_title") or "课程"
    chapters = tree.get("chapters", [])
    platform = tree.get("platform", "")
    source_url = tree.get("source_url", "")
    generated_at = tree.get("generated_at", "")

    # 序列化数据(供前端 JS 用)
    data_json = json.dumps({
        "chapters": chapters,
        "records": records,
        "by_lesson": {
            f"{k[0]}|{k[1]}": v for k, v in by_lesson.items()
        },
        "stats": stats,
        "course_title": course,
    }, ensure_ascii=False)

    # ── HTML
    parts: list[str] = []
    parts.append("<!DOCTYPE html>")
    parts.append('<html lang="zh-CN">')
    parts.append("<head>")
    parts.append('<meta charset="utf-8">')
    parts.append(f"<title>{html.escape(page_title)} — 验课</title>")
    parts.append(_render_css())
    parts.append("</head>")
    parts.append("<body>")
    parts.append(_render_header(course, platform, source_url, generated_at, stats))
    parts.append(_render_toolbar(stats))
    parts.append('<div class="main">')
    parts.append(_render_sidebar(chapters))
    parts.append(_render_content_placeholder())
    parts.append("</div>")
    parts.append(_render_footer(data_json))
    parts.append("</body>")
    parts.append("</html>")
    return "\n".join(parts)


def _render_css() -> str:
    return """<style>
* { box-sizing: border-box; }
body { margin: 0; font-family: -apple-system, "Segoe UI", sans-serif;
       background: #f8fafc; color: #1e293b; font-size: 14px; }
.header { background: #fff; border-bottom: 1px solid #e2e8f0;
         padding: 16px 24px; position: sticky; top: 0; z-index: 10; }
.header h1 { margin: 0 0 4px; font-size: 18px; }
.header .meta { color: #64748b; font-size: 12px; }
.stats { display: inline-flex; gap: 8px; margin-top: 8px; }
.stat { padding: 2px 10px; border-radius: 12px; font-size: 12px;
        background: #f1f5f9; color: #475569; }
.stat.failed { background: #fee2e2; color: #b91c1c; }
.stat.suspicious { background: #fef3c7; color: #92400e; }
.stat.downloaded { background: #d1fae5; color: #065f46; }
.stat.skipped_existing { background: #e2e8f0; color: #475569; }
.toolbar { background: #fff; border-bottom: 1px solid #e2e8f0;
          padding: 8px 24px; display: flex; gap: 12px; align-items: center; }
.toolbar input { flex: 1; padding: 6px 12px; border: 1px solid #cbd5e1;
               border-radius: 4px; font-size: 14px; }
.toolbar button { padding: 6px 12px; border: 1px solid #cbd5e1;
                 background: #fff; border-radius: 4px; cursor: pointer;
                 font-size: 13px; }
.toolbar button.active { background: #3b82f6; color: #fff; border-color: #3b82f6; }
.main { display: flex; height: calc(100vh - 130px); }
.sidebar { width: 280px; background: #fff; border-right: 1px solid #e2e8f0;
          overflow-y: auto; padding: 8px 0; }
.sidebar .chapter { padding: 6px 12px; cursor: pointer; user-select: none; }
.sidebar .chapter:hover { background: #f1f5f9; }
.sidebar .chapter.selected { background: #dbeafe; font-weight: 600; }
.sidebar .chapter-title { display: flex; justify-content: space-between; }
.sidebar .lessons { padding-left: 16px; }
.sidebar .lesson { padding: 4px 12px; cursor: pointer; color: #475569; }
.sidebar .lesson:hover { background: #f1f5f9; }
.sidebar .lesson.selected { background: #dbeafe; color: #1e40af; }
.content { flex: 1; overflow-y: auto; padding: 24px; }
.content .lesson-detail h2 { margin: 0 0 4px; font-size: 16px; }
.content .lesson-detail .ls-id { color: #64748b; margin-bottom: 12px; }
.resource { padding: 10px 12px; margin: 6px 0; border-radius: 4px;
           background: #fff; border-left: 4px solid #cbd5e1;
           display: flex; align-items: center; gap: 12px; }
.resource .icon { font-weight: bold; width: 16px; text-align: center; }
.resource .name { flex: 1; font-family: ui-monospace, monospace; }
.resource .meta { color: #64748b; font-size: 12px; }
.resource .role-tag { padding: 1px 6px; background: #e2e8f0;
                   border-radius: 3px; font-size: 11px; color: #475569; }
.resource .reason { color: #b91c1c; font-size: 12px; margin-top: 4px; }
.resource[data-status="downloaded"] { border-left-color: #10b981; }
.resource[data-status="skipped_existing"] { border-left-color: #9ca3af; }
.resource[data-status="failed"] { border-left-color: #ef4444; background: #fef2f2; }
.resource[data-status="suspicious"] { border-left-color: #f59e0b; background: #fffbeb; }
.empty { color: #94a3b8; text-align: center; padding: 60px; }
.footer { display: none; }
</style>"""


def _render_header(course: str, platform: str, source_url: str,
                   generated_at: str, stats: dict[str, int]) -> str:
    stat_html = ""
    for s in ("downloaded", "skipped_existing", "failed", "suspicious"):
        if stats.get(s):
            stat_html += f'<span class="stat {s}">{_STATUS_ICONS.get(s,"")} {_label(s)} {stats[s]}</span>'
    return f"""<div class="header">
<h1>{html.escape(course)} — 验课</h1>
<div class="meta">
  平台: {html.escape(platform or "?")} | 生成: {html.escape(generated_at or "?")}
  {(' | <a href="' + html.escape(source_url) + '" target="_blank">来源</a>') if source_url else ''}
</div>
<div class="stats">{stat_html}</div>
</div>"""


def _render_toolbar(stats: dict[str, int]) -> str:
    return """<div class="toolbar">
<input type="search" id="search" placeholder="搜索章节/课时/文件名..." />
<button data-filter="all" class="active">全部</button>
<button data-filter="failed">只看失败</button>
<button data-filter="suspicious">只看可疑</button>
<button data-filter="missing_english">缺英文</button>
<button data-filter="missing_ppt">缺PPT</button>
</div>"""


def _render_sidebar(chapters: list[dict]) -> str:
    parts = ['<div class="sidebar" id="sidebar">']
    for ch in chapters:
        ch_idx = ch.get("index", 0)
        ch_title = ch.get("title", "")
        parts.append(
            f'<div class="chapter" data-chapter="{ch_idx}">'
            f'<div class="chapter-title">'
            f'<span>{html.escape(ch_title)}</span>'
            f'<span style="color:#94a3b8;font-size:11px">{len(ch.get("lessons", []))}</span>'
            f'</div>'
            f'<div class="lessons">'
        )
        for ls in ch.get("lessons", []):
            ls_id = ls.get("id", "")
            ls_title = ls.get("title", "")
            parts.append(
                f'<div class="lesson" data-chapter="{ch_idx}" data-lesson="{ls_id}">'
                f'{ls_id} {html.escape(ls_title)}'
                f'</div>'
            )
        parts.append("</div></div>")
    parts.append("</div>")
    return "\n".join(parts)


def _render_content_placeholder() -> str:
    return """<div class="content" id="content">
<div class="empty">← 请在左侧选择章节查看详情,或用顶部搜索/筛选</div>
</div>"""


def _render_footer(data_json: str) -> str:
    # 把数据塞到 <script>,前端 JS 用来渲染内容
    return f"""<script>
window.__REVIEW_DATA__ = {data_json};
(function() {{
  const data = window.__REVIEW_DATA__;
  const byLesson = data.by_lesson;
  const chapters = data.chapters;
  const records = data.records;
  const filters = {{}};
  const content = document.getElementById('content');
  const sidebar = document.getElementById('sidebar');

  function getLessonsForChapter(chIdx) {{
    const ch = chapters.find(c => c.index === chIdx);
    return ch ? ch.lessons : [];
  }}

  function renderLesson(chIdx, lsId) {{
    const ch = chapters.find(c => c.index === chIdx);
    if (!ch) return;
    const ls = ch.lessons.find(l => l.id === lsId);
    if (!ls) return;
    const key = chIdx + '|' + lsId;
    const resources = byLesson[key] || [];
    let html = '<div class="lesson-detail">';
    html += '<h2>' + esc(ls.title) + '</h2>';
    html += '<div class="ls-id">' + chIdx + '.' + lsId + ' · 章节: ' + esc(ch.title) + '</div>';
    if (resources.length === 0) {{
      html += '<div class="empty">该课时无资源记录</div>';
    }} else {{
      for (const r of resources) {{
        html += renderResource(r);
      }}
    }}
    html += '</div>';
    content.innerHTML = html;
  }}

  function renderResource(r) {{
    const status = r.status || 'unknown';
    const icon = {{'downloaded':'✓','skipped_existing':'↻','failed':'✗','suspicious':'?'}}[status] || '·';
    const sizeStr = r.size_bytes ? formatSize(r.size_bytes) : '';
    let meta = '';
    if (sizeStr) meta += sizeStr;
    if (r.role && r.role !== 'attachment') {{
      meta += (meta ? ' · ' : '') + '<span class="role-tag">' + esc(r.role) + '</span>';
    }}
    if (r.original_name) {{
      meta += (meta ? ' · ' : '') + '原名: ' + esc(r.original_name);
    }}
    let html = '<div class="resource" data-status="' + status + '">';
    html += '<span class="icon" style="color:' + (statusColors[status] || '#cbd5e1') + '">' + icon + '</span>';
    html += '<span class="name">' + esc(r.saved_name || '(未保存)') + '</span>';
    if (meta) html += '<span class="meta">' + meta + '</span>';
    html += '</div>';
    if (r.reason) {{
      html += '<div class="reason" style="margin-left:32px">' + esc(r.reason) + '</div>';
    }}
    return html;
  }}

  function formatSize(b) {{
    if (b < 1024) return b + ' B';
    if (b < 1024*1024) return (b/1024).toFixed(1) + ' KB';
    if (b < 1024*1024*1024) return (b/(1024*1024)).toFixed(1) + ' MB';
    return (b/(1024*1024*1024)).toFixed(2) + ' GB';
  }}

  const statusColors = {{
    'downloaded': '#10b981', 'skipped_existing': '#9ca3af',
    'failed': '#ef4444', 'suspicious': '#f59e0b'
  }};

  function esc(s) {{
    if (s == null) return '';
    return String(s).replace(/[&<>"]/g, c => ({{
      '&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'
    }}[c]));
  }}

  // 点击左侧
  sidebar.addEventListener('click', e => {{
    const ls = e.target.closest('.lesson');
    const ch = e.target.closest('.chapter');
    if (ls) {{
      document.querySelectorAll('.lesson,.chapter').forEach(el =>
        el.classList.remove('selected'));
      ls.classList.add('selected');
      renderLesson(parseInt(ls.dataset.chapter), ls.dataset.lesson);
    }} else if (ch) {{
      // 点击章 → 显示该章第 1 节
      document.querySelectorAll('.lesson,.chapter').forEach(el =>
        el.classList.remove('selected'));
      ch.classList.add('selected');
      const lessons = getLessonsForChapter(parseInt(ch.dataset.chapter));
      if (lessons.length > 0) {{
        ch.querySelector('.lesson').classList.add('selected');
        renderLesson(parseInt(ch.dataset.chapter), lessons[0].id);
      }}
    }}
  }});

  // 搜索 + 筛选
  const searchEl = document.getElementById('search');
  searchEl.addEventListener('input', applyFilters);

  document.querySelectorAll('.toolbar button').forEach(btn => {{
    btn.addEventListener('click', () => {{
      document.querySelectorAll('.toolbar button').forEach(b =>
        b.classList.remove('active'));
      btn.classList.add('active');
      filters.status = btn.dataset.filter;
      applyFilters();
    }});
  }});

  function applyFilters() {{
    const q = (searchEl.value || '').toLowerCase();
    const filterMode = filters.status || 'all';
    // 重新渲染左侧(高亮匹配项)
    document.querySelectorAll('.chapter, .lesson').forEach(el => {{
      const text = el.textContent.toLowerCase();
      const matchSearch = !q || text.indexOf(q) >= 0;
      const filter = matchFilter(el, filterMode);
      el.style.display = (matchSearch && filter) ? '' : 'none';
    }});
    // 重新渲染右侧(只在当前 lesson 里筛)
    const sel = document.querySelector('.lesson.selected');
    if (sel) {{
      const key = sel.dataset.chapter + '|' + sel.dataset.lesson;
      const rs = (byLesson[key] || []).filter(r => {{
        if (filterMode === 'all') return true;
        if (filterMode === 'failed' || filterMode === 'suspicious')
          return r.status === filterMode;
        if (filterMode === 'missing_english')
          return r.role !== 'english' && !r.saved_name.includes('_English');
        if (filterMode === 'missing_ppt')
          return !r.saved_name.endsWith('.pptx');
        return true;
      }});
      // 重新渲染 content
      const chIdx = parseInt(sel.dataset.chapter);
      const ch = chapters.find(c => c.index === chIdx);
      const ls = ch.lessons.find(l => l.id === sel.dataset.lesson);
      let html = '<div class="lesson-detail">';
      html += '<h2>' + esc(ls.title) + '</h2>';
      html += '<div class="ls-id">' + chIdx + '.' + ls.id + '</div>';
      if (rs.length === 0) html += '<div class="empty">无匹配资源</div>';
      for (const r of rs) html += renderResource(r);
      html += '</div>';
      content.innerHTML = html;
    }}
  }}

  function matchFilter(el, mode) {{
    if (mode === 'all') return true;
    // 简化:对 .lesson 元素,根据其 lesson_id 取对应 records 判定
    if (el.classList.contains('lesson')) {{
      const key = el.dataset.chapter + '|' + el.dataset.lesson;
      const rs = byLesson[key] || [];
      if (rs.length === 0) return mode === 'all';
      if (mode === 'failed' || mode === 'suspicious')
        return rs.some(r => r.status === mode);
      if (mode === 'missing_english')
        return !rs.some(r => r.role === 'english' || (r.saved_name||'').includes('_English'));
      if (mode === 'missing_ppt')
        return !rs.some(r => (r.saved_name||'').toLowerCase().endsWith('.pptx'));
    }}
    return true;
  }}
}})();
</script>"""


def _label(status: str) -> str:
    return {
        "downloaded": "已下载",
        "skipped_existing": "已存在",
        "failed": "失败",
        "suspicious": "可疑",
    }.get(status, status)