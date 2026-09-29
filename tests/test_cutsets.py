"""POST /v1/cut-sets：最小割集语义、排序、NOT 拒绝、5000 组上限等。

用穷举真值表求“使顶事件为真的极小事件集”做交叉验证。
"""

import itertools
import random

from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


# ---------- 穷举真值表参考实现 ----------

def brute_min_cut_sets(ev_specs, gate_specs, top):
    """枚举全部事件赋值，返回排序后的全部最小割集（list of list）。"""
    ev_ids = sorted(ev_specs)
    gate = {g[0]: (g[1], g[2]) for g in gate_specs}

    def evaluate(state, node):
        if node in state:
            return state[node]
        op, inputs = gate[node]
        vals = [evaluate(state, i) for i in inputs]
        return all(vals) if op == "AND" else any(vals)

    sat = []
    for bits in itertools.product((0, 1), repeat=len(ev_ids)):
        state = dict(zip(ev_ids, bits))
        if evaluate(state, top):
            sat.append(frozenset(e for e, b in zip(ev_ids, bits) if b))
    minimal = [s for s in sat if not any(t < s for t in sat)]
    return sorted((sorted(s) for s in minimal), key=lambda g: (len(g), g))


def make_payload(ev_specs, gate_specs, top):
    events = [{"id": k, "p": v} for k, v in ev_specs.items()]
    gates = [{"id": i, "op": op, "inputs": ins}
             for i, op, ins in gate_specs]
    return events, gates, top


def post_cut(ev_specs, gate_specs, top):
    events, gates, _ = make_payload(ev_specs, gate_specs, top)
    return client.post("/v1/cut-sets",
                       json={"events": events, "gates": gates, "top": top})


def is_sorted(cut_sets):
    return cut_sets == sorted(cut_sets, key=lambda g: (len(g), g))


# ---------- 基本功能 ----------

def test_top_is_single_event():
    r = post_cut({"a": "1/2"}, [], "a")
    assert r.status_code == 200, r.text
    assert r.json() == {"cut_sets": [["a"]]}


def test_and_or_basic():
    r = post_cut({"a": "1/2", "b": "1/3"},
                 [("g", "AND", ["a", "b"])], "g")
    assert r.status_code == 200, r.text
    assert r.json() == {"cut_sets": [["a", "b"]]}

    r = post_cut({"a": "1/2", "b": "1/3"},
                 [("g", "OR", ["a", "b"])], "g")
    assert r.json() == {"cut_sets": [["a"], ["b"]]}


def test_absorption_or_of_and():
    # 规格中的例子：A OR (A AND B) 仅返回 [A]
    r = post_cut({"a": "1/2", "b": "1/3"},
                 [("ab", "AND", ["a", "b"]),
                  ("top", "OR", ["a", "ab"])], "top")
    assert r.status_code == 200, r.text
    assert r.json() == {"cut_sets": [["a"]]}


def test_shared_event_in_group_counted_once():
    # (a∨b) ∧ (a∨c) = a ∨ (b∧c)：共享的 a 在割集内只计一次
    r = post_cut({"a": "1/2", "b": "1/3", "c": "1/5"},
                 [("g1", "OR", ["a", "b"]),
                  ("g2", "OR", ["a", "c"]),
                  ("top", "AND", ["g1", "g2"])], "top")
    assert r.json() == {"cut_sets": [["a"], ["b", "c"]]}


def test_duplicate_input_reference_dedup():
    # 同一子门重复引用：(a∧b) ∨ (a∧b)
    r = post_cut({"a": "1/2", "b": "1/3"},
                 [("g", "AND", ["a", "b"]),
                  ("top", "OR", ["g", "g"])], "top")
    assert r.json() == {"cut_sets": [["a", "b"]]}

    # 同一事件在 AND 输入中重复：a ∧ a ∧ b
    r = post_cut({"a": "1/2", "b": "1/3"},
                 [("g", "AND", ["a", "a", "b"])], "g")
    assert r.json() == {"cut_sets": [["a", "b"]]}


def test_sorting_within_and_between_groups():
    # 组内按 id 字典序、组间按长度再字典序（长度优先于字典序）
    r = post_cut({"a": "1/2", "b": "1/2", "c": "1/2", "d": "1/2",
                  "e": "1/2"},
                 [("g1", "AND", ["b", "a"]),       # {a,b}
                  ("g2", "AND", ["e", "c"]),       # {c,e}
                  ("top", "OR", ["g1", "d", "g2"])],
                 "top")
    assert r.status_code == 200, r.text
    # 单事件 {d} 长度为 1 排在最前，尽管字典序上 "d" > "a"
    assert r.json() == {"cut_sets": [["d"], ["a", "b"], ["c", "e"]]}


def test_p_valid_but_not_used():
    # p 仍须合法，但取值不影响割集：两组不同概率返回相同结果
    base_gates = [("g1", "AND", ["a", "b"]),
                  ("g2", "OR", ["a", "c"]),
                  ("top", "AND", ["g1", "g2"])]
    r1 = post_cut({"a": "1/2", "b": "1/3", "c": "0/1"},
                  base_gates, "top")
    r2 = post_cut({"a": "1/1", "b": "999999/1000000", "c": "7/11"},
                  base_gates, "top")
    assert r1.status_code == r2.status_code == 200
    assert r1.json() == r2.json()
    # (a∧b) ∧ (a∨c) = a∧b
    assert r1.json() == {"cut_sets": [["a", "b"]]}


def test_analyze_endpoint_still_works_same_format():
    payload = {
        "events": [{"id": "a", "p": "1/2"}, {"id": "b", "p": "1/3"}],
        "gates": [{"id": "g", "op": "OR", "inputs": ["a", "b"]}],
        "top": "g",
    }
    r = client.post("/v1/cut-sets", json=payload)
    assert r.status_code == 200
    assert r.json() == {"cut_sets": [["a"], ["b"]]}
    r = client.post("/v1/analyze", json=payload)
    assert r.status_code == 200
    assert r.json()["top_probability"] == "2/3"


# ---------- NOT 一律拒绝 ----------

def test_not_gate_rejected():
    r = post_cut({"a": "1/2", "b": "1/3"},
                 [("n", "NOT", ["b"]),
                  ("g", "AND", ["a", "n"])], "g")
    assert r.status_code == 422
    detail = r.json()["detail"]
    assert "NOT" in detail and "割集" in detail


def test_unreachable_not_gate_still_rejected():
    # 顶层不可达的 NOT 门同样拒绝
    r = post_cut({"a": "1/2", "b": "1/3"},
                 [("g", "OR", ["a", "b"]),
                  ("h", "NOT", ["a"])], "g")
    assert r.status_code == 422
    assert "NOT" in r.json()["detail"]


# ---------- 沿用 /v1/analyze 的校验 ----------

def test_bad_probability_still_rejected():
    r = post_cut({"a": "3/2"}, [], "a")
    assert r.status_code == 422 and "概率" in r.json()["detail"]


def test_unknown_reference_and_top_rejected():
    r = post_cut({"a": "1/2"}, [("g", "AND", ["a", "x"])], "g")
    assert r.status_code == 422 and "未知" in r.json()["detail"]
    r = post_cut({"a": "1/2"}, [], "nope")
    assert r.status_code == 422 and "顶层" in r.json()["detail"]


def test_cycle_rejected():
    r = post_cut({"a": "1/2"}, [("g", "OR", ["a", "g"])], "g")
    assert r.status_code == 422 and "环" in r.json()["detail"]


def test_bad_arity_and_counts_rejected():
    # AND 只有 1 输入
    r = post_cut({"a": "1/2"}, [("g", "AND", ["a"])], "g")
    assert r.status_code == 422
    # 0 个事件
    r = client.post("/v1/cut-sets",
                    json={"events": [], "gates": [], "top": "x"})
    assert r.status_code == 422
    # 33 个事件
    evs = [{"id": f"e{i}", "p": "1/2"} for i in range(33)]
    r = client.post("/v1/cut-sets",
                    json={"events": evs, "gates": [], "top": "e0"})
    assert r.status_code == 422
    # 多余字段
    r = client.post("/v1/cut-sets", json={
        "events": [{"id": "a", "p": "1/2", "x": 1}],
        "gates": [], "top": "a"})
    assert r.status_code == 422


# ---------- 5000 组上限 ----------

def _product_circuit(group_sizes, prefix="e", extra_singletons=0):
    """构造 AND(OR(...), ...)：割集数 = ∏ group_sizes。

    返回 (ev_specs, gate_specs, top_id)，各 OR 门使用不相交的事件。
    """
    ev_specs, or_gates = {}, []
    idx = 0
    for gi, size in enumerate(group_sizes):
        refs = []
        for _ in range(size):
            eid = f"{prefix}{idx:02d}"
            ev_specs[eid] = "1/2"
            refs.append(eid)
            idx += 1
        or_gates.append((f"or{gi}", "OR", refs))
    top = ("prod", "AND", [g[0] for g in or_gates])
    gates = or_gates + [top]
    if extra_singletons:
        extra_refs = []
        for _ in range(extra_singletons):
            eid = f"{prefix}{idx:02d}"
            ev_specs[eid] = "1/2"
            extra_refs.append(eid)
            idx += 1
        gates.append(("big", "OR", ["prod"] + extra_refs))
        return ev_specs, gates, "big"
    return ev_specs, gates, "prod"


def test_exactly_5000_cut_sets_ok():
    # 8 × 5^4 = 5000，恰好不超限
    ev_specs, gates, top = _product_circuit([8, 5, 5, 5, 5])
    assert len(ev_specs) == 28
    r = post_cut(ev_specs, gates, top)
    assert r.status_code == 200, r.text
    cut_sets = r.json()["cut_sets"]
    assert len(cut_sets) == 5000
    assert is_sorted(cut_sets)
    assert all(len(g) == 5 for g in cut_sets)


def test_5001_cut_sets_rejected():
    # 5000 组后再 OR 一个事件 → 5001，超限
    ev_specs, gates, top = _product_circuit([8, 5, 5, 5, 5],
                                            extra_singletons=1)
    r = post_cut(ev_specs, gates, top)
    assert r.status_code == 422
    detail = r.json()["detail"]
    assert "5000" in detail and "割集" in detail and "big" in detail


def test_huge_gate_65536_rejected():
    # 2^8 × 2^8 = 65536 组，19 个门、32 个事件
    ev_specs = {f"e{i:02d}": "1/2" for i in range(16)}
    ev_specs.update({f"f{i:02d}": "1/2" for i in range(16)})
    or_a = [(f"oa{i}", "OR", [f"e{2*i:02d}", f"e{2*i+1:02d}"])
            for i in range(8)]
    or_b = [(f"ob{i}", "OR", [f"f{2*i:02d}", f"f{2*i+1:02d}"])
            for i in range(8)]
    gates = or_a + [("ga", "AND", [g[0] for g in or_a])]
    gates += or_b + [("gb", "AND", [g[0] for g in or_b])]
    gates += [("huge", "AND", ["ga", "gb"])]
    r = post_cut(ev_specs, gates, "huge")
    assert r.status_code == 422
    detail = r.json()["detail"]
    assert "5000" in detail and "huge" in detail


def test_limit_applies_to_unreachable_gate():
    # 巨型门不可达于顶层，仍应 422（“任一门”）
    or_a = [(f"oa{i}", "OR", [f"e{2*i:02d}", f"e{2*i+1:02d}"])
            for i in range(8)]
    or_b = [(f"ob{i}", "OR", [f"f{2*i:02d}", f"f{2*i+1:02d}"])
            for i in range(8)]
    ev_specs = {f"e{i:02d}": "1/2" for i in range(16)}
    ev_specs.update({f"f{i:02d}": "1/2" for i in range(16)})
    gates = or_a + [("ga", "AND", [g[0] for g in or_a])]
    gates += or_b + [("gb", "AND", [g[0] for g in or_b])]
    gates += [("huge", "AND", ["ga", "gb"])]
    r = post_cut(ev_specs, gates, "e00")
    assert r.status_code == 422
    assert "huge" in r.json()["detail"]


# ---------- 随机单调电路与穷举真值表对照 ----------

def test_random_monotone_circuits():
    rng = random.Random(20260926)
    for _ in range(30):
        n = rng.randint(1, 8)
        ev_ids = [f"e{i}" for i in range(n)]
        ev_specs = {eid: f"{rng.randint(0, 9)}/10" for eid in ev_ids}
        gates = []
        available = list(ev_ids)
        for j in range(rng.randint(0, 12)):
            if len(available) < 2:
                break
            gid = f"g{j}"
            k = rng.randint(2, min(8, len(available)))
            op = rng.choice(["AND", "OR"])
            inputs = rng.sample(available, k)
            gates.append((gid, op, inputs))
            available.append(gid)
        top = rng.choice(available)

        r = post_cut(ev_specs, gates, top)
        assert r.status_code == 200, (r.text, ev_specs, gates)
        cut_sets = r.json()["cut_sets"]
        assert cut_sets == brute_min_cut_sets(ev_specs, gates, top), (
            ev_specs, gates, top)
        assert is_sorted(cut_sets)


def test_deterministic_cases_against_brute_force():
    cases = [
        # 多层 AND/OR、共享事件
        ({"a": "1/2", "b": "1/3", "c": "1/5"},
         [("g1", "AND", ["a", "b"]),
          ("g2", "AND", ["a", "c"]),
          ("top", "OR", ["g1", "g2"])], "top"),
        # 8 输入 AND / OR
        ({"a": "1/2", "b": "1/2", "c": "1/2", "d": "1/2",
          "e": "1/2", "f": "1/2", "g": "1/2", "h": "1/2"},
         [("t", "AND", ["a", "b", "c", "d", "e", "f", "g", "h"])], "t"),
        ({"a": "1/2", "b": "1/2", "c": "1/2", "d": "1/2",
          "e": "1/2", "f": "1/2", "g": "1/2", "h": "1/2"},
         [("t", "OR", ["a", "b", "c", "d", "e", "f", "g", "h"])], "t"),
        # 顶层为事件，门全部不可达
        ({"a": "1/2", "b": "1/2"},
         [("g", "AND", ["a", "b"])], "a"),
        # p 为 0/1 边界，结构上无影响
        ({"a": "0/1", "b": "1/1", "c": "1/1"},
         [("g1", "OR", ["a", "b"]),
          ("top", "AND", ["g1", "c"])], "top"),
    ]
    for ev_specs, gates, top in cases:
        r = post_cut(ev_specs, gates, top)
        assert r.status_code == 200, r.text
        assert r.json()["cut_sets"] == brute_min_cut_sets(
            ev_specs, gates, top)
