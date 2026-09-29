"""用穷举真值表交叉验证 ROBDD 计算结果，并覆盖各类 422 错误。"""

import itertools
import random
from fractions import Fraction

import pytest
from fastapi.testclient import TestClient

from app.bdd import NodeLimitExceeded, ROBDD
from app.main import app

client = TestClient(app)

# ---------- 真值表参考实现 ----------

def brute_force(events, gates, top):
    """枚举全部事件赋值，返回 (top_prob, {id: importance})，均为 Fraction。"""
    ev_ids = sorted(events)
    probs = {eid: Fraction(events[eid]) for eid in ev_ids}
    # 兼容 dict 形态或 (id, op, inputs) 元组形态的门列表
    gate = {}
    for g in gates:
        # 兼容 dict 形态或 (id, op, inputs) 元组形态的门列表
        if isinstance(g, dict):
            gate[g["id"]] = (g["op"], g["inputs"])
        else:
            gate[g[0]] = (g[1], g[2])

    def evaluate(state, node):
        if node in probs:
            return state[node]
        op, inputs = gate[node]
        vals = [evaluate(state, i) for i in inputs]
        if op == "AND":
            return int(all(vals))
        if op == "OR":
            return int(any(vals))
        return int(not vals[0])

    def proba_with_force(forced):
        free = [e for e in ev_ids if e not in forced]
        total = Fraction(0)
        for bits in itertools.product((0, 1), repeat=len(free)):
            state = dict(forced)
            state.update(dict(zip(free, bits)))
            if evaluate(state, top):
                w = Fraction(1)
                for e in free:
                    w *= probs[e] if state[e] else (Fraction(1) - probs[e])
                total += w
        return total

    top_p = proba_with_force({})
    importance = {}
    for e in ev_ids:
        p1 = proba_with_force({e: 1})
        p0 = proba_with_force({e: 0})
        importance[e] = p1 - p0
    return top_p, importance


def f2s(f):
    return f"{f.numerator}/{f.denominator}"


def post(events, gates, top):
    return client.post(
        "/v1/analyze",
        json={"events": events, "gates": gates, "top": top},
    )


def make_payload(ev_specs, gate_specs, top):
    """ev_specs: dict id->'n/d'; gate_specs: list of (id, op, [inputs])。"""
    events = [{"id": k, "p": v} for k, v in ev_specs.items()]
    gates = [{"id": i, "op": op, "inputs": ins} for i, op, ins in gate_specs]
    return events, gates, top


# ---------- 基本功能 ----------

def test_health():
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


def test_single_event_as_top():
    r = post(*make_payload({"a": "1/2"}, [], "a"))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["top_probability"] == "1/2"
    # 顶层就是该事件：P(a|a=1)-P(a|a=0) = 1-0 = 1
    assert body["importances"] == {"a": "1/1"}


def test_and_or_not_examples():
    cases = [
        # AND
        ({"a": "1/2", "b": "1/3"}, [("g", "AND", ["a", "b"])], "g"),
        # OR
        ({"a": "1/2", "b": "1/3"}, [("g", "OR", ["a", "b"])], "g"),
        # NOT
        ({"a": "1/5"}, [("g", "NOT", ["a"])], "g"),
        # AND + NOT
        ({"a": "1/2", "b": "1/3"},
         [("n", "NOT", ["b"]), ("g", "AND", ["a", "n"])], "g"),
        # OR of ANDs（共享 a）
        ({"a": "2/7", "b": "3/11", "c": "5/13"},
         [("g1", "AND", ["a", "b"]),
          ("g2", "AND", ["a", "c"]),
          ("top", "OR", ["g1", "g2"])], "top"),
        # 非平凡异或：(a∨b) ∧ ¬(a∧b)
        ({"a": "3/7", "b": "5/11"},
         [("o", "OR", ["a", "b"]),
          ("ad", "AND", ["a", "b"]),
          ("n", "NOT", ["ad"]),
          ("top", "AND", ["o", "n"])], "top"),
        # 8 输入 AND / OR
        ({"e0": "1/2", "e1": "1/3", "e2": "1/5", "e3": "2/7",
          "e4": "3/11", "e5": "4/13", "e6": "5/17", "e7": "6/19"},
         [("g", "AND", ["e0", "e1", "e2", "e3", "e4", "e5", "e6", "e7"])],
         "g"),
        ({"e0": "1/2", "e1": "1/3", "e2": "1/5", "e3": "2/7",
          "e4": "3/11", "e5": "4/13", "e6": "5/17", "e7": "6/19"},
         [("g", "OR", ["e0", "e1", "e2", "e3", "e4", "e5", "e6", "e7"])],
         "g"),
        # p=0 与 p=1 的边界
        ({"a": "0/1", "b": "1/1"},
         [("g", "OR", ["a", "b"])], "g"),
        ({"a": "0/1", "b": "1/1"},
         [("g", "AND", ["a", "b"])], "g"),
        # 同一输入重复引用
        ({"a": "1/2", "b": "1/3"},
         [("g", "OR", ["a", "a", "b"])], "g"),
        # 未被 top 使用的门不影响结果
        ({"a": "1/2"},
         [("unused", "NOT", ["a"])], "a"),
    ]
    for ev, gates, top in cases:
        events, gate_list, _ = make_payload(ev, gates, top)
        r = post(events, gate_list, top)
        assert r.status_code == 200, r.text
        body = r.json()
        exp_p, exp_imp = brute_force(ev, gates, top)
        assert body["top_probability"] == f2s(exp_p), (ev, gates)
        assert body["importances"] == {k: f2s(v) for k, v in exp_imp.items()}, (ev, gates)


def test_random_small_circuits():
    """随机生成 ≤12 事件、≤20 门的故障树，逐例穷举对照。"""
    rng = random.Random(20260926)
    checked = 0
    while checked < 40:
        n = rng.randint(1, 12)
        ev_ids = [f"e{i}" for i in range(n)]
        ev_specs = {}
        for e in ev_ids:
            d = rng.randint(1, 8)
            ev_specs[e] = f"{rng.randint(0, d)}/{d}"
        gates = []
        available = list(ev_ids)
        m = rng.randint(0, 20)
        ok = True
        for j in range(m):
            gid = f"g{j}"
            op = rng.choice(["AND", "OR", "NOT"]) if len(available) >= 2 else "NOT"
            if op == "NOT":
                inputs = [rng.choice(available)]
            else:
                k = rng.randint(2, min(8, len(available)))
                inputs = rng.sample(available, k)
            gates.append((gid, op, inputs))
            available.append(gid)
        top = rng.choice(available)

        # 随机电路天然无环（门只能引用之前的节点）
        events, gate_list, _ = make_payload(ev_specs, gates, top)
        r = post(events, gate_list, top)
        if r.status_code != 200:
            assert False, (r.status_code, r.text, ev_specs, gates)
        body = r.json()
        exp_p, exp_imp = brute_force(ev_specs, gates, top)
        assert body["top_probability"] == f2s(exp_p)
        assert body["importances"] == {k: f2s(v) for k, v in exp_imp.items()}
        checked += 1


def test_fraction_is_reduced_and_extreme():
    # 1000000/1000000 = 1/1；2/4 -> 1/2
    r = post(*make_payload({"a": "2/4", "b": "4/8"},
                           [("g", "AND", ["a", "b"])], "g"))
    assert r.status_code == 200
    assert r.json()["top_probability"] == "1/4"

    r = post(*make_payload({"a": "1000000/1000000"}, [], "a"))
    assert r.status_code == 200
    assert r.json()["top_probability"] == "1/1"
    assert r.json()["importances"]["a"] == "1/1"

    r = post(*make_payload({"a": "0/1000000"}, [], "a"))
    assert r.json()["top_probability"] == "0/1"
    assert r.json()["importances"]["a"] == "1/1"


def test_lexicographic_variable_order_sharing():
    """同一事件被多处引用时共享状态：a∧b 与 a∧c 的 OR 不等同于独立副本。"""
    # P((a∧b)∨(a∧c)) = P(a)(P(b)+P(c)-P(b)P(c))
    r = post(*make_payload(
        {"a": "1/2", "b": "1/2", "c": "1/2"},
        [("g1", "AND", ["a", "b"]),
         ("g2", "AND", ["a", "c"]),
         ("top", "OR", ["g1", "g2"])],
        "top"))
    assert r.status_code == 200, r.text
    # 1/2 * (1/2 + 1/2 - 1/4) = 3/8
    assert r.json()["top_probability"] == "3/8"
    imp = r.json()["importances"]
    # I(a): 强制 a=1 => P(b∨c)=3/4；a=0 => 0
    assert imp["a"] == "3/4"
    assert imp["b"] == "1/4"
    assert imp["c"] == "1/4"


# ---------- 422 错误路径 ----------

def test_unknown_reference_in_gate():
    r = post(*make_payload({"a": "1/2"},
                           [("g", "AND", ["a", "x"])], "g"))
    assert r.status_code == 422
    assert "未知" in r.json()["detail"]


def test_unknown_top():
    r = post(*make_payload({"a": "1/2"}, [], "nope"))
    assert r.status_code == 422
    assert "顶层" in r.json()["detail"]


def test_direct_cycle():
    r = post(*make_payload({"a": "1/2"},
                           [("g", "OR", ["a", "g"])], "g"))
    assert r.status_code == 422
    assert "环" in r.json()["detail"]


def test_indirect_cycle():
    r = post(*make_payload({"a": "1/2"},
                           [("g1", "OR", ["a", "g2"]),
                            ("g2", "AND", ["a", "g1"])],
                           "g1"))
    assert r.status_code == 422
    assert "环" in r.json()["detail"]


def test_cycle_unreachable_from_top_still_rejected():
    # h 自成环，虽然 top 不经过它，仍应报错
    r = post(*make_payload({"a": "1/2"},
                           [("g", "NOT", ["a"]),
                            ("h", "OR", ["a", "h"])],
                           "g"))
    assert r.status_code == 422
    assert "环" in r.json()["detail"]


@pytest.mark.parametrize("bad", ["1/2/3", "2", "1/0", "3/2", "1/-1",
                                 "abc", "1/3.0", "+1/2", " 1/2",
                                 "1000001/1000001", "0x1/2"])
def test_bad_probability(bad):
    r = post(*make_payload({"a": bad}, [], "a"))
    assert r.status_code == 422
    assert "概率" in r.json()["detail"]


def test_probability_boundary_1e6_ok():
    r = post(*make_payload({"a": "999999/1000000"}, [], "a"))
    assert r.status_code == 200
    assert r.json()["top_probability"] == "999999/1000000"


def test_duplicate_event_id():
    r = client.post("/v1/analyze", json={
        "events": [{"id": "a", "p": "1/2"}, {"id": "a", "p": "1/3"}],
        "gates": [], "top": "a"})
    assert r.status_code == 422
    assert "重复" in r.json()["detail"]


def test_gate_id_clashes_with_event():
    r = post(*make_payload({"a": "1/2"},
                           [("a", "NOT", ["a"])], "a"))
    assert r.status_code == 422
    assert "冲突" in r.json()["detail"]


def test_duplicate_gate_id():
    r = post(*make_payload({"a": "1/2", "b": "1/3"},
                           [("g", "AND", ["a", "b"]),
                            ("g", "OR", ["a", "b"])], "g"))
    assert r.status_code == 422
    assert "重复" in r.json()["detail"]


def test_bad_arities():
    # NOT 有 2 输入
    r = post(*make_payload({"a": "1/2", "b": "1/3"},
                           [("g", "NOT", ["a", "b"])], "g"))
    assert r.status_code == 422 and "NOT" in r.json()["detail"]
    # NOT 有 0 输入
    r = post(*make_payload({"a": "1/2"}, [("g", "NOT", [])], "g"))
    assert r.status_code == 422
    # AND 只有 1 输入
    r = post(*make_payload({"a": "1/2"}, [("g", "AND", ["a"])], "g"))
    assert r.status_code == 422
    # OR 有 9 输入
    ids = [f"e{i}" for i in range(9)]
    r = post(*make_payload({e: "1/2" for e in ids},
                           [("g", "OR", ids)], "g"))
    assert r.status_code == 422


def test_counts_out_of_range():
    # 0 个事件
    r = client.post("/v1/analyze", json={"events": [], "gates": [], "top": "x"})
    assert r.status_code == 422
    # 33 个事件
    evs = [{"id": f"e{i}", "p": "1/2"} for i in range(33)]
    r = client.post("/v1/analyze",
                    json={"events": evs, "gates": [], "top": "e0"})
    assert r.status_code == 422
    # 65 个门
    gates = [{"id": f"g{i}", "op": "NOT", "inputs": ["e0"]} for i in range(65)]
    r = client.post("/v1/analyze", json={
        "events": [{"id": "e0", "p": "1/2"}],
        "gates": gates, "top": "g0"})
    assert r.status_code == 422


def test_unknown_op_and_extra_fields():
    r = client.post("/v1/analyze", json={
        "events": [{"id": "a", "p": "1/2"}],
        "gates": [{"id": "g", "op": "XOR", "inputs": ["a"]}],
        "top": "g"})
    assert r.status_code == 422

    r = client.post("/v1/analyze", json={
        "events": [{"id": "a", "p": "1/2", "extra": 1}],
        "gates": [], "top": "a"})
    assert r.status_code == 422


def test_non_ascii_id_rejected():
    r = client.post("/v1/analyze", json={
        "events": [{"id": "事件", "p": "1/2"}], "gates": [], "top": "事件"})
    assert r.status_code == 422


# ---------- ROBDD 单元测试与节点上限 ----------

def test_robdd_terminal_probability():
    bdd = ROBDD(2)
    p, imp = bdd.root_probability(0, [Fraction(1, 2), Fraction(1, 3)])
    assert (p, imp) == (Fraction(0), [Fraction(0), Fraction(0)])
    p, imp = bdd.root_probability(1, [Fraction(1, 2), Fraction(1, 3)])
    assert p == Fraction(1) and imp == [Fraction(0), Fraction(0)]


def test_node_limit_constant_and_violation(monkeypatch):
    # 生产上限为 50000
    import app.main as main_mod
    assert main_mod.MAX_NONTERMINAL_NODES == 50_000

    # 把上限调小，异或链很快超限（事件基础节点也算非终端节点）
    monkeypatch.setattr(main_mod, "MAX_NONTERMINAL_NODES", 5)
    evs = {f"e{i}": "1/2" for i in range(8)}
    # 级联异或：((e0 xor e1) xor e2) ...，用门展开
    gates = []
    prev = "e0"
    for i in range(1, 8):
        o, a, n, x = f"o{i}", f"a{i}", f"n{i}", f"x{i}"
        gates.append((o, "OR", [prev, f"e{i}"]))
        gates.append((a, "AND", [prev, f"e{i}"]))
        gates.append((n, "NOT", [a]))
        gates.append((x, "AND", [o, n]))
        prev = x
    events, gate_list, _ = make_payload(evs, gates, prev)
    r = post(events, gate_list, prev)
    assert r.status_code == 422
    detail = r.json()["detail"]
    assert "非终端节点" in detail and "5" in detail


def test_node_limit_directly():
    # 2 个上限：变量节点 a、b 已占满，AND 产生第 3 个节点即抛错
    bdd = ROBDD(3, limit=2)
    a = bdd.mk(0, 0, 1)
    b = bdd.mk(1, 0, 1)
    with pytest.raises(NodeLimitExceeded):
        bdd.and_(a, b)
