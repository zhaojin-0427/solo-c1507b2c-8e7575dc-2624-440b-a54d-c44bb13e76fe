"""事故时序校验台 —— 依赖/区间推理与冲突检测引擎。

输入: 事件(区间)、依赖(先于/触发/持续期间)、互斥状态;
输出: 因果环、前置倒置、不可能时长、互斥重叠、疑似时钟偏移,
以及最短冲突链、证据引用与最小调整方案。

所有时间均为 UTC 毫秒。事件 end_ts 为 None 表示瞬时事件。
"""
from collections import defaultdict, deque

from timeutil import fmt_ms

DEP_LABEL = {'before': '先于', 'triggers': '触发', 'during': '包含'}
CONF_RANK = {'high': 3, 'medium': 2, 'low': 1}

TYPE_LABEL = {
    'cycle': '因果环',
    'inversion': '前置倒置',
    'duration': '不可能时长',
    'overlap': '互斥状态重叠',
    'skew': '疑似时钟偏移',
    'containment': '期间包含矛盾',
    'latency': '触发延迟异常',
}
SEVERITY_LABEL = {'high': '高', 'medium': '中', 'low': '低'}


def ev_end(e):
    return e['end_ts'] if e['end_ts'] is not None else e['start_ts']


# ---------------------------------------------------------------- 图结构

def dep_edges(deps):
    """把依赖统一为有向边 (u, v, dep, label): u 必须先于 v 发生。"""
    for d in deps:
        if d['type'] in ('before', 'triggers'):
            yield d['from_id'], d['to_id'], d, DEP_LABEL[d['type']]
        elif d['type'] == 'during':
            # from 发生在 to 的持续期间 => to 必须先开始
            yield d['to_id'], d['from_id'], d, '包含'


def _adjacency(deps):
    adj = defaultdict(list)
    for u, v, d, label in dep_edges(deps):
        adj[u].append((v, d, label))
    return adj


def shortest_path(deps, src, dst):
    """BFS 求 src -> dst 的最短依赖路径, 返回 [(u, dep, label, v), ...]。"""
    adj = _adjacency(deps)
    q = deque([(src, [])])
    seen = {src}
    while q:
        n, path = q.popleft()
        for w, d, label in adj.get(n, []):
            if w == dst:
                return path + [(n, d, label, w)]
            if w not in seen:
                seen.add(w)
                q.append((w, path + [(n, d, label, w)]))
    return None


def find_cycles(deps, event_ids):
    """找出依赖图中的基本环(Tarjan SCC + SCC 内最短环)。"""
    adj = _adjacency(deps)
    cycles = []
    # 自环
    for u, v, d, label in dep_edges(deps):
        if u == v:
            cycles.append([(u, d, label, v)])
    # Tarjan 强连通分量(迭代实现)
    index_of, low, on, st = {}, {}, set(), []
    sccs = []
    counter = [0]
    for root in event_ids:
        if root in index_of:
            continue
        index_of[root] = low[root] = counter[0]
        counter[0] += 1
        st.append(root)
        on.add(root)
        work = [(root, iter(adj.get(root, [])))]
        while work:
            v, it = work[-1]
            descended = False
            for w, _d, _l in it:
                if w not in index_of:
                    index_of[w] = low[w] = counter[0]
                    counter[0] += 1
                    st.append(w)
                    on.add(w)
                    work.append((w, iter(adj.get(w, []))))
                    descended = True
                    break
                elif w in on:
                    low[v] = min(low[v], index_of[w])
            if descended:
                continue
            work.pop()
            if work:
                pv = work[-1][0]
                low[pv] = min(low[pv], low[v])
            if low[v] == index_of[v]:
                scc = []
                while True:
                    w = st.pop()
                    on.discard(w)
                    scc.append(w)
                    if w == v:
                        break
                if len(scc) > 1:
                    sccs.append(scc)
    for scc in sccs:
        cyc = _extract_cycle(adj, set(scc), scc[0])
        if cyc:
            cycles.append(cyc)
    return cycles


def _extract_cycle(adj, scc, start):
    """在 SCC 内 BFS 找从 start 出发回到 start 的最短环。"""
    q = deque()
    seen = set()
    for w, d, label in adj.get(start, []):
        if w in scc:
            q.append([(start, d, label, w)])
            seen.add(w)
    while q:
        path = q.popleft()
        last = path[-1][3]
        if last == start:
            return path
        for w, d, label in adj.get(last, []):
            if w in scc and w not in seen:
                seen.add(w)
                q.append(path + [(last, d, label, w)])
    return None


# ---------------------------------------------------------------- 展示辅助

def _ev_step(eid, evs):
    e = evs.get(eid)
    if not e:
        return {'kind': 'event', 'id': eid, 'title': f'#{eid}'}
    return {'kind': 'event', 'id': eid, 'title': e['title'],
            'start_ts': e['start_ts'], 'end_ts': e['end_ts'],
            'timezone': e.get('timezone')}


def chain_from_path(path, evs):
    """[(u, dep, label, v), ...] -> 事件/依赖交替的冲突链。"""
    steps = []
    for (u, d, label, v) in path:
        if not steps:
            steps.append(_ev_step(u, evs))
        steps.append({'kind': 'edge', 'dep_id': d['id'], 'type': d['type'], 'label': label})
        steps.append(_ev_step(v, evs))
    return steps


def _evidence_for(event_ids, evs):
    out = []
    for i in event_ids:
        e = evs.get(i)
        if not e:
            continue
        out.append({
            'event_id': i, 'title': e['title'],
            'source': e.get('source') or '未标注来源',
            'evidence': e.get('evidence') or '',
            'confidence': e.get('confidence', 'medium'),
            'timezone': e.get('timezone') or 'UTC',
            'start_ts': e['start_ts'], 'end_ts': e['end_ts'],
        })
    return out


def _inversion_chain(d, a, b, deps, evs):
    """前置倒置的冲突链: 直接边 + 若存在的反向传递路径(构成环的证据)。"""
    base = [(a['id'], d, DEP_LABEL[d['type']], b['id'])]
    back = shortest_path(deps, b['id'], a['id'])
    if back:
        return chain_from_path(base + back, evs)
    return chain_from_path(base, evs)


# ---------------------------------------------------------------- 冲突检测

def detect(events, deps, excls):
    """检测全部冲突。返回内部结构(含 fixes,供求解器使用)。"""
    evs = {e['id']: e for e in events}
    conflicts = []

    def add(**kw):
        kw.setdefault('severity', 'high')
        kw.setdefault('dep_ids', [])
        kw.setdefault('fixes', [])
        kw.setdefault('amount_ms', 0)
        kw.setdefault('chain', None)
        kw['evidence'] = _evidence_for(kw.get('event_ids', []), evs)
        conflicts.append(kw)

    # ---- 不可能时长: 结束早于开始 ----
    for e in events:
        if e['end_ts'] is not None and e['end_ts'] < e['start_ts']:
            v = e['start_ts'] - e['end_ts']
            add(type='duration',
                title=f'不可能时长:「{e["title"]}」结束早于开始',
                detail=f'结束时间比开始时间早 {fmt_ms(v)},通常是录入或时区转换错误。',
                event_ids=[e['id']], amount_ms=v,
                fixes=[[(e['id'], 0, v)], [(e['id'], -v, 0)]])

    # ---- 依赖违反 ----
    skew_groups = defaultdict(list)  # (来源A, 来源B) -> [(dep, a, b, 偏移)]
    for d in deps:
        a = evs.get(d['from_id'])
        b = evs.get(d['to_id'])
        if not a or not b:
            continue
        if d['type'] == 'before':
            gap = d.get('min_gap_ms') or 0
            v = ev_end(a) - (b['start_ts'] - gap)
            if v > 0:
                half = v // 2
                add(type='inversion', subtype='before',
                    title=f'前置倒置:「{a["title"]}」未先于「{b["title"]}」',
                    detail=(f'依赖要求「{a["title"]}」至少比「{b["title"]}」早 {fmt_ms(gap)} 结束,'
                            f'实际却晚 {fmt_ms(v)}。'),
                    event_ids=[a['id'], b['id']], dep_ids=[d['id']], amount_ms=v,
                    chain=_inversion_chain(d, a, b, deps, evs),
                    fixes=[[(a['id'], -v, -v)], [(b['id'], v, v)],
                           [(a['id'], -half, -half), (b['id'], v - half, v - half)]])
        elif d['type'] == 'triggers':
            # 触发延迟从成因「开始」起算: 长区间成因(如慢查询)在结果出现时往往仍在持续
            latency = b['start_ts'] - a['start_ts']
            mn = d.get('min_gap_ms') or 0
            mx = d.get('max_gap_ms')
            if latency < 0:
                key = (a.get('source') or a.get('timezone') or '未知来源',
                       b.get('source') or b.get('timezone') or '未知来源')
                skew_groups[key].append((d, a, b, -latency))
            elif latency < mn:
                v = mn - latency
                add(type='inversion', subtype='triggers',
                    title=f'触发间隔过短:「{a["title"]}」→「{b["title"]}」',
                    detail=(f'触发依赖要求最小间隔 {fmt_ms(mn)},实际仅 {fmt_ms(latency)},'
                            f'差 {fmt_ms(v)}。'),
                    event_ids=[a['id'], b['id']], dep_ids=[d['id']], amount_ms=v,
                    chain=_inversion_chain(d, a, b, deps, evs),
                    fixes=[[(b['id'], v, v)], [(a['id'], -v, -v)]])
            if mx is not None and latency > mx:
                v = latency - mx
                add(type='latency', severity='medium', subtype='triggers_max',
                    title=f'触发延迟异常:「{a["title"]}」→「{b["title"]}」相隔过久',
                    detail=(f'触发依赖要求最大延迟 {fmt_ms(mx)},实际 {fmt_ms(latency)},'
                            f'超出 {fmt_ms(v)};可能遗漏了中间事件或时间有误。'),
                    event_ids=[a['id'], b['id']], dep_ids=[d['id']], amount_ms=v,
                    chain=_inversion_chain(d, a, b, deps, evs),
                    fixes=[[(b['id'], -v, -v)], [(a['id'], v, v)]])
        elif d['type'] == 'during':
            lv = b['start_ts'] - a['start_ts']   # >0: a 开始得太早,越出 b 左边界
            rv = ev_end(a) - ev_end(b)           # >0: a 结束得太晚,越出 b 右边界
            if lv > 0 or rv > 0:
                fixes = []
                if lv > 0 and rv > 0:
                    # a 比 b 还长, 移动 a 无解, 只能压缩 a 或扩展 b
                    fixes.append([(a['id'], lv, -rv)])
                    fixes.append([(b['id'], -lv, rv)])
                else:
                    if lv > 0:
                        fixes.append([(a['id'], lv, lv)])
                        fixes.append([(b['id'], -lv, 0)])
                    if rv > 0:
                        fixes.append([(a['id'], -rv, -rv)])
                        fixes.append([(b['id'], 0, rv)])
                parts = []
                if lv > 0:
                    parts.append(f'开始早出 {fmt_ms(lv)}')
                if rv > 0:
                    parts.append(f'结束晚出 {fmt_ms(rv)}')
                add(type='containment',
                    title=f'期间包含矛盾:「{a["title"]}」未落在「{b["title"]}」期间内',
                    detail=f'依赖要求「{a["title"]}」完全处于「{b["title"]}」的持续期间内,'
                           f'实际{", ".join(parts)}。',
                    event_ids=[a['id'], b['id']], dep_ids=[d['id']],
                    amount_ms=max(lv, rv),
                    chain=_inversion_chain(d, b, a, deps, evs),
                    fixes=fixes)

    # ---- 因果环 ----
    ids = [e['id'] for e in events]
    for cyc in find_cycles(deps, ids):
        cyc_ids = []
        for (u, _d, _l, _v) in cyc:
            if u not in cyc_ids:
                cyc_ids.append(u)

        def edge_cost(item):
            _u, d, _l, _v = item
            fa = evs.get(d['from_id'], {})
            fb = evs.get(d['to_id'], {})
            return (CONF_RANK.get(fa.get('confidence'), 2)
                    + CONF_RANK.get(fb.get('confidence'), 2))
        weakest = min(cyc, key=edge_cost)
        wdep = weakest[1]
        wa = evs.get(wdep['from_id'], {}).get('title', '?')
        wb = evs.get(wdep['to_id'], {}).get('title', '?')
        names = ' → '.join((evs[i]['title'] if i in evs else f'#{i}')
                           for i in cyc_ids + [cyc_ids[0]])
        add(type='cycle',
            title=f'因果环:{names}',
            detail=('依赖构成闭环,任何时间赋值都无法同时满足全部约束,'
                    '仅靠移动事件无法消除。'
                    f'建议优先复核环上置信度最低的依赖「{wa}」{DEP_LABEL.get(wdep["type"], wdep["type"])}「{wb}」,'
                    '考虑删除、反转或放宽它。'),
            event_ids=cyc_ids, dep_ids=[x[1]['id'] for x in cyc],
            chain=chain_from_path(cyc, evs), fixes=[])

    # ---- 互斥状态重叠 ----
    for x in excls:
        a = evs.get(x['a_id'])
        b = evs.get(x['b_id'])
        if not a or not b:
            continue
        ov = min(ev_end(a), ev_end(b)) - max(a['start_ts'], b['start_ts'])
        if ov > 0:
            if a['start_ts'] <= b['start_ts']:
                fixes = [[(a['id'], -ov, -ov)], [(b['id'], ov, ov)]]
            else:
                fixes = [[(b['id'], -ov, -ov)], [(a['id'], ov, ov)]]
            add(type='overlap',
                title=f'互斥状态重叠:「{a["title"]}」与「{b["title"]}」',
                detail=(f'两者被标记为互斥({x.get("reason") or "未填写原因"}),'
                        f'但时间区间重叠了 {fmt_ms(ov)}。'),
                event_ids=[a['id'], b['id']], amount_ms=ov, fixes=fixes,
                chain=[_ev_step(a['id'], evs),
                       {'kind': 'edge', 'dep_id': x['id'], 'type': 'exclusion', 'label': '互斥'},
                       _ev_step(b['id'], evs)])

    # ---- 疑似时钟偏移: 触发依赖出现负延迟 ----
    for (sa, sb), items in skew_groups.items():
        offsets = sorted(it[3] for it in items)
        med = offsets[len(offsets) // 2]
        consistent = (len(offsets) > 1
                      and (offsets[-1] - offsets[0]) <= max(1000, 0.25 * med))
        ids_flat, dep_ids, chain = [], [], None
        fixes = []
        for (d, a, b, off) in items:
            dep_ids.append(d['id'])
            for i in (a['id'], b['id']):
                if i not in ids_flat:
                    ids_flat.append(i)
            if chain is None:
                chain = chain_from_path([(a['id'], d, '触发', b['id'])], evs)
            fixes.append([(b['id'], off, off)])
        # 同源事件整体校正
        group_ids = [e['id'] for e in events
                     if (e.get('source') or e.get('timezone') or '未知来源') == sb]
        if len(group_ids) > 1:
            fixes.append([(gid, med, med) for gid in group_ids])
        detail = (f'「{sa}」→「{sb}」的 {len(items)} 条触发依赖出现负延迟'
                  f'(结果早于成因 {fmt_ms(med)}),'
                  f'疑似「{sb}」的时钟偏慢约 {fmt_ms(med)}(记录时间早于实际时间)。')
        if consistent:
            detail += '多处偏移量接近,支持系统性时钟偏移的假设。'
        add(type='skew', severity='medium',
            title=f'疑似时钟偏移:「{sb}」相对「{sa}」偏慢约 {fmt_ms(med)}',
            detail=detail, event_ids=ids_flat, dep_ids=dep_ids, chain=chain,
            amount_ms=med, fixes=fixes)

    return conflicts


def public_conflict(c):
    """去掉内部字段(如 fixes),生成可下发/可导出的冲突描述。"""
    return {k: v for k, v in c.items() if k != 'fixes'}


def analyze(events, deps, excls):
    """对外入口: 排序、编号、补充标签。"""
    evs = {e['id']: e for e in events}
    confs = detect(events, deps, excls)
    order = {'high': 0, 'medium': 1, 'low': 2}
    confs.sort(key=lambda c: (order.get(c['severity'], 3), -c.get('amount_ms', 0)))
    out = []
    for i, c in enumerate(confs, 1):
        p = public_conflict(c)
        p['id'] = f'c{i}'
        if not p.get('chain'):
            p['chain'] = [_ev_step(eid, evs) for eid in p['event_ids']]
        p['type_label'] = TYPE_LABEL.get(p['type'], p['type'])
        p['severity_label'] = SEVERITY_LABEL.get(p['severity'], p['severity'])
        out.append(p)
    by_type = {}
    for c in out:
        by_type[c['type_label']] = by_type.get(c['type_label'], 0) + 1
    return {'conflicts': out, 'summary': {'total': len(out), 'by_type': by_type}}


# ---------------------------------------------------------------- 最小调整方案

def _choose_fix(options, strategy, sim):
    """从一条冲突的可行修复动作中按策略选一个。动作: [(事件id, 开始位移, 结束位移)]"""
    valid = [o for o in options
             if o and all(not sim[eid].get('locked') for eid, _ds, _de in o)]
    if not valid:
        return None

    def net(o):
        return sum(ds + de for _, ds, de in o)

    def cost(o):
        return sum(abs(ds) + abs(de) for _, ds, de in o)

    if strategy == 'push_later':
        return min(valid, key=lambda o: (net(o) < 0, cost(o)))
    if strategy == 'pull_earlier':
        return min(valid, key=lambda o: (net(o) > 0, cost(o)))
    return min(valid, key=cost)


def _solve(events, deps, excls, strategy, max_iter=120):
    sim = {e['id']: dict(e) for e in events}
    before = detect(list(sim.values()), deps, excls)
    seen_states = {_state_sig(sim)}
    for _ in range(max_iter):
        confs = detect(list(sim.values()), deps, excls)
        op = None
        for c in confs:
            if c.get('fixes'):
                op = _choose_fix(c['fixes'], strategy, sim)
                if op:
                    break
        if not op:
            break
        for eid, ds, de in op:
            ev = sim[eid]
            ev['start_ts'] += ds
            if ev['end_ts'] is not None:
                ev['end_ts'] += de
        sig = _state_sig(sim)
        if sig in seen_states:      # 进入振荡, 停止
            break
        seen_states.add(sig)
    after = detect(list(sim.values()), deps, excls)
    moves = []
    for e in events:
        s = sim[e['id']]
        if s['start_ts'] != e['start_ts'] or s['end_ts'] != e['end_ts']:
            moves.append({'event_id': e['id'], 'title': e['title'],
                          'from_start': e['start_ts'], 'from_end': e['end_ts'],
                          'to_start': s['start_ts'], 'to_end': s['end_ts'],
                          'delta_ms': s['start_ts'] - e['start_ts']})
    return {'moves': moves,
            'total_shift_ms': sum(abs(m['delta_ms']) for m in moves),
            'resolved': len(before) - len(after),
            'unresolved': [public_conflict(c) for c in after]}


def _state_sig(sim):
    return tuple(sorted((i, s['start_ts'], s['end_ts']) for i, s in sim.items()))


def solve_plans(events, deps, excls):
    """生成若干最小调整方案(锁定事件不参与移动)。"""
    strategies = [
        ('push_later', '方案 A · 顺延后续事件', '优先把后续事件向后顺延,保持前置事件时间不变'),
        ('pull_earlier', '方案 B · 提前前置事件', '优先把前置事件提前,保持后续事件时间不变'),
        ('min_total', '方案 C · 最小总移动量', '每一步都选择总移动量最小的可行调整'),
    ]
    plans = []
    seen = set()
    for key, name, desc in strategies:
        plan = _solve(events, deps, excls, key)
        plan.update(strategy=key, name=name, description=desc)
        sig = tuple(sorted((m['event_id'], m['to_start'], m['to_end'])
                           for m in plan['moves']))
        plan['duplicate'] = bool(plan['moves']) and sig in seen
        seen.add(sig)
        plans.append(plan)
    return plans
