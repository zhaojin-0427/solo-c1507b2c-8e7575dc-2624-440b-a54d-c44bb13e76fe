/* 事故时序校验台 —— 前端逻辑 */
'use strict';

// ---------------------------------------------------------------- 工具

const $ = (sel) => document.querySelector(sel);
const esc = (s) => String(s ?? '').replace(/[&<>"']/g,
  (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

const NAMED_TZ = {
  UTC: 0, GMT: 0, Z: 0, CST: 480, BEIJING: 480, PRC: 480, HKT: 480, SGT: 480,
  JST: 540, KST: 540, IST: 330, CET: 60, CEST: 120, BST: 60, MSK: 180,
  EST: -300, EDT: -240, MST: -420, MDT: -360, PST: -480, PDT: -420,
  HST: -600, AKST: -540,
};

function tzOffsetMin(tz) {
  const s = (tz || 'UTC').trim();
  if (!s) return 0;
  const up = s.toUpperCase();
  if (up in NAMED_TZ) return NAMED_TZ[up];
  const m = s.match(/^([+-])(\d{1,2}):?(\d{2})?$/);
  if (m) {
    const v = (+m[2]) * 60 + (+(m[3] || 0));
    return m[1] === '+' ? v : -v;
  }
  try {  // IANA 名称
    const now = new Date();
    const parts = new Intl.DateTimeFormat('en-US', {
      timeZone: s, hour12: false, year: 'numeric', month: '2-digit', day: '2-digit',
      hour: '2-digit', minute: '2-digit', second: '2-digit',
    }).formatToParts(now);
    const get = (t) => +(parts.find((p) => p.type === t)?.value || 0);
    const asUTC = Date.UTC(get('year'), get('month') - 1, get('day'),
      get('hour') % 24, get('minute'), get('second'));
    return Math.round((asUTC - now.getTime()) / 60000);
  } catch (e) { return 0; }
}

const pad2 = (n) => String(n).padStart(2, '0');

function fmtTs(ms, tz, withSeconds = true) {
  if (ms === null || ms === undefined) return '—';
  const off = tzOffsetMin(tz);
  const d = new Date(ms + off * 60000);
  const date = `${d.getUTCFullYear()}-${pad2(d.getUTCMonth() + 1)}-${pad2(d.getUTCDate())}`;
  const time = `${pad2(d.getUTCHours())}:${pad2(d.getUTCMinutes())}` +
    (withSeconds ? `:${pad2(d.getUTCSeconds())}` : '');
  const sign = off >= 0 ? '+' : '-';
  const ao = Math.abs(off);
  return `${date} ${time} ${sign}${pad2(Math.floor(ao / 60))}:${pad2(ao % 60)}`;
}

function fmtDur(ms) {
  const sign = ms < 0 ? '-' : '';
  ms = Math.abs(Math.round(ms));
  if (ms < 1000) return `${sign}${ms} 毫秒`;
  const s = ms / 1000;
  if (s < 60) return `${sign}${s.toFixed(1)} 秒`;
  const m = s / 60;
  if (m < 60) return `${sign}${m.toFixed(1)} 分钟`;
  const h = m / 60;
  if (h < 24) return `${sign}${h.toFixed(1)} 小时`;
  return `${sign}${(h / 24).toFixed(1)} 天`;
}

function toast(msg, isError = false) {
  const el = document.createElement('div');
  el.className = 'toast' + (isError ? ' error' : '');
  el.textContent = msg;
  $('#toast-wrap').appendChild(el);
  setTimeout(() => el.remove(), 3200);
}

async function api(path, method = 'GET', body = null) {
  const opt = { method, headers: {} };
  if (body !== null) {
    opt.headers['Content-Type'] = 'application/json';
    opt.body = JSON.stringify(body);
  }
  const resp = await fetch(path, opt);
  const data = await resp.json().catch(() => ({}));
  if (!resp.ok) throw new Error(data.error || `请求失败 (${resp.status})`);
  return data;
}

// ---------------------------------------------------------------- 全局状态

const S = {
  events: [], deps: [], excls: [], conflicts: [],
  canUndo: false, hasBaseline: false,
  view: { start: 0, pxPerMs: 0.002 },
  fittedOnce: false,
  editingId: null,       // 表单正在编辑的事件
  selectedId: null,      // 选中的事件
  hlConflict: null,      // 高亮的冲突 id
  displayTz: 'UTC',
  drag: null,
};

const PALETTE = ['#5b8def', '#f0a35e', '#5ec49a', '#e06c9f', '#9b7ede',
  '#4fb6c9', '#d9a441', '#7fb069', '#e07a5f', '#81a4cd'];
const groupColorMap = new Map();

function groupColor(name, events) {
  const custom = (events || S.events).find((e) => (e.group_name || '未分组') === name && e.color);
  if (custom) return custom.color;
  if (!groupColorMap.has(name)) {
    groupColorMap.set(name, PALETTE[groupColorMap.size % PALETTE.length]);
  }
  return groupColorMap.get(name);
}

const evEnd = (e) => (e.end_ts !== null && e.end_ts !== undefined ? e.end_ts : e.start_ts);
const evById = (id) => S.events.find((e) => e.id === id);

// ---------------------------------------------------------------- 数据加载

async function refresh() {
  const data = await api('/api/state');
  applyState(data);
}

function applyState(data) {
  S.events = data.events;
  S.deps = data.dependencies;
  S.excls = data.exclusions;
  S.conflicts = data.analysis.conflicts;
  S.canUndo = data.can_undo;
  S.hasBaseline = data.has_baseline;
  if (!S.fittedOnce && S.events.length) {
    fitView();
    S.fittedOnce = true;
  }
  renderAll();
  return data;
}

function renderAll() {
  $('#btn-undo').disabled = !S.canUndo;
  const n = S.conflicts.length;
  const badge = $('#conflict-badge');
  badge.textContent = n;
  badge.classList.toggle('zero', n === 0);
  $('#event-count').textContent = `(${S.events.length})`;
  renderTimeline();
  renderEventList();
  renderConflicts();
  renderDeps();
}

// ---------------------------------------------------------------- 时间轴

const ROW_H = 26, LANE_LABEL_H = 20, LANE_PAD = 10, TOP = 36, LEFT = 150, RIGHT = 24;
const TICK_STEPS = [1000, 5000, 15000, 30000, 60000, 300000, 900000, 1800000,
  3600000, 7200000, 21600000, 43200000, 86400000, 604800000];

function timelineWidth() {
  return Math.max($('#timeline').clientWidth, 400);
}

function fitView() {
  if (!S.events.length) return;
  const t0 = Math.min(...S.events.map((e) => e.start_ts));
  const t1 = Math.max(...S.events.map(evEnd));
  const span = Math.max(t1 - t0, 60000);
  const plotW = timelineWidth() - LEFT - RIGHT;
  S.view.pxPerMs = plotW / (span * 1.12);
  S.view.start = t0 - span * 0.06;
}

function layoutLanes(events) {
  const groups = new Map();
  [...events].sort((a, b) => a.start_ts - b.start_ts || a.id - b.id).forEach((e) => {
    const g = e.group_name || '未分组';
    if (!groups.has(g)) groups.set(g, []);
    groups.get(g).push(e);
  });
  const lanes = [];
  const pos = {};
  groups.forEach((evs, name) => {
    const rows = [];
    evs.forEach((e) => {
      const s = e.start_ts, en = evEnd(e);
      let placed = -1;
      for (let i = 0; i < rows.length; i++) {
        if (s >= rows[i]) { rows[i] = en; placed = i; break; }
      }
      if (placed < 0) { rows.push(en); placed = rows.length - 1; }
      pos[e.id] = { lane: lanes.length, row: placed };
    });
    lanes.push({ name, rows: Math.max(1, rows.length) });
  });
  return { lanes, pos };
}

function buildTimelineSVG(events, deps, opts) {
  // opts: {interactive, view, width, hlConflict, conflicts, selectedId}
  const { view, width } = opts;
  const conflicts = opts.conflicts || [];
  const { lanes, pos } = layoutLanes(events);
  const laneY = [];
  let y = TOP;
  lanes.forEach((l) => { laneY.push(y); y += LANE_LABEL_H + l.rows * ROW_H + LANE_PAD; });
  const height = y + 10;
  const plotW = width - LEFT - RIGHT;
  const x = (t) => LEFT + (t - view.start) * view.pxPerMs;

  const badEvents = new Set(), badDeps = new Set();
  const hlEvents = new Set(), hlDeps = new Set();
  conflicts.forEach((c) => {
    (c.event_ids || []).forEach((i) => badEvents.add(i));
    (c.dep_ids || []).forEach((i) => badDeps.add(i));
  });
  if (opts.hlConflict) {
    const c = conflicts.find((k) => k.id === opts.hlConflict);
    if (c) {
      (c.event_ids || []).forEach((i) => hlEvents.add(i));
      (c.dep_ids || []).forEach((i) => hlDeps.add(i));
    }
  }

  const P = [];
  P.push(`<svg width="${width}" height="${height}" viewBox="0 0 ${width} ${height}" ` +
    `xmlns="http://www.w3.org/2000/svg" font-family="Menlo,Consolas,monospace">`);
  P.push(`<defs>
    <marker id="m-arr" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse"><path d="M0,0 L10,5 L0,10 z" fill="#8a93a6"/></marker>
    <marker id="m-arr-red" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse"><path d="M0,0 L10,5 L0,10 z" fill="#e5484d"/></marker>
    <marker id="m-arr-hl" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse"><path d="M0,0 L10,5 L0,10 z" fill="#f0a35e"/></marker>
  </defs>`);
  P.push(`<rect class="tl-bg" x="0" y="0" width="${width}" height="${height}" fill="#0f1218"/>`);

  // 刻度
  const step = TICK_STEPS.find((s) => s * view.pxPerMs >= 90) || TICK_STEPS[TICK_STEPS.length - 1];
  const t0 = view.start, t1 = view.start + plotW / view.pxPerMs;
  for (let t = Math.ceil(t0 / step) * step; t <= t1; t += step) {
    const tx = x(t);
    P.push(`<line x1="${tx.toFixed(1)}" y1="${TOP - 8}" x2="${tx.toFixed(1)}" y2="${height - 6}" stroke="#202634" stroke-width="1"/>`);
    const lbl = fmtTs(t, S.displayTz, step < 60000).slice(11);
    P.push(`<text x="${(tx + 4).toFixed(1)}" y="${TOP - 12}" font-size="10" fill="#8a93a6">${esc(lbl)}</text>`);
  }

  // 泳道
  lanes.forEach((lane, li) => {
    const ly = laneY[li];
    const lh = LANE_LABEL_H + lane.rows * ROW_H;
    const color = groupColor(lane.name, events);
    P.push(`<rect class="lane-bg" x="4" y="${ly}" width="${width - 8}" height="${lh}" fill="#161a23" rx="6"/>`);
    P.push(`<circle cx="17" cy="${ly + 12}" r="4" fill="${color}"/>`);
    P.push(`<text x="27" y="${ly + 16}" font-size="11" fill="#c8cfdb" font-weight="bold">${esc(lane.name)}</text>`);
  });

  // 事件条
  const centers = {};
  events.forEach((e) => {
    const p = pos[e.id];
    if (!p) return;
    const ey = laneY[p.lane] + LANE_LABEL_H + p.row * ROW_H;
    const color = e.color || groupColor(e.group_name || '未分组', events);
    const cls = ['ev'];
    if (e.locked) cls.push('locked');
    if (badEvents.has(e.id)) cls.push('bad');
    if (hlEvents.has(e.id)) cls.push('hl');
    if (opts.selectedId === e.id) cls.push('sel');
    const stroke = hlEvents.has(e.id) ? '#f0a35e' : (badEvents.has(e.id) ? '#e5484d' : color);
    const sw = hlEvents.has(e.id) || badEvents.has(e.id) ? 2.2 : 1.2;
    const dash = e.confidence === 'medium' ? ' stroke-dasharray="5 3"'
      : e.confidence === 'low' ? ' stroke-dasharray="2 3"' : '';
    const label = `${esc(e.title)}${e.locked ? ' 🔒' : ''}`;
    if (e.end_ts === null || e.end_ts === undefined) {
      const cx = x(e.start_ts);
      P.push(`<g class="${cls.join(' ')}" data-id="${e.id}">` +
        `<polygon points="${cx.toFixed(1)},${ey + 2} ${(cx + 7).toFixed(1)},${ey + 9} ${cx.toFixed(1)},${ey + 16} ${(cx - 7).toFixed(1)},${ey + 9}" ` +
        `fill="${color}" stroke="${stroke}" stroke-width="${sw}"${dash}/>` +
        `<text x="${(cx + 10).toFixed(1)}" y="${ey + 13}" font-size="10.5" fill="#dfe4ee">${label}</text></g>`);
      centers[e.id] = { x1: cx, x2: cx, y: ey + 9 };
    } else {
      const x1 = x(e.start_ts), x2 = x(e.end_ts);
      const w = Math.max(3, x2 - x1);
      const handles = opts.interactive && !e.locked
        ? `<rect class="handle" data-id="${e.id}" data-side="l" x="${(x1 - 3).toFixed(1)}" y="${ey}" width="7" height="18" fill="transparent"/>` +
          `<rect class="handle" data-id="${e.id}" data-side="r" x="${(x1 + w - 4).toFixed(1)}" y="${ey}" width="7" height="18" fill="transparent"/>`
        : '';
      P.push(`<g class="${cls.join(' ')}" data-id="${e.id}">` +
        `<rect class="bar" x="${x1.toFixed(1)}" y="${ey}" width="${w.toFixed(1)}" height="18" rx="4" ` +
        `fill="${color}33" stroke="${stroke}" stroke-width="${sw}"${dash}/>${handles}` +
        `<text x="${(x1 + w + 6).toFixed(1)}" y="${ey + 13}" font-size="10.5" fill="#dfe4ee">${label}</text></g>`);
      centers[e.id] = { x1, x2: x1 + w, y: ey + 9 };
    }
  });

  // 依赖箭头
  const evs = Object.fromEntries(events.map((e) => [e.id, e]));
  deps.forEach((d) => {
    const a = evs[d.from_id], b = evs[d.to_id];
    if (!a || !b || !centers[a.id] || !centers[b.id]) return;
    const ca = centers[a.id], cb = centers[b.id];
    const x1 = ca.x2 + 2, x2 = cb.x1 - 3;
    const violated = badDeps.has(d.id);
    const hl = hlDeps.has(d.id);
    const color = hl ? '#f0a35e' : violated ? '#e5484d' : '#8a93a6';
    const marker = hl ? 'm-arr-hl' : violated ? 'm-arr-red' : 'm-arr';
    const dash = violated ? ' stroke-dasharray="4 3"' : '';
    const rel = { before: '先于', triggers: '触发', during: '包含' }[d.type] || d.type;
    P.push(`<path d="M${x1.toFixed(1)},${ca.y.toFixed(1)} C${(x1 + 36).toFixed(1)},${ca.y.toFixed(1)} ${(x2 - 36).toFixed(1)},${cb.y.toFixed(1)} ${x2.toFixed(1)},${cb.y.toFixed(1)}" ` +
      `fill="none" stroke="${color}" stroke-width="${hl ? 2.2 : 1.3}"${dash} marker-end="url(#${marker})"/>`);
    P.push(`<text x="${((x1 + x2) / 2).toFixed(1)}" y="${((ca.y + cb.y) / 2 - 4).toFixed(1)}" font-size="9" fill="${color}" text-anchor="middle">${rel}</text>`);
  });

  P.push('</svg>');
  return P.join('');
}

function renderTimeline() {
  const el = $('#timeline');
  const width = Math.max(el.clientWidth, 400);
  if (!S.events.length) {
    el.innerHTML = '<div class="empty-state">暂无事件 —— 在左侧录入,或点击「载入演示数据」</div>';
    return;
  }
  el.innerHTML = buildTimelineSVG(S.events, S.deps, {
    interactive: true, view: S.view, width,
    conflicts: S.conflicts, hlConflict: S.hlConflict, selectedId: S.selectedId,
  });
}

// 时间轴交互
(function bindTimeline() {
  const el = $('#timeline');

  el.addEventListener('wheel', (e) => {
    if (!S.events.length) return;
    e.preventDefault();
    const rect = el.getBoundingClientRect();
    const mx = e.clientX - rect.left + el.scrollLeft;
    const anchorT = S.view.start + (mx - LEFT) / S.view.pxPerMs;
    const factor = e.deltaY < 0 ? 1.25 : 0.8;
    S.view.pxPerMs = Math.min(50, Math.max(1e-6, S.view.pxPerMs * factor));
    S.view.start = anchorT - (mx - LEFT) / S.view.pxPerMs;
    renderTimeline();
  }, { passive: false });

  el.addEventListener('dblclick', (e) => {
    if (e.target.closest('.ev')) return;
    const rect = el.getBoundingClientRect();
    const t = S.view.start + (e.clientX - rect.left + el.scrollLeft - LEFT) / S.view.pxPerMs;
    startEdit(null);
    const form = $('#event-form');
    form.start.value = fmtTs(Math.round(t / 1000) * 1000, S.displayTz);
    form.timezone.value = S.displayTz;
    form.title.focus();
    toast('已在该时刻预填新事件,请补全标题');
  });

  el.addEventListener('pointerdown', (e) => {
    const handle = e.target.closest('.handle');
    const evG = e.target.closest('.ev');
    const rect = el.getBoundingClientRect();
    const startX = e.clientX - rect.left + el.scrollLeft;
    if (handle) {
      const ev = evById(+handle.dataset.id);
      if (!ev || ev.locked) return;
      S.drag = { mode: handle.dataset.side === 'l' ? 'resize-l' : 'resize-r', id: ev.id,
        startX, origStart: ev.start_ts, origEnd: evEnd(ev), moved: false };
      e.preventDefault();
      return;
    }
    if (evG) {
      const ev = evById(+evG.dataset.id);
      if (!ev) return;
      S.drag = { mode: ev.locked ? 'click' : 'move', id: ev.id, startX,
        origStart: ev.start_ts, origEnd: evEnd(ev), moved: false };
      e.preventDefault();
      return;
    }
    // 空白: 平移
    S.drag = { mode: 'pan', startX, origViewStart: S.view.start, moved: false };
    e.preventDefault();
  });

  window.addEventListener('pointermove', (e) => {
    const d = S.drag;
    if (!d) return;
    const rect = el.getBoundingClientRect();
    const curX = e.clientX - rect.left + el.scrollLeft;
    const dx = curX - d.startX;
    if (Math.abs(dx) > 3) d.moved = true;
    if (!d.moved) return;
    if (d.mode === 'pan') {
      S.view.start = d.origViewStart - dx / S.view.pxPerMs;
      renderTimeline();
      return;
    }
    const dms = dx / S.view.pxPerMs;
    const g = el.querySelector(`g.ev[data-id="${d.id}"]`);
    if (!g) return;
    g.classList.add('dragging');
    if (d.mode === 'move') {
      g.setAttribute('transform', `translate(${dx.toFixed(1)},0)`);
    } else {
      // resize: 直接改 bar 几何
      const bar = g.querySelector('.bar');
      if (!bar) return;
      let ns = d.origStart, ne = d.origEnd;
      if (d.mode === 'resize-l') ns = Math.min(d.origStart + dms, d.origEnd);
      else ne = Math.max(d.origEnd + dms, d.origStart);
      const nx1 = LEFT + (ns - S.view.start) * S.view.pxPerMs;
      const nx2 = LEFT + (ne - S.view.start) * S.view.pxPerMs;
      bar.setAttribute('x', nx1.toFixed(1));
      bar.setAttribute('width', Math.max(3, nx2 - nx1).toFixed(1));
    }
  });

  window.addEventListener('pointerup', async (e) => {
    const d = S.drag;
    S.drag = null;
    if (!d) return;
    if (d.mode === 'pan') return;
    if (d.mode === 'click' || !d.moved) {
      selectEvent(d.id);
      return;
    }
    const rect = el.getBoundingClientRect();
    const dx = (e.clientX - rect.left + el.scrollLeft) - d.startX;
    const dms = dx / S.view.pxPerMs;
    const round = (v) => Math.round(v / 1000) * 1000;
    const body = {};
    if (d.mode === 'move') {
      body.start_ts = round(d.origStart + dms);
      const ev = evById(d.id);
      body.end_ts = ev.end_ts == null ? null : round(d.origEnd + dms);
    } else if (d.mode === 'resize-l') {
      body.start_ts = round(Math.min(d.origStart + dms, d.origEnd));
    } else if (d.mode === 'resize-r') {
      body.end_ts = round(Math.max(d.origEnd + dms, d.origStart));
    }
    try {
      applyState(await api(`/api/events/${d.id}`, 'PUT', body));
      toast('已更新,冲突已重新计算');
    } catch (ex) { toast(ex.message, true); refresh(); }
  });
})();

$('#zoom-in').onclick = () => { S.view.pxPerMs = Math.min(50, S.view.pxPerMs * 1.5); renderTimeline(); };
$('#zoom-out').onclick = () => { S.view.pxPerMs = Math.max(1e-6, S.view.pxPerMs / 1.5); renderTimeline(); };
$('#zoom-fit').onclick = () => { fitView(); renderTimeline(); };
$('#display-tz').onchange = (e) => { S.displayTz = e.target.value; renderTimeline(); };
window.addEventListener('resize', () => renderTimeline());

// ---------------------------------------------------------------- 事件表单与列表

function startEdit(id) {
  S.editingId = id;
  const form = $('#event-form');
  $('#form-title').textContent = id ? `编辑事件 #${id}` : '新增事件';
  $('#form-cancel').hidden = !id;
  $('#form-delete').hidden = !id;
  $('#form-submit').textContent = id ? '保存修改' : '保存事件';
  if (!id) { form.reset(); form.timezone.value = S.displayTz; return; }
  const ev = evById(id);
  if (!ev) return;
  form.title.value = ev.title;
  form.start.value = fmtTs(ev.start_ts, ev.timezone);
  form.end.value = ev.end_ts == null ? '' : fmtTs(ev.end_ts, ev.timezone);
  form.timezone.value = ev.timezone || 'UTC';
  form.confidence.value = ev.confidence || 'medium';
  form.group_name.value = ev.group_name || '';
  form.source.value = ev.source || '';
  form.color.value = ev.color || '#5b8def';
  form.evidence.value = ev.evidence || '';
  form.locked.checked = !!ev.locked;
}

function selectEvent(id) {
  S.selectedId = id;
  startEdit(id);
  renderTimeline();
  renderEventList();
}

$('#form-cancel').onclick = () => { S.selectedId = null; startEdit(null); renderTimeline(); renderEventList(); };

$('#form-delete').onclick = async () => {
  if (!S.editingId) return;
  if (!confirm('确认删除该事件?相关依赖与互斥也会删除。')) return;
  try {
    applyState(await api(`/api/events/${S.editingId}`, 'DELETE'));
    S.selectedId = null; S.editingId = null;
    startEdit(null);
    toast('事件已删除');
  } catch (ex) { toast(ex.message, true); }
};

$('#event-form').onsubmit = async (e) => {
  e.preventDefault();
  const form = e.target;
  const body = {
    title: form.title.value.trim(),
    start: form.start.value.trim(),
    end: form.end.value.trim() || null,
    timezone: form.timezone.value.trim() || 'UTC',
    confidence: form.confidence.value,
    group_name: form.group_name.value.trim(),
    source: form.source.value.trim(),
    color: form.color.value,
    evidence: form.evidence.value.trim(),
    locked: form.locked.checked,
  };
  try {
    if (S.editingId) {
      applyState(await api(`/api/events/${S.editingId}`, 'PUT', body));
      toast('事件已更新');
    } else {
      applyState(await api('/api/events', 'POST', body));
      toast('事件已创建');
    }
    S.selectedId = null;
    startEdit(null);
  } catch (ex) { toast(ex.message, true); }
};

function renderEventList() {
  const el = $('#event-list');
  if (!S.events.length) {
    el.innerHTML = '<div class="empty-state">暂无事件</div>';
    return;
  }
  const badIds = new Set();
  S.conflicts.forEach((c) => (c.event_ids || []).forEach((i) => badIds.add(i)));
  el.innerHTML = S.events.map((e) => {
    const color = e.color || groupColor(e.group_name || '未分组');
    return `<div class="event-row ${S.selectedId === e.id ? 'selected' : ''}" data-id="${e.id}">
      <span class="dot" style="background:${color}"></span>
      <span class="time">${esc(fmtTs(e.start_ts, e.timezone, false).slice(5, 16))}</span>
      <span class="title">${esc(e.title)}</span>
      ${badIds.has(e.id) ? '<span class="bad">⚠</span>' : ''}
      ${e.locked ? '<span class="lock">🔒</span>' : ''}
    </div>`;
  }).join('');
  el.querySelectorAll('.event-row').forEach((row) => {
    row.onclick = () => selectEvent(+row.dataset.id);
  });
}

// ---------------------------------------------------------------- 批量导入

$('#btn-bulk').onclick = async () => {
  const text = $('#bulk-text').value.trim();
  if (!text) { toast('请先粘贴内容', true); return; }
  try {
    const data = await api('/api/events/bulk', 'POST', { text, default_tz: $('#bulk-tz').value || 'UTC' });
    applyState(data);
    const b = data.bulk;
    let htmlStr = `<div class="ok-text">成功导入 ${b.created} 条</div>`;
    if (b.errors.length) {
      htmlStr += b.errors.map((er) =>
        `<div class="err">第 ${er.line} 行: ${esc(er.error)}</div>`).join('');
    }
    $('#bulk-result').innerHTML = htmlStr;
    if (b.created) { $('#bulk-text').value = ''; toast(`导入 ${b.created} 条事件`); }
  } catch (ex) { toast(ex.message, true); }
};

// ---------------------------------------------------------------- 冲突面板

function chainHtml(chain) {
  return '<div class="chain">' + (chain || []).map((st) => {
    if (st.kind === 'event') {
      const t = st.start_ts != null ? fmtTs(st.start_ts, st.timezone || S.displayTz) : '';
      return `<span class="chip">${esc(st.title)}<span class="chip-time">${esc(t)}</span></span>`;
    }
    return `<span class="chip edge-chip">—[${esc(st.label)}]→</span>`;
  }).join('') + '</div>';
}

function evidenceHtml(evidence) {
  return (evidence || []).map((ev) => `
    <div class="evidence-item">
      <b>${esc(ev.title)}</b><span class="conf-badge conf-${esc(ev.confidence)}">置信度 ${esc({ high: '高', medium: '中', low: '低' }[ev.confidence] || ev.confidence)}</span>
      <div class="src">来源: ${esc(ev.source)} · ${esc(fmtTs(ev.start_ts, ev.timezone))}</div>
      ${ev.evidence ? `<blockquote>${esc(ev.evidence)}</blockquote>` : ''}
    </div>`).join('');
}

function renderConflicts() {
  const sum = $('#conflict-summary');
  if (!S.conflicts.length) {
    sum.innerHTML = S.events.length ? '<span class="ok-text">✓ 未检测到冲突,时间线自洽</span>' : '';
    $('#conflict-list').innerHTML = '';
    return;
  }
  const byType = {};
  S.conflicts.forEach((c) => { byType[c.type_label] = (byType[c.type_label] || 0) + 1; });
  sum.innerHTML = Object.entries(byType).map(([k, v]) => `${k} × ${v}`).join(' · ');
  $('#conflict-list').innerHTML = S.conflicts.map((c) => `
    <div class="conflict-card sev-${esc(c.severity)} ${S.hlConflict === c.id ? 'hl' : ''}" data-id="${esc(c.id)}">
      <div class="head"><span class="type-badge">${esc(c.type_label)}</span>
        <span class="sev-badge sev-${esc(c.severity)}">● ${esc(c.severity_label)}</span></div>
      <h3>${esc(c.title)}</h3>
      <p>${esc(c.detail)}</p>
      <div class="muted">最短冲突链</div>
      ${chainHtml(c.chain)}
      <div class="muted">涉及证据</div>
      ${evidenceHtml(c.evidence)}
      <div class="card-actions">
        <button class="btn sm" data-act="locate" data-id="${esc(c.id)}">在时间轴上定位</button>
      </div>
    </div>`).join('');
  $('#conflict-list').querySelectorAll('.conflict-card').forEach((card) => {
    card.onclick = (e) => {
      if (e.target.dataset.act) return;
      S.hlConflict = S.hlConflict === card.dataset.id ? null : card.dataset.id;
      renderTimeline();
      renderConflicts();
    };
    card.querySelector('[data-act="locate"]').onclick = () => {
      const c = S.conflicts.find((k) => k.id === card.dataset.id);
      if (!c) return;
      S.hlConflict = c.id;
      const ts = [];
      (c.chain || []).forEach((st) => {
        if (st.kind === 'event' && st.start_ts != null) {
          ts.push(st.start_ts, st.end_ts != null ? st.end_ts : st.start_ts);
        }
      });
      if (ts.length) {
        const t0 = Math.min(...ts), t1 = Math.max(...ts);
        const span = Math.max(t1 - t0, 30000);
        const plotW = timelineWidth() - LEFT - RIGHT;
        S.view.pxPerMs = plotW / (span * 2.2);
        S.view.start = t0 - span * 0.6;
      }
      renderTimeline();
      renderConflicts();
    };
  });
}

// ---------------------------------------------------------------- 调整方案

$('#btn-suggest').onclick = async () => {
  try {
    const data = await api('/api/suggestions', 'POST', {});
    renderPlans(data.plans);
  } catch (ex) { toast(ex.message, true); }
};

function renderPlans(plans) {
  const el = $('#plan-list');
  if (!plans || !plans.length) {
    el.innerHTML = '<div class="empty-state">没有可生成的方案</div>';
    return;
  }
  el.innerHTML = plans.map((p, i) => {
    const deltaCell = (v) => v === 0 ? '<span class="muted">—</span>'
      : `<span class="${v > 0 ? 'delta-pos' : 'delta-neg'}">${v > 0 ? '+' : '−'}${esc(fmtDur(Math.abs(v)))}</span>`;
    const moves = p.moves.length ? `<table>
      <thead><tr><th>事件</th><th>新开始</th><th>开始移动</th><th>结束移动</th></tr></thead>
      <tbody>${p.moves.map((m) => `<tr>
        <td>${esc(m.title)}</td>
        <td>${esc(fmtTs(m.to_start, S.displayTz))}</td>
        <td>${deltaCell(m.delta_start_ms ?? m.delta_ms ?? 0)}</td>
        <td>${deltaCell(m.delta_end_ms ?? 0)}</td>
      </tr>`).join('')}</tbody></table>` : '<p class="muted">无需移动任何事件。</p>';
    const un = p.unresolved.length
      ? `<div class="unresolved">仍未解决 ${p.unresolved.length} 项:<ul>${p.unresolved.map((u) => `<li>${esc(u.title)}</li>`).join('')}</ul></div>`
      : '<p class="ok-text">✓ 应用后全部冲突可解决</p>';
    return `<div class="plan-card">
      <h3>${esc(p.name)}${p.duplicate ? ' <span class="muted">(与其他方案相同)</span>' : ''}</h3>
      <div class="muted">${esc(p.description)} · 解决 ${p.resolved} 项 · 总移动量 ${esc(fmtDur(p.total_shift_ms))}</div>
      ${moves}${un}
      <button class="btn primary block" data-strategy="${esc(p.strategy)}" ${p.moves.length ? '' : 'disabled'}>应用此方案</button>
    </div>`;
  }).join('');
  el.querySelectorAll('button[data-strategy]').forEach((btn) => {
    btn.onclick = async () => {
      if (!confirm('应用该方案将移动上述事件(锁定事件不受影响),可撤销。继续?')) return;
      try {
        applyState(await api('/api/apply_plan', 'POST', { strategy: btn.dataset.strategy }));
        toast('方案已应用,冲突已重新计算');
        $('#plan-list').innerHTML = '';
      } catch (ex) { toast(ex.message, true); }
    };
  });
}

// ---------------------------------------------------------------- 依赖与互斥

function renderDeps() {
  const opts = S.events.map((e) =>
    `<option value="${e.id}">#${e.id} ${esc(e.title)}</option>`).join('');
  ['#dep-from', '#dep-to', '#excl-a', '#excl-b'].forEach((sel) => {
    const el = $(sel);
    const cur = el.value;
    el.innerHTML = opts;
    el.value = cur;
  });
  const violated = new Set();
  S.conflicts.forEach((c) => (c.dep_ids || []).forEach((i) => violated.add(i)));
  const relLabel = { before: '先于', triggers: '触发', during: '持续期间' };
  const rows = S.deps.map((d) => {
    const a = evById(d.from_id), b = evById(d.to_id);
    if (!a || !b) return '';
    const gaps = [];
    if (d.min_gap_ms) gaps.push(`≥${fmtDur(d.min_gap_ms)}`);
    if (d.max_gap_ms != null) gaps.push(`≤${fmtDur(d.max_gap_ms)}`);
    return `<div class="dep-row ${violated.has(d.id) ? 'violated' : ''}">
      <span>${esc(a.title)}</span><span class="rel">—[${relLabel[d.type]}${gaps.length ? ' ' + gaps.join(' ') : ''}]→</span><span>${esc(b.title)}</span>
      ${d.note ? `<span class="note">${esc(d.note)}</span>` : ''}
      <button class="del" data-kind="dep" data-id="${d.id}" title="删除">×</button></div>`;
  });
  S.excls.forEach((x) => {
    const a = evById(x.a_id), b = evById(x.b_id);
    if (!a || !b) return;
    rows.push(`<div class="dep-row ${violated.has(x.id) ? 'violated' : ''}">
      <span>${esc(a.title)}</span><span class="rel">⇔ 互斥</span><span>${esc(b.title)}</span>
      ${x.reason ? `<span class="note">${esc(x.reason)}</span>` : ''}
      <button class="del" data-kind="excl" data-id="${x.id}" title="删除">×</button></div>`);
  });
  $('#dep-list').innerHTML = rows.join('') || '<div class="empty-state">暂无依赖或互斥</div>';
  $('#dep-list').querySelectorAll('.del').forEach((btn) => {
    btn.onclick = async () => {
      const url = btn.dataset.kind === 'dep' ? `/api/dependencies/${btn.dataset.id}` : `/api/exclusions/${btn.dataset.id}`;
      try {
        applyState(await api(url, 'DELETE'));
        toast('已删除');
      } catch (ex) { toast(ex.message, true); }
    };
  });
}

$('#btn-add-dep').onclick = async () => {
  const body = {
    type: $('#dep-type').value,
    from_id: +$('#dep-from').value,
    to_id: +$('#dep-to').value,
    min_gap_ms: Math.round((+$('#dep-min').value || 0) * 1000),
    max_gap_ms: $('#dep-max').value === '' ? null : Math.round(+$('#dep-max').value * 1000),
    note: $('#dep-note').value.trim(),
  };
  if (!body.from_id || !body.to_id) { toast('请先创建事件', true); return; }
  try {
    applyState(await api('/api/dependencies', 'POST', body));
    $('#dep-note').value = '';
    toast('依赖已添加');
  } catch (ex) { toast(ex.message, true); }
};

$('#btn-add-excl').onclick = async () => {
  const body = {
    a_id: +$('#excl-a').value,
    b_id: +$('#excl-b').value,
    reason: $('#excl-reason').value.trim(),
  };
  if (!body.a_id || !body.b_id) { toast('请先创建事件', true); return; }
  try {
    applyState(await api('/api/exclusions', 'POST', body));
    $('#excl-reason').value = '';
    toast('互斥已添加');
  } catch (ex) { toast(ex.message, true); }
};

// ---------------------------------------------------------------- 版本对比

$('#btn-compare').onclick = async () => {
  try {
    const data = await api('/api/compare');
    renderCompare(data);
  } catch (ex) { toast(ex.message, true); }
};

$('#btn-baseline').onclick = async () => {
  try {
    applyState(await api('/api/baseline', 'POST', {}));
    toast('已将当前状态设为对比基准');
  } catch (ex) { toast(ex.message, true); }
};

function miniTimeline(events, label) {
  if (!events.length) return '<div class="empty-state">空</div>';
  const t0 = Math.min(...events.map((e) => e.start_ts));
  const t1 = Math.max(...events.map(evEnd));
  const span = Math.max(t1 - t0, 60000);
  const width = 340;
  const view = { start: t0 - span * 0.06, pxPerMs: (width - LEFT - RIGHT) / (span * 1.12) };
  return `<h4>${esc(label)}</h4>` + buildTimelineSVG(events, [], {
    interactive: false, view, width, conflicts: [],
  });
}

function renderCompare(data) {
  const el = $('#compare-result');
  if (!data.has_baseline) {
    el.innerHTML = '<div class="empty-state">还没有基准版本。首次变更后会自动建立。</div>';
    return;
  }
  const d = data.diff;
  const rows = d.items.map((it) => {
    if (it.change === 'added') {
      return `<tr><td>${esc(it.title)}</td><td class="chg-added">新增</td><td>—</td><td>—</td></tr>`;
    }
    if (it.change === 'removed') {
      return `<tr><td>${esc(it.title)}</td><td class="chg-removed">已删除</td><td>—</td><td>—</td></tr>`;
    }
    return `<tr><td>${esc(it.title)}</td><td class="chg-moved">移动 ${it.delta_ms >= 0 ? '+' : '−'}${esc(fmtDur(Math.abs(it.delta_ms)))}</td>
      <td>${esc(fmtTs(it.from_start, S.displayTz))}</td><td>${esc(fmtTs(it.to_start, S.displayTz))}</td></tr>`;
  }).join('');
  el.innerHTML = `
    <div class="muted">基准: ${esc(data.baseline_label)} · 未变化事件 ${d.unchanged} 个</div>
    ${d.items.length ? `<table class="diff-table">
      <thead><tr><th>事件</th><th>变化</th><th>原开始</th><th>现开始</th></tr></thead>
      <tbody>${rows}</tbody></table>` : '<p class="ok-text">与基准完全一致</p>'}
    <div class="compare-cols">
      <div>${miniTimeline(data.baseline_events, '原始版本')}</div>
      <div>${miniTimeline(data.current_events, '当前版本')}</div>
    </div>`;
}

// ---------------------------------------------------------------- 头部操作

$('#btn-undo').onclick = async () => {
  try {
    const data = await api('/api/undo', 'POST', {});
    applyState(data);
    toast(`已撤销: ${data.undone}`);
  } catch (ex) { toast(ex.message, true); }
};

document.addEventListener('keydown', (e) => {
  if ((e.ctrlKey || e.metaKey) && e.key === 'z' && !/INPUT|TEXTAREA|SELECT/.test(e.target.tagName)) {
    e.preventDefault();
    $('#btn-undo').click();
  }
});

$('#btn-seed').onclick = async () => {
  if (!confirm('载入演示数据将替换当前全部数据(可撤销),继续?')) return;
  try {
    applyState(await api('/api/seed', 'POST', {}));
    S.fittedOnce = false;
    fitView(); S.fittedOnce = true;
    renderAll();
    toast('演示数据已载入,包含多种预设冲突');
  } catch (ex) { toast(ex.message, true); }
};

$('#btn-reset').onclick = async () => {
  if (!confirm('确认清空全部事件、依赖与互斥?(可撤销)')) return;
  try {
    applyState(await api('/api/reset', 'POST', {}));
    toast('已清空');
  } catch (ex) { toast(ex.message, true); }
};

$('#btn-export').onclick = () => {
  window.location = `/api/export?tz=${encodeURIComponent(S.displayTz)}`;
};

// tabs
document.querySelectorAll('.tab').forEach((tab) => {
  tab.onclick = () => {
    document.querySelectorAll('.tab').forEach((t) => t.classList.remove('active'));
    document.querySelectorAll('.tab-page').forEach((p) => p.classList.remove('active'));
    tab.classList.add('active');
    $(`#tab-${tab.dataset.tab}`).classList.add('active');
  };
});

// ---------------------------------------------------------------- 启动

refresh().catch((ex) => toast(ex.message, true));
