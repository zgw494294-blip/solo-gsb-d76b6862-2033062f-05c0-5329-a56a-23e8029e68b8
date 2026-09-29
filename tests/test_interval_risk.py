"""POST /v1/interval-risk：概率区间风险判定的语义、精确性与 422 校验。

用穷举全部区间端点组合（非区间事件仍按独立先验加权）的真值表参考实现
做交叉验证，覆盖 AND/OR/NOT、共享引用、未被顶层引用的区间事件、并列
端点排序与 safe/unsafe/uncertain 边界判定。
"""

import itertools
import random
from fractions import Fraction

import pytest
from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


# ---------- 参考实现：枚举 2^k 个端点组合 ----------

def f2s(f):
    return f"{f.numerator}/{f.denominator}"


def brute_intervals(ev_specs, gate_specs, top, interval_ids, lows, highs):
    """返回 {端点掩码: 顶事件概率 Fraction}，掩码置位取 high，否则 low。

    掩码位序与接口一致：interval_ids 按事件 id 字典序排列；非区间事件
    保持原 p，枚举全部完整事件状态精确加权。
    """
    ev_ids = sorted(ev_specs)
    probs = {e: Fraction(ev_specs[e]) for e in ev_ids}
    gates = {g[0]: (g[1], g[2]) for g in gate_specs}

    def evaluate(state, node):
        if node in probs:
            return state[node]
        op, inputs = gates[node]
        vals = [evaluate(state, i) for i in inputs]
        if op == "AND":
            return int(all(vals))
        if op == "OR":
            return int(any(vals))
        return int(not vals[0])

    result = {}
    for bits in itertools.product((0, 1), repeat=len(interval_ids)):
        ps = dict(probs)
        mask = 0
        for i, take_high in enumerate(bits):
            ps[interval_ids[i]] = highs[i] if take_high else lows[i]
            mask |= take_high << i
        total = Fraction(0)
        for state_bits in itertools.product((0, 1), repeat=len(ev_ids)):
            state = dict(zip(ev_ids, state_bits))
            if evaluate(state, top):
                w = Fraction(1)
                for eid in ev_ids:
                    w *= ps[eid] if state[eid] else (Fraction(1) - ps[eid])
                total += w
        result[mask] = total
    return result


def tie_min(values, k):
    """并列时按事件 id 字典序（位 0 为字典序最高位）、low 优先。"""
    best_v = min(values.values())
    order = lambda m: int(f"{m:0{k}b}"[::-1], 2)
    candidates = [m for m, v in values.items() if v == best_v]
    return best_v, min(candidates, key=order)


def make_body(ev_specs, gate_specs, top, intervals, limit):
    events = [{"id": k, "p": v} for k, v in ev_specs.items()]
    gates = [{"id": i, "op": op, "inputs": ins} for i, op, ins in gate_specs]
    return {
        "events": events,
        "gates": gates,
        "top": top,
        "intervals": intervals,
        "limit": limit,
    }


def interval_dict(interval_ids, lows, highs):
    return {
        eid: {"low": f2s(lo), "high": f2s(hi)}
        for eid, lo, hi in zip(interval_ids, lows, highs)
    }


# ---------- 基础语义：OR 树手算值 ----------

def test_or_tree_basic():
    evs = {
        "motor_overheat": "1/100",
        "coolant_loss": "2/100",
        "pump_fail": "1/200",
    }
    gates = [
        ("cooling_fail", "OR", ["coolant_loss", "pump_fail"]),
        ("top", "OR", ["motor_overheat", "cooling_fail"]),
    ]
    intervals = {
        "coolant_loss": {"low": "0/1", "high": "5/100"},
        "pump_fail": {"low": "0/1", "high": "1/100"},
    }
    r = client.post(
        "/v1/interval-risk", json=make_body(evs, gates, "top", intervals, "1/20")
    )
    assert r.status_code == 200, r.text
    data = r.json()
    # min：两区间事件取 0，仅 motor 1/100
    assert data["min_probability"] == "1/100"
    # max：1 − 99/100 · 19/20 · 99/100
    assert Fraction(data["max_probability"]) == 1 - Fraction(99, 100) * Fraction(
        19, 20
    ) * Fraction(99, 100)
    assert data["min_endpoints"] == {
        "coolant_loss": "0/1",
        "pump_fail": "0/1",
    }
    assert data["max_endpoints"] == {
        "coolant_loss": "1/20",
        "pump_fail": "1/100",
    }
    assert data["verdict"] == "uncertain"


def test_not_gate_and_shared_reference():
    # top = a AND NOT b；b 在两处被引用（NOT 与其后的 OR 输入），共享状态
    evs = {"a": "1/2", "b": "1/2"}
    gates = [
        ("nb", "NOT", ["b"]),
        ("top", "AND", ["a", "nb"]),
    ]
    intervals = {
        "a": {"low": "1/4", "high": "3/4"},
        "b": {"low": "1/4", "high": "3/4"},
    }
    r = client.post(
        "/v1/interval-risk", json=make_body(evs, gates, "top", intervals, "1/2")
    )
    assert r.status_code == 200, r.text
    data = r.json()
    # P = a·(1−b)：最小取 a 低 b 高 = 1/4·1/4 = 1/16；
    # 最大取 a 高 b 低 = 3/4·3/4 = 9/16
    assert data["min_probability"] == "1/16"
    assert data["max_probability"] == "9/16"
    assert list(data["min_endpoints"]) == ["a", "b"]  # 按 id 字典序
    assert data["min_endpoints"] == {"a": "1/4", "b": "3/4"}
    assert data["max_endpoints"] == {"a": "3/4", "b": "1/4"}


def test_interval_event_not_referenced_by_top():
    # top 只依赖 a；b 是已声明但未被引用的区间事件，min/max 都等于 P(a)
    evs = {"a": "1/3", "b": "1/2"}
    r = client.post(
        "/v1/interval-risk",
        json=make_body(
            evs, [], "a",
            {"b": {"low": "0/1", "high": "1/1"}}, "1/2",
        ),
    )
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["min_probability"] == "1/3"
    assert data["max_probability"] == "1/3"
    # b 不影响取值：并列按 low 优先，两个端点取值都为 b 的 low
    assert data["min_endpoints"] == {"b": "0/1"}
    assert data["max_endpoints"] == {"b": "0/1"}


def test_degenerate_interval_point():
    # low == high == 原 p：端点唯一
    evs = {"a": "1/2"}
    r = client.post(
        "/v1/interval-risk",
        json=make_body(evs, [], "a", {"a": {"low": "1/2", "high": "1/2"}}, "1/2"),
    )
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["min_probability"] == data["max_probability"] == "1/2"
    assert data["min_endpoints"] == data["max_endpoints"] == {"a": "1/2"}
    # max == limit：safe
    assert data["verdict"] == "safe"


def test_verdict_boundaries():
    # top = a；区间 [1/4, 3/4]
    body = make_body(
        {"a": "1/2"}, [], "a", {"a": {"low": "1/4", "high": "3/4"}}, None
    )
    for limit, want in [
        ("3/4", "safe"),    # max == limit
        ("4/5", "safe"),    # max < limit（0.75 < 0.8）
        ("1/4", "uncertain"),  # min == limit 时不是 unsafe（min > limit 才是）
        ("1/5", "unsafe"),  # min > limit（0.25 > 0.2）
        ("1/2", "uncertain"),
    ]:
        body["limit"] = limit
        r = client.post("/v1/interval-risk", json=body)
        assert r.status_code == 200, r.text
        assert r.json()["verdict"] == want, (limit, r.json())


# ---------- 穷举交叉验证（随机电路）----------

def build_random_case(seed):
    rng = random.Random(seed)
    n = rng.randint(1, 6)
    ev_ids = [f"e{i}" for i in range(n)]
    evs = {e: f"{rng.randint(0, 6)}/6" for e in ev_ids}
    gates = []
    nodes = list(ev_ids)
    for gi in range(rng.randint(0, 5)):
        gid = f"g{gi}"
        op = rng.choice(["AND", "OR", "NOT"])
        if op == "NOT":
            ins = [rng.choice(nodes)]
        else:
            pool = nodes if len(nodes) >= 2 else nodes * 2
            ins = rng.sample(pool, rng.randint(2, min(3, len(pool))))
        gates.append((gid, op, ins))
        nodes.append(gid)
    top = rng.choice(nodes)
    k = rng.randint(1, n)
    chosen = sorted(rng.sample(ev_ids, k))
    lows, highs = [], []
    for eid in chosen:
        p = Fraction(evs[eid])
        # 合法端点：low <= p <= high（p=0 时 low 只能为 0；p=1 时 high 只能为 1）
        lo = Fraction(rng.randint(0, int(p * 6)), 6)
        hi = Fraction(rng.randint(int(p * 6), 6), 6)
        lows.append(lo)
        highs.append(hi)
    return evs, gates, top, chosen, lows, highs


@pytest.mark.parametrize("seed", range(120))
def test_random_circuits_against_brute_force(seed):
    evs, gates, top, chosen, lows, highs = build_random_case(seed)
    # 先做静态校验（随机门可能成环），环直接跳过
    intervals = interval_dict(chosen, lows, highs)
    body = make_body(evs, gates, top, intervals, "1/2")
    r = client.post("/v1/interval-risk", json=body)
    if r.status_code == 422:
        assert "环" in r.json()["detail"]
        return
    assert r.status_code == 200, r.text
    data = r.json()

    values = brute_intervals(evs, gates, top, chosen, lows, highs)
    k = len(chosen)
    min_v, min_m = tie_min(values, k)
    max_v, max_m = tie_min({m: -v for m, v in values.items()}, k)
    max_v = -max_v

    assert Fraction(data["min_probability"]) == min_v
    assert Fraction(data["max_probability"]) == max_v

    def want_endpoints(mask):
        return {
            eid: f2s(highs[i] if mask >> i & 1 else lows[i])
            for i, eid in enumerate(chosen)
        }

    assert data["min_endpoints"] == want_endpoints(min_m)
    assert data["max_endpoints"] == want_endpoints(max_m)


def test_eight_interval_events_against_brute_force():
    n = 8
    ev_ids = [f"e{i}" for i in range(n)]
    evs = {e: "1/2" for e in ev_ids}
    gates = [
        ("g0", "AND", ["e0", "e1", "e2", "e3"]),
        ("g1", "OR", ["e4", "e5", "e6", "e7"]),
        ("top", "OR", ["g0", "g1"]),
    ]
    chosen = ev_ids
    lows = [Fraction(1, 4)] * 8
    highs = [Fraction(3, 4)] * 8
    intervals = interval_dict(chosen, lows, highs)
    r = client.post(
        "/v1/interval-risk", json=make_body(evs, gates, "top", intervals, "1/2")
    )
    assert r.status_code == 200, r.text
    data = r.json()
    values = brute_intervals(evs, gates, "top", chosen, lows, highs)
    assert Fraction(data["min_probability"]) == min(values.values())
    assert Fraction(data["max_probability"]) == max(values.values())


# ---------- 422 校验 ----------

BASE = {
    "events": [
        {"id": "a", "p": "1/100"},
        {"id": "b", "p": "2/100"},
    ],
    "gates": [{"id": "top", "op": "OR", "inputs": ["a", "b"]}],
    "top": "top",
    "limit": "1/10",
}


def post_intervals(intervals, limit="1/10"):
    return client.post(
        "/v1/interval-risk", json={**BASE, "intervals": intervals, "limit": limit}
    )


def test_unknown_interval_id():
    r = post_intervals({"zzz": {"low": "0/1", "high": "1/2"}})
    assert r.status_code == 422
    assert "未声明" in r.json()["detail"]


def test_gate_id_as_interval():
    r = post_intervals({"top": {"low": "0/1", "high": "1/2"}})
    assert r.status_code == 422
    assert "未声明" in r.json()["detail"]


def test_low_greater_than_p():
    r = post_intervals({"b": {"low": "3/100", "high": "4/100"}})
    assert r.status_code == 422
    assert "low" in r.json()["detail"]


def test_high_less_than_p():
    r = post_intervals({"b": {"low": "0/1", "high": "1/100"}})
    assert r.status_code == 422
    assert "high" in r.json()["detail"]


def test_low_greater_than_high_collected():
    # 多项错误在同一条 detail 中一并说明
    r = post_intervals({"b": {"low": "4/100", "high": "1/100"}})
    assert r.status_code == 422
    detail = r.json()["detail"]
    assert "b" in detail


def test_malformed_endpoint_fraction():
    r = post_intervals({"a": {"low": "x", "high": "1/2"}})
    assert r.status_code == 422
    assert "low" in r.json()["detail"]


def test_endpoint_probability_out_of_range():
    r = post_intervals({"a": {"low": "2/1", "high": "3/1"}})
    assert r.status_code == 422


def test_bad_limit():
    for lim in ["2/1", "abc", "1/0", "1/1000001"]:
        r = post_intervals({"a": {"low": "0/1", "high": "1/2"}}, limit=lim)
        assert r.status_code == 422, lim


def test_empty_intervals_rejected():
    r = post_intervals({})
    assert r.status_code == 422


def test_too_many_intervals_rejected():
    body = {
        "events": [{"id": f"e{i}", "p": "1/2"} for i in range(9)],
        "gates": [],
        "top": "e0",
        "intervals": {f"e{i}": {"low": "0/1", "high": "1/2"} for i in range(9)},
        "limit": "1/2",
    }
    r = client.post("/v1/interval-risk", json=body)
    assert r.status_code == 422


def test_underlying_tree_validation_reused():
    # 未知 top 引用、环、非法 p 等沿用 /v1/analyze 校验
    body = {
        "events": [{"id": "a", "p": "1/2"}],
        "gates": [{"id": "g", "op": "NOT", "inputs": ["g"]}],
        "top": "g",
        "intervals": {"a": {"low": "0/1", "high": "1/2"}},
        "limit": "1/2",
    }
    r = client.post("/v1/interval-risk", json=body)
    assert r.status_code == 422
    assert "环" in r.json()["detail"]


def test_node_limit(monkeypatch):
    import app.main as main_mod
    assert main_mod.MAX_NONTERMINAL_NODES == 50_000
    monkeypatch.setattr(main_mod, "MAX_NONTERMINAL_NODES", 5)
    evs = {f"e{i}": "1/2" for i in range(8)}
    gates = []
    prev = "e0"
    for i in range(1, 8):
        o, a, n, x = f"o{i}", f"a{i}", f"n{i}", f"x{i}"
        gates.append((o, "OR", [prev, f"e{i}"]))
        gates.append((a, "AND", [prev, f"e{i}"]))
        gates.append((n, "NOT", [a]))
        gates.append((x, "AND", [o, n]))
        prev = x
    intervals = {f"e{i}": {"low": "0/1", "high": "1/2"} for i in range(8)}
    r = client.post(
        "/v1/interval-risk", json=make_body(evs, gates, prev, intervals, "1/2")
    )
    assert r.status_code == 422
    assert "非终端节点" in r.json()["detail"]
