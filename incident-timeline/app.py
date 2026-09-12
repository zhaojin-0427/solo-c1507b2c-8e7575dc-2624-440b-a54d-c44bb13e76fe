"""事故时序校验台 —— Flask 应用入口。

本机运行: python3 app.py  然后访问 http://127.0.0.1:5000
"""
import json
import os
import sqlite3
import time

from flask import Flask, Response, g, jsonify, render_template, request

import analysis
import calibration as calib
import export as export_mod
from timeutil import parse_time

BASE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE, 'data.db')

app = Flask(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  title TEXT NOT NULL,
  description TEXT DEFAULT '',
  start_ts INTEGER NOT NULL,
  end_ts INTEGER,
  timezone TEXT DEFAULT 'UTC',
  confidence TEXT DEFAULT 'medium',
  source TEXT DEFAULT '',
  evidence TEXT DEFAULT '',
  locked INTEGER DEFAULT 0,
  group_name TEXT DEFAULT '',
  color TEXT DEFAULT '',
  clock_source_id INTEGER,
  created_at INTEGER
);
CREATE TABLE IF NOT EXISTS dependencies (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  type TEXT NOT NULL,
  from_id INTEGER NOT NULL,
  to_id INTEGER NOT NULL,
  min_gap_ms INTEGER DEFAULT 0,
  max_gap_ms INTEGER,
  note TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS exclusions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  a_id INTEGER NOT NULL,
  b_id INTEGER NOT NULL,
  reason TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS revisions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  label TEXT,
  created_at INTEGER,
  pinned INTEGER DEFAULT 0,
  snapshot TEXT
);
CREATE TABLE IF NOT EXISTS clock_sources (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL,
  description TEXT DEFAULT '',
  is_baseline INTEGER DEFAULT 0,
  created_at INTEGER
);
CREATE TABLE IF NOT EXISTS calibration_points (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  a_event_id INTEGER NOT NULL,
  b_event_id INTEGER NOT NULL,
  a_source_id INTEGER NOT NULL,
  b_source_id INTEGER NOT NULL,
  tolerance_ms INTEGER DEFAULT 1000,
  note TEXT DEFAULT '',
  created_at INTEGER
);
CREATE TABLE IF NOT EXISTS calibration_versions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  label TEXT,
  created_at INTEGER,
  active INTEGER DEFAULT 0,
  signature TEXT,
  detail TEXT
);
"""

CONFIDENCES = ('high', 'medium', 'low')
DEP_TYPES = ('before', 'triggers', 'during')


# ---------------------------------------------------------------- 基础设施

def get_db():
    if 'db' not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
    return g.db


@app.teardown_appcontext
def close_db(_exc):
    db = g.pop('db', None)
    if db is not None:
        db.close()


def init_db():
    db = sqlite3.connect(DB_PATH)
    db.executescript(SCHEMA)
    # 旧库迁移
    ev_cols = [r[1] for r in db.execute('PRAGMA table_info(events)')]
    if 'clock_source_id' not in ev_cols:
        db.execute('ALTER TABLE events ADD COLUMN clock_source_id INTEGER')
    cs_cols = [r[1] for r in db.execute('PRAGMA table_info(clock_sources)')]
    if 'lock_source' not in cs_cols:
        db.execute('ALTER TABLE clock_sources ADD COLUMN lock_source INTEGER DEFAULT 0')
    if 'lock_offset_ms' not in cs_cols:
        db.execute('ALTER TABLE clock_sources ADD COLUMN lock_offset_ms REAL')
    if 'lock_drift_ms_per_hour' not in cs_cols:
        db.execute('ALTER TABLE clock_sources ADD COLUMN lock_drift_ms_per_hour REAL')
    db.commit()
    db.close()


def load_state():
    db = get_db()
    events = [dict(r) for r in db.execute('SELECT * FROM events ORDER BY start_ts, id')]
    deps = [dict(r) for r in db.execute('SELECT * FROM dependencies ORDER BY id')]
    excls = [dict(r) for r in db.execute('SELECT * FROM exclusions ORDER BY id')]
    return events, deps, excls


def load_clock():
    db = get_db()
    sources = [dict(r) for r in db.execute('SELECT * FROM clock_sources ORDER BY id')]
    points = [dict(r) for r in db.execute('SELECT * FROM calibration_points ORDER BY id')]
    versions = [dict(r) for r in
                db.execute('SELECT * FROM calibration_versions ORDER BY id DESC')]
    return sources, points, versions


def current_locks(sources):
    """锁定状态存于 clock_sources 行内(lock_source / lock_offset_ms /
    lock_drift_ms_per_hour 三列, 迁移时补列)。"""
    locks = {'sources': [], 'offset': {}, 'drift': {}}
    for s in sources:
        if s.get('lock_source'):
            locks['sources'].append(s['id'])
        if s.get('lock_offset_ms') is not None:
            locks['offset'][s['id']] = float(s['lock_offset_ms'])
        if s.get('lock_drift_ms_per_hour') is not None:
            locks['drift'][s['id']] = float(s['lock_drift_ms_per_hour'])
    return locks


def push_snapshot(label):
    """在每次变更前保存快照(供撤销); 首个快照同时固化为对比基准。"""
    db = get_db()
    events, deps, excls = load_state()
    snap = json.dumps({'events': events, 'dependencies': deps, 'exclusions': excls},
                      ensure_ascii=False)
    now = int(time.time())
    db.execute('INSERT INTO revisions(label, created_at, pinned, snapshot) VALUES (?,?,0,?)',
               (label, now, snap))
    if not db.execute('SELECT 1 FROM revisions WHERE pinned=1 LIMIT 1').fetchone():
        db.execute('INSERT INTO revisions(label, created_at, pinned, snapshot) VALUES (?,?,1,?)',
                   ('原始版本', now, snap))
    db.commit()


def restore_snapshot(snap):
    db = get_db()
    db.execute('DELETE FROM events')
    db.execute('DELETE FROM dependencies')
    db.execute('DELETE FROM exclusions')
    for e in snap['events']:
        db.execute(
            'INSERT INTO events(id,title,description,start_ts,end_ts,timezone,confidence,'
            'source,evidence,locked,group_name,color,created_at) '
            'VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)',
            (e['id'], e['title'], e.get('description', ''), e['start_ts'], e.get('end_ts'),
             e.get('timezone', 'UTC'), e.get('confidence', 'medium'), e.get('source', ''),
             e.get('evidence', ''), e.get('locked', 0), e.get('group_name', ''),
             e.get('color', ''), e.get('created_at', int(time.time()))))
    for d in snap['dependencies']:
        db.execute('INSERT INTO dependencies(id,type,from_id,to_id,min_gap_ms,max_gap_ms,note) '
                   'VALUES (?,?,?,?,?,?,?)',
                   (d['id'], d['type'], d['from_id'], d['to_id'],
                    d.get('min_gap_ms', 0), d.get('max_gap_ms'), d.get('note', '')))
    for x in snap['exclusions']:
        db.execute('INSERT INTO exclusions(id,a_id,b_id,reason) VALUES (?,?,?,?)',
                   (x['id'], x['a_id'], x['b_id'], x.get('reason', '')))
    db.commit()


def _calibrated_event_view(events, fit_result):
    """按校准结果生成'校准时间'事件视图(不改原始记录);
    无归属来源或来源不连通的事件保持原时间。返回 (视图事件, {id: shift})。"""
    view, shifts = [], {}
    for e in events:
        v = dict(e)
        sid = e.get('clock_source_id')
        if sid is not None and fit_result is not None:
            cs = calib.calibrated_ts(e['start_ts'], sid, fit_result)
            if cs is not None:
                v['start_ts'] = round(cs)
                shifts[e['id']] = v['start_ts'] - e['start_ts']
                if e.get('end_ts') is not None:
                    ce = calib.calibrated_ts(e['end_ts'], sid, fit_result)
                    if ce is not None:
                        v['end_ts'] = round(ce)
        view.append(v)
    return view, shifts


def _calibration_state():
    """组装时钟校准全部派生状态: 活拟合/诊断/候选/校准后分析/版本。"""
    events, deps, excls = load_state()
    sources, points, versions = load_clock()
    locks = current_locks(sources)
    sig = calib.version_signature(sources, points)

    live = None
    issues, point_diag, chains = [], [], {}
    live_fit = None
    if sources:
        try:
            live_fit = calib.fit(sources, points, events, locks)
        except calib.LinAlgError as ex:
            issues.append({'kind': 'locks_conflict',
                           'message': f'锁定参数互相矛盾, 无法求解: {ex}'})
            live_fit = calib.fit(sources, points, events,
                                 {'sources': [], 'offset': {}, 'drift': {}})
        chains, _base = calib.reachable_from_baseline(sources, points)
        point_diag = calib.diagnose_points(sources, points, events, live_fit)
        issues += calib.source_issues(sources, points, events, live_fit)
        live = calib._public_fit(live_fit, sources, points)

    # 激活版本 / 过期判断(须在校准视图计算之前确定使用哪套参数)
    active = next((v for v in versions if v.get('active')), None)
    active_detail = json.loads(active['detail']) if active else None
    stale = bool(active and active.get('signature') != sig)

    # 校准视图优先采用激活版本参数; 版本过期(校准关系已变化)时回退 live,
    # 版本摘要仍保留用于对照, 并明确提示过期。
    version_fit = None
    if active_detail and not stale:
        version_fit = {
            'params': {p['source_id']: {
                'correction_ms': p['correction_ms'],
                'offset_ms': p['offset_ms'],
                'drift_ms_per_hour': p['drift_ms_per_hour'],
            } for p in active_detail['fit']['params']},
            't0': active_detail['fit'].get('t0', 0.0),
            'base_id': active_detail['fit'].get('base_id'),
            'residuals': {int(k): v for k, v in
                          active_detail['fit']['residuals'].items()},
        }
    view_fit = version_fit if version_fit is not None else live_fit

    # 校准后的事件视图与冲突分析
    cal_events, shifts = _calibrated_event_view(
        events, view_fit if sources and not any(i['kind'] == 'baseline'
                                                for i in issues) else None)
    cal_analysis = analysis.analyze(cal_events, deps, excls)
    # 校准视图中冲突身份集合(供时间轴叠加)
    raw_analysis = analysis.analyze(events, deps, excls)
    raw_conf_ids = _conflict_identities(raw_analysis)
    cal_conf_ids = _conflict_identities(cal_analysis)

    # 误差带(每个来源一个半宽), 与当前视图参数一致
    bands = {}
    if view_fit is not None:
        for s in sources:
            bands[s['id']] = calib.uncertainty_band(
                s['id'], view_fit.get('t0', 0.0), points, view_fit, chains)

    version_list = [{
        'id': v['id'], 'label': v['label'], 'created_at': v['created_at'],
        'active': bool(v['active']), 'stale': v['signature'] != sig,
        'signature': v['signature'],
    } for v in versions]

    return {
        'sources': sources,
        'points': points,
        'locks': locks,
        'live_fit': live,
        'issues': issues,
        'point_diagnostics': point_diag,
        'bands_ms': {str(k): v for k, v in bands.items()},
        'shifts_ms': {str(k): v for k, v in shifts.items()},
        'calibrated_analysis': cal_analysis,
        'raw_conflict_keys': sorted(raw_conf_ids),
        'cal_conflict_keys': sorted(cal_conf_ids),
        'signature': sig,
        'versions': version_list,
        'active_version_id': active['id'] if active else None,
        'active_version_stale': stale,
        'active_version': _version_summary(active_detail, sources) if active_detail else None,
    }


def _conflict_identities(an):
    """冲突身份集合: (type, dep_ids 排序, event_ids 排序), 用于原始/校准对照。"""
    out = set()
    for c in an['conflicts']:
        out.add((c['type'],
                 tuple(sorted(c.get('dep_ids') or [])),
                 tuple(sorted(c.get('event_ids') or []))))
    return out


def _version_summary(detail, sources):
    """版本对外摘要(去掉候选大对象, 保留参数与指标)。"""
    fit = detail.get('fit', {})
    return {
        'plan_key': detail.get('plan_key'),
        'plan_name': detail.get('plan_name'),
        'params': fit.get('params', []),
        't0': fit.get('t0'),
        'metrics': {k: detail.get(k) for k in
                    ('conflict_count', 'total_correction_ms',
                     'max_residual_ms', 'sigma_ms',
                     'contradictory_point_ids', 'dropped_point_ids')},
        'locks': detail.get('locks', {}),
    }


def state_json():
    events, deps, excls = load_state()
    db = get_db()
    return {
        'events': events,
        'dependencies': deps,
        'exclusions': excls,
        'analysis': analysis.analyze(events, deps, excls),
        'calibration': _calibration_state(),
        'can_undo': db.execute('SELECT 1 FROM revisions WHERE pinned=0 LIMIT 1').fetchone() is not None,
        'has_baseline': db.execute('SELECT 1 FROM revisions WHERE pinned=1 LIMIT 1').fetchone() is not None,
    }


def err(msg, code=400):
    return jsonify({'ok': False, 'error': msg}), code


# ---------------------------------------------------------------- 页面

@app.get('/')
def index():
    return render_template('index.html')


@app.get('/api/state')
def api_state():
    return jsonify(state_json())


# ---------------------------------------------------------------- 事件

def _event_fields(data, existing=None):
    f = {}
    if 'title' in data:
        title = str(data['title']).strip()
        if not title:
            raise ValueError('标题不能为空')
        f['title'] = title
    tz = data.get('timezone') or (existing or {}).get('timezone') or 'UTC'
    if 'timezone' in data:
        f['timezone'] = str(data['timezone']).strip() or 'UTC'
    if 'start_ts' in data:
        f['start_ts'] = int(data['start_ts'])
    elif 'start' in data and data['start']:
        f['start_ts'], parsed_tz = parse_time(data['start'], tz)
        if 'timezone' not in data and not (existing or {}).get('timezone'):
            f['timezone'] = parsed_tz
    if 'end_ts' in data:
        f['end_ts'] = int(data['end_ts']) if data['end_ts'] is not None else None
    elif 'end' in data:
        f['end_ts'] = parse_time(data['end'], tz)[0] if data['end'] else None
    for k in ('description', 'source', 'evidence', 'group_name', 'color'):
        if k in data:
            f[k] = str(data[k])
    if 'clock_source_id' in data:
        v = data['clock_source_id']
        f['clock_source_id'] = int(v) if v not in (None, '', 0, '0') else None
    if 'confidence' in data:
        if data['confidence'] not in CONFIDENCES:
            raise ValueError('置信度须为 high/medium/low')
        f['confidence'] = data['confidence']
    if 'locked' in data:
        f['locked'] = 1 if data['locked'] else 0
    return f


@app.post('/api/events')
def create_event():
    data = request.get_json(force=True)
    try:
        f = _event_fields(data)
    except (ValueError, TypeError) as ex:
        return err(str(ex))
    if 'title' not in f or 'start_ts' not in f:
        return err('新增事件至少需要标题和开始时间')
    push_snapshot('新增事件')
    db = get_db()
    cols = ','.join(f.keys()) + ',created_at'
    marks = ','.join('?' * len(f)) + ',?'
    db.execute(f'INSERT INTO events({cols}) VALUES ({marks})',
               (*f.values(), int(time.time())))
    db.commit()
    return jsonify(state_json())


@app.put('/api/events/<int:eid>')
def update_event(eid):
    db = get_db()
    row = db.execute('SELECT * FROM events WHERE id=?', (eid,)).fetchone()
    if not row:
        return err('事件不存在', 404)
    data = request.get_json(force=True)
    try:
        f = _event_fields(data, existing=dict(row))
    except (ValueError, TypeError) as ex:
        return err(str(ex))
    if not f:
        return jsonify(state_json())
    # 拖动等高频操作也入撤销栈, 标签区分
    label = '拖动事件' if set(f) <= {'start_ts', 'end_ts'} else '编辑事件'
    push_snapshot(label)
    sets = ','.join(f'{k}=?' for k in f)
    db.execute(f'UPDATE events SET {sets} WHERE id=?', (*f.values(), eid))
    db.commit()
    return jsonify(state_json())


@app.delete('/api/events/<int:eid>')
def delete_event(eid):
    push_snapshot('删除事件')
    db = get_db()
    db.execute('DELETE FROM events WHERE id=?', (eid,))
    db.execute('DELETE FROM dependencies WHERE from_id=? OR to_id=?', (eid, eid))
    db.execute('DELETE FROM exclusions WHERE a_id=? OR b_id=?', (eid, eid))
    db.execute('DELETE FROM calibration_points WHERE a_event_id=? OR b_event_id=?',
               (eid, eid))
    db.commit()
    return jsonify(state_json())


@app.post('/api/events/bulk')
def bulk_events():
    data = request.get_json(force=True)
    text = data.get('text', '')
    default_tz = data.get('default_tz', 'UTC')
    created, errors = 0, []
    push_snapshot('批量导入')
    db = get_db()
    for i, line in enumerate(text.splitlines(), 1):
        line = line.strip()
        if not line or line.startswith('#'):
            continue
        try:
            ev = _parse_bulk_line(line, default_tz)
            db.execute(
                'INSERT INTO events(title,start_ts,end_ts,timezone,confidence,source,'
                'evidence,group_name,description,created_at) VALUES (?,?,?,?,?,?,?,?,?,?)',
                (ev['title'], ev['start_ts'], ev['end_ts'], ev['timezone'], ev['confidence'],
                 ev['source'], ev['evidence'], ev['group_name'], ev['description'],
                 int(time.time())))
            created += 1
        except ValueError as ex:
            errors.append({'line': i, 'text': line, 'error': str(ex)})
    db.commit()
    resp = state_json()
    resp['bulk'] = {'created': created, 'errors': errors}
    return jsonify(resp)


def _parse_bulk_line(line, default_tz):
    """批量行格式: 开始 | [结束] | [置信度] | 标题 | key=value ...

    例: 2026-09-10 14:03:22 +08:00 | 2026-09-10 14:05:10 +08:00 | high |
        主库 CPU 打满 | source=慢查询日志 | evidence=... | group=数据库
    """
    parts = [p.strip() for p in line.split('|')]
    if not parts or not parts[0]:
        raise ValueError('缺少开始时间')
    start_ts, tz = parse_time(parts[0], default_tz)
    idx = 1
    end_ts = None
    if idx < len(parts) and parts[idx]:
        try:
            end_ts = parse_time(parts[idx], default_tz)[0]
            idx += 1
        except ValueError:
            pass  # 不是时间, 当作后续字段
    conf = 'medium'
    conf_map = {'高': 'high', '中': 'medium', '低': 'low'}
    if idx < len(parts) and parts[idx].lower() in ('high', 'medium', 'low', '高', '中', '低'):
        c = parts[idx].lower()
        conf = conf_map.get(c, c)
        idx += 1
    if idx >= len(parts) or not parts[idx]:
        raise ValueError('缺少标题')
    title = parts[idx]
    idx += 1
    kv = {}
    for p in parts[idx:]:
        if '=' in p:
            k, v = p.split('=', 1)
            kv[k.strip().lower()] = v.strip()
    return {'title': title, 'start_ts': start_ts, 'end_ts': end_ts,
            'confidence': conf, 'timezone': kv.get('tz', tz),
            'source': kv.get('source', ''), 'evidence': kv.get('evidence', ''),
            'group_name': kv.get('group', ''), 'description': kv.get('desc', '')}


# ---------------------------------------------------------------- 依赖 / 互斥

@app.post('/api/dependencies')
def create_dep():
    data = request.get_json(force=True)
    try:
        dtype = data['type']
        if dtype not in DEP_TYPES:
            raise ValueError('依赖类型须为 before/triggers/during')
        from_id, to_id = int(data['from_id']), int(data['to_id'])
        min_gap = int(data.get('min_gap_ms') or 0)
        max_gap = data.get('max_gap_ms')
        max_gap = int(max_gap) if max_gap not in (None, '') else None
        if max_gap is not None and max_gap < min_gap:
            raise ValueError('最大间隔不能小于最小间隔')
    except (KeyError, ValueError, TypeError) as ex:
        return err(f'参数错误: {ex}')
    db = get_db()
    n = db.execute('SELECT COUNT(*) c FROM events WHERE id IN (?,?)', (from_id, to_id)).fetchone()['c']
    if n < (1 if from_id == to_id else 2):
        return err('依赖引用了不存在的事件')
    push_snapshot('新增依赖')
    db.execute('INSERT INTO dependencies(type,from_id,to_id,min_gap_ms,max_gap_ms,note) '
               'VALUES (?,?,?,?,?,?)',
               (dtype, from_id, to_id, min_gap, max_gap, str(data.get('note', ''))))
    db.commit()
    return jsonify(state_json())


@app.delete('/api/dependencies/<int:did>')
def delete_dep(did):
    push_snapshot('删除依赖')
    db = get_db()
    db.execute('DELETE FROM dependencies WHERE id=?', (did,))
    db.commit()
    return jsonify(state_json())


@app.post('/api/exclusions')
def create_exclusion():
    data = request.get_json(force=True)
    try:
        a_id, b_id = int(data['a_id']), int(data['b_id'])
        if a_id == b_id:
            raise ValueError('互斥需要两个不同的事件')
    except (KeyError, ValueError, TypeError) as ex:
        return err(f'参数错误: {ex}')
    push_snapshot('新增互斥')
    db = get_db()
    db.execute('INSERT INTO exclusions(a_id,b_id,reason) VALUES (?,?,?)',
               (a_id, b_id, str(data.get('reason', ''))))
    db.commit()
    return jsonify(state_json())


@app.delete('/api/exclusions/<int:xid>')
def delete_exclusion(xid):
    push_snapshot('删除互斥')
    db = get_db()
    db.execute('DELETE FROM exclusions WHERE id=?', (xid,))
    db.commit()
    return jsonify(state_json())


# ---------------------------------------------------------------- 撤销 / 基准 / 对比

@app.post('/api/undo')
def undo():
    db = get_db()
    row = db.execute('SELECT * FROM revisions WHERE pinned=0 ORDER BY id DESC LIMIT 1').fetchone()
    if not row:
        return err('没有可撤销的操作')
    restore_snapshot(json.loads(row['snapshot']))
    db.execute('DELETE FROM revisions WHERE id=?', (row['id'],))
    db.commit()
    resp = state_json()
    resp['undone'] = row['label']
    return jsonify(resp)


@app.post('/api/baseline')
def set_baseline():
    db = get_db()
    events, deps, excls = load_state()
    snap = json.dumps({'events': events, 'dependencies': deps, 'exclusions': excls},
                      ensure_ascii=False)
    db.execute('DELETE FROM revisions WHERE pinned=1')
    db.execute('INSERT INTO revisions(label, created_at, pinned, snapshot) VALUES (?,?,1,?)',
               ('基准(手动设定)', int(time.time()), snap))
    db.commit()
    return jsonify(state_json())


@app.get('/api/compare')
def compare():
    db = get_db()
    base = db.execute('SELECT * FROM revisions WHERE pinned=1 ORDER BY id LIMIT 1').fetchone()
    if not base:
        return jsonify({'has_baseline': False})
    bsnap = json.loads(base['snapshot'])
    events, _, _ = load_state()
    return jsonify({
        'has_baseline': True,
        'baseline_label': base['label'],
        'baseline_at': base['created_at'],
        'baseline_events': bsnap['events'],
        'current_events': events,
        'diff': _diff_events(bsnap['events'], events),
    })


def _diff_events(base_events, cur_events):
    b = {e['id']: e for e in base_events}
    c = {e['id']: e for e in cur_events}
    diff = []
    for i, e in c.items():
        if i not in b:
            diff.append({'event_id': i, 'title': e['title'], 'change': 'added'})
        else:
            o = b[i]
            if o['start_ts'] != e['start_ts'] or o['end_ts'] != e['end_ts']:
                diff.append({'event_id': i, 'title': e['title'], 'change': 'moved',
                             'from_start': o['start_ts'], 'from_end': o['end_ts'],
                             'to_start': e['start_ts'], 'to_end': e['end_ts'],
                             'delta_ms': e['start_ts'] - o['start_ts']})
    for i, e in b.items():
        if i not in c:
            diff.append({'event_id': i, 'title': e['title'], 'change': 'removed'})
    unchanged = sum(1 for i in c if i in b
                    and b[i]['start_ts'] == c[i]['start_ts'] and b[i]['end_ts'] == c[i]['end_ts'])
    return {'items': diff, 'unchanged': unchanged}


# ---------------------------------------------------------------- 调整方案

@app.post('/api/suggestions')
def suggestions():
    events, deps, excls = load_state()
    plans = analysis.solve_plans(events, deps, excls)
    return jsonify({'plans': plans})


@app.post('/api/apply_plan')
def apply_plan():
    strategy = (request.get_json(force=True) or {}).get('strategy')
    events, deps, excls = load_state()
    plan = next((p for p in analysis.solve_plans(events, deps, excls)
                 if p['strategy'] == strategy), None)
    if not plan:
        return err('方案不存在', 404)
    if not plan['moves']:
        return err('该方案没有需要应用的移动')
    push_snapshot(f'应用{plan["name"]}')
    db = get_db()
    for m in plan['moves']:
        db.execute('UPDATE events SET start_ts=?, end_ts=? WHERE id=? AND locked=0',
                   (m['to_start'], m['to_end'], m['event_id']))
    db.commit()
    return jsonify(state_json())


# ---------------------------------------------------------------- 时钟来源

@app.post('/api/clock_sources')
def create_clock_source():
    data = request.get_json(force=True)
    name = str(data.get('name', '')).strip()
    if not name:
        return err('时钟来源名称不能为空')
    db = get_db()
    cur = db.execute(
        'INSERT INTO clock_sources(name, description, is_baseline, created_at) '
        'VALUES (?,?,?,?)',
        (name, str(data.get('description', '')),
         1 if data.get('is_baseline') else 0, int(time.time())))
    if data.get('is_baseline'):
        db.execute('UPDATE clock_sources SET is_baseline=0 WHERE id<>?',
                   (cur.lastrowid,))
    db.commit()
    return jsonify(state_json())


@app.put('/api/clock_sources/<int:sid>')
def update_clock_source(sid):
    db = get_db()
    row = db.execute('SELECT * FROM clock_sources WHERE id=?', (sid,)).fetchone()
    if not row:
        return err('时钟来源不存在', 404)
    data = request.get_json(force=True)
    if 'name' in data:
        name = str(data['name']).strip()
        if not name:
            return err('名称不能为空')
        db.execute('UPDATE clock_sources SET name=?, description=? WHERE id=?',
                   (name, str(data.get('description', row['description'])), sid))
    if data.get('is_baseline'):
        db.execute('UPDATE clock_sources SET is_baseline=0')
        db.execute('UPDATE clock_sources SET is_baseline=1 WHERE id=?', (sid,))
    db.commit()
    return jsonify(state_json())


@app.delete('/api/clock_sources/<int:sid>')
def delete_clock_source(sid):
    db = get_db()
    row = db.execute('SELECT * FROM clock_sources WHERE id=?', (sid,)).fetchone()
    if not row:
        return err('时钟来源不存在', 404)
    if row['is_baseline']:
        return err('不能删除基准时钟来源, 请先指定其他来源为基准')
    db.execute('DELETE FROM clock_sources WHERE id=?', (sid,))
    db.execute('DELETE FROM calibration_points WHERE a_source_id=? OR b_source_id=?',
               (sid, sid))
    db.execute('UPDATE events SET clock_source_id=NULL WHERE clock_source_id=?', (sid,))
    db.execute('UPDATE clock_sources SET lock_source=0, lock_offset_ms=NULL, '
               'lock_drift_ms_per_hour=NULL WHERE id=?', (sid,))
    db.commit()
    return jsonify(state_json())


@app.post('/api/clock_sources/<int:sid>/locks')
def update_locks(sid):
    """设置锁定。body:
    {"lock_source": bool, "lock_offset_ms": number|null,
     "lock_drift_ms_per_hour": number|null}"""
    db = get_db()
    row = db.execute('SELECT * FROM clock_sources WHERE id=?', (sid,)).fetchone()
    if not row:
        return err('时钟来源不存在', 404)
    if row['is_baseline']:
        return err('基准来源的时钟恒为 0, 无需锁定')
    data = request.get_json(force=True) or {}
    ls = 1 if data.get('lock_source') else 0
    lo = data.get('lock_offset_ms')
    ld = data.get('lock_drift_ms_per_hour')
    lo = float(lo) if lo not in (None, '') else None
    ld = float(ld) if ld not in (None, '') else None
    db.execute('UPDATE clock_sources SET lock_source=?, lock_offset_ms=?, '
               'lock_drift_ms_per_hour=? WHERE id=?', (ls, lo, ld, sid))
    db.commit()
    return jsonify(state_json())


# ---------------------------------------------------------------- 校准点

@app.post('/api/calibration_points')
def create_calibration_point():
    data = request.get_json(force=True)
    try:
        a_eid, b_eid = int(data['a_event_id']), int(data['b_event_id'])
        a_sid, b_sid = int(data['a_source_id']), int(data['b_source_id'])
        tol = max(int(round(float(data.get('tolerance_ms', 1000))),), 0)
    except (KeyError, TypeError, ValueError) as ex:
        return err(f'参数错误: {ex}')
    if a_eid == b_eid:
        return err('校准点需要两个不同的事件')
    db = get_db()
    n = db.execute('SELECT COUNT(*) c FROM events WHERE id IN (?,?)',
                   (a_eid, b_eid)).fetchone()['c']
    if n < 2:
        return err('引用了不存在的事件')
    s = db.execute('SELECT COUNT(*) c FROM clock_sources WHERE id IN (?,?)',
                   (a_sid, b_sid)).fetchone()['c']
    if s < 2:
        return err('引用了不存在的时钟来源')
    if a_sid == b_sid:
        return err('两个事件应来自不同时钟来源')
    dup = db.execute(
        'SELECT id FROM calibration_points WHERE '
        '((a_event_id=? AND b_event_id=?) OR (a_event_id=? AND b_event_id=?)) LIMIT 1',
        (a_eid, b_eid, b_eid, a_eid)).fetchone()
    if dup:
        return err(f'这两个事件已在校准点 #{dup["id"]} 中配对')
    db.execute(
        'INSERT INTO calibration_points(a_event_id,b_event_id,a_source_id,'
        'b_source_id,tolerance_ms,note,created_at) VALUES (?,?,?,?,?,?,?)',
        (a_eid, b_eid, a_sid, b_sid, tol, str(data.get('note', '')),
         int(time.time())))
    db.commit()
    return jsonify(state_json())


@app.put('/api/calibration_points/<int:pid>')
def update_calibration_point(pid):
    db = get_db()
    row = db.execute('SELECT * FROM calibration_points WHERE id=?', (pid,)).fetchone()
    if not row:
        return err('校准点不存在', 404)
    data = request.get_json(force=True) or {}
    fields = []
    vals = []
    if 'tolerance_ms' in data:
        try:
            tol = max(int(round(float(data['tolerance_ms']))), 0)
        except (TypeError, ValueError):
            return err('允许误差须为数字(毫秒)')
        fields.append('tolerance_ms=?'); vals.append(tol)
    if 'note' in data:
        fields.append('note=?'); vals.append(str(data['note']))
    if fields:
        vals.append(pid)
        db.execute(f'UPDATE calibration_points SET {",".join(fields)} WHERE id=?', vals)
        db.commit()
    return jsonify(state_json())


@app.delete('/api/calibration_points/<int:pid>')
def delete_calibration_point(pid):
    db = get_db()
    db.execute('DELETE FROM calibration_points WHERE id=?', (pid,))
    db.commit()
    return jsonify(state_json())


# ---------------------------------------------------------------- 候选 / 版本

def _candidate_plans():
    events, deps, excls = load_state()
    sources, points, _ = load_clock()
    locks = current_locks(sources)

    def conflict_count(cal_map):
        view = []
        for e in events:
            v = dict(e)
            m = cal_map.get(e['id'])
            if m:
                v['start_ts'] = round(m['start_ts'])
                v['end_ts'] = round(m['end_ts']) if m['end_ts'] is not None else None
            view.append(v)
        an = analysis.analyze(view, deps, excls)
        return sum(1 for c in an['conflicts'] if c['severity'] in ('high', 'medium'))

    return calib.candidates(sources, points, events, locks, deps, excls,
                            conflict_count)


@app.post('/api/calibration/candidates')
def api_candidates():
    sources, _, _ = load_clock()
    if not sources:
        return err('请先建立时钟来源')
    try:
        plans = _candidate_plans()
    except calib.LinAlgError as ex:
        return err(f'锁定参数互相矛盾, 无法求解: {ex}')
    return jsonify({'plans': plans})


@app.post('/api/calibration/versions')
def save_calibration_version():
    """把选定候选另存为校准版本(不改写事件原始时间)。
    body: {"plan_key": "robust", "label": "..."}"""
    data = request.get_json(force=True) or {}
    plan_key = data.get('plan_key')
    plans = _candidate_plans()
    plan = next((p for p in plans if p['key'] == plan_key), None)
    if plan is None:
        return err('候选方案不存在')
    sources, points, _ = load_clock()
    sig = calib.version_signature(sources, points)
    detail = json.dumps({
        'plan_key': plan['key'], 'plan_name': plan['name'],
        'fit': plan['fit'],
        'conflict_count': plan['conflict_count'],
        'total_correction_ms': plan['total_correction_ms'],
        'max_residual_ms': plan['max_residual_ms'],
        'sigma_ms': plan['sigma_ms'],
        'contradictory_point_ids': plan['contradictory_point_ids'],
        'dropped_point_ids': plan['dropped_point_ids'],
        'locks': current_locks(sources),
    }, ensure_ascii=False)
    db = get_db()
    cur = db.execute(
        'INSERT INTO calibration_versions(label, created_at, active, signature, detail) '
        'VALUES (?,?,0,?,?)',
        (str(data.get('label') or plan['name']).strip() or plan['name'],
         int(time.time()), sig, detail))
    # 新版本默认激活
    db.execute('UPDATE calibration_versions SET active=0')
    db.execute('UPDATE calibration_versions SET active=1 WHERE id=?', (cur.lastrowid,))
    db.commit()
    return jsonify(state_json())


@app.post('/api/calibration/versions/<int:vid>/activate')
def activate_calibration_version(vid):
    db = get_db()
    row = db.execute('SELECT id FROM calibration_versions WHERE id=?', (vid,)).fetchone()
    if not row:
        return err('校准版本不存在', 404)
    db.execute('UPDATE calibration_versions SET active=0')
    db.execute('UPDATE calibration_versions SET active=1 WHERE id=?', (vid,))
    db.commit()
    return jsonify(state_json())


@app.delete('/api/calibration/versions/<int:vid>')
def delete_calibration_version(vid):
    db = get_db()
    db.execute('DELETE FROM calibration_versions WHERE id=?', (vid,))
    db.commit()
    return jsonify(state_json())


# ---------------------------------------------------------------- 导出 / 演示 / 清空

@app.get('/api/export')
def export_html():
    events, deps, excls = load_state()
    an = analysis.analyze(events, deps, excls)
    plans = analysis.solve_plans(events, deps, excls)
    display_tz = request.args.get('tz') or 'UTC'
    doc = export_mod.render_export(events, deps, excls, an, plans, display_tz=display_tz)
    return Response(doc, mimetype='text/html',
                    headers={'Content-Disposition': 'attachment; filename="postmortem.html"'})


@app.post('/api/seed')
def seed():
    push_snapshot('载入演示数据')
    _clear_all()
    _insert_demo()
    return jsonify(state_json())


@app.post('/api/reset')
def reset():
    push_snapshot('清空数据')
    _clear_all()
    return jsonify(state_json())


def _clear_all():
    db = get_db()
    db.execute('DELETE FROM events')
    db.execute('DELETE FROM dependencies')
    db.execute('DELETE FROM exclusions')
    db.execute('DELETE FROM calibration_points')
    db.execute('DELETE FROM clock_sources')
    db.execute('DELETE FROM calibration_versions')
    db.commit()


def _insert_demo():
    """电商故障复盘演示数据, 故意包含各类冲突。"""
    db = get_db()
    D = '2026-09-08 '
    Z = '+08:00'

    def t(s):
        return parse_time(D + s + ' ' + Z, 'UTC')[0]

    events = [
        # title, start, end, conf, source, evidence, group, locked
        ('发布 v2.31 开始', t('14:00:00'), t('14:05:00'), 'high', 'deploy.log',
         '14:00:00 deploy v2.31 started by release-bot', '变更', 0),
        ('配置中心推送限流规则', t('14:02:30'), None, 'medium', 'config-audit',
         'push ratelimit rule rl-882 to order-service', '变更', 0),
        ('订单服务错误率告警', t('14:03:10'), None, 'high', 'Prometheus',
         'ALERT order_error_rate > 5% (value 17.2%)', '监控', 0),
        ('数据库主库 CPU 打满', t('14:01:20'), t('14:20:40'), 'high', 'CloudWatch',
         'db-master cpu_utilization >= 95% for 19m', '数据库', 0),
        ('ORDER 表全表扫描激增', t('14:01:05'), t('14:18:00'), 'medium', 'slow-query.log',
         'rows_examined=48M, SELECT * FROM orders WHERE ...', '数据库', 0),
        ('缓存节点重启(日志机时钟存疑)', t('13:59:00'), t('13:59:40'), 'low', 'cache-node-7.log',
         '13:59:00 cache node 7 restarting...', '应用', 0),
        ('缓存连接池耗尽(日志机时钟存疑)', t('13:58:20'), t('13:58:50'), 'low', 'cache-node-7.log',
         '13:58:20 pool exhausted, evicting...', '应用', 0),
        ('订单服务自动重启', t('14:06:00'), t('14:07:30'), 'medium', 'k8s events',
         'pod order-7d9f restarted (OOMKilled)', '应用', 0),
        ('流量切换至备用集群', t('14:15:00'), None, 'low', '运维值班记录',
         '14:15 值班长下令切流到 dr-cluster', '恢复', 0),
        ('告警恢复', t('14:21:00'), None, 'high', 'Prometheus',
         'RESOLVED order_error_rate', '恢复', 0),
        ('用户投诉高峰', t('14:10:00'), t('14:25:00'), 'medium', '客服系统',
         '14:10-14:25 投诉工单 320 件, 为日常 8 倍', '监控', 0),
        ('数据库切换只读模式', t('14:08:00'), t('14:12:00'), 'low', 'DBA 记录',
         'set global read_only=ON (事后回忆, 无审计日志)', '数据库', 0),
        ('回滚操作', t('14:30:00'), t('14:25:00'), 'medium', 'deploy.log',
         'rollback v2.31 -> v2.30 (时间疑似录入错误)', '恢复', 0),
    ]
    ids = []
    for (title, s, e, conf, src, evi, grp, locked) in events:
        cur = db.execute(
            'INSERT INTO events(title,start_ts,end_ts,timezone,confidence,source,evidence,'
            'group_name,locked,created_at) VALUES (?,?,?,?,?,?,?,?,?,?)',
            (title, s, e, Z, conf, src, evi, grp, locked, int(time.time())))
        ids.append(cur.lastrowid)

    _insert_demo_clocks(db, ids, t)

    deps = [
        # type, from_idx, to_idx, min_gap_s, max_gap_s, note
        ('before', 0, 2, 0, None, '发布应先于告警结束(发布团队说法)'),
        ('before', 2, 0, 0, None, '客服团队认为告警在发布前就已出现'),
        ('triggers', 4, 3, 0, 120, '慢查询导致主库 CPU 升高'),
        ('triggers', 2, 7, 0, None, '告警触发自动重启策略'),
        ('triggers', 2, 5, 0, None, '告警后缓存节点重启(负延迟, 疑似时钟偏移)'),
        ('triggers', 2, 6, 0, None, '告警后缓存连接池耗尽(同一台日志机)'),
        ('during', 4, 3, 0, None, '慢查询应发生在 CPU 打满期间'),
        ('before', 8, 9, 0, None, '切流后告警才恢复'),
    ]
    for (tp, fi, ti, mn, mx, note) in deps:
        db.execute('INSERT INTO dependencies(type,from_id,to_id,min_gap_ms,max_gap_ms,note) '
                   'VALUES (?,?,?,?,?,?)',
                   (tp, ids[fi], ids[ti], mn * 1000, mx * 1000 if mx else None, note))
    db.execute('INSERT INTO exclusions(a_id,b_id,reason) VALUES (?,?,?)',
               (ids[3], ids[11], '主库高负载时不应处于只读模式(状态互斥)'))
    db.commit()


def _insert_demo_clocks(db, ids, t):
    """多源时钟校准演示:
      - NTP 基准(Prometheus/CloudWatch 走机房授时, 视为基准);
      - cache-node-7 日志机: 时钟慢约 4 分钟, 记录值早于真实时间;
      - 边缘网关客服系统: 有轻微漂移;
      - DBA 手抄记录: 只有一个配点, 漂移不可辨识(欠定示例)。
    另有一条配点故意矛盾, 供稳健拟合识别剔除。
    事件 ids 顺序见 _insert_demo: 2=告警 5=缓存重启 6=连接池耗尽
    10=告警恢复 11=投诉高峰。
    """
    def add_source(name, desc, base=0):
        cur = db.execute(
            'INSERT INTO clock_sources(name,description,is_baseline,created_at) '
            'VALUES (?,?,?,?)',
            (name, desc, base, int(time.time())))
        return cur.lastrowid

    s_ntp = add_source('机房 NTP 授时(Prometheus/CloudWatch)',
                       '监控与云指标统一走机房 NTP, 作为基准时钟', 1)
    s_cache = add_source('cache-node-7 日志机',
                         '本地 ntpd 故障, 时钟整体偏慢, 另有轻微漂移')
    s_gw = add_source('边缘网关(客服系统)', '跨地域网关, 时钟有线性漂移')
    s_dba = add_source('DBA 手抄记录', '人工回忆补记, 只有一条配点')

    def mk_event(title, ts, src_id, text, group='校准时标'):
        cur = db.execute(
            'INSERT INTO events(title,start_ts,end_ts,timezone,confidence,source,'
            'evidence,group_name,clock_source_id,created_at) '
            'VALUES (?,?,?,?,?,?,?,?,?,?)',
            (title, ts, None, '+08:00', 'high', text, text, group, src_id,
             int(time.time())))
        return cur.lastrowid

    # 把已有事件挂到来源
    db.execute('UPDATE events SET clock_source_id=? WHERE id IN (?,?,?)',
               (s_ntp, ids[2], ids[3], ids[9]))
    db.execute('UPDATE events SET clock_source_id=? WHERE id IN (?,?)',
               (s_cache, ids[5], ids[6]))
    db.execute('UPDATE events SET clock_source_id=? WHERE id=?', (s_gw, ids[10]))
    db.execute('UPDATE events SET clock_source_id=? WHERE id=?', (s_dba, ids[11]))

    # cache-node-7 时钟(演示): 参考时刻慢约 250 秒 + 漂移 3 秒/天。
    # 配点 1(13:59 附近): 缓存连接池耗尽, 缓存机日志(ids[6]=13:58:20) vs
    # 基准侧监控代理观测(14:02:30), 差 250 秒
    n_pool = mk_event('[校准时标] 连接池耗尽(基准侧观测)',
                      t('14:02:30'), s_ntp, '14:02:30 NTP-synced: cache pool exhausted')
    db.execute('INSERT INTO calibration_points'
               '(a_event_id,b_event_id,a_source_id,b_source_id,tolerance_ms,note,created_at) '
               'VALUES (?,?,?,?,?,?,?)',
               (ids[6], n_pool, s_cache, s_ntp, 2000,
                '连接池耗尽: 日志机 13:58:20 vs 基准 14:02:30(慢约 4 分 10 秒)',
                int(time.time())))

    # 配点 2(约 10 分钟后): 缓存服务恢复, 基准 14:08:00 vs 日志机 14:03:50
    # (偏差 250 秒, 与配点1相差亚秒级 => 0.3 秒/天的微小漂移)
    n_rec = mk_event('[校准时标] 缓存服务恢复(基准侧观测)',
                     t('14:08:00'), s_ntp, '14:08:00 NTP-synced: cache service recovered')
    c_rec = mk_event('[校准时标] 缓存服务恢复(缓存机日志)',
                     t('14:03:50'), s_cache, '14:03:50 cache service recovered (local clock)')
    db.execute('INSERT INTO calibration_points'
               '(a_event_id,b_event_id,a_source_id,b_source_id,tolerance_ms,note,created_at) '
               'VALUES (?,?,?,?,?,?,?)',
               (c_rec, n_rec, s_cache, s_ntp, 2000,
                '缓存恢复: 与配点1时刻拉开约 10 分钟, 亚秒级偏差 => 微小漂移',
                int(time.time())))

    # 矛盾配点: 缓存节点重启(ids[5]=13:59:00, 真实约 14:03:10)被人工错误对应到
    # 14:06 的基准观测, 差近 3 分钟、远超标称误差;
    # 普通拟合被带偏, 稳健拟合应剔除
    n_bad = mk_event('[校准时标] 缓存重启(矛盾的人工对应)',
                     t('14:06:00'), s_ntp, '14:06:00 人工猜测的重启时刻(与自动观测矛盾)')
    db.execute('INSERT INTO calibration_points'
               '(a_event_id,b_event_id,a_source_id,b_source_id,tolerance_ms,note,created_at) '
               'VALUES (?,?,?,?,?,?,?)',
               (ids[5], n_bad, s_cache, s_ntp, 2000,
                '人工对应 14:06 与时钟推算的 ~14:03:10 矛盾(稳健拟合应剔除)',
                int(time.time())))

    # 边缘网关(客服): 慢约 5 秒, 且漂移明显(偏差 5s→6s, 约 96 秒/天)
    g1 = mk_event('[校准时标] 投诉工单首件(网关时间戳)',
                  t('14:10:00'), s_gw, '14:10:00 gateway ts, first complaint ticket')
    n1 = mk_event('[校准时标] 投诉工单首件(受理系统时间)',
                  t('14:10:05'), s_ntp, '14:10:05 support system, first complaint')
    db.execute('INSERT INTO calibration_points'
               '(a_event_id,b_event_id,a_source_id,b_source_id,tolerance_ms,note,created_at) '
               'VALUES (?,?,?,?,?,?,?)',
               (g1, n1, s_gw, s_ntp, 2000,
                '投诉首件: 网关 14:10:00 vs 受理系统 14:10:05', int(time.time())))
    g2 = mk_event('[校准时标] 投诉工单末件(网关时间戳)',
                  t('14:25:00'), s_gw, '14:25:00 gateway ts, last complaint ticket')
    n2 = mk_event('[校准时标] 投诉工单末件(受理系统时间)',
                  t('14:25:06'), s_ntp, '14:25:06 support system, last complaint')
    db.execute('INSERT INTO calibration_points'
               '(a_event_id,b_event_id,a_source_id,b_source_id,tolerance_ms,note,created_at) '
               'VALUES (?,?,?,?,?,?,?)',
               (g2, n2, s_gw, s_ntp, 2000,
                '投诉末件: 偏差增大到 6 秒, 说明网关时钟存在明显线性漂移',
                int(time.time())))

    # DBA 手抄: 只有一个配点 -> 漂移不可辨识(欠定), 偏移约 -20s
    n_dba = mk_event('[校准时标] 只读切换(审计代理近似时刻)',
                     t('14:08:20'), s_ntp, '14:08:20 audit proxy approximate time')
    db.execute('INSERT INTO calibration_points'
               '(a_event_id,b_event_id,a_source_id,b_source_id,tolerance_ms,note,created_at) '
               'VALUES (?,?,?,?,?,?,?)',
               (ids[11], n_dba, s_dba, s_ntp, 30000,
                '仅一条配点, 只能估偏移, 漂移不可辨识', int(time.time())))


init_db()

if __name__ == '__main__':
    app.run(host='127.0.0.1', port=5000, debug=False)
