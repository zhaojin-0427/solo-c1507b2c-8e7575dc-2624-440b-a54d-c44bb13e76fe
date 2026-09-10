"""自包含 HTML 复盘材料导出: 内联 SVG 时间轴 + 冲突说明 + 证据引用。"""
import html
from datetime import datetime, timezone

from analysis import ev_end, DEP_LABEL
from timeutil import fmt_ms, fmt_ts

PALETTE = ['#5b8def', '#f0a35e', '#5ec49a', '#e06c9f', '#9b7ede',
           '#4fb6c9', '#d9a441', '#7fb069', '#e07a5f', '#81a4cd']

TICK_STEPS_MS = [1000, 5000, 15000, 30000, 60000, 300000, 900000, 1800000,
                 3600000, 7200000, 21600000, 43200000, 86400000, 604800000]


def _esc(s):
    return html.escape(str(s if s is not None else ''), quote=True)


def _group_color(name, groups):
    if name not in groups:
        groups[name] = PALETTE[len(groups) % len(PALETTE)]
    return groups[name]


def _layout_lanes(events):
    """按分组分泳道, 泳道内按区间重叠分配子行。返回 (lanes, pos)。"""
    groups = {}
    for e in sorted(events, key=lambda x: x['start_ts']):
        groups.setdefault(e.get('group_name') or '未分组', []).append(e)
    lanes, pos = [], {}
    for name, evs in groups.items():
        rows = []
        for e in evs:
            s, en = e['start_ts'], ev_end(e)
            for ri, last_end in enumerate(rows):
                if s >= last_end:
                    rows[ri] = en
                    pos[e['id']] = (len(lanes), ri)
                    break
            else:
                rows.append(en)
                pos[e['id']] = (len(lanes), len(rows) - 1)
        lanes.append({'name': name, 'events': evs, 'rows': max(1, len(rows))})
    return lanes, pos


def render_svg(events, deps, conflicts, width=1180, display_tz='UTC'):
    """服务端渲染只读 SVG 时间轴。"""
    if not events:
        return '<p class="empty">暂无事件</p>'
    t0 = min(e['start_ts'] for e in events)
    t1 = max(ev_end(e) for e in events)
    pad = max(int((t1 - t0) * 0.05), 30000)
    t0, t1 = t0 - pad, t1 + pad
    span = t1 - t0

    LEFT, RIGHT, TOP, ROW_H, LANE_PAD = 150, 24, 36, 26, 10
    plot_w = width - LEFT - RIGHT
    px = lambda t: LEFT + (t - t0) / span * plot_w

    lanes, pos = _layout_lanes(events)
    lane_y, y = [], TOP
    for lane in lanes:
        lane_y.append(y)
        y += 20 + lane['rows'] * ROW_H + LANE_PAD
    height = y + 8

    conflict_events, conflict_deps = set(), set()
    for c in conflicts:
        conflict_events.update(c.get('event_ids', []))
        conflict_deps.update(c.get('dep_ids', []))

    group_colors = {}
    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
        f'viewBox="0 0 {width} {height}" font-family="Menlo, Consolas, monospace">',
        '<defs>'
        '<marker id="ar" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse">'
        '<path d="M0,0 L10,5 L0,10 z" fill="#8a93a6"/></marker>'
        '<marker id="arr" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse">'
        '<path d="M0,0 L10,5 L0,10 z" fill="#e5484d"/></marker>'
        '</defs>',
        f'<rect x="0" y="0" width="{width}" height="{height}" fill="#14171d" rx="8"/>',
    ]
    # 刻度
    step = next((s for s in TICK_STEPS_MS if s / span * plot_w >= 90), TICK_STEPS_MS[-1])
    t = t0 - (t0 % step)
    while t <= t1:
        if t >= t0:
            x = px(t)
            parts.append(f'<line x1="{x:.1f}" y1="{TOP - 8}" x2="{x:.1f}" y2="{height - 8}" stroke="#262c38" stroke-width="1"/>')
            parts.append(f'<text x="{x + 3:.1f}" y="{TOP - 12}" font-size="10" fill="#8a93a6">{_esc(fmt_ts(t, display_tz, with_seconds=step < 60000)[11:])}</text>')
        t += step
    # 泳道
    for li, lane in enumerate(lanes):
        ly = lane_y[li]
        lh = 20 + lane['rows'] * ROW_H
        color = _group_color(lane['name'], group_colors)
        parts.append(f'<rect x="4" y="{ly}" width="{width - 8}" height="{lh}" fill="#1a1f29" rx="6"/>')
        parts.append(f'<circle cx="16" cy="{ly + 12}" r="4" fill="{color}"/>')
        parts.append(f'<text x="26" y="{ly + 16}" font-size="11" fill="#c8cfdb" font-weight="bold">{_esc(lane["name"])}</text>')
    # 事件条
    centers = {}
    for e in events:
        li, ri = pos[e['id']]
        ey = lane_y[li] + 20 + ri * ROW_H
        color = _group_color(e.get('group_name') or '未分组', group_colors)
        bad = e['id'] in conflict_events
        stroke = '#e5484d' if bad else color
        dash = {'high': '', 'medium': ' stroke-dasharray="5 3"', 'low': ' stroke-dasharray="2 3"'}.get(e.get('confidence'), '')
        sw = 2.2 if bad else 1.2
        if e['end_ts'] is None:
            cx = px(e['start_ts'])
            parts.append(f'<polygon points="{cx:.1f},{ey + 3} {cx + 7:.1f},{ey + 10} {cx:.1f},{ey + 17} {cx - 7:.1f},{ey + 10}" '
                         f'fill="{color}" stroke="{stroke}" stroke-width="{sw}"{dash}/>')
            lx = cx + 10
            centers[e['id']] = (cx, cx, ey + 10)
        else:
            x1, x2 = px(e['start_ts']), px(e['end_ts'])
            w = max(3, x2 - x1)
            parts.append(f'<rect x="{x1:.1f}" y="{ey}" width="{w:.1f}" height="18" rx="4" '
                         f'fill="{color}33" stroke="{stroke}" stroke-width="{sw}"{dash}/>')
            lx = x1 + w + 6
            centers[e['id']] = (x1, x1 + w, ey + 9)
        lock = ' 🔒' if e.get('locked') else ''
        parts.append(f'<text x="{lx:.1f}" y="{ey + 13}" font-size="10.5" fill="#dfe4ee">{_esc(e["title"])}{lock}</text>')
    # 依赖箭头
    evs = {e['id']: e for e in events}
    for d in deps:
        a, b = evs.get(d['from_id']), evs.get(d['to_id'])
        if not a or not b or a['id'] not in centers or b['id'] not in centers:
            continue
        _x1a, x1b, y1 = centers[a['id']]
        x2a, _x2b, y2 = centers[b['id']]
        x1 = x1b + 2
        x2 = x2a - 3
        violated = d['id'] in conflict_deps
        color = '#e5484d' if violated else '#8a93a6'
        marker = 'arr' if violated else 'ar'
        dash = ' stroke-dasharray="4 3"' if violated else ''
        label = DEP_LABEL.get(d['type'], d['type'])
        mx = (x1 + x2) / 2
        parts.append(f'<path d="M{x1:.1f},{y1:.1f} C{x1 + 36:.1f},{y1:.1f} {x2 - 36:.1f},{y2:.1f} {x2:.1f},{y2:.1f}" '
                     f'fill="none" stroke="{color}" stroke-width="1.4"{dash} marker-end="url(#{marker})"/>')
        parts.append(f'<text x="{mx:.1f}" y="{(y1 + y2) / 2 - 4:.1f}" font-size="9" fill="{color}" text-anchor="middle">{label}</text>')
    parts.append('</svg>')
    return ''.join(parts)


def _chain_html(chain, evs_tz=True):
    out = []
    for step in chain:
        if step['kind'] == 'event':
            t = fmt_ts(step.get('start_ts'), step.get('timezone') or 'UTC')
            out.append(f'<span class="chip ev-chip">{_esc(step["title"])}'
                       f'<span class="chip-time">{_esc(t)}</span></span>')
        else:
            out.append(f'<span class="chip edge-chip">—[{_esc(step["label"])}]→</span>')
    return '<div class="chain">' + ''.join(out) + '</div>'


def _evidence_html(evidence):
    rows = []
    for ev in evidence:
        quote = f'<blockquote>{_esc(ev["evidence"])}</blockquote>' if ev.get('evidence') else ''
        rows.append(
            f'<div class="evidence-item">'
            f'<div class="evidence-head"><b>{_esc(ev["title"])}</b>'
            f'<span class="badge conf-{_esc(ev["confidence"])}">{_esc(ev["confidence"])}</span></div>'
            f'<div class="evidence-meta">来源: {_esc(ev["source"])} · '
            f'{_esc(fmt_ts(ev["start_ts"], ev["timezone"]))} → {_esc(fmt_ts(ev["end_ts"], ev["timezone"]))}</div>'
            f'{quote}</div>')
    return ''.join(rows)


def _delta_cell(v):
    if not v:
        return '—'
    return ('+' if v > 0 else '-') + fmt_ms(abs(v))


def _plans_html(plans):
    if not plans:
        return ''
    out = ['<h2>最小调整方案(生成时快照)</h2>']
    for p in plans:
        rows = ''.join(
            f'<tr><td>{_esc(m["title"])}</td>'
            f'<td>{_esc(fmt_ts(m["from_start"]))} → {_esc(fmt_ts(m["to_start"]))}</td>'
            f'<td>{_esc(_delta_cell(m.get("delta_start_ms", 0)))}</td>'
            f'<td>{_esc(_delta_cell(m.get("delta_end_ms", 0)))}</td></tr>'
            for m in p['moves'])
        moves = (f'<table><thead><tr><th>事件</th><th>开始时间变化</th><th>开始移动</th><th>结束移动</th></tr></thead>'
                 f'<tbody>{rows}</tbody></table>' if p['moves'] else '<p>无需移动。</p>')
        un = ''.join(f'<li>{_esc(u["title"])}</li>' for u in p['unresolved'])
        unresolved = f'<p class="warn">仍未解决:</p><ul>{un}</ul>' if un else '<p class="ok">全部冲突可解决。</p>'
        out.append(f'<div class="plan"><h3>{_esc(p["name"])}</h3>'
                   f'<p class="muted">{_esc(p["description"])} · 解决 {p["resolved"]} 项 · '
                   f'总移动量 {fmt_ms(p["total_shift_ms"])}</p>{moves}{unresolved}</div>')
    return ''.join(out)


def render_export(events, deps, excls, analysis, plans, display_tz='UTC'):
    conflicts = analysis['conflicts']
    svg = render_svg(events, deps, conflicts, display_tz=display_tz)
    generated = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')

    conf_html = []
    for c in conflicts:
        conf_html.append(
            f'<div class="conflict sev-{_esc(c["severity"])}">'
            f'<div class="conflict-head"><span class="badge">{_esc(c["type_label"])}</span>'
            f'<span class="sev">严重程度: {_esc(c["severity_label"])}</span></div>'
            f'<h3>{_esc(c["title"])}</h3><p>{_esc(c["detail"])}</p>'
            f'<h4>最短冲突链</h4>{_chain_html(c["chain"])}'
            f'<h4>涉及证据</h4>{_evidence_html(c["evidence"])}</div>')
    if not conf_html:
        conf_html.append('<p class="ok">未检测到冲突,时间线自洽。</p>')

    ev_rows = ''.join(
        f'<tr><td>{_esc(e["title"])}</td><td>{_esc(fmt_ts(e["start_ts"], e.get("timezone")))}</td>'
        f'<td>{_esc(fmt_ts(e["end_ts"], e.get("timezone"))) if e["end_ts"] is not None else "瞬时"}</td>'
        f'<td>{_esc(e.get("confidence"))}</td><td>{_esc(e.get("source") or "—")}</td>'
        f'<td>{"🔒" if e.get("locked") else ""}</td></tr>'
        for e in events)

    dep_rows = ''.join(
        f'<tr><td>{_esc(_t(events, d["from_id"]))}</td><td>{_esc(DEP_LABEL.get(d["type"], d["type"]))}</td>'
        f'<td>{_esc(_t(events, d["to_id"]))}</td><td>{_esc(d.get("note") or "")}</td></tr>'
        for d in deps)
    excl_rows = ''.join(
        f'<tr><td>{_esc(_t(events, x["a_id"]))}</td><td>互斥</td>'
        f'<td>{_esc(_t(events, x["b_id"]))}</td><td>{_esc(x.get("reason") or "")}</td></tr>'
        for x in excls)

    return f'''<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>事故复盘材料 · {generated}</title>
<style>
body{{font-family:-apple-system,"PingFang SC","Microsoft YaHei",sans-serif;background:#0f1218;color:#dfe4ee;margin:0;padding:32px;line-height:1.6}}
main{{max-width:1240px;margin:0 auto}}
h1{{font-size:24px}} h2{{font-size:18px;margin-top:36px;border-bottom:1px solid #262c38;padding-bottom:8px}}
h3{{font-size:15px;margin:8px 0}} h4{{font-size:13px;color:#8a93a6;margin:14px 0 6px}}
.muted{{color:#8a93a6;font-size:13px}} .ok{{color:#5ec49a}} .warn{{color:#f0a35e}}
.badge{{display:inline-block;background:#262c38;border-radius:4px;padding:2px 8px;font-size:12px}}
.conf-high{{color:#e5484d}} .conf-medium{{color:#f0a35e}} .conf-low{{color:#8a93a6}}
.conflict{{background:#1a1f29;border:1px solid #262c38;border-left:4px solid #e5484d;border-radius:8px;padding:16px 20px;margin:14px 0}}
.conflict.sev-medium{{border-left-color:#f0a35e}}
.conflict-head{{display:flex;gap:12px;align-items:center;color:#8a93a6;font-size:12px}}
.chain{{display:flex;flex-wrap:wrap;gap:6px;align-items:center;margin:6px 0}}
.chip{{background:#14171d;border:1px solid #2c3342;border-radius:6px;padding:4px 10px;font-size:12.5px}}
.chip-time{{color:#8a93a6;margin-left:8px;font-size:11px}}
.edge-chip{{color:#f0a35e;border-color:#3d3428}}
.evidence-item{{background:#14171d;border-radius:6px;padding:10px 14px;margin:6px 0}}
.evidence-head{{display:flex;gap:10px;align-items:center}}
.evidence-meta{{color:#8a93a6;font-size:12px;margin-top:2px}}
blockquote{{margin:8px 0 0;padding:6px 12px;border-left:3px solid #5b8def;color:#aeb6c6;font-size:13px;background:#10141b}}
table{{border-collapse:collapse;width:100%;font-size:13px;margin:10px 0}}
th,td{{border:1px solid #262c38;padding:6px 10px;text-align:left}}
th{{background:#1a1f29;color:#8a93a6}}
.plan{{background:#1a1f29;border:1px solid #262c38;border-radius:8px;padding:14px 18px;margin:12px 0}}
footer{{margin-top:40px;color:#5c6474;font-size:12px}}
.empty{{color:#8a93a6}}
svg{{max-width:100%;height:auto}}
</style></head><body><main>
<h1>事故复盘材料 · 时序校验报告</h1>
<p class="muted">生成时间: {generated} · 事件 {len(events)} 个 · 依赖 {len(deps)} 条 · 互斥 {len(excls)} 组 · 冲突 {analysis["summary"]["total"]} 项</p>
<h2>时间轴(显示时区: {_esc(display_tz)})</h2>
{svg}
<h2>冲突说明({analysis["summary"]["total"]})</h2>
{''.join(conf_html)}
{_plans_html(plans)}
<h2>事件清单</h2>
<table><thead><tr><th>事件</th><th>开始</th><th>结束</th><th>置信度</th><th>来源</th><th>锁定</th></tr></thead>
<tbody>{ev_rows}</tbody></table>
<h2>依赖与互斥</h2>
<table><thead><tr><th>从</th><th>关系</th><th>到</th><th>备注</th></tr></thead>
<tbody>{dep_rows}{excl_rows}</tbody></table>
<footer>由「事故时序校验台」导出的自包含复盘材料,可离线查看。</footer>
</main></body></html>'''


def _t(events, eid):
    for e in events:
        if e['id'] == eid:
            return e['title']
    return f'#{eid}'
