"""事故时序校验台 —— Flask 应用入口。

本机运行: python3 app.py  然后访问 http://127.0.0.1:5000
"""
import json
import os
import sqlite3
import time

from flask import Flask, Response, g, jsonify, render_template, request

import analysis
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
    db.commit()
    db.close()


def load_state():
    db = get_db()
    events = [dict(r) for r in db.execute('SELECT * FROM events ORDER BY start_ts, id')]
    deps = [dict(r) for r in db.execute('SELECT * FROM dependencies ORDER BY id')]
    excls = [dict(r) for r in db.execute('SELECT * FROM exclusions ORDER BY id')]
    return events, deps, excls


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


def state_json():
    events, deps, excls = load_state()
    db = get_db()
    return {
        'events': events,
        'dependencies': deps,
        'exclusions': excls,
        'analysis': analysis.analyze(events, deps, excls),
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


init_db()

if __name__ == '__main__':
    app.run(host='127.0.0.1', port=5000, debug=False)
