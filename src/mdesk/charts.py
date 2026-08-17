"""内联 SVG 图表。不依赖任何外部库，生成的字符串直接嵌进 HTML。

颜色一律走 CSS 变量，深浅色模式由样式表统一切换，图表本身不感知主题。
"""

import html
import json

from markupsafe import Markup


def _scale(v, lo, hi, a, b):
    if hi == lo:
        return (a + b) / 2
    return a + (v - lo) / (hi - lo) * (b - a)


def _fmt(v: float) -> str:
    av = abs(v)
    if av >= 1000:
        return f"{v:,.0f}"
    if av >= 100:
        return f"{v:.1f}"
    if av >= 1:
        return f"{v:.2f}"
    return f"{v:.4f}"


def sparkline(values, width: int = 104, height: int = 26) -> str:
    """表格里的迷你走势。单系列，不需要图例，靠所在行的标的名承担识别。"""
    vals = [v for v in values if v is not None]
    if len(vals) < 2:
        return Markup(f'<svg width="{width}" height="{height}" role="presentation"></svg>')

    lo, hi = min(vals), max(vals)
    pad = 3
    pts = [
        (
            _scale(i, 0, len(vals) - 1, pad, width - pad),
            _scale(v, lo, hi, height - pad, pad),
        )
        for i, v in enumerate(vals)
    ]
    d = "M" + " L".join(f"{x:.1f},{y:.1f}" for x, y in pts)
    last_x, last_y = pts[-1]
    change = (vals[-1] / vals[0] - 1) * 100 if vals[0] else 0
    return Markup(
        f'<svg width="{width}" height="{height}" viewBox="0 0 {width} {height}" '
        f'role="img" aria-label="近期走势 {change:+.1f}%">'
        f'<title>区间 {_fmt(vals[0])} → {_fmt(vals[-1])}，{change:+.1f}%</title>'
        f'<path d="{d}" fill="none" stroke="var(--series-1)" stroke-width="1.5" '
        f'stroke-linejoin="round" stroke-linecap="round"/>'
        f'<circle cx="{last_x:.1f}" cy="{last_y:.1f}" r="2.2" fill="var(--series-1)"/>'
        f"</svg>"
    )


def line_chart(x_labels, series, height: int = 300, width: int = 880,
               y_label: str = "") -> str:
    """折线图。series 为 [{name, color, values}]，两条以上时调用方必须给图例。"""
    series = [s for s in series if any(v is not None for v in s["values"])]
    if not series or len(x_labels) < 2:
        return Markup('<p class="dim">数据不足，无法绘图</p>')

    left, right, top, bottom = 56, 14, 14, 30
    all_vals = [v for s in series for v in s["values"] if v is not None]
    lo, hi = min(all_vals), max(all_vals)
    span = (hi - lo) or (abs(hi) or 1) * 0.1
    lo, hi = lo - span * 0.06, hi + span * 0.06

    def px(i):
        return _scale(i, 0, len(x_labels) - 1, left, width - right)

    def py(v):
        return _scale(v, lo, hi, height - bottom, top)

    parts = [
        f'<svg class="chart" viewBox="0 0 {width} {height}" width="{width}" '
        f'height="{height}" role="img" preserveAspectRatio="xMidYMid meet">'
    ]

    # 横向网格与 y 轴刻度
    for k in range(5):
        v = lo + (hi - lo) * k / 4
        y = py(v)
        parts.append(
            f'<line x1="{left}" y1="{y:.1f}" x2="{width - right}" y2="{y:.1f}" '
            f'stroke="var(--grid)" stroke-width="1"/>'
        )
        parts.append(
            f'<text x="{left - 9}" y="{y + 4:.1f}" text-anchor="end" font-size="11" '
            f'fill="var(--ink-muted)" style="font-variant-numeric:tabular-nums">{_fmt(v)}</text>'
        )

    # x 轴刻度，最多 6 个，避免标签打架
    step = max(1, (len(x_labels) - 1) // 5)
    for i in range(0, len(x_labels), step):
        parts.append(
            f'<text x="{px(i):.1f}" y="{height - 9}" text-anchor="middle" font-size="11" '
            f'fill="var(--ink-muted)">{html.escape(str(x_labels[i]))}</text>'
        )

    for s in series:
        pts, seg = [], []
        for i, v in enumerate(s["values"]):
            if v is None:
                if len(seg) > 1:
                    pts.append(seg)
                seg = []
            else:
                seg.append((px(i), py(v)))
        if len(seg) > 1:
            pts.append(seg)
        for chunk in pts:
            d = "M" + " L".join(f"{x:.1f},{y:.1f}" for x, y in chunk)
            parts.append(
                f'<path d="{d}" fill="none" stroke="{s["color"]}" stroke-width="2" '
                f'stroke-linejoin="round" stroke-linecap="round"/>'
            )

    # 悬停层：竖线加圆点，坐标预先算好交给页面里的脚本
    parts.append(
        f'<line class="cross" x1="0" y1="{top}" x2="0" y2="{height - bottom}" '
        f'stroke="var(--axis)" stroke-width="1" opacity="0"/>'
    )
    for s in series:
        parts.append(f'<circle class="hd" r="4" fill="{s["color"]}" '
                     f'stroke="var(--surface)" stroke-width="2" opacity="0"/>')
    parts.append(
        f'<rect class="hit" x="{left}" y="{top}" width="{width - left - right}" '
        f'height="{height - top - bottom}" fill="transparent"/>'
    )
    parts.append("</svg>")

    payload = {
        "left": left, "right": right, "width": width,
        "x": [str(v) for v in x_labels],
        "px": [round(px(i), 1) for i in range(len(x_labels))],
        "series": [
            {
                "name": s["name"],
                "color": s["color"],
                "py": [None if v is None else round(py(v), 1) for v in s["values"]],
                "val": [None if v is None else _fmt(v) for v in s["values"]],
            }
            for s in series
        ],
        "yLabel": y_label,
    }
    attr = html.escape(json.dumps(payload, ensure_ascii=False), quote=True)
    return Markup(f'<div class="chartbox" data-chart="{attr}">' + "".join(parts) + "</div>")


def volume_chart(x_labels, volumes, width: int = 880, height: int = 96) -> str:
    """成交量柱。与上方价格图共用同一条时间轴，所以左右留白必须和 line_chart 一致。"""
    vals = [v for v in volumes if v]
    if not vals or len(x_labels) < 2:
        return Markup('<p class="dim">无成交量数据</p>')

    left, right, top, bottom = 56, 14, 8, 18
    hi = max(vals)
    n = len(x_labels)
    plot_w = width - left - right
    bar_w = max(plot_w / n * 0.72, 0.7)

    parts = [f'<svg viewBox="0 0 {width} {height}" width="{width}" height="{height}" '
             f'role="img" aria-label="成交量">']
    base_y = height - bottom
    for i, v in enumerate(volumes):
        if not v:
            continue
        x = left + plot_w * (i / (n - 1)) - bar_w / 2
        h = max((v / hi) * (base_y - top), 0.6)
        parts.append(f'<rect x="{x:.2f}" y="{base_y - h:.2f}" width="{bar_w:.2f}" '
                     f'height="{h:.2f}" fill="var(--series-1)" opacity="0.5"/>')
    parts.append(f'<line x1="{left}" y1="{base_y}" x2="{width - right}" y2="{base_y}" '
                 f'stroke="var(--axis)" stroke-width="1"/>')
    parts.append(f'<text x="{left - 9}" y="{top + 9}" text-anchor="end" font-size="11" '
                 f'fill="var(--ink-muted)">{_human(hi)}</text>')
    parts.append("</svg>")
    return Markup("".join(parts))


def _human(v: float) -> str:
    for unit, div in (("亿", 1e8), ("万", 1e4)):
        if abs(v) >= div:
            return f"{v / div:.1f}{unit}"
    return f"{v:.0f}"


def bar_chart(rows, width: int = 880, row_h: int = 26, label_w: int = 210) -> str:
    """横向条形图。rows 为 [(label, value, tooltip)]，单一量纲，按值排序由调用方决定。"""
    rows = [r for r in rows if r[1] is not None]
    if not rows:
        return Markup('<p class="dim">暂无数据</p>')

    height = len(rows) * row_h + 16
    hi = max(r[1] for r in rows) or 1
    bar_left = label_w + 12
    bar_max = width - bar_left - 62
    parts = [f'<svg viewBox="0 0 {width} {height}" width="{width}" height="{height}" role="img">']

    for i, (label, value, tip) in enumerate(rows):
        y = 8 + i * row_h
        h = 14
        w = max(_scale(value, 0, hi, 0, bar_max), 1.5)
        r = min(4, w)
        # 数据端 4px 圆角，基线端保持方角
        d = (f"M{bar_left},{y} H{bar_left + w - r} A{r},{r} 0 0 1 {bar_left + w},{y + r} "
             f"V{y + h - r} A{r},{r} 0 0 1 {bar_left + w - r},{y + h} H{bar_left} Z")
        parts.append(f"<g><title>{html.escape(tip)}</title>")
        parts.append(
            f'<text x="{label_w}" y="{y + 11}" text-anchor="end" font-size="12" '
            f'fill="var(--ink-2)">{html.escape(label)}</text>'
        )
        parts.append(f'<path d="{d}" fill="var(--series-1)"/>')
        parts.append(
            f'<text x="{bar_left + w + 8}" y="{y + 11}" font-size="12" '
            f'fill="var(--ink-muted)" style="font-variant-numeric:tabular-nums">'
            f'{value:,.0f}</text>'
        )
        parts.append("</g>")

    parts.append(f'<line x1="{bar_left}" y1="4" x2="{bar_left}" y2="{height - 4}" '
                 f'stroke="var(--axis)" stroke-width="1"/>')
    parts.append("</svg>")
    return Markup("".join(parts))


# 页面内联的悬停脚本。所有折线图共用一份，不引任何外部资源。
HOVER_JS = """
document.querySelectorAll('.chartbox').forEach(function (box) {
  var cfg = JSON.parse(box.dataset.chart);
  var svg = box.querySelector('svg');
  var cross = svg.querySelector('.cross');
  var dots = svg.querySelectorAll('.hd');
  var hit = svg.querySelector('.hit');
  var tip = document.createElement('div');
  tip.className = 'tip';
  box.appendChild(tip);

  function nearest(clientX) {
    var r = svg.getBoundingClientRect();
    var vx = (clientX - r.left) / r.width * cfg.width;
    var best = 0, bd = Infinity;
    for (var i = 0; i < cfg.px.length; i++) {
      var d = Math.abs(cfg.px[i] - vx);
      if (d < bd) { bd = d; best = i; }
    }
    return best;
  }

  function show(e) {
    var i = nearest(e.clientX);
    cross.setAttribute('x1', cfg.px[i]);
    cross.setAttribute('x2', cfg.px[i]);
    cross.setAttribute('opacity', '1');
    var lines = '<b>' + cfg.x[i] + '</b>';
    cfg.series.forEach(function (s, k) {
      var dot = dots[k];
      if (s.py[i] === null) { dot.setAttribute('opacity', '0'); return; }
      dot.setAttribute('cx', cfg.px[i]);
      dot.setAttribute('cy', s.py[i]);
      dot.setAttribute('opacity', '1');
      lines += '<span><i style="background:' + s.color + '"></i>' +
               s.name + '<em>' + s.val[i] + '</em></span>';
    });
    tip.innerHTML = lines;
    var r = svg.getBoundingClientRect();
    var left = cfg.px[i] / cfg.width * r.width;
    tip.style.left = Math.min(Math.max(left, 70), r.width - 70) + 'px';
    tip.style.opacity = '1';
  }

  function hide() {
    cross.setAttribute('opacity', '0');
    dots.forEach(function (d) { d.setAttribute('opacity', '0'); });
    tip.style.opacity = '0';
  }

  hit.addEventListener('mousemove', show);
  hit.addEventListener('mouseleave', hide);
  hit.addEventListener('touchmove', function (e) {
    if (e.touches.length) { show(e.touches[0]); }
  }, { passive: true });
});
"""

# 表格排序与分组。纯前端，不依赖任何库。
# 数值列排序读单元格的 data-v，避免被「3.97 万亿」这类格式化文本干扰。
TABLE_JS = """
(function () {
  var table = document.getElementById('wl');
  if (!table) return;
  var tbody = table.tBodies[0];
  var rows = Array.prototype.slice.call(tbody.querySelectorAll('tr:not(.grouprow)'));
  var groupBy = 'market', sortCol = null, sortDir = 1;

  function cellValue(tr, i, kind) {
    var td = tr.cells[i];
    if (kind === 'num') {
      var raw = td.getAttribute('data-v');
      if (raw === null || raw === '') return null;
      var n = parseFloat(raw);
      return isNaN(n) ? null : n;
    }
    return (td.textContent || '').trim();
  }

  function render() {
    var ordered = rows.slice();
    if (sortCol !== null) {
      var kind = table.tHead.rows[0].cells[sortCol].getAttribute('data-sort');
      ordered.sort(function (a, b) {
        var x = cellValue(a, sortCol, kind), y = cellValue(b, sortCol, kind);
        // 空值恒排在后面，不参与升降序
        if (x === null && y === null) return 0;
        if (x === null) return 1;
        if (y === null) return -1;
        if (kind === 'num') return (x - y) * sortDir;
        return x.localeCompare(y, 'zh') * sortDir;
      });
    }
    tbody.innerHTML = '';
    if (groupBy === 'none') {
      ordered.forEach(function (r) { tbody.appendChild(r); });
      return;
    }
    var seen = [], buckets = {};
    ordered.forEach(function (r) {
      var k = r.dataset[groupBy] || '未分类';
      if (!buckets[k]) { buckets[k] = []; seen.push(k); }
      buckets[k].push(r);
    });
    var ncols = table.tHead.rows[0].cells.length;
    seen.forEach(function (k) {
      var tr = document.createElement('tr');
      tr.className = 'grouprow';
      var td = document.createElement('td');
      td.colSpan = ncols;
      td.innerHTML = k + '<span>' + buckets[k].length + ' 只</span>';
      tr.appendChild(td);
      tbody.appendChild(tr);
      buckets[k].forEach(function (r) { tbody.appendChild(r); });
    });
  }

  table.tHead.rows[0].querySelectorAll('th[data-sort]').forEach(function (th, _i) {
    th.addEventListener('click', function () {
      var idx = Array.prototype.indexOf.call(th.parentNode.cells, th);
      sortDir = (sortCol === idx) ? -sortDir : -1;   // 首次点击默认降序
      sortCol = idx;
      table.tHead.rows[0].querySelectorAll('th').forEach(function (o) {
        o.removeAttribute('aria-sort');
      });
      th.setAttribute('aria-sort', sortDir === 1 ? 'ascending' : 'descending');
      render();
    });
  });

  document.querySelectorAll('.toolbar button[data-group]').forEach(function (b) {
    b.addEventListener('click', function () {
      groupBy = b.dataset.group;
      document.querySelectorAll('.toolbar button[data-group]').forEach(function (o) {
        o.setAttribute('aria-pressed', String(o === b));
      });
      render();
    });
  });

  render();
})();
"""

HOVER_CSS = """
.chartbox { position: relative; }
.chartbox .tip {
  position: absolute; top: 6px; transform: translateX(-50%);
  background: var(--surface); border: 1px solid var(--border); border-radius: 8px;
  padding: 7px 10px; font-size: 12px; pointer-events: none; opacity: 0;
  transition: opacity .1s; white-space: nowrap; box-shadow: 0 2px 10px rgba(0,0,0,.10);
  color: var(--ink);
}
.chartbox .tip b { display: block; color: var(--ink-muted); font-weight: 500;
  font-size: 11px; margin-bottom: 3px; }
.chartbox .tip span { display: flex; align-items: center; gap: 6px; }
.chartbox .tip i { width: 9px; height: 2px; border-radius: 1px; flex: none; }
.chartbox .tip em { font-style: normal; margin-left: auto; padding-left: 14px;
  font-variant-numeric: tabular-nums; }
"""
