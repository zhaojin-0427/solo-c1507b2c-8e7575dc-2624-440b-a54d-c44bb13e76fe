"""调整方案算法回归测试。

反例 1: 修复不可能时长只改区间结束端点(1000ms), 移动量必须按起止端点
         实际变化计算, 不得报成 delta_ms=0 / total_shift_ms=0。
反例 2: 两个独立前置约束共享同一后续事件(A→C, B→C 各倒置 1000ms),
         min_total 必须选出全局最小方案(只动 C 一次, 共 1000ms),
         而不是贪心地把 A、B 各提前 1000ms(共 2000ms)。

同时校验: 方案明细、总移动量、未解决矛盾 与 应用方案后的事件区间一致。
"""
import unittest

from analysis import detect, solve_plans


def ev(eid, start, end=None, locked=0, title=None):
    return {'id': eid, 'title': title or f'事件{eid}', 'start_ts': start,
            'end_ts': end, 'locked': locked, 'confidence': 'medium',
            'source': 'test', 'evidence': '', 'timezone': 'UTC',
            'group_name': '', 'color': '', 'description': ''}


def dep(did, dtype, from_id, to_id, min_gap_ms=0, max_gap_ms=None):
    return {'id': did, 'type': dtype, 'from_id': from_id, 'to_id': to_id,
            'min_gap_ms': min_gap_ms, 'max_gap_ms': max_gap_ms, 'note': ''}


def plans_by_strategy(events, deps, excls=None):
    return {p['strategy']: p for p in solve_plans(events, deps, excls or [])}


def apply_plan(events, plan):
    """按方案明细(to_start/to_end)应用, 返回应用后的事件列表。"""
    sim = {e['id']: dict(e) for e in events}
    for m in plan['moves']:
        sim[m['event_id']]['start_ts'] = m['to_start']
        sim[m['event_id']]['end_ts'] = m['to_end']
    return [sim[e['id']] for e in events]


def assert_plan_consistent(tc, events, deps, excls, plan):
    """方案明细、总量、未解决矛盾 必须与应用后的事件区间一致。"""
    for m in plan['moves']:
        # 明细自洽: 新端点 = 原端点 + 位移
        tc.assertEqual(m['to_start'], m['from_start'] + m['delta_start_ms'])
        orig_end = m['from_end'] if m['from_end'] is not None else m['from_start']
        new_end = m['to_end'] if m['to_end'] is not None else m['to_start']
        tc.assertEqual(new_end, orig_end + m['delta_end_ms'])
        # 移动量 = 起止端点实际变化
        tc.assertEqual(m['move_ms'],
                       max(abs(m['delta_start_ms']), abs(m['delta_end_ms'])))
    # 总量 = 各事件移动量之和
    tc.assertEqual(plan['total_shift_ms'],
                   sum(m['move_ms'] for m in plan['moves']))
    # 不移动锁定事件
    locked = {e['id'] for e in events if e.get('locked')}
    tc.assertFalse(locked & {m['event_id'] for m in plan['moves']})
    # 应用后重新检测到的冲突 = 方案报告的未解决矛盾
    after = apply_plan(events, plan)
    remaining = detect(after, deps, excls)
    tc.assertEqual([u['title'] for u in plan['unresolved']],
                   [c['title'] for c in remaining])
    return after


class TestEndpointMoveAccounting(unittest.TestCase):
    """反例 1: 不可能时长的修复只改结束端点(1000ms), 不得报成 0。"""

    def setUp(self):
        self.events = [ev(1, 5000, 4000)]   # 结束比开始早 1000ms

    def test_duration_conflict_detected(self):
        confs = detect(self.events, [], [])
        self.assertEqual(len(confs), 1)
        self.assertEqual(confs[0]['type'], 'duration')
        self.assertEqual(confs[0]['amount_ms'], 1000)

    def test_push_later_reports_endpoint_change(self):
        p = plans_by_strategy(self.events, [])['push_later']
        self.assertEqual(p['total_shift_ms'], 1000)   # 修复前误报为 0
        self.assertEqual(len(p['moves']), 1)
        m = p['moves'][0]
        self.assertEqual(m['delta_start_ms'], 0)
        self.assertEqual(m['delta_end_ms'], 1000)
        self.assertEqual(m['move_ms'], 1000)
        self.assertEqual((m['to_start'], m['to_end']), (5000, 5000))
        self.assertEqual(p['unresolved'], [])
        after = assert_plan_consistent(self, self.events, [], [], p)
        self.assertEqual(detect(after, [], []), [])

    def test_pull_earlier_reports_endpoint_change(self):
        p = plans_by_strategy(self.events, [])['pull_earlier']
        self.assertEqual(p['total_shift_ms'], 1000)
        m = p['moves'][0]
        self.assertEqual(m['delta_start_ms'], -1000)
        self.assertEqual(m['delta_end_ms'], 0)
        self.assertEqual((m['to_start'], m['to_end']), (4000, 4000))
        assert_plan_consistent(self, self.events, [], [], p)

    def test_min_total_reports_endpoint_change(self):
        p = plans_by_strategy(self.events, [])['min_total']
        self.assertEqual(p['total_shift_ms'], 1000)   # 修复前误报为 0
        self.assertEqual(len(p['moves']), 1)
        self.assertEqual(p['moves'][0]['move_ms'], 1000)
        self.assertEqual(p['unresolved'], [])
        after = assert_plan_consistent(self, self.events, [], [], p)
        self.assertEqual(detect(after, [], []), [])


class TestSharedSuccessorMinTotal(unittest.TestCase):
    """反例 2: A→C、B→C 两条独立前置约束各倒置 1000ms(瞬时事件)。"""

    def setUp(self):
        self.events = [ev(1, 1000), ev(2, 1000), ev(3, 0)]
        self.deps = [dep(1, 'before', 1, 3), dep(2, 'before', 2, 3)]

    def test_min_total_is_globally_minimal(self):
        plans = plans_by_strategy(self.events, self.deps)
        mt = plans['min_total']
        # 全局最优: 只把 C 顺延一次(1000ms), 而非分别提前 A、B(2000ms)
        self.assertEqual(mt['total_shift_ms'], 1000)   # 修复前误为 2000
        self.assertEqual(len(mt['moves']), 1)
        self.assertEqual(mt['moves'][0]['event_id'], 3)
        self.assertEqual(mt['moves'][0]['delta_start_ms'], 1000)
        self.assertEqual(mt['unresolved'], [])
        # 不差于任何一个贪心方案
        self.assertLessEqual(mt['total_shift_ms'],
                             plans['push_later']['total_shift_ms'])
        self.assertLessEqual(mt['total_shift_ms'],
                             plans['pull_earlier']['total_shift_ms'])
        for p in plans.values():
            assert_plan_consistent(self, self.events, self.deps, [], p)

    def test_min_total_respects_locked(self):
        events = [ev(1, 1000), ev(2, 1000), ev(3, 0, locked=1)]
        mt = plans_by_strategy(events, self.deps)['min_total']
        # C 被锁定, 只能分别提前 A、B
        self.assertEqual(mt['total_shift_ms'], 2000)
        self.assertEqual({m['event_id'] for m in mt['moves']}, {1, 2})
        self.assertEqual(mt['unresolved'], [])
        assert_plan_consistent(self, events, self.deps, [], mt)

    def test_all_locked_no_moves(self):
        events = [ev(1, 1000, locked=1), ev(2, 1000, locked=1), ev(3, 0, locked=1)]
        mt = plans_by_strategy(events, self.deps)['min_total']
        self.assertEqual(mt['moves'], [])
        self.assertEqual(mt['total_shift_ms'], 0)
        self.assertEqual(len(mt['unresolved']), 2)   # 两条前置倒置均无法解决
        assert_plan_consistent(self, events, self.deps, [], mt)


class TestUnresolvedConsistency(unittest.TestCase):
    """不可解的因果环: 所有方案都必须如实保留未解决矛盾。"""

    def test_cycle_remains_unresolved(self):
        events = [ev(1, 0, 1000), ev(2, 500, 1500)]
        deps = [dep(1, 'before', 1, 2), dep(2, 'before', 2, 1)]
        for p in plans_by_strategy(events, deps).values():
            self.assertTrue(p['unresolved'])
            titles = {u['title'] for u in p['unresolved']}
            self.assertTrue(any('因果环' in t for t in titles))
            assert_plan_consistent(self, events, deps, [], p)


class TestChainPropagation(unittest.TestCase):
    """回归: 锁定 E3 时, min_total 必须让调整沿后继链 E3→E2→E1 传播,
    而不是只修 E1 的时长就把 E3→E2 倒置留作未解决。"""

    DEPS = [dep(1, 'before', 3, 2), dep(2, 'before', 2, 1)]

    def test_propagates_along_successor_chain(self):
        # E3 锁定 [0,3000]; E2=[500,1500]; E1=[1500,500] 结束早于开始
        events = [ev(1, 1500, 500), ev(2, 500, 1500), ev(3, 0, 3000, locked=1)]
        p = plans_by_strategy(events, self.DEPS)['min_total']
        # 链上矛盾全部消除
        self.assertEqual(p['unresolved'], [])
        # E3 不动; E2 顺延到 E3 之后; E1 顺延到 E2 之后并修复时长
        by_id = {m['event_id']: m for m in p['moves']}
        self.assertNotIn(3, by_id)
        self.assertEqual((by_id[2]['delta_start_ms'], by_id[2]['delta_end_ms']),
                         (2500, 2500))
        self.assertEqual((by_id[1]['delta_start_ms'], by_id[1]['delta_end_ms']),
                         (2500, 3500))
        # 总移动量按起止端点实际变化计算
        self.assertEqual(p['total_shift_ms'], 2500 + 3500)
        # 应用后区间: E3 不变, E2=[3000,4000], E1=[4000,4000]
        after = {e['id']: e for e in assert_plan_consistent(
            self, events, self.DEPS, [], p)}
        self.assertEqual((after[3]['start_ts'], after[3]['end_ts']), (0, 3000))
        self.assertEqual((after[2]['start_ts'], after[2]['end_ts']), (3000, 4000))
        self.assertEqual((after[1]['start_ts'], after[1]['end_ts']), (4000, 4000))
        self.assertEqual(detect(list(after.values()), self.DEPS, []), [])

    def test_propagates_with_mid_chain_variant(self):
        # 同链不同位置: E2=[1000,2000], E1=[1500,500]
        events = [ev(1, 1500, 500), ev(2, 1000, 2000), ev(3, 0, 3000, locked=1)]
        p = plans_by_strategy(events, self.DEPS)['min_total']
        self.assertEqual(p['unresolved'], [])
        self.assertEqual(p['total_shift_ms'], 2000 + 3500)
        after = {e['id']: e for e in assert_plan_consistent(
            self, events, self.DEPS, [], p)}
        self.assertEqual((after[2]['start_ts'], after[2]['end_ts']), (3000, 4000))
        self.assertEqual((after[1]['start_ts'], after[1]['end_ts']), (4000, 4000))
        self.assertEqual(detect(list(after.values()), self.DEPS, []), [])

    def test_min_total_is_minimal_over_chain(self):
        # 传播方案的总移动量应等于理论下界:
        # E2 至少 +2500(E3 锁定), E1 至少 +2500 且时长修复 1000
        events = [ev(1, 1500, 500), ev(2, 500, 1500), ev(3, 0, 3000, locked=1)]
        plans = plans_by_strategy(events, self.DEPS)
        mt = plans['min_total']
        self.assertEqual(mt['total_shift_ms'], 6000)
        self.assertLessEqual(mt['total_shift_ms'],
                             plans['push_later']['total_shift_ms'])


if __name__ == '__main__':
    unittest.main()
