"""事故时序校验台 —— 多源日志时钟校准引擎。

时钟模型
========
每个日志来源 s 一台时钟。记录值(原始时间) t 与基准时钟上的真实时间 T
之间为线性关系:

    T = (1 + d_s) * t + b_s

其中 b_s 为固定偏移(ms), d_s 为线性漂移率(无量纲, ms/ms, 通常极小)。
基准来源 b_s = d_s = 0。

校准点把两个来源中代表同一事实的两个瞬时观测 t_a(来源 A)、t_b(来源 B)
配成一组, 允许误差 ε(ms)。它给出一条观测方程:

    (1+d_A) t_a + b_a - (1+d_B) t_b - b_b = 0
=>  t_a - t_b + t_a d_a - t_b d_b + b_a - b_b = 0

求解: 带等式约束(锁定来源/偏移/漂移)的加权最小二乘(权重 1/ε²),
纯 Python 高斯消元实现, 不依赖 numpy。

诊断
====
- disconnected: 来源经校准点与基准不连通(图上不可达), 参数不可辨识;
- underdetermined: 连通但秩不足(如两个来源间只有一个校准点, 漂移不可辨识);
- contradictory: 某校准点残差超过允许误差 + 3σ, 或两条校准关系互相打架;
- 同源自配对、引用缺失事件、重复配对点 等结构性问题。

候选方案: 偏移+漂移 / 仅偏移(漂移锁 0) / 稳健拟合(自动降权离群点),
比较冲突数、总修正量、最大残差。所有时间均为 UTC 毫秒。
"""
from collections import defaultdict, deque

from timeutil import fmt_ms

# 漂移率展示换算: 内部 d 单位 ms/小时 -> 秒/天 (×24 小时 ÷1000)
DRIFT_DISPLAY_SCALE = 24.0 / 1000.0
SIGMA_K = 3.0                                   # 矛盾点判定系数


# ---------------------------------------------------------------- 图与连通性

def source_graph(sources, points):
    """来源 -> 邻接 [(other_source_id, point_id)] (仅含结构有效的点)。"""
    valid = {s['id'] for s in sources}
    adj = {s['id']: [] for s in sources}
    for p in points:
        a, b = p.get('a_source_id'), p.get('b_source_id')
        if a in valid and b in valid and a != b:
            adj[a].append((b, p['id']))
            adj[b].append((a, p['id']))
    return adj


def reachable_from_baseline(sources, points):
    """BFS: 返回 {来源id: 到基准经过的校准点 id 列表(最短链)}。"""
    base = baseline_source_id(sources)
    adj = source_graph(sources, points)
    if base is None:
        return {}, None
    prev = {base: (None, None)}
    q = deque([base])
    while q:
        u = q.popleft()
        for v, pid in adj.get(u, []):
            if v not in prev:
                prev[v] = (u, pid)
                q.append(v)
    chains = {}
    for sid in prev:
        chain, cur = [], sid
        while prev[cur][0] is not None:
            par, pid = prev[cur]
            chain.append(pid)
            cur = par
        chains[sid] = chain
    return chains, base


def baseline_source_id(sources):
    for s in sources:
        if s.get('is_baseline'):
            return s['id']
    return sources[0]['id'] if sources else None


def point_events(point, evs):
    a = evs.get(point.get('a_event_id'))
    b = evs.get(point.get('b_event_id'))
    return a, b


# ---------------------------------------------------------------- 线性代数(无 numpy)

def _solve_linear(A, b, tol=None):
    """高斯列主元消元解 Ax=b。返回 (x, rank)。rank < 变量数 时秩亏。"""
    n = len(A)
    M = [list(A[i]) + [b[i]] for i in range(n)]
    scale = max((max(abs(v) for v in row[:-1]) if row[:-1] else 0) for row in M) \
        if M else 0
    eps = tol if tol is not None else (scale or 1) * 1e-11
    piv_cols = []
    row = 0
    for col in range(n):
        if row >= n:
            break
        piv = max(range(row, n), key=lambda r: abs(M[r][col]))
        if abs(M[piv][col]) <= eps:
            continue
        M[row], M[piv] = M[piv], M[row]
        for r in range(n):
            if r == row:
                continue
            f = M[r][col] / M[row][col]
            if f == 0:
                continue
            for c in range(col, n + 1):
                M[r][c] -= f * M[row][c]
        piv_cols.append(col)
        row += 1
    # 无解(矛盾行)检测: 消元后近似 0=非0
    for r in M:
        if max(abs(v) for v in r[:-1]) <= eps and abs(r[-1]) > max(eps * 100, 1e-9):
            raise LinAlgError('约束相互矛盾, 无精确解')
    x = [0.0] * n
    for i, col in enumerate(piv_cols):
        x[col] = M[i][n] / M[i][col]
    return x, len(piv_cols)


class LinAlgError(Exception):
    pass


# ---------------------------------------------------------------- 参数求解

def _param_index(sources):
    """每个非基准来源两个参数: (b_idx, d_idx)。"""
    base = baseline_source_id(sources)
    idx, k = {}, 0
    for s in sources:
        if s['id'] == base:
            continue
        idx[s['id']] = (k, k + 1)
        k += 2
    return idx, base, k


# 内部时间单位: 小时(3600_000 ms)。漂移列系数用"小时"计, 与偏移列(系数 1)
# 尺度相当, 法方程条件数好; 残差/偏移/容差仍统一换算为毫秒对外。
TIME_UNIT_MS = 3_600_000.0


def _point_rows(point, evs, idx, t0):
    """校准点 -> 观测方程行 (coef: {参数列: 系数}, rhs_ms, weight)。

    稳定参数化(中心化 + 按小时缩放):
        修正量 = c_ms + d·u, 其中 u = (t - t0)/3600000 (小时)
        d 的单位为 ms/小时; T_a = T_b:
        t_a - t_b + c_a - c_b + d_a·u_a - d_b·u_b = 0
    => X·p = t_b - t_a (右端单位 ms)
    """
    a = evs.get(point.get('a_event_id'))
    b = evs.get(point.get('b_event_id'))
    if not a or not b:
        return None
    sa, sb = point.get('a_source_id'), point.get('b_source_id')
    if sa == sb or (sa not in idx and sb not in idx):
        return None
    ta, tb = a['start_ts'], b['start_ts']
    ua, ub = (ta - t0) / TIME_UNIT_MS, (tb - t0) / TIME_UNIT_MS
    coef = {}
    if sa in idx:
        ci, di = idx[sa]
        coef[ci] = coef.get(ci, 0) + 1.0
        coef[di] = coef.get(di, 0) + ua
    if sb in idx:
        ci, di = idx[sb]
        coef[ci] = coef.get(ci, 0) - 1.0
        coef[di] = coef.get(di, 0) - ub
    tol = max(float(point.get('tolerance_ms') or 0), 1.0)
    return coef, float(tb - ta), 1.0 / (tol * tol)


def fit(sources, points, events, locks, weights=None):
    """带锁定约束的加权最小二乘(中心化时间, 数值稳定)。

    时钟模型: T = t + c_s + d_s·(t - t0)/3600000
      c_s = 参考时刻 t0 处的修正量(ms); d_s = 漂移率(ms/小时)。
      对外换算: 无量纲漂移 d_ms/ms = d/3600000;
                offset_ms = c - d·t0/3600000 (T = (1+d')t + offset)。

    locks: {'sources': [sid...], 'offset': {sid: 值ms},
            'drift': {sid: 值ms/小时}}
      offset 锁定给的是"绝对偏移 offset_ms", 内部折成参考时刻约束。

    返回 dict:
      params      {sid: {'offset_ms','correction_ms'(参考时刻),'drift'(ms/小时), 各 locked 标志}}
      residuals   {point_id: 残差ms (T_a - T_b)}
      sigma       残差标准差(ms), 自由度不足时 None
      rank, npar, nobs, base_id, t0
      deficient_cols [{source_id, kind:'offset'/'drift'}]  不可辨识参数
    """
    evs = {e['id']: e for e in events}
    idx, base, npar = _param_index(sources)

    # 参考时刻: 全部有效观测的中点, 让中心化后的 u 数量级最小
    obs_ts = []
    for p in points:
        a = evs.get(p.get('a_event_id'))
        b = evs.get(p.get('b_event_id'))
        if a and b and p.get('a_source_id') != p.get('b_source_id'):
            obs_ts += [a['start_ts'], b['start_ts']]
    t0 = (min(obs_ts) + max(obs_ts)) / 2 if obs_ts else 0.0

    rows = []
    for p in points:
        r = _point_rows(p, evs, idx, t0)
        if r is None:
            continue
        coef, rhs, w0 = r
        w = (weights or {}).get(p['id'], w0)
        if w and w > 0:
            rows.append((p['id'], coef, rhs, w))

    # ---- 锁定 -> 等式约束行 ----
    # 变量顺序 c(ms), d(ms/小时)。"锁定来源" => c=d=0;
    # "锁定偏移 offset_ms=v" => c - d·t0/3600000 = v; "锁定漂移 d=v" => d = v。
    locked_src = set(locks.get('sources') or [])
    locked_off = dict(locks.get('offset') or {})
    locked_dft = dict(locks.get('drift') or {})
    C_rows, c_rhs = [], []
    for sid in list(locked_src):
        if sid in idx:
            ci, di = idx[sid]
            C_rows.append({ci: 1.0}); c_rhs.append(0.0)
            C_rows.append({di: 1.0}); c_rhs.append(0.0)
    for sid, val in locked_dft.items():
        if sid in idx and sid not in locked_src:
            _, di = idx[sid]
            C_rows.append({di: 1.0}); c_rhs.append(float(val))
    for sid, val in locked_off.items():
        if sid in idx and sid not in locked_src and sid not in locked_dft:
            ci, di = idx[sid]
            C_rows.append({ci: 1.0, di: -t0 / TIME_UNIT_MS}); c_rhs.append(float(val))

    # 用约束消元: 把每个被锁定的参数变量表达为自由变量的仿射函数
    # elim[var] = (const, {free_var: coef}), 即 p_var = const + Σ coef·p_f
    Cwork = [dict(r) for r in C_rows]
    cwork = list(c_rhs)
    used = [False] * len(Cwork)
    elim = {}
    for col in range(npar):
        cand = [i for i in range(len(Cwork))
                if not used[i] and abs(Cwork[i].get(col, 0)) > 1e-12]
        if not cand:
            continue
        i = max(cand, key=lambda r: abs(Cwork[r].get(col, 0)))
        used[i] = True
        row, piv = Cwork[i], Cwork[i][col]
        const_acc = cwork[i] / piv
        terms = {k: -v / piv for k, v in row.items()
                 if k != col and abs(v) > 1e-15}
        # 展开此前已消元的变量(锁定行只有一列, 正常不会出现, 保留以保证一般性)
        changed, guard = True, 0
        while changed and guard < npar + 2:
            changed = False
            guard += 1
            for ev_var, (ec, eterms) in list(elim.items()):
                if ev_var in terms:
                    f = terms.pop(ev_var)
                    const_acc += f * ec
                    for k2, v2 in eterms.items():
                        terms[k2] = terms.get(k2, 0) + f * v2
                    changed = True
        elim[col] = (const_acc, terms)
        # 从其余约束行中消去该列
        for j in range(len(Cwork)):
            if used[j]:
                continue
            f = Cwork[j].pop(col, 0)
            if f == 0:
                continue
            cwork[j] -= f * const_acc
            for k2, v2 in terms.items():
                Cwork[j][k2] = Cwork[j].get(k2, 0) - f * v2
    # 未消费的约束行只可能是冗余(锁定行均为单变量, 不会真正矛盾)

    free_cols = [c for c in range(npar) if c not in elim]

    # 构造自由变量的法方程 XtWX f = XtWy
    # 观测 Σ coef_j·p_j = rhs, 代入 p_j = const_j + Σ F_jk·f_k:
    #   f 系数 = Σ coef_j·F_jk; 右端 = rhs - Σ coef_j·const_j
    nf = len(free_cols)
    XtX = [[0.0] * nf for _ in range(nf)]
    Xty = [0.0] * nf
    rss = 0.0
    obs_rows = []   # (pid, free_coef, rhs_sub, sqrt_w) 供残差计算
    for pid, coef, rhs, w in rows:
        sw = w ** 0.5
        fc = [0.0] * nf
        const_part = 0.0
        for var, av in coef.items():
            if var in elim:
                ec, et = elim[var]
                const_part += av * ec
                for fv, fco in et.items():
                    fc[free_cols.index(fv)] += av * fco
            else:
                fc[free_cols.index(var)] += av
        rhs2 = rhs - const_part
        obs_rows.append((pid, fc, rhs2, sw))
        for i in range(nf):
            if fc[i] == 0:
                continue
            wai = sw * sw * fc[i]
            Xty[i] += wai * rhs2
            for j in range(nf):
                XtX[i][j] += wai * fc[j]

    # 解自由变量; 秩亏时加极小岭参数取最小范数解, 并标出不可辨识参数
    rank, null_free = _rank_and_null(XtX)
    deficient = set()
    sol_free = None
    if nf > 0:
        if rank < nf:
            scale = max((max(abs(v) for v in row) for row in XtX), default=0) or 1.0
            ridge = scale * 1e-10
            for fi in null_free:
                col = free_cols[fi]
                sid = _col_source(col, idx)
                if sid is not None:
                    deficient.add((sid, 'offset' if col % 2 == 0 else 'drift'))
        else:
            ridge = 0.0
        A = [row[:] for row in XtX]
        if ridge:
            for i in range(nf):
                A[i][i] += ridge
        sol_free = _solve_linear(A, Xty)[0]

    values = [0.0] * npar
    for i, col in enumerate(free_cols):
        values[col] = sol_free[i] if sol_free is not None else 0.0
    for col, (const_acc, terms) in elim.items():
        values[col] = const_acc + sum(v * values[k] for k, v in terms.items())

    # 残差
    residuals, rss_all = {}, 0.0
    for pid, fc, rhs2, sw in obs_rows:
        pred = sum(fc[i] * (sol_free[i] if sol_free is not None else 0.0)
                   for i in range(nf))
        r = pred - rhs2
        residuals[pid] = r
        rss_all += (sw * r) ** 2
    # 被结构过滤的点残差为 None
    nobs = len(obs_rows)
    dof = max(nobs - rank - len(elim), 0)
    sigma = (rss_all / dof) ** 0.5 if dof > 0 else None

    # 内部变量按来源成对排列: [c_s1(ms), d_s1(ms/小时), c_s2, d_s2, ...]
    # offset_ms = c - d·t0/3600000
    params = {}
    for sid, (ci, di) in idx.items():
        c_val, d_val = values[ci], values[di]
        off = c_val - d_val * t0 / TIME_UNIT_MS
        lo = sid in locked_src or sid in locked_off
        ld = sid in locked_src or sid in locked_dft
        params[sid] = {
            'correction_ms': c_val,
            'offset_ms': off,
            'drift_ms_per_hour': d_val,
            'drift': d_val / TIME_UNIT_MS,
            'locked_offset': bool(lo),
            'locked_drift': bool(ld),
        }
    params[base] = {'correction_ms': 0.0, 'offset_ms': 0.0,
                    'drift_ms_per_hour': 0.0, 'drift': 0.0,
                    'locked_offset': True, 'locked_drift': True}
    # 秩亏列映射到来源参数
    for sid, (ci, di) in idx.items():
        if ci not in elim and _free_col_of(ci, free_cols) in null_free:
            deficient.add((sid, 'offset'))
        if di not in elim and _free_col_of(di, free_cols) in null_free:
            deficient.add((sid, 'drift'))
    deficient_cols = [{'source_id': sid, 'kind': kind} for sid, kind in sorted(deficient)]

    return {
        'params': params, 'residuals': residuals, 'sigma': sigma,
        'rank': rank, 'npar': npar, 'nobs': nobs, 'base_id': base, 't0': t0,
        'deficient_cols': deficient_cols,
    }


def _rank_and_null(M):
    """对称半正定矩阵高斯消元, 返回 (秩, 近零列的索引集合)。"""
    n = len(M)
    if n == 0:
        return 0, set()
    A = [row[:] for row in M]
    scale = max((max(abs(v) for v in row) for row in A), default=0) or 1.0
    eps = scale * 1e-9
    nulls = set()
    rank = 0
    for col in range(n):
        piv = max(range(col, n), key=lambda r: abs(A[r][col]))
        if abs(A[piv][col]) <= eps:
            nulls.add(col)
            continue
        if piv != col:
            A[piv], A[col] = A[col], A[piv]
        d = A[col][col]
        rank += 1
        for r in range(n):
            if r == col:
                continue
            f = A[r][col] / d
            if f == 0:
                continue
            for c in range(col, n):
                A[r][c] -= f * A[col][c]
    return rank, nulls


def _free_col_of(var, free_cols):
    try:
        return free_cols.index(var)
    except ValueError:
        return -1


def _col_source(col, idx):
    """参数列 -> 来源 id (b 列偶数 / d 列奇数)。"""
    for sid, (bi, di) in idx.items():
        if col in (bi, di):
            return sid
    return None


# ---------------------------------------------------------------- 校准时间 / 误差带 / 指标

def calibrated_ts(ts, source_id, fit_result):
    """原始毫秒 -> 校准后毫秒。来源无参数(如不连通)时返回 None。"""
    if source_id is None or fit_result is None:
        return None
    p = fit_result.get('params', {}).get(source_id)
    if p is None:
        return None
    t0 = fit_result.get('t0', 0.0)
    return ts + p['correction_ms'] + p['drift_ms_per_hour'] * (ts - t0) / TIME_UNIT_MS


def correction_at(ts, source_id, fit_result):
    """该来源在时刻 ts 的修正量 = 校准值 - 原始值。"""
    if fit_result is None:
        return None
    p = fit_result.get('params', {}).get(source_id)
    if p is None:
        return None
    t0 = fit_result.get('t0', 0.0)
    return p['correction_ms'] + p['drift_ms_per_hour'] * (ts - t0) / TIME_UNIT_MS


def calibrate_events(events, event_source_map, fit_result):
    """返回 {event_id: {'start_ts', 'end_ts', 'shift_ms'}}; 无法校准则缺省。"""
    out = {}
    for e in events:
        sid = event_source_map.get(e['id'])
        if sid is None:
            continue
        cs = calibrated_ts(e['start_ts'], sid, fit_result)
        if cs is None:
            continue
        ce = calibrated_ts(e['end_ts'], sid, fit_result) if e.get('end_ts') is not None else None
        out[e['id']] = {'start_ts': cs, 'end_ts': ce,
                        'shift_ms': cs - e['start_ts'], 'source_id': sid}
    return out


def uncertainty_band(source_id, t_mid, points, fit_result, chains):
    """来源在 t_mid 处的误差带半宽(ms):
    链上各校准点 (|残差| + 允许误差) 的最大值, 沿传递链累积取平方和根。
    无观测约束的来源(仅基准)返回 0。"""
    if fit_result is None:
        return None
    if source_id == fit_result['base_id']:
        return 0.0
    chain_pids = chains.get(source_id)
    if not chain_pids:
        return None
    pmap = {p['id']: p for p in points}
    total = 0.0
    used = False
    for pid in chain_pids:
        p = pmap.get(pid)
        if not p:
            continue
        r = fit_result['residuals'].get(pid)
        if r is None:
            continue
        used = True
        v = abs(r) + float(p.get('tolerance_ms') or 0)
        total += v * v
    return total ** 0.5 if used else None


def metrics(fit_result, points, robust_sigma=None):
    """最大残差; 矛盾点数(残差 > 容差 + 3σ, σ 取稳健尺度)。"""
    max_abs = 0.0
    over = []
    sigma = robust_sigma if robust_sigma is not None else (fit_result.get('sigma') or 0.0)
    pmap = {p['id']: p for p in points}
    for pid, r in fit_result.get('residuals', {}).items():
        ar = abs(r)
        max_abs = max(max_abs, ar)
        tol = float(pmap[pid].get('tolerance_ms') or 0)
        if ar > tol + SIGMA_K * sigma:
            over.append(pid)
    return {'max_abs_residual_ms': max_abs,
            'contradictory_point_ids': over,
            'sigma_ms': sigma}


def robust_residual_sigma(fit_result):
    """从拟合残差直接估计稳健 σ(MAD), 供矛盾点判定使用。"""
    abs_res = sorted(abs(r) for r in fit_result.get('residuals', {}).values())
    if not abs_res:
        return 0.0
    med = abs_res[len(abs_res) // 2]
    mad = sorted(abs(r - med) for r in abs_res)[len(abs_res) // 2]
    s = 1.4826 * mad
    if s <= 1e-9:
        # 半数以上残差近似相同: 用非零残差的均值兜底
        nz = [r for r in abs_res if r > 1e-6]
        s = sum(nz) / max(len(nz), 1) if nz else 0.0
    return s


# ---------------------------------------------------------------- 候选方案

def candidates(sources, points, events, locks, deps, excls, conflict_counter):
    """生成三个候选方案。conflict_counter(cal_events)->冲突数(依赖/互斥/倒置)。

    event_source_map 由调用方通过事件的 clock_source_id 字段给出。
    """
    evs = {e['id']: e for e in events}
    esmap = {e['id']: e.get('clock_source_id') for e in events
             if e.get('clock_source_id') is not None}

    base_fit = _safe_fit(sources, points, events, locks)
    plans = []

    # 方案 1: 偏移 + 漂移
    plans.append(_plan('offset_drift', '方案 ① · 偏移 + 线性漂移',
                       '同时估计固定偏移与线性漂移率', sources, points, events,
                       locks, deps, excls, conflict_counter, esmap, base_fit))

    # 方案 2: 仅偏移(所有漂移锁 0, 保留用户显式漂移锁定)
    locks2 = {'sources': list(locks.get('sources') or []),
              'offset': dict(locks.get('offset') or {}),
              'drift': {sid: 0.0 for sid, _ in _param_index(sources)[0].items()
                        if sid not in (locks.get('drift') or {})
                        and sid not in (locks.get('sources') or [])}}
    locks2['drift'].update(locks.get('drift') or {})
    fit2 = _safe_fit(sources, points, events, locks2)
    plans.append(_plan('offset_only', '方案 ② · 仅固定偏移(漂移=0)',
                       '假设各时钟只快/慢一个常量, 无漂移', sources, points, events,
                       locks2, deps, excls, conflict_counter, esmap, fit2))

    # 方案 3: 稳健拟合(迭代降权离群点)
    fit3 = _robust_fit(sources, points, events, locks)
    plans.append(_plan('robust', '方案 ③ · 稳健拟合(抑制离群点)',
                       'Tukey 双权迭代降权, 自动剔除相互打架的配点', sources, points,
                       events, locks, deps, excls, conflict_counter, esmap, fit3))
    return plans


def _safe_fit(sources, points, events, locks):
    try:
        return fit(sources, points, events, locks)
    except LinAlgError:
        # 锁定矛盾: 忽略全部锁定再拟, 由诊断层报告
        return fit(sources, points, events,
                   {'sources': [], 'offset': {}, 'drift': {}})


def _robust_fit(sources, points, events, locks):
    """稳健拟合。校准点通常很少(个位数), IRLS/MAD 在小样本且离群点占比
    高时会被尺度估计带死; 这里枚举点子集求最大一致内点集
    (LMedS 风格): 对每个结构合法的点子集(大小 1..k)求解, 用
    |残差| ≤ 容差 + 2σ 判内点, 取内点最多、中值残差最小的解, 再以内点
    加权最小二乘精炼。锁定约束在精炼阶段生效。
    """
    from itertools import combinations

    evs = {e['id']: e for e in events}
    idx = _param_index(sources)[0]
    valid = [p for p in points
             if _point_rows(p, evs, idx, 0.0) is not None]
    if len(valid) < 3:
        return _safe_fit(sources, points, events, locks)

    # 可被数据辨识的参数个数(锁定后), 决定假设子集的最小规模
    free_locks = {'sources': [], 'offset': {}, 'drift': {}}
    n_eff = max(2, _safe_fit(sources, points, events, free_locks)['rank'])
    n_eff = min(n_eff, len(valid))

    def score(sol_points):
        sub_points = [p for p in points if p['id'] in {q['id'] for q in sol_points}]
        try:
            fr = fit(sources, sub_points, events, free_locks)
        except LinAlgError:
            return None
        res_all = {}
        for p in valid:
            r = _residual_for(p, evs, fr)
            if r is not None:
                res_all[p['id']] = r
        if not res_all:
            return None
        srt = sorted(abs(r) for r in res_all.values())
        med = srt[len(srt) // 2]
        # 参考噪声尺度: 中值绝对偏差(去 0), 兜底取中位残差
        mad = sorted(abs(abs(r) - med) for r in res_all.values())[len(srt) // 2]
        sigma_ref = max(1.4826 * mad, med * 0.5, 1.0)
        inliers, outliers = [], []
        for p in valid:
            tol = max(float(p.get('tolerance_ms') or 0), 1.0)
            if abs(res_all[p['id']]) <= tol + 2 * sigma_ref:
                inliers.append(p['id'])
            else:
                outliers.append(p['id'])
        return (-len(inliers), med, sigma_ref, inliers, outliers)

    best = None
    # 子集规模从 n_eff 往上枚举, 限制组合总量(校准点很少)
    for k in range(n_eff, min(len(valid), n_eff + 3) + 1):
        combos = list(combinations(valid, k))
        if len(combos) > 4000:
            combos = combos[:4000]
        for sub in combos:
            sc = score(list(sub))
            if sc is None:
                continue
            if best is None or sc[:2] < best[:2]:
                best = sc
        if best is not None and -best[0] >= len(valid) - 1:
            break

    if best is None:
        return _safe_fit(sources, points, events, locks)
    inlier_ids = set(best[3])
    inlier_points = [p for p in points if p['id'] in inlier_ids]
    # 内点集可能仍欠秩, 补回能降低残差的点直至满秩
    result = None
    for attempt in range(len(valid) + 1):
        try:
            result = fit(sources, inlier_points, events, locks)
        except LinAlgError:
            result = None
        if result is not None and result['rank'] >= min(n_eff, len(inlier_points) * 2):
            break
        # 找残差最小的外点补入
        rest = [p for p in valid if p['id'] not in inlier_ids]
        if not rest or result is None:
            break
        probe = result
        rest.sort(key=lambda p: abs(_residual_for(p, evs, probe) or 1e15))
        inlier_ids.add(rest[0]['id'])
        inlier_points.append(rest[0])
    if result is None:
        return _safe_fit(sources, points, events, locks)
    # 用稳健参数计算全部点(含被剔除点)的残差, 供诊断/指标使用
    full_res = dict(result.get('residuals', {}))
    for p in valid:
        r = _residual_for(p, evs, result)
        if r is not None:
            full_res[p['id']] = r
    result['residuals'] = full_res
    result['robust_outliers'] = [p['id'] for p in valid
                                 if p['id'] not in inlier_ids]
    return result


def _residual_for(point, evs, fit_result):
    """给定拟合结果, 计算单个校准点的残差 T_a - T_b(ms)。"""
    a = evs.get(point.get('a_event_id'))
    b = evs.get(point.get('b_event_id'))
    if not a or not b:
        return None
    ca = calibrated_ts(a['start_ts'], point.get('a_source_id'), fit_result)
    cb = calibrated_ts(b['start_ts'], point.get('b_source_id'), fit_result)
    if ca is None or cb is None:
        return None
    return ca - cb


def _plan(key, name, desc, sources, points, events, locks, deps, excls,
          conflict_counter, esmap, fit_result):
    cal = calibrate_events(events, esmap, fit_result)
    total_correction = 0
    for sid, p in fit_result['params'].items():
        if sid == fit_result['base_id']:
            continue
        ts = [e['start_ts'] for e in events if esmap.get(e['id']) == sid]
        if ts:
            total_correction += sum(abs(correction_at(t, sid, fit_result))
                                    for t in ts) / len(ts)
    n_conf = conflict_counter(cal) if conflict_counter else 0
    rsig = robust_residual_sigma(fit_result)
    mt = metrics(fit_result, points, robust_sigma=rsig)
    # 稳健方案: 被一致内点集剔除的点
    dropped = fit_result.get('robust_outliers', []) if key == 'robust' else []
    # 矛盾点至少包含被剔除点(残差尺度可能被污染)
    if key == 'robust':
        for pid in dropped:
            if pid not in mt['contradictory_point_ids']:
                mt['contradictory_point_ids'].append(pid)
    return {
        'key': key, 'name': name, 'description': desc,
        'conflict_count': n_conf,
        'total_correction_ms': round(total_correction, 3),
        'max_residual_ms': round(mt['max_abs_residual_ms'], 3),
        'sigma_ms': round(rsig, 3),
        'contradictory_point_ids': mt['contradictory_point_ids'],
        'dropped_point_ids': dropped,
        'fit': _public_fit(fit_result, sources, points),
    }


def _public_fit(fit_result, sources, points):
    """去掉内部大对象, 附带每个来源参数展示值。"""
    smap = {s['id']: s for s in sources}
    params = []
    for sid, p in fit_result['params'].items():
        s = smap.get(sid)
        params.append({
            'source_id': sid,
            'source_name': s['name'] if s else f'#{sid}',
            'is_baseline': sid == fit_result['base_id'],
            'offset_ms': round(p['offset_ms'], 3),
            'correction_ms': round(p['correction_ms'], 3),
            'drift_ms_per_hour': p['drift_ms_per_hour'],
            'drift_sec_per_day': round(p['drift_ms_per_hour'] * DRIFT_DISPLAY_SCALE, 3),
            'locked_offset': p['locked_offset'],
            'locked_drift': p['locked_drift'],
        })
    return {
        'params': params,
        'residuals': {str(pid): round(r, 3)
                      for pid, r in fit_result['residuals'].items()},
        'sigma_ms': round(fit_result.get('sigma') or 0, 3),
        'rank': fit_result['rank'], 'npar': fit_result['npar'],
        'nobs': fit_result['nobs'], 'base_id': fit_result['base_id'],
        't0': fit_result.get('t0', 0.0),
        'deficient_cols': fit_result['deficient_cols'],
    }


# ---------------------------------------------------------------- 点诊断 / 版本签名

def diagnose_points(sources, points, events, fit_result):
    """每个校准点的状态: ok / contradictory / same_source / dangling / duplicate。"""
    evs = {e['id']: e for e in events}
    valid_src = {s['id'] for s in sources}
    out = []
    seen_pairs = {}
    sigma = robust_residual_sigma(fit_result) if fit_result else 0.0
    for p in points:
        status, msgs = 'ok', []
        a, b = point_events(p, evs)
        sa, sb = p.get('a_source_id'), p.get('b_source_id')
        if not a or not b:
            status = 'dangling'
            msgs.append('引用的事件已被删除')
        elif sa not in valid_src or sb not in valid_src:
            status = 'dangling'
            msgs.append('引用的时钟来源已被删除')
        elif sa == sb:
            status = 'same_source'
            msgs.append('两个事件属于同一来源,无法约束时钟差异')
        else:
            pair = tuple(sorted((a['id'], b['id'])))
            if pair in seen_pairs:
                status = 'duplicate'
                msgs.append(f'与校准点 #{seen_pairs[pair]} 配对了相同的两个事件')
            else:
                seen_pairs[pair] = p['id']
            r = (fit_result or {}).get('residuals', {}).get(p['id'])
            if r is not None:
                tol = float(p.get('tolerance_ms') or 0)
                if abs(r) > tol + SIGMA_K * sigma:
                    status = 'contradictory' if status == 'ok' else status
                    msgs.append(f'残差 {fmt_ms(abs(r))} 超过允许误差 {fmt_ms(tol)}'
                                f' + {SIGMA_K:g}σ({fmt_ms(SIGMA_K * sigma)}),'
                                '两条时钟关系互相打架')
        out.append({'id': p['id'], 'status': status, 'messages': msgs})
    return out


def source_issues(sources, points, events, fit_result):
    """来源级问题: 未设基准 / 不连通 / 参数不可辨识。"""
    issues = []
    nbase = sum(1 for s in sources if s.get('is_baseline'))
    if sources and nbase != 1:
        issues.append({'kind': 'baseline', 'message': '需要恰好指定一个基准时钟来源'})
    chains, base = reachable_from_baseline(sources, points)
    evs = {e['id']: e for e in events}
    for s in sources:
        if s['id'] == base:
            continue
        if s['id'] not in chains:
            issues.append({'kind': 'disconnected', 'source_id': s['id'],
                           'message': f'来源「{s["name"]}」与基准之间没有任何校准点链路,'
                                      '偏移与漂移均无法求解'})
    if fit_result:
        smap = {s['id']: s for s in sources}
        for d in fit_result.get('deficient_cols', []):
            sid = d['source_id']
            if sid not in chains:
                continue   # 不连通已单独报告, 不重复
            s = smap.get(sid)
            kind_label = '固定偏移' if d['kind'] == 'offset' else '漂移率'
            issues.append({'kind': 'underdetermined', 'source_id': sid,
                           'param': d['kind'],
                           'message': f'来源「{s["name"] if s else sid}」的{kind_label}'
                                      '不可辨识: 校准点数量不足, 需要更多(跨时刻的)配点或锁定该参数'})
    return issues


def calibrate_event_map(events, fit_result):
    esmap = {e['id']: e.get('clock_source_id') for e in events
             if e.get('clock_source_id') is not None}
    return calibrate_events(events, esmap, fit_result)


def version_signature(sources, points):
    """校准关系指纹: 来源基准/事件归属/配对/容差 变化后, 已存版本即过期。"""
    s = [(x['id'], 1 if x.get('is_baseline') else 0) for x in sources]
    p = [(x['id'], x.get('a_event_id'), x.get('b_event_id'),
          x.get('a_source_id'), x.get('b_source_id'),
          round(float(x.get('tolerance_ms') or 0), 3)) for x in points]
    return repr((sorted(s), sorted(p)))
