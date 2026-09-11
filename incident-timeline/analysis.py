"""事故时序校验台 —— 依赖/区间推理与冲突检测引擎。

输入: 事件(区间)、依赖(先于/触发/持续期间)、互斥状态;
输出: 因果环、前置倒置、不可能时长、互斥重叠、疑似时钟偏移,
以及最短冲突链、证据引用与最小调整方案。

所有时间均为 UTC 毫秒。事件 end_ts 为 None 表示瞬时事件。
"""
import heapq
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
            # 分离位移须按端点距离计算: 一个事件包含另一个时,
            # 仅移动重叠量 ov 无法将两者分开
            shift_ab = ev_end(a) - b['start_ts']   # 让 A 整体移到 B 之前
            shift_ba = ev_end(b) - a['start_ts']   # 让 B 整体移到 A 之前
            fixes = [[(a['id'], -shift_ab, -shift_ab)],
                     [(b['id'], shift_ab, shift_ab)],
                     [(b['id'], -shift_ba, -shift_ba)],
                     [(a['id'], shift_ba, shift_ba)]]
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
#
# 移动量度量: 每个事件的移动量 = max(|开始位移|, |结束位移|),
# 方案总移动量 = 各事件移动量之和。瞬时事件(end_ts=None)结束位移视为 0。
# 修复动作表示为 [(事件id, 开始位移, 结束位移), ...]。

def _apply_op(sim, op):
    for eid, ds, de in op:
        ev = sim[eid]
        ev['start_ts'] += ds
        if ev['end_ts'] is not None:
            ev['end_ts'] += de


def _op_cost(op):
    """单个修复动作的代价, 与方案移动量度量一致。"""
    return sum(max(abs(ds), abs(de)) for _, ds, de in op)


def _plan_from_sim(events, sim, initial, remaining):
    """由最终状态生成方案: 按起止端点的实际变化计算每个事件的移动量。"""
    moves = []
    total = 0
    for e in events:
        s = sim[e['id']]
        orig_end = e['end_ts'] if e['end_ts'] is not None else e['start_ts']
        new_end = s['end_ts'] if s['end_ts'] is not None else s['start_ts']
        ds = s['start_ts'] - e['start_ts']
        de = new_end - orig_end
        if ds == 0 and de == 0:
            continue
        mv = max(abs(ds), abs(de))
        total += mv
        moves.append({'event_id': e['id'], 'title': e['title'],
                      'from_start': e['start_ts'], 'from_end': e['end_ts'],
                      'to_start': s['start_ts'], 'to_end': s['end_ts'],
                      'delta_start_ms': ds, 'delta_end_ms': de,
                      # 兼容字段: 单一代表位移(优先开始端, 开始不变时取结束端)
                      'delta_ms': ds if ds != 0 else de,
                      'move_ms': mv})
    return {'moves': moves,
            'total_shift_ms': total,
            'resolved': len(initial) - len(remaining),
            'unresolved': [public_conflict(c) for c in remaining]}


def _ckey(c):
    """冲突身份: 同一冲突在迭代中保持稳定(用于失败尝试计数与放弃标记)。"""
    return (c['type'],
            tuple(sorted(c.get('dep_ids') or [])),
            tuple(sorted(c.get('event_ids') or [])))


def _fix_budget(c):
    """同一冲突沿单条路径允许的最大修复尝试次数。

    正常冲突一次修复即解; 时钟偏移组可能每条边修一次;
    预算需覆盖该冲突的修复选项数, 同时阻止不可能约束的无限振荡。
    """
    return max(2, len(c.get('dep_ids') or []), len(c.get('fixes') or []))


def _op_sig(op):
    return tuple(sorted(op))


def _workable(confs, sim, excluded=()):
    """[(key, conflict, [有效修复选项])]: 未排除、未锁定、有可行修复的冲突。"""
    out = []
    for c in confs:
        key = _ckey(c)
        if key in excluded:
            continue
        valid = [o for o in c.get('fixes', [])
                 if o and all(not sim[eid].get('locked') for eid, _, _ in o)]
        if valid:
            out.append((key, c, valid))
    return out


def _rank_fixes(options, strategy, tried=()):
    """贪心策略: 有效修复动作按策略偏好排序(避开本冲突已尝试过的)。"""
    avail = [o for o in options if _op_sig(o) not in tried]
    if not avail:
        return []

    def net(o):
        return sum(ds + de for _, ds, de in o)

    if strategy == 'push_later':
        return sorted(avail, key=lambda o: (net(o) < 0, _op_cost(o)))
    if strategy == 'pull_earlier':
        return sorted(avail, key=lambda o: (net(o) > 0, _op_cost(o)))
    return sorted(avail, key=_op_cost)


def _sig_after(sim, op):
    """应用修复动作后的状态签名(不改动 sim)。"""
    deltas = {eid: (ds, de) for eid, ds, de in op}
    sig = []
    for i, s in sim.items():
        ds, de = deltas.get(i, (0, 0))
        en = s['end_ts'] + de if s['end_ts'] is not None else None
        sig.append((i, s['start_ts'] + ds, en))
    return tuple(sorted(sig))


def _greedy(events, deps, excls, strategy, excluded=frozenset(), max_iter=200):
    """贪心迭代求解。每个修复动作应用前做状态级振荡检测: 会让全局状态
    回到已见值的动作(如"把刚沿链移后的事件又移回去")直接跳过, 改选
    下一候选, 使调整能沿后继链传播; 全部候选都试过或都会导致振荡的
    冲突留在未解决清单中。"""
    sim = {e['id']: dict(e) for e in events}
    initial = detect(list(sim.values()), deps, excls)
    tried = defaultdict(set)
    seen = {_state_sig(sim)}
    confs = initial
    for _ in range(max_iter):
        op = chosen = None
        for key, c, valid in _workable(confs, sim, excluded):
            for o in _rank_fixes(valid, strategy, tried[key]):
                if _sig_after(sim, o) in seen:
                    tried[key].add(_op_sig(o))   # 导致振荡的动作, 不再选
                    continue
                op, chosen = o, key
                break
            if op:
                break
        if not op:
            break
        tried[chosen].add(_op_sig(op))
        _apply_op(sim, op)
        seen.add(_state_sig(sim))
        confs = detect(list(sim.values()), deps, excls)
    return _plan_from_sim(events, sim, initial, confs)


def _infeasible_cycle_dep_ids(events, deps):
    """差分约束可行性判定: 环上 (from 事件时长 + 最小间隔) 权重之和 > 0
    的依赖环, 任何时间赋值都无法满足(如两条互相矛盾的"先于"且事件
    有时长)。返回这些环上依赖的 id 集合。before/triggers 修复只做整体
    平移, 时长不变, 因此用当前时长判定是精确的。
    """
    evs = {e['id']: e for e in events}
    infeasible = set()
    for cyc in find_cycles(deps, [e['id'] for e in events]):
        weight = 0
        cyc_dep_ids = []
        for (u, d, _label, _v) in cyc:
            cyc_dep_ids.append(d['id'])
            if d['type'] == 'before':
                src = evs.get(d['from_id'])
                if src is not None:
                    weight += ev_end(src) - src['start_ts']
                weight += d.get('min_gap_ms') or 0
            elif d['type'] == 'triggers':
                weight += d.get('min_gap_ms') or 0
            # during 派生边权重为 0(只约束开始先后)
        if weight > 0:
            infeasible.update(cyc_dep_ids)
    return infeasible


def _structural_excluded(events, deps, excls):
    """不可解冲突的键集合: 依赖落在不可行环上的冲突, 移动事件无法消除,
    求解时应排除(留在未解决清单中如实报告)。"""
    bad_deps = _infeasible_cycle_dep_ids(events, deps)
    if not bad_deps:
        return frozenset()
    return frozenset(_ckey(c) for c in detect(events, deps, excls)
                     if any(d in bad_deps for d in (c.get('dep_ids') or [])))


def _solve(events, deps, excls, strategy):
    """贪心求解: 排除不可行环上的冲突后迭代修复。"""
    excluded = _structural_excluded(events, deps, excls)
    return _greedy(events, deps, excls, strategy, excluded=excluded)


def _solve_optimal(events, deps, excls, max_expand=5000):
    """min_total: 排除不可行环上的冲突后, 在状态空间上 Dijkstra 搜索
    总移动量最小的无冲突方案(调整沿后继链传播, 锁定事件不动)。

    目标状态 = 不存在可行修复; 边权 = 修复动作代价(非负), 首个出队的
    目标状态即最优。同一冲突沿一条路径的修复尝试次数受限; 超出扩展
    上限时回退为贪心结果。
    """
    excluded = _structural_excluded(events, deps, excls)
    sim0 = {e['id']: dict(e) for e in events}
    initial = detect(list(sim0.values()), deps, excls)
    if not _workable(initial, sim0, excluded):
        return _plan_from_sim(events, sim0, initial, initial)

    INF = 10 ** 18
    # 堆元素: (累计移动量, 序号, sim, 各冲突已尝试次数)
    pq = [(0, 0, sim0, frozenset())]
    best_known = {}
    counter = 1
    best = None
    expands = 0
    while pq and expands < max_expand:
        cost, _, sim, attempts = heapq.heappop(pq)
        expands += 1
        att = dict(attempts)
        confs = detect(list(sim.values()), deps, excls)
        workable = _workable(confs, sim, excluded)
        if not workable:
            best = (sim, confs)
            break
        for key, c, valid in workable:
            if att.get(key, 0) >= _fix_budget(c):
                continue
            natt = tuple(sorted({**att, key: att.get(key, 0) + 1}.items()))
            for o in valid:
                nsim = {i: dict(s) for i, s in sim.items()}
                _apply_op(nsim, o)
                ncost = cost + _op_cost(o)
                sig = (_state_sig(nsim), natt)
                if best_known.get(sig, INF) <= ncost:
                    continue
                best_known[sig] = ncost
                heapq.heappush(pq, (ncost, counter, nsim, natt))
                counter += 1
    if best is None:
        return _solve(events, deps, excls, 'min_total')   # 超预算, 回退贪心
    sim, confs = best
    return _plan_from_sim(events, sim, initial, confs)


def _state_sig(sim):
    return tuple(sorted((i, s['start_ts'], s['end_ts']) for i, s in sim.items()))


def solve_plans(events, deps, excls):
    """生成若干最小调整方案(锁定事件不参与移动)。"""
    strategies = [
        ('push_later', '方案 A · 顺延后续事件', '优先把后续事件向后顺延,保持前置事件时间不变'),
        ('pull_earlier', '方案 B · 提前前置事件', '优先把前置事件提前,保持后续事件时间不变'),
        ('min_total', '方案 C · 最小总移动量', '搜索总移动量最小的可行方案,不移动锁定事件'),
    ]
    plans = []
    seen = set()
    for key, name, desc in strategies:
        if key == 'min_total':
            plan = _solve_optimal(events, deps, excls)
        else:
            plan = _solve(events, deps, excls, key)
        plan.update(strategy=key, name=name, description=desc)
        sig = tuple(sorted((m['event_id'], m['to_start'], m['to_end'])
                           for m in plan['moves']))
        plan['duplicate'] = bool(plan['moves']) and sig in seen
        seen.add(sig)
        plans.append(plan)
    return plans
