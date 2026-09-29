"""POST /v1/compare-trees：两版故障树差异概率与等价性的语义、精确性与 422 校验。

用穷举真值表（枚举全部事件状态）作为参考实现做交叉验证。
"""

import itertools
import random
from fractions import Fraction

import pytest
from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


# ---------- 穷举真值表参考实现 ----------

def brute_compare(ev_specs, base_gates, base_top, rev_gates, rev_top):
    """枚举全部事件赋值，返回 (added, removed, change, equivalent)。

    added = P(¬B∧R)，removed = P(B∧¬R)，change = P(B⊕R)；
    equivalent 要求全部赋值（含零概率赋值）下 B 与 R 都相同。
    """
    ev_ids = sorted(ev_specs)
    probs = {e: Fraction(ev_specs[e]) for e in ev_ids}

    def make_eval(gate_specs):
        gates = {g[0]: (g[1], g[2]) for g in gate_specs}

        def evaluate(state, node):
            if node in state:
                return state[node]
            op, inputs = gates[node]
            vals = [evaluate(state, i) for i in inputs]
            if op == "AND":
                return all(vals)
            if op == "OR":
                return any(vals)
            return not vals[0]

        return evaluate

    eval_base = make_eval(base_gates)
    eval_rev = make_eval(rev_gates)

    added = removed = change = Fraction(0)
    equivalent = True
    for bits in itertools.product((0, 1), repeat=len(ev_ids)):
        state = dict(zip(ev_ids, (bool(b) for b in bits)))
        b_val = eval_base(state, base_top)
        r_val = eval_rev(state, rev_top)
        if b_val == r_val:
            continue
        equivalent = False
        weight = Fraction(1)
        for e in ev_ids:
            weight *= probs[e] if state[e] else (Fraction(1) - probs[e])
        change += weight
        if r_val:
            added += weight
        else:
            removed += weight
    return added, removed, change, equivalent


def f2s(f):
    return f"{f.numerator}/{f.denominator}"


def make_body(ev_specs, base_gates, base_top, rev_gates, rev_top):
    return {
        "events": [{"id": k, "p": v} for k, v in ev_specs.items()],
        "baseline": {
            "gates": [
                {"id": i, "op": op, "inputs": ins}
                for i, op, ins in base_gates
            ],
            "top": base_top,
        },
        "revised": {
            "gates": [
                {"id": i, "op": op, "inputs": ins}
                for i, op, ins in rev_gates
            ],
            "top": rev_top,
        },
    }


def post_compare(ev_specs, base_gates, base_top, rev_gates, rev_top):
    return client.post(
        "/v1/compare-trees",
        json=make_body(ev_specs, base_gates, base_top, rev_gates, rev_top),
    )


def assert_matches_brute(ev_specs, base_gates, base_top, rev_gates, rev_top):
    r = post_compare(ev_specs, base_gates, base_top, rev_gates, rev_top)
    assert r.status_code == 200, r.text
    data = r.json()
    added, removed, change, equivalent = brute_compare(
        ev_specs, base_gates, base_top, rev_gates, rev_top
    )
    assert data["added_probability"] == f2s(added)
    assert data["removed_probability"] == f2s(removed)
    assert data["change_probability"] == f2s(change)
    assert data["equivalent"] is equivalent
    # 三个概率的内部一致性：change = added + removed（两者互斥）
    assert Fraction(data["change_probability"]) == (
        Fraction(data["added_probability"])
        + Fraction(data["removed_probability"])
    )
    return data


MOTOR_EV = {
    "motor_overheat": "1/100",
    "coolant_loss": "2/100",
    "pump_fail": "1/200",
}


# ---------- 基本语义：README 泵冗余改版示例 ----------

def test_readme_redundant_pump_example():
    # 改版把冷却失效从“任一失效”改为“同时失效”（泵冗余），
    # 两版门 id 同名（cooling_fail/top）但定义独立
    base_gates = [
        ("cooling_fail", "OR", ["coolant_loss", "pump_fail"]),
        ("top", "OR", ["motor_overheat", "cooling_fail"]),
    ]
    rev_gates = [
        ("cooling_fail", "AND", ["coolant_loss", "pump_fail"]),
        ("top", "OR", ["motor_overheat", "cooling_fail"]),
    ]
    data = assert_matches_brute(MOTOR_EV, base_gates, "top", rev_gates, "top")
    # R ⊆ B：added = 0；removed = P(B) − P(R)
    #   = 34651/1000000 − 10099/1000000 = 3069/125000
    assert data == {
        "added_probability": "0/1",
        "removed_probability": "3069/125000",
        "change_probability": "3069/125000",
        "equivalent": False,
    }


def test_and_to_or_symmetric_difference():
    # B = a∧b，R = a∨b：added = P(R)−P(B) = 1/2，removed = 0
    data = assert_matches_brute(
        {"a": "1/2", "b": "1/3"},
        [("g", "AND", ["a", "b"])], "g",
        [("g", "OR", ["a", "b"])], "g",
    )
    assert data["added_probability"] == "1/2"
    assert data["removed_probability"] == "0/1"
    assert data["change_probability"] == "1/2"
    assert data["equivalent"] is False


def test_event_as_top_without_gates():
    # 两版都可以没有门，直接以事件为顶
    data = assert_matches_brute(
        {"a": "2/5", "b": "1/7"}, [], "a", [], "b"
    )
    # added = P(¬a∧b) = 3/5·1/7 = 3/35；removed = P(a∧¬b) = 2/5·6/7 = 12/35
    assert data["added_probability"] == "3/35"
    assert data["removed_probability"] == "12/35"
    assert data["change_probability"] == "3/7"
    assert data["equivalent"] is False


# ---------- 等价性语义 ----------

def test_identical_structure_is_equivalent():
    data = assert_matches_brute(
        MOTOR_EV,
        [("cooling_fail", "OR", ["coolant_loss", "pump_fail"]),
         ("top", "OR", ["motor_overheat", "cooling_fail"])],
        "top",
        [("cooling_fail", "OR", ["coolant_loss", "pump_fail"]),
         ("top", "OR", ["motor_overheat", "cooling_fail"])],
        "top",
    )
    assert data == {
        "added_probability": "0/1",
        "removed_probability": "0/1",
        "change_probability": "0/1",
        "equivalent": True,
    }


def test_same_function_different_structure_is_equivalent():
    # a ∨ (a∧b) 吸收为 a：结构不同但函数相同
    data = assert_matches_brute(
        {"a": "1/2", "b": "1/3"},
        [("g", "AND", ["a", "b"]), ("top", "OR", ["a", "g"])],
        "top",
        [],
        "a",
    )
    assert data["equivalent"] is True
    assert data["change_probability"] == "0/1"


def test_zero_probability_difference_still_not_equivalent():
    # a 的 p=0/1：B=b 与 R=b∨a 仅在零概率赋值 a=true 上不同，
    # 差异概率全为 0，但仍算不等价
    data = assert_matches_brute(
        {"a": "0/1", "b": "1/2"},
        [],
        "b",
        [("g", "OR", ["b", "a"])],
        "g",
    )
    assert data == {
        "added_probability": "0/1",
        "removed_probability": "0/1",
        "change_probability": "0/1",
        "equivalent": False,
    }


def test_certain_event_difference_is_not_equivalent():
    # a 的 p=1/1：B = a∧b 与 R = b 仅在零概率赋值 a=false 上不同
    data = assert_matches_brute(
        {"a": "1/1", "b": "1/2"},
        [("g", "AND", ["a", "b"])],
        "g",
        [],
        "b",
    )
    assert data["change_probability"] == "0/1"
    assert data["equivalent"] is False


# ---------- 共享状态与先验 ----------

def test_same_event_shares_state_across_versions():
    # B=a、R=¬a，p(a)=1/4：同一事件共享状态，
    # added = P(¬a) = 3/4（若当作独立副本则为 9/16）
    data = assert_matches_brute(
        {"a": "1/4"},
        [],
        "a",
        [("n", "NOT", ["a"])],
        "n",
    )
    assert data["added_probability"] == "3/4"
    assert data["removed_probability"] == "1/4"
    assert data["change_probability"] == "1/1"
    assert data["equivalent"] is False


def test_shared_event_in_both_trees():
    # 两版都引用同一事件 a：B = a∧b，R = a∧c
    # added = P(a∧¬b∧c)，removed = P(a∧b∧¬c)
    data = assert_matches_brute(
        {"a": "1/2", "b": "1/3", "c": "1/5"},
        [("g", "AND", ["a", "b"])],
        "g",
        [("g", "AND", ["a", "c"])],
        "g",
    )
    assert Fraction(data["added_probability"]) == (
        Fraction(1, 2) * Fraction(2, 3) * Fraction(1, 5)
    )
    assert Fraction(data["removed_probability"]) == (
        Fraction(1, 2) * Fraction(1, 3) * Fraction(4, 5)
    )


# ---------- 门 id 同名但定义独立 ----------

def test_same_gate_id_independent_definitions():
    # 两版都有门 g：baseline 为 AND，revised 为 OR，各自解析到本版定义
    data = assert_matches_brute(
        {"a": "1/2", "b": "1/2"},
        [("g", "AND", ["a", "b"])],
        "g",
        [("g", "OR", ["a", "b"])],
        "g",
    )
    assert data["added_probability"] == "1/2"
    assert data["removed_probability"] == "0/1"
    assert data["equivalent"] is False


def test_same_gate_id_same_definition_is_equivalent():
    data = assert_matches_brute(
        {"a": "1/2", "b": "1/2"},
        [("g", "AND", ["a", "b"])],
        "g",
        [("g", "AND", ["a", "b"])],
        "g",
    )
    assert data["equivalent"] is True


# ---------- 随机电路对穷举真值表交叉验证 ----------

def test_random_circuit_pairs():
    """随机生成共享事件的故障树版本对，逐例穷举对照。"""
    rng = random.Random(20260927)
    checked = 0
    while checked < 40:
        n = rng.randint(1, 9)
        ev_specs = {}
        for i in range(n):
            d = rng.randint(1, 8)
            ev_specs[f"e{i}"] = f"{rng.randint(0, d)}/{d}"

        def random_version():
            gates = []
            available = sorted(ev_specs)
            for j in range(rng.randint(0, 10)):
                gid = f"g{j}"
                op = (
                    rng.choice(["AND", "OR", "NOT"])
                    if len(available) >= 2
                    else "NOT"
                )
                if op == "NOT":
                    inputs = [rng.choice(available)]
                else:
                    k = rng.randint(2, min(8, len(available)))
                    inputs = rng.sample(available, k)
                gates.append((gid, op, inputs))
                available = available + [gid]
            return gates, rng.choice(available)

        base_gates, base_top = random_version()
        rev_gates, rev_top = random_version()
        assert_matches_brute(ev_specs, base_gates, base_top, rev_gates, rev_top)
        checked += 1


# ---------- 422 错误路径 ----------

def test_cross_version_reference_rejected():
    # baseline 的门引用了仅在 revised 中定义的门 h
    body = make_body(
        {"a": "1/2"},
        [("g", "NOT", ["h"])], "g",
        [("h", "NOT", ["a"])], "h",
    )
    r = client.post("/v1/compare-trees", json=body)
    assert r.status_code == 422
    detail = r.json()["detail"]
    assert "跨版引用" in detail and "'h'" in detail


def test_cross_version_reference_via_top_rejected():
    # revised 的 top 指向仅在 baseline 中定义的门
    body = make_body(
        {"a": "1/2"},
        [("g", "NOT", ["a"])], "g",
        [], "g",
    )
    r = client.post("/v1/compare-trees", json=body)
    assert r.status_code == 422
    assert "跨版引用" in r.json()["detail"]


def test_same_named_gate_is_not_cross_reference():
    # 两版各自定义了同名门 g：本版引用解析到本版定义，不算跨版引用
    body = make_body(
        {"a": "1/2", "b": "1/3"},
        [("g", "AND", ["a", "b"])], "g",
        [("g", "OR", ["a", "b"])], "g",
    )
    r = client.post("/v1/compare-trees", json=body)
    assert r.status_code == 200, r.text


def test_unknown_reference_within_version():
    # 引用两版都未定义的 id：按未知节点报错
    body = make_body(
        {"a": "1/2"},
        [("g", "AND", ["a", "x"])], "g",
        [], "a",
    )
    r = client.post("/v1/compare-trees", json=body)
    assert r.status_code == 422
    assert "未知" in r.json()["detail"]


def test_unknown_top_in_version():
    body = make_body(
        {"a": "1/2"},
        [], "a",
        [], "nope",
    )
    r = client.post("/v1/compare-trees", json=body)
    assert r.status_code == 422
    assert "顶层" in r.json()["detail"]


def test_cycle_in_either_version_rejected():
    # 环在 baseline
    body = make_body(
        {"a": "1/2"},
        [("g", "OR", ["a", "g"])], "g",
        [], "a",
    )
    r = client.post("/v1/compare-trees", json=body)
    assert r.status_code == 422
    assert "环" in r.json()["detail"]
    # 环在 revised
    body = make_body(
        {"a": "1/2"},
        [], "a",
        [("g1", "OR", ["a", "g2"]), ("g2", "AND", ["a", "g1"])], "g1",
    )
    r = client.post("/v1/compare-trees", json=body)
    assert r.status_code == 422
    assert "环" in r.json()["detail"]


def test_bad_probability_in_shared_events():
    body = make_body({"a": "3/2"}, [], "a", [], "a")
    r = client.post("/v1/compare-trees", json=body)
    assert r.status_code == 422
    assert "概率" in r.json()["detail"]


def test_gate_validation_reused_per_version():
    # NOT 元数错误（revised）
    body = make_body(
        {"a": "1/2", "b": "1/3"},
        [], "a",
        [("g", "NOT", ["a", "b"])], "g",
    )
    r = client.post("/v1/compare-trees", json=body)
    assert r.status_code == 422
    assert "NOT" in r.json()["detail"]
    # 门 id 与事件 id 冲突（baseline）
    body = make_body(
        {"a": "1/2"},
        [("a", "NOT", ["a"])], "a",
        [], "a",
    )
    r = client.post("/v1/compare-trees", json=body)
    assert r.status_code == 422
    assert "冲突" in r.json()["detail"]
    # 同一版本内门 id 重复
    body = make_body(
        {"a": "1/2", "b": "1/3"},
        [("g", "AND", ["a", "b"]), ("g", "OR", ["a", "b"])], "g",
        [], "a",
    )
    r = client.post("/v1/compare-trees", json=body)
    assert r.status_code == 422
    assert "重复" in r.json()["detail"]


def test_structure_errors():
    # 缺 baseline / revised、版本内缺 top、额外字段、事件数超限
    r = client.post("/v1/compare-trees", json={
        "events": [{"id": "a", "p": "1/2"}],
        "baseline": {"gates": [], "top": "a"},
    })
    assert r.status_code == 422
    r = client.post("/v1/compare-trees", json={
        "events": [{"id": "a", "p": "1/2"}],
        "baseline": {"gates": []},
        "revised": {"gates": [], "top": "a"},
    })
    assert r.status_code == 422
    r = client.post("/v1/compare-trees", json={
        "events": [{"id": "a", "p": "1/2"}],
        "baseline": {"gates": [], "top": "a"},
        "revised": {"gates": [], "top": "a"},
        "extra": 1,
    })
    assert r.status_code == 422
    r = client.post("/v1/compare-trees", json={
        "events": [{"id": "a", "p": "1/2"}],
        "baseline": {"gates": [], "top": "a", "junk": 0},
        "revised": {"gates": [], "top": "a"},
    })
    assert r.status_code == 422
    evs = [{"id": f"e{i}", "p": "1/2"} for i in range(33)]
    r = client.post("/v1/compare-trees", json={
        "events": evs,
        "baseline": {"gates": [], "top": "e0"},
        "revised": {"gates": [], "top": "e0"},
    })
    assert r.status_code == 422
    # 单版本门数超过 64
    gates = [{"id": f"g{i}", "op": "NOT", "inputs": ["e0"]} for i in range(65)]
    r = client.post("/v1/compare-trees", json={
        "events": [{"id": "e0", "p": "1/2"}],
        "baseline": {"gates": gates, "top": "g0"},
        "revised": {"gates": [], "top": "e0"},
    })
    assert r.status_code == 422


def test_node_limit_counts_both_versions_and_diff(monkeypatch):
    # 生产上限为 50000
    import app.main as main_mod
    assert main_mod.MAX_NONTERMINAL_NODES == 50_000

    # 把上限调小：两版建图及差异运算在同一 ROBDD 上累计，很快超限
    monkeypatch.setattr(main_mod, "MAX_NONTERMINAL_NODES", 6)
    evs = {f"e{i}": "1/2" for i in range(8)}

    def xor_chain():
        gates = []
        prev = "e0"
        for i in range(1, 8):
            o, a, n, x = f"o{i}", f"a{i}", f"n{i}", f"x{i}"
            gates.append((o, "OR", [prev, f"e{i}"]))
            gates.append((a, "AND", [prev, f"e{i}"]))
            gates.append((n, "NOT", [a]))
            gates.append((x, "AND", [o, n]))
            prev = x
        return gates, prev

    base_gates, base_top = xor_chain()
    rev_gates, rev_top = xor_chain()
    r = post_compare(evs, base_gates, base_top, rev_gates, rev_top)
    assert r.status_code == 422
    detail = r.json()["detail"]
    assert "非终端节点" in detail and "6" in detail
