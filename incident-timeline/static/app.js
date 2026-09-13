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
  cal: null,             // 时钟校准状态(/api/state.calibration)
  timeMode: 'raw',       // raw | calibrated
  showBands: true,
  showGhost: true,
  candidates: null,      // 最近生成的候选方案
  tracePointIds: null,   // 异常追溯: 高亮的校准点
  warnedStale: false,
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
  S.cal = data.calibration || null;
  if (!S.fittedOnce && S.events.length) {
    fitView();
    S.fittedOnce = true;
  }
  renderAll();
  maybeWarnStale();
  return data;
}

// 当前显示用事件: 校准模式下用校准后时间(浅拷贝), 否则原始事件
function activeCalParams() {
  if (!S.cal) return null;
  if (S.cal.active_version && !S.cal.active_version_stale) {
    const m = {};
    S.cal.active_version.params.forEach((p) => { m[p.source_id] = p; });
    return { params: m, t0: S.cal.active_version.t0,
             baseId: S.cal.active_version.params.find((p) => p.is_baseline)?.source_id };
  }
  if (S.cal.live_fit) {
    const m = {};
    S.cal.live_fit.params.forEach((p) => { m[p.source_id] = p; });
    return { params: m, t0: S.cal.live_fit.t0, baseId: S.cal.live_fit.base_id };
  }
  return null;
}

function calibratedMs(ms, sid, cp) {
  if (sid == null || !cp) return ms;
  const p = cp.params[sid];
  if (!p) return ms;
  return ms + p.correction_ms + p.drift_ms_per_hour * (ms - cp.t0) / 3600000;
}

function calViewEvents() {
  const cp = activeCalParams();
  if (!cp) return S.events;
  return S.events.map((e) => {
    if (e.clock_source_id == null || !cp.params[e.clock_source_id]) return e;
    const v = { ...e };
    v.start_ts = Math.round(calibratedMs(e.start_ts, e.clock_source_id, cp));
    if (e.end_ts != null) v.end_ts = Math.round(calibratedMs(e.end_ts, e.clock_source_id, cp));
    return v;
  });
}

function displayEvents() {
  if (S.timeMode !== 'calibrated' || !S.cal) return S.events;
  return S.cal.calibrated_analysis ? calViewEvents() : S.events;
}

function currentConflicts() {
  if (S.timeMode === 'calibrated' && S.cal && S.cal.calibrated_analysis) {
    return S.cal.calibrated_analysis.conflicts;
  }
  return S.conflicts;
}

function clockIssueCount() {
  if (!S.cal) return 0;
  const issues = (S.cal.issues || []).length;
  const badPoints = (S.cal.point_diagnostics || [])
    .filter((p) => p.status === 'contradictory' || p.status === 'dangling'
      || p.status === 'same_source' || p.status === 'duplicate').length;
  return issues + badPoints;
}

function maybeWarnStale() {
  if (S.cal && S.cal.active_version_stale && !S.warnedStale) {
    S.warnedStale = true;
    toast('当前校准版本已过期: 校准关系发生了变化, 正在显示实时估计', true);
  }
  if (S.cal && !S.cal.active_version_stale) S.warnedStale = false;
}

function renderAll() {
  $('#btn-undo').disabled = !S.canUndo;
  const n = S.conflicts.length;
  const badge = $('#conflict-badge');
  badge.textContent = n;
  badge.classList.toggle('zero', n === 0);
  const calN = clockIssueCount();
  const cb = $('#clock-badge');
  cb.textContent = calN;
  cb.classList.toggle('zero', calN === 0);
  $('#event-count').textContent = `(${S.events.length})`;
  renderTimeline();
  renderEventList();
  renderConflicts();
  renderDeps();
  renderClockPanel();
  renderVersionPill();
}

// ---------------------------------------------------------------- 时间轴

const ROW_H = 26, LANE_LABEL_H = 20, LANE_PAD = 10, TOP = 36, LEFT = 150, RIGHT = 24;
const TICK_STEPS = [1000, 5000, 15000, 30000, 60000, 300000, 900000, 1800000,
  3600000, 7200000, 21600000, 43200000, 86400000, 604800000];

function timelineWidth() {
  return Math.max($('#timeline').clientWidth, 400);
}

function fitView() {
  const evs = displayEvents();
  if (!evs.length) return;
  const t0 = Math.min(...evs.map((e) => e.start_ts));
  const t1 = Math.max(...evs.map(evEnd));
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
  // opts: {interactive, view, width, hlConflict, conflicts, selectedId,
  //        mode, rawEvents(原始时间副本), showBands, showGhost, bandsById,
  //        traceEventIds, resolvedDeps:Set, newDeps:Set}
  const { view, width } = opts;
  const conflicts = opts.conflicts || [];
  const { lanes, pos } = layoutLanes(events);
  const laneY = [];
  let y = TOP;
  lanes.forEach((l) => { laneY.push(y); y += LANE_LABEL_H + l.rows * ROW_H + LANE_PAD; });
  const height = y + 10;
  const plotW = width - LEFT - RIGHT;
  const x = (t) => LEFT + (t - view.start) * view.pxPerMs;
  const calibrated = opts.mode === 'calibrated';

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
  const traceIds = opts.traceEventIds || new Set();
  const rawById = {};
  (opts.rawEvents || []).forEach((e) => { rawById[e.id] = e; });
  const bandsById = opts.bandsById || {};

  const P = [];
  P.push(`<svg width="${width}" height="${height}" viewBox="0 0 ${width} ${height}" ` +
    `xmlns="http://www.w3.org/2000/svg" font-family="Menlo,Consolas,monospace">`);
  P.push(`<defs>
    <marker id="m-arr" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse"><path d="M0,0 L10,5 L0,10 z" fill="#8a93a6"/></marker>
    <marker id="m-arr-red" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse"><path d="M0,0 L10,5 L0,10 z" fill="#e5484d"/></marker>
    <marker id="m-arr-hl" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse"><path d="M0,0 L10,5 L0,10 z" fill="#f0a35e"/></marker>
    <marker id="m-arr-green" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse"><path d="M0,0 L10,5 L0,10 z" fill="#5ec49a"/></marker>
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

  // 误差带(校准模式): 每个有归属来源的事件, 在其校准位置画半透明带
  if (calibrated && opts.showBands) {
    events.forEach((e) => {
      if (e.clock_source_id == null) return;
      const p = pos[e.id];
      if (!p) return;
      const band = bandsById[e.clock_source_id];
      if (band == null || band <= 0) return;
      const ey = laneY[p.lane] + LANE_LABEL_H + p.row * ROW_H;
      const half = band * view.pxPerMs;
      const cx = x(e.start_ts);
      if (e.end_ts == null) {
        P.push(`<polygon class="band" points="${(cx - half).toFixed(1)},${ey + 1} ${cx.toFixed(1)},${ey + 9} ${(cx - half).toFixed(1)},${ey + 17} ${(cx - half - 5).toFixed(1)},${ey + 9}"/>`);
        P.push(`<polygon class="band" points="${(cx + half).toFixed(1)},${ey + 1} ${(cx + half + 5).toFixed(1)},${ey + 9} ${(cx + half).toFixed(1)},${ey + 17} ${cx.toFixed(1)},${ey + 9}"/>`);
      } else {
        P.push(`<rect class="band" x="${(x(e.start_ts) - half).toFixed(1)}" y="${ey - 1}" `
          + `width="${Math.max(2, (e.end_ts - e.start_ts) * view.pxPerMs + 2 * half).toFixed(1)}" height="20" rx="5"/>`);
      }
    });
  }

  // 校准前原始位置虚影(校准模式)
  if (calibrated && opts.showGhost) {
    events.forEach((e) => {
      const raw = rawById[e.id];
      if (!raw || (raw.start_ts === e.start_ts && raw.end_ts === e.end_ts)) return;
      const p = pos[e.id];
      if (!p) return;
      const ey = laneY[p.lane] + LANE_LABEL_H + p.row * ROW_H;
      if (raw.end_ts == null) {
        const cx = x(raw.start_ts);
        P.push(`<polygon class="ghost-poly" points="${cx.toFixed(1)},${ey + 2} ${(cx + 7).toFixed(1)},${ey + 9} ${cx.toFixed(1)},${ey + 16} ${(cx - 7).toFixed(1)},${ey + 9}"/>`);
      } else {
        const x1 = x(raw.start_ts), x2 = x(raw.end_ts);
        P.push(`<rect class="ghost" x="${x1.toFixed(1)}" y="${ey}" width="${Math.max(3, x2 - x1).toFixed(1)}" height="18" rx="4"/>`);
      }
    });
  }

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
    if (calibrated) cls.push('calibrated');
    if (traceIds.has(e.id)) cls.push('trace');
    let stroke = hlEvents.has(e.id) || traceIds.has(e.id) ? '#f0a35e'
      : (badEvents.has(e.id) ? '#e5484d' : color);
    if (calibrated && !badEvents.has(e.id) && e.clock_source_id != null
        && shiftForEvent(e) !== 0) stroke = '#5ec49a';
    const sw = (hlEvents.has(e.id) || badEvents.has(e.id) || traceIds.size) ? 2.2 : 1.2;
    const dash = e.confidence === 'medium' ? ' stroke-dasharray="5 3"'
      : e.confidence === 'low' ? ' stroke-dasharray="2 3"' : '';
    const tag = calibrated && e.clock_source_id != null && shiftForEvent(e) !== 0 ? ' ⌚' : '';
    const label = `${esc(e.title)}${e.locked ? ' 🔒' : ''}${tag}`;
    // 校准模式不允许拖动(原始时间不可改写)
    const editable = opts.interactive && !e.locked && !calibrated;
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
      const handles = editable
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
    const resolved = calibrated && (opts.resolvedDeps || new Set()).has(d.id);
    let color = '#8a93a6', marker = 'm-arr', dash = '';
    if (hl) { color = '#f0a35e'; marker = 'm-arr-hl'; }
    else if (violated) { color = '#e5484d'; marker = 'm-arr-red'; dash = ' stroke-dasharray="4 3"'; }
    else if (resolved) { color = '#5ec49a'; marker = 'm-arr-green'; dash = ' stroke-dasharray="6 3"'; }
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
  const calibrated = S.timeMode === 'calibrated' && S.cal && S.cal.live_fit;
  const evs = calibrated ? calViewEvents() : S.events;
  let conflicts = S.conflicts;
  const resolvedDeps = new Set();
  if (calibrated) {
    conflicts = S.cal.calibrated_analysis.conflicts;
    // 原始冲突依赖中, 校准后不再冲突的 = 被时钟校准消除
    const calDepBad = new Set();
    conflicts.forEach((c) => (c.dep_ids || []).forEach((i) => calDepBad.add(i)));
    S.conflicts.forEach((c) => (c.dep_ids || []).forEach((i) => {
      if (!calDepBad.has(i)) resolvedDeps.add(i);
    }));
  }
  // 追溯校准点 -> 相关事件
  const traceIds = new Set();
  if (S.tracePointIds && S.cal) {
    S.cal.points.filter((p) => S.tracePointIds.has(p.id)).forEach((p) => {
      traceIds.add(p.a_event_id); traceIds.add(p.b_event_id);
    });
  }
  const bandsById = {};
  if (S.cal) Object.entries(S.cal.bands_ms).forEach(([k, v]) => { bandsById[k] = v; });
  el.innerHTML = buildTimelineSVG(evs, S.deps, {
    interactive: true, view: S.view, width,
    conflicts, hlConflict: S.hlConflict, selectedId: S.selectedId,
    mode: S.timeMode, rawEvents: S.events,
    showBands: S.showBands, showGhost: S.showGhost, bandsById,
    traceEventIds: traceIds, resolvedDeps,
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
    if (S.timeMode === 'calibrated') {
      toast('校准视图下时间由时钟参数推导, 请切回原始时间新建/改动事件', true);
      return;
    }
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
    const calibrated = S.timeMode === 'calibrated';
    if (handle) {
      if (calibrated) return;
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
      // 校准模式不允许拖动改写原始时间, 只做选中
      S.drag = { mode: (ev.locked || calibrated) ? 'click' : 'move', id: ev.id, startX,
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
  populateClockSourceSelects();
  S.editingId = id;
  const form = $('#event-form');
  $('#form-title').textContent = id ? `编辑事件 #${id}` : '新增事件';
  $('#form-cancel').hidden = !id;
  $('#form-delete').hidden = !id;
  $('#form-submit').textContent = id ? '保存修改' : '保存事件';
  if (!id) {
    form.reset();
    form.timezone.value = S.displayTz;
    form.clock_source_id.value = '';
    return;
  }
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
  form.clock_source_id.value = ev.clock_source_id == null ? '' : ev.clock_source_id;
}

function populateClockSourceSelects() {
  const opts = '<option value="">— 不参与时钟校准 —</option>' +
    (S.cal ? S.cal.sources.map((s) =>
      `<option value="${s.id}">${esc(s.name)}${s.is_baseline ? ' ★基准' : ''}</option>`).join('') : '');
  const sel = $('#event-clock-source');
  if (sel) { const cur = sel.value; sel.innerHTML = opts; sel.value = cur; }
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
    clock_source_id: form.clock_source_id.value ? +form.clock_source_id.value : null,
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

function conflictByIdentity(list, c) {
  return list.find((k) =>
    (c.dep_ids || []).slice().sort().join(',') === (k.dep_ids || []).slice().sort().join(',')
    && (c.event_ids || []).slice().sort().join(',') === (k.event_ids || []).slice().sort().join(',')
    && c.type === k.type);
}

function renderConflicts() {
  const sum = $('#conflict-summary');
  const calibrated = S.timeMode === 'calibrated' && S.cal && S.cal.calibrated_analysis;
  const list = calibrated ? S.cal.calibrated_analysis.conflicts : S.conflicts;
  // 校准模式下, 标记每条冲突是否在校准后消失(被时钟校准解决)
  const calIds = new Set();
  if (calibrated) {
    (S.cal.calibrated_analysis.conflicts || []).forEach((c) =>
      (c.dep_ids || []).forEach((i) => calIds.add(i)));
  }
  if (!list.length) {
    sum.innerHTML = calibrated
      ? '<span class="ok-text">✓ 校准后这些时钟相关冲突已消除</span>'
      : (S.events.length ? '<span class="ok-text">✓ 未检测到冲突,时间线自洽</span>' : '');
    $('#conflict-list').innerHTML = calibrated && S.conflicts.length
      ? S.conflicts.map((c) => resolvedCard(c)).join('') : '';
    bindResolvedCards();
    return;
  }
  const byType = {};
  list.forEach((c) => { byType[c.type_label] = (byType[c.type_label] || 0) + 1; });
  sum.innerHTML = (calibrated ? '校准时间 · ' : '')
    + Object.entries(byType).map(([k, v]) => `${k} × ${v}`).join(' · ');
  $('#conflict-list').innerHTML = list.map((c) => {
    // 是否能追溯到校准点
    const tps = tracePointsForConflict(c);
    const traceBtn = tps.length
      ? `<button class="btn sm" data-act="trace" data-points="${tps.join(',')}">追溯校准点 (#${tps.join(', #')})</button>`
      : '';
    return `
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
        ${traceBtn}
      </div>
    </div>`;
  }).join('');
  $('#conflict-list').querySelectorAll('.conflict-card').forEach((card) => {
    card.onclick = (e) => {
      if (e.target.dataset.act) return;
      S.hlConflict = S.hlConflict === card.dataset.id ? null : card.dataset.id;
      renderTimeline();
      renderConflicts();
    };
    card.querySelector('[data-act="locate"]').onclick = () => {
      const c = list.find((k) => k.id === card.dataset.id);
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
    card.querySelector('[data-act="trace"]')?.addEventListener('click', () => {
      const ids = card.querySelector('[data-act="trace"]').dataset.points.split(',').map(Number);
      tracePoints(ids);
    });
  });
}

// 与冲突事件相关的校准点
function tracePointsForConflict(c) {
  if (!S.cal) return [];
  const evIds = new Set(c.event_ids || []);
  return S.cal.points
    .filter((p) => evIds.has(p.a_event_id) || evIds.has(p.b_event_id))
    .map((p) => p.id);
}

function tracePoints(ids) {
  S.tracePointIds = new Set(ids);
  if (S.timeMode !== 'calibrated') {
    S.timeMode = 'calibrated';
    document.querySelectorAll('#time-toggle .seg').forEach((s) =>
      s.classList.toggle('active', s.dataset.mode === 'calibrated'));
  }
  fitView();
  renderTimeline();
  toast(`已追溯 ${ids.length} 个校准点, 相关事件在时间轴上高亮`);
}

function resolvedCard(c) {
  return `<div class="conflict-card sev-${esc(c.severity)}" style="opacity:.55;border-left-color:var(--green)">
    <div class="head"><span class="type-badge">${esc(c.type_label)}</span>
      <span style="color:var(--green)">✓ 校准后消除</span></div>
    <h3>${esc(c.title)}</h3>
    <div class="card-actions">
      <button class="btn sm" data-act="locate-resolved" data-id="${esc(c.id)}">在时间轴上定位</button>
    </div></div>`;
}

function bindResolvedCards() {
  $('#conflict-list').querySelectorAll('[data-act="locate-resolved"]').forEach((btn) => {
    btn.onclick = () => {
      const c = S.conflicts.find((k) => k.id === btn.dataset.id);
      if (!c) return;
      const ts = [];
      (c.chain || []).forEach((st) => {
        if (st.kind === 'event' && st.start_ts != null) ts.push(st.start_ts);
      });
      if (ts.length) {
        const t0 = Math.min(...ts), t1 = Math.max(...ts);
        const span = Math.max(t1 - t0, 30000);
        const plotW = timelineWidth() - LEFT - RIGHT;
        S.view.pxPerMs = plotW / (span * 2.2);
        S.view.start = t0 - span * 0.6;
      }
      renderTimeline();
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

// ---------------------------------------------------------------- 时间模式 / 误差带

document.querySelectorAll('#time-toggle .seg').forEach((seg) => {
  seg.onclick = () => {
    const mode = seg.dataset.mode;
    if (mode === S.timeMode) return;
    S.timeMode = mode;
    document.querySelectorAll('#time-toggle .seg').forEach((s) => s.classList.toggle('active', s === seg));
    if (mode === 'calibrated' && (!S.cal || !S.cal.live_fit)) {
      toast('尚未估计出时钟参数, 请先在「时钟校准」页建立来源与配点', true);
    }
    fitView();
    renderTimeline();
  };
});
$('#show-bands').onchange = (e) => { S.showBands = e.target.checked; renderTimeline(); };
$('#show-ghost').onchange = (e) => { S.showGhost = e.target.checked; renderTimeline(); };

function renderVersionPill() {
  const pill = $('#version-pill');
  if (!pill) return;
  const av = S.cal && S.cal.active_version;
  if (!av) { pill.hidden = true; pill.textContent = ''; return; }
  const stale = S.cal.active_version_stale;
  pill.hidden = false;
  pill.className = 'version-pill' + (stale ? ' stale' : '');
  pill.textContent = (stale ? '⚠ 版本已过期: ' : '校准版本: ') +
    (av.plan_name || '') + ` #${S.cal.active_version_id}`;
}

// ---------------------------------------------------------------- 时钟校准面板

function srcName(id) {
  if (!S.cal) return `#${id}`;
  const s = S.cal.sources.find((x) => x.id === id);
  return s ? s.name : `#${id}`;
}

function evTitle(id) {
  const e = evById(id);
  return e ? e.title : `#${id}`;
}

function renderClockPanel() {
  if (!S.cal) return;
  renderIssues();
  renderSources();
  renderPoints();
  renderLiveParams();
  renderCandidates();
  renderVersions();
}

function renderIssues() {
  const el = $('#clock-issues');
  if (!el) return;
  const issues = S.cal.issues || [];
  const badPoints = (S.cal.point_diagnostics || []).filter((p) => p.status !== 'ok');
  if (!issues.length && !badPoints.length) {
    el.innerHTML = '<div class="ok-text" style="margin:8px 0">✓ 校准关系自洽</div>';
    return;
  }
  let html = '<div class="issue-list">';
  issues.forEach((i) => {
    html += `<div class="issue ${esc(i.kind)}">${esc(i.message)}</div>`;
  });
  badPoints.forEach((p) => {
    (p.messages || []).forEach((m) => {
      html += `<div class="issue ${esc(p.status)}">校准点 #${p.id}: ${esc(m)}</div>`;
    });
  });
  el.innerHTML = html + '</div>';
}

function renderSources() {
  // 事件表单下拉
  populateClockSourceSelects();
  const el = $('#source-list');
  if (!el) return;
  if (!S.cal.sources.length) {
    el.innerHTML = '<div class="empty-state">还没有时钟来源</div>';
  } else {
    const disc = new Set((S.cal.issues || [])
      .filter((i) => i.kind === 'disconnected').map((i) => i.source_id));
    el.innerHTML = S.cal.sources.map((s) => {
      const locks = S.cal.locks || {};
      const offVal = locks.offset && s.id in locks.offset ? locks.offset[s.id] : '';
      const dftVal = locks.drift && s.id in locks.drift ? locks.drift[s.id] : '';
      const srcLocked = (locks.sources || []).includes(s.id);
      return `<div class="source-row" data-id="${s.id}">
        <div class="top">
          <span class="nm">${esc(s.name)}</span>
          ${s.is_baseline ? '<span class="base-tag">★ 基准</span>'
            : disc.has(s.id) ? '<span class="disc-tag">未连通</span>' : ''}
          ${!s.is_baseline ? `<button class="link-btn" data-act="baseline" title="设为基准">设基准</button>` : ''}
          ${!s.is_baseline ? `<button class="link-btn danger" data-act="del">删除</button>` : ''}
        </div>
        ${s.description ? `<div class="ds">${esc(s.description)}</div>` : ''}
        ${!s.is_baseline ? `<div class="lock-grid">
          <label class="ck"><input type="checkbox" data-lock="source" ${srcLocked ? 'checked' : ''}> 锁定来源</label>
          <input type="number" step="any" placeholder="锁定偏移(ms)" data-lock="offset" value="${offVal === '' ? '' : Number(offVal).toFixed(0)}">
          <input type="number" step="any" placeholder="锁定漂移(ms/时)" data-lock="drift" value="${dftVal === '' ? '' : Number(dftVal).toFixed(3)}">
          <button class="btn sm" data-act="lock">应用</button>
        </div>` : ''}
      </div>`;
    }).join('');

    el.querySelectorAll('.source-row').forEach((row) => {
      const sid = +row.dataset.id;
      row.querySelector('[data-act="del"]')?.addEventListener('click', async () => {
        if (!confirm('删除该时钟来源? 相关校准点将一并删除, 事件归属清空。')) return;
        try { applyState(await api(`/api/clock_sources/${sid}`, 'DELETE')); toast('已删除来源'); }
        catch (ex) { toast(ex.message, true); }
      });
      row.querySelector('[data-act="baseline"]')?.addEventListener('click', async () => {
        try { applyState(await api(`/api/clock_sources/${sid}`, 'PUT', { is_baseline: true })); toast('已设为基准'); }
        catch (ex) { toast(ex.message, true); }
      });
      row.querySelector('[data-act="lock"]')?.addEventListener('click', async () => {
        const body = {
          lock_source: row.querySelector('[data-lock="source"]').checked,
          lock_offset_ms: row.querySelector('[data-lock="offset"]').value,
          lock_drift_ms_per_hour: row.querySelector('[data-lock="drift"]').value,
        };
        try { applyState(await api(`/api/clock_sources/${sid}/locks`, 'POST', body)); toast('锁定已更新'); }
        catch (ex) { toast(ex.message, true); }
      });
    });
  }

  // 校准点的来源下拉
  const srcOpts = S.cal.sources.map((s) =>
    `<option value="${s.id}">${esc(s.name)}</option>`).join('');
  ['#cp-as', '#cp-bs'].forEach((sel) => {
    const el2 = $(sel);
    if (el2) el2.innerHTML = srcOpts;
  });
  // 事件下拉(显示当前归属)
  const evOpts = S.events.map((e) => {
    const src = e.clock_source_id == null ? '' : ` [${esc(srcName(e.clock_source_id))}]`;
    return `<option value="${e.id}">#${e.id} ${esc(e.title)}${src}</option>`;
  }).join('');
  ['#cp-a', '#cp-b'].forEach((sel) => {
    const el2 = $(sel);
    if (el2) { const cur = el2.value; el2.innerHTML = evOpts; if (cur) el2.value = cur; }
  });
  // 选中事件时自动带出其归属来源
  const syncSrc = (evSel, srcSel) => {
    const e = evById(+evSel.value);
    if (e && e.clock_source_id != null) srcSel.value = e.clock_source_id;
  };
  $('#cp-a').onchange = () => syncSrc($('#cp-a'), $('#cp-as'));
  $('#cp-b').onchange = () => syncSrc($('#cp-b'), $('#cp-bs'));
}

$('#btn-add-source').onclick = async () => {
  const name = $('#cs-name').value.trim();
  if (!name) { toast('请填写来源名称', true); return; }
  try {
    applyState(await api('/api/clock_sources', 'POST', {
      name, description: $('#cs-desc').value.trim(),
      is_baseline: $('#cs-baseline').checked,
    }));
    $('#cs-name').value = ''; $('#cs-desc').value = ''; $('#cs-baseline').checked = false;
    toast('时钟来源已建立');
  } catch (ex) { toast(ex.message, true); }
};

function pointStatus(id) {
  const d = (S.cal.point_diagnostics || []).find((x) => x.id === id);
  return d ? d.status : 'ok';
}

const POINT_STATUS_LABEL = {
  ok: '自洽', contradictory: '矛盾', same_source: '同来源',
  duplicate: '重复', dangling: '悬空',
};

function renderPoints() {
  const el = $('#point-list');
  if (!el) return;
  if (!S.cal.points.length) {
    el.innerHTML = '<div class="empty-state">还没有校准点</div>';
    return;
  }
  const res = (S.cal.live_fit ? S.cal.live_fit.residuals : {}) || {};
  el.innerHTML = S.cal.points.map((p) => {
    const st = pointStatus(p.id);
    const r = res[String(p.id)];
    return `<div class="point-row ${st === 'contradictory' ? 'bad' : ''}" data-id="${p.id}">
      <div class="pair">
        <span class="pa">${esc(evTitle(p.a_event_id))}</span>
        <span class="rel">≡</span>
        <span class="pb">${esc(evTitle(p.b_event_id))}</span>
        <span class="status-dot st-${st}">${POINT_STATUS_LABEL[st] || st}</span>
        <button class="link-btn danger" data-act="del" style="margin-left:auto">删除</button>
      </div>
      <div class="meta">
        <span>${esc(srcName(p.a_source_id))} ↔ ${esc(srcName(p.b_source_id))}</span>
        <span>允许 ±${esc(fmtDur(p.tolerance_ms))}</span>
        ${r != null ? `<span class="resid">残差 ${r > 0 ? '+' : ''}${esc(fmtDur(Math.abs(r)))}</span>` : ''}
        ${p.note ? `<span>${esc(p.note)}</span>` : ''}
      </div>
    </div>`;
  }).join('');
  el.querySelectorAll('.point-row').forEach((row) => {
    const pid = +row.dataset.id;
    row.querySelector('[data-act="del"]').onclick = async () => {
      try { applyState(await api(`/api/calibration_points/${pid}`, 'DELETE')); toast('校准点已删除'); }
      catch (ex) { toast(ex.message, true); }
    };
    // 点击校准点 -> 时间轴追溯
    row.onclick = (e) => {
      if (e.target.closest('button')) return;
      tracePoint(pid);
    };
  });
}

$('#btn-add-point').onclick = async () => {
  const body = {
    a_event_id: +$('#cp-a').value,
    b_event_id: +$('#cp-b').value,
    a_source_id: +$('#cp-as').value,
    b_source_id: +$('#cp-bs').value,
    tolerance_ms: Math.round((+$('#cp-tol').value || 0) * 1000),
    note: $('#cp-note').value.trim(),
  };
  if (!body.a_event_id || !body.b_event_id) { toast('请选择两个事件', true); return; }
  if (!body.a_source_id || !body.b_source_id) { toast('请先建立并选择两个时钟来源', true); return; }
  try {
    applyState(await api('/api/calibration_points', 'POST', body));
    $('#cp-note').value = '';
    toast('校准点已添加');
  } catch (ex) { toast(ex.message, true); }
};

function renderLiveParams() {
  const el = $('#live-params');
  if (!el) return;
  if (!S.cal.live_fit) { el.innerHTML = ''; return; }
  el.innerHTML = '<div class="live-params">' + S.cal.live_fit.params.map((p) => {
    const lock = [];
    if (p.locked_offset) lock.push('偏移已锁');
    if (p.locked_drift) lock.push('漂移已锁');
    return `<div class="lp-row">
      <span>${esc(p.source_name)}${p.is_baseline ? ' ★' : ''}</span>
      <span class="v">${p.is_baseline ? '基准(0)'
        : `修正 ${p.correction_ms > 0 ? '+' : ''}${fmtDur(Math.abs(p.correction_ms))} · 漂移 ${p.drift_sec_per_day > 0 ? '+' : ''}${p.drift_sec_per_day} 秒/天`}</span>
      ${lock.length ? `<span class="muted">${lock.join('、')}</span>` : ''}
    </div>`;
  }).join('') + '</div>';
}

$('#btn-candidates').onclick = async () => {
  try {
    const data = await api('/api/calibration/candidates', 'POST', {});
    S.candidates = data.plans;
    renderCandidates();
  } catch (ex) { toast(ex.message, true); }
};

function renderCandidates() {
  const el = $('#candidate-list');
  if (!el) return;
  if (!S.candidates) { el.innerHTML = ''; return; }
  el.innerHTML = S.candidates.map((p) => {
    const params = p.fit.params.filter((q) => !q.is_baseline).map((q) =>
      `<div>${esc(q.source_name)}: 修正 ${q.correction_ms > 0 ? '+' : '−'}${fmtDur(Math.abs(q.correction_ms))}`
      + ` · 漂移 ${q.drift_sec_per_day > 0 ? '+' : '−'}${Math.abs(q.drift_sec_per_day)} 秒/天`
      + `${q.locked_offset ? ' 🔒偏移' : ''}${q.locked_drift ? ' 🔒漂移' : ''}</div>`).join('');
    const contr = (p.contradictory_point_ids || []).length;
    return `<div class="cand-card" data-key="${esc(p.key)}">
      <h4>${esc(p.name)}</h4>
      <div class="muted">${esc(p.description)}</div>
      <div class="cand-metrics">
        <span>依赖冲突 <b>${p.conflict_count}</b></span>
        <span>总修正量 <b>${esc(fmtDur(p.total_correction_ms))}</b></span>
        <span>最大残差 <b>${esc(fmtDur(p.max_residual_ms))}</b></span>
        ${contr ? `<span style="color:var(--red)">矛盾点 ${contr}</span>` : ''}
        ${(p.dropped_point_ids || []).length ? `<span style="color:var(--orange)">剔除点 ${p.dropped_point_ids.join(',')}</span>` : ''}
      </div>
      <div class="cand-params">${params}</div>
      <button class="btn primary block" data-act="save">另存为校准版本</button>
    </div>`;
  }).join('');
  el.querySelectorAll('.cand-card').forEach((card) => {
    card.querySelector('[data-act="save"]').onclick = async () => {
      const key = card.dataset.key;
      const label = prompt('版本名称:', `校准版本 ${new Date().toLocaleString()}`);
      if (label === null) return;
      try {
        applyState(await api('/api/calibration/versions', 'POST', { plan_key: key, label }));
        S.candidates = null;
        renderCandidates();
        toast('已另存为校准版本(事件原始时间未改动)');
      } catch (ex) { toast(ex.message, true); }
    };
  });
}

function renderVersions() {
  const el = $('#version-list');
  if (!el) return;
  const vs = S.cal.versions || [];
  if (!vs.length) { el.innerHTML = '<div class="empty-state">还没有校准版本</div>'; return; }
  el.innerHTML = vs.map((v) => {
    const when = new Date(v.created_at * 1000).toLocaleString();
    return `<div class="ver-row ${v.active ? 'active' : ''}" data-id="${v.id}">
      <span class="vlabel">${esc(v.label)}<div class="vtime">${when}</div></span>
      ${v.active ? '<span class="active-tag">使用中</span>' : ''}
      ${v.stale ? '<span class="stale-tag">已过期</span>' : ''}
      ${!v.active ? '<button class="link-btn" data-act="activate">切回此版本</button>' : ''}
      <button class="link-btn danger" data-act="del">删除</button>
    </div>`;
  }).join('');
  el.querySelectorAll('.ver-row').forEach((row) => {
    const vid = +row.dataset.id;
    row.querySelector('[data-act="activate"]')?.addEventListener('click', async () => {
      try { applyState(await api(`/api/calibration/versions/${vid}/activate`, 'POST')); toast('已切换到该版本参数'); }
      catch (ex) { toast(ex.message, true); }
    });
    row.querySelector('[data-act="del"]').addEventListener('click', async () => {
      try { applyState(await api(`/api/calibration/versions/${vid}`, 'DELETE')); toast('版本已删除'); }
      catch (ex) { toast(ex.message, true); }
    });
  });
}

// 异常追溯: tracePoints() 在冲突面板中定义, 这里提供单个校准点的入口
function tracePoint(pid) {
  tracePoints([pid]);
  const p = S.cal.points.find((x) => x.id === pid);
  if (p) toast(`校准点 #${pid}: ${evTitle(p.a_event_id)} ≡ ${evTitle(p.b_event_id)}`);
}

// ---------------------------------------------------------------- 启动

refresh().catch((ex) => toast(ex.message, true));
