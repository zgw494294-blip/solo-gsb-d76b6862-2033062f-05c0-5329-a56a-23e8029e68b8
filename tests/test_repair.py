"""POST /v1/repair-plan：修复方案优选的语义、排序、NOT 求值与 422 校验。

用穷举（枚举所有可修复子集）作为参考实现做交叉验证。
"""

import itertools
import random

import pytest
from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


# ---------- 穷举真值表参考实现 ----------

def brute_repair(ev_specs, gate_specs, top, states, costs):
    """枚举可修复事件的全部子集，返回 (repair_events, total_cost) 或 None。"""
    ev_ids = sorted(ev_specs)
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

    repairable = [e for e in ev_ids if states[e]]
    best = None
    for bits in itertools.product((0, 1), repeat=len(repairable)):
        state = dict(states)
        # bit=0 表示修复为 false，1 表示保持 true
        for e, bit in zip(repairable, bits):
            state[e] = bool(bit)
        if evaluate(state, top):
            continue
        repaired = tuple(
            e for e, bit in zip(repairable, bits) if bit == 0
        )
        total = sum(costs[e] for e in repaired)
        key = (total, len(repaired), sorted(repaired))
        if best is None or key < best[0]:
            best = (key, list(sorted(repaired)), total)
    if best is None:
        return None
    return best[1], best[2]


def make_events(ev_specs):
    return [{"id": k, "p": v} for k, v in ev_specs.items()]


def post_plan(ev_specs, gate_specs, top, states, costs):
    return client.post("/v1/repair-plan", json={
        "events": make_events(ev_specs),
        "gates": [{"id": i, "op": op, "inputs": ins}
                  for i, op, ins in gate_specs],
        "top": top,
        "states": states,
        "repair_costs": costs,
    })


def default_states(ev_specs, **overrides):
    states = {e: True for e in ev_specs}
    states.update(overrides)
    return states


def default_costs(ev_specs, **overrides):
    costs = {e: 1 for e in ev_specs}
    costs.update(overrides)
    return costs


# ---------- 基本语义 ----------

def test_or_gate_requires_all_repaired():
    ev = {"a": "1/2", "b": "1/2"}
    r = post_plan(ev, [("g", "OR", ["a", "b"])], "g",
                  default_states(ev), default_costs(ev, a=5, b=3))
    assert r.status_code == 200, r.text
    # OR 必须让每个输入都为 false，两个事件都要修
    assert r.json() == {
        "possible": True, "repair_events": ["a", "b"], "total_cost": 8}


def test_and_gate_repairs_cheapest_single():
    ev = {"a": "1/2", "b": "1/2"}
    r = post_plan(ev, [("g", "AND", ["a", "b"])], "g",
                  default_states(ev), default_costs(ev, a=5, b=3))
    assert r.status_code == 200, r.text
    # AND 只需任一输入为 false，修更便宜的 b
    assert r.json() == {
        "possible": True, "repair_events": ["b"], "total_cost": 3}


def test_top_already_false_empty_plan():
    # 顶事件在当前状态下已经为 false：空方案、零成本
    ev = {"a": "1/2", "b": "1/2"}
    r = post_plan(ev, [("g", "OR", ["a", "b"])], "g",
                  default_states(ev, a=False, b=False),
                  default_costs(ev, a=1, b=1))
    assert r.status_code == 200, r.text
    assert r.json() == {
        "possible": True, "repair_events": [], "total_cost": 0}

    # 顶层就是当前为 false 的事件
    r = post_plan({"a": "1/2"}, [], "a",
                  {"a": False}, {"a": 1})
    assert r.json() == {
        "possible": True, "repair_events": [], "total_cost": 0}


def test_and_gate_requires_all_repaired():
    ev = {"a": "1/2", "b": "1/2"}
    r = post_plan(ev, [("g", "AND", ["a", "b"])], "g",
                  default_states(ev), default_costs(ev, a=2, b=4))
    assert r.status_code == 200, r.text
    assert r.json() == {
        "possible": True, "repair_events": ["a"], "total_cost": 2}


def test_infeasible_frozen_false_with_not():
    # top = a OR NOT b，当前 a=true、b=false => true OR true = true。
    # 修复 a 后仍为 false OR true = true；b 当前为 false 无法变成 true。
    ev = {"a": "1/2", "b": "1/2"}
    gates = [("n", "NOT", ["b"]), ("g", "OR", ["a", "n"])]
    r = post_plan(ev, gates, "g",
                  default_states(ev, a=True, b=False),
                  default_costs(ev))
    assert r.status_code == 200, r.text
    assert r.json() == {
        "possible": False, "repair_events": [], "total_cost": None}


def test_not_gate_evaluated_normally_feasible():
    # top = NOT a：a=true 时顶事件为 false（空方案）；a=false 时不可行
    ev = {"a": "1/2"}
    r = post_plan(ev, [("g", "NOT", ["a"])], "g",
                  {"a": True}, {"a": 1})
    assert r.json() == {
        "possible": True, "repair_events": [], "total_cost": 0}

    r = post_plan(ev, [("g", "NOT", ["a"])], "g",
                  {"a": False}, {"a": 1})
    assert r.json() == {
        "possible": False, "repair_events": [], "total_cost": None}

    # top = a AND NOT b：a=true,b=true => true AND false = false，空方案
    ev2 = {"a": "1/2", "b": "1/2"}
    gates = [("n", "NOT", ["b"]), ("g", "AND", ["a", "n"])]
    r = post_plan(ev2, gates, "g",
                  default_states(ev2), default_costs(ev2))
    assert r.json() == {
        "possible": True, "repair_events": [], "total_cost": 0}


def test_shared_reference_repaired_once():
    # (a∧b)∨(a∧c)：修复共享的 a 一处即可，费用只计一次
    ev = {"a": "1/2", "b": "1/2", "c": "1/2"}
    gates = [("g1", "AND", ["a", "b"]),
             ("g2", "AND", ["a", "c"]),
             ("top", "OR", ["g1", "g2"])]
    r = post_plan(ev, gates, "top",
                  default_states(ev), default_costs(ev, a=1, b=1, c=1))
    assert r.status_code == 200, r.text
    assert r.json() == {
        "possible": True, "repair_events": ["a"], "total_cost": 1}

    # a 很贵时改为同时修复 b、c（共享门的费用结构正常参与比较）
    r = post_plan(ev, gates, "top",
                  default_states(ev), default_costs(ev, a=5, b=1, c=1))
    assert r.json() == {
        "possible": True, "repair_events": ["b", "c"], "total_cost": 2}


def test_tie_break_by_count_then_lexicographic():
    # top = a AND (b OR c)，全 true：修 a（1 个，费用 3）
    # 或同时修 b、c（2 个，费用 1+2=3）；等成本时修复数少者胜
    ev = {"a": "1/2", "b": "1/2", "c": "1/2"}
    gates = [("bc", "OR", ["b", "c"]),
             ("top", "AND", ["a", "bc"])]
    r = post_plan(ev, gates, "top",
                  default_states(ev), default_costs(ev, a=3, b=1, c=2))
    assert r.json()["repair_events"] == ["a"]
    assert r.json()["total_cost"] == 3

    # 让 a 更贵：总成本优先，改取 {b,c}
    r = post_plan(ev, gates, "top",
                  default_states(ev), default_costs(ev, a=4, b=1, c=2))
    assert r.json()["repair_events"] == ["b", "c"]
    assert r.json()["total_cost"] == 3

    # 等成本且等修复数：OR(a,b) 二者同价，取字典序更小的 a
    ev2 = {"a": "1/2", "b": "1/2"}
    r = post_plan(ev2, [("g", "OR", ["a", "b"])], "g",
                  default_states(ev2), default_costs(ev2, a=3, b=3))
    assert r.json()["repair_events"] == ["a", "b"]
    # 单事件分支：AND(a,b) 同价，取字典序更小的 a
    r = post_plan(ev2, [("g", "AND", ["a", "b"])], "g",
                  default_states(ev2), default_costs(ev2, a=3, b=3))
    assert r.json() == {
        "possible": True, "repair_events": ["a"], "total_cost": 3}


def test_currently_false_events_frozen():
    # OR(a,b)：a=false 冻结、b=true，修复 b 即可
    ev = {"a": "1/2", "b": "1/2"}
    r = post_plan(ev, [("g", "OR", ["a", "b"])], "g",
                  default_states(ev, a=False, b=True),
                  default_costs(ev))
    assert r.json() == {
        "possible": True, "repair_events": ["b"], "total_cost": 1}

    # AND(a,b)：a=false 已使顶事件为 false，空方案（无需动 b）
    r = post_plan(ev, [("g", "AND", ["a", "b"])], "g",
                  default_states(ev, a=False, b=True),
                  default_costs(ev, a=100, b=100))
    assert r.json() == {
        "possible": True, "repair_events": [], "total_cost": 0}


def test_repair_events_sorted_by_id():
    ev = {"c": "1/2", "a": "1/2", "b": "1/2"}
    # OR 需要修复全部三个，输出仍按事件 id 字典序
    r = post_plan(ev, [("g", "OR", ["c", "a", "b"])], "g",
                  default_states(ev), default_costs(ev))
    assert r.json()["repair_events"] == ["a", "b", "c"]


def test_cost_boundaries():
    ev = {"a": "1/2"}
    r = post_plan(ev, [], "a", {"a": True}, {"a": 1_000_000})
    assert r.status_code == 200, r.text
    assert r.json() == {
        "possible": True, "repair_events": ["a"], "total_cost": 1_000_000}


# ---------- 422：states / repair_costs 覆盖与取值 ----------

def test_states_missing_key():
    r = client.post("/v1/repair-plan", json={
        "events": make_events({"a": "1/2", "b": "1/2"}),
        "gates": [], "top": "a",
        "states": {"a": True},
        "repair_costs": {"a": 1, "b": 1}})
    assert r.status_code == 422
    detail = r.json()["detail"]
    assert "states" in detail and "缺少" in detail and "'b'" in detail


def test_states_extra_key():
    r = client.post("/v1/repair-plan", json={
        "events": make_events({"a": "1/2"}),
        "gates": [], "top": "a",
        "states": {"a": True, "x": False},
        "repair_costs": {"a": 1}})
    assert r.status_code == 422
    detail = r.json()["detail"]
    assert "states" in detail and "不属于" in detail and "'x'" in detail


def test_costs_missing_and_extra_reported_together():
    r = client.post("/v1/repair-plan", json={
        "events": make_events({"a": "1/2", "b": "1/2"}),
        "gates": [], "top": "a",
        "states": {"a": True, "b": False},
        "repair_costs": {"a": 1, "x": 2}})
    assert r.status_code == 422
    detail = r.json()["detail"]
    assert "repair_costs" in detail
    assert "'b'" in detail and "'x'" in detail


def test_states_and_costs_both_incomplete_both_reasons():
    r = client.post("/v1/repair-plan", json={
        "events": make_events({"a": "1/2", "b": "1/2"}),
        "gates": [], "top": "a",
        "states": {"a": True},
        "repair_costs": {"b": 1}})
    assert r.status_code == 422
    detail = r.json()["detail"]
    assert "states" in detail and "repair_costs" in detail


def test_states_and_costs_fields_required():
    body = {
        "events": make_events({"a": "1/2"}),
        "gates": [], "top": "a",
    }
    r = client.post("/v1/repair-plan", json=body)
    assert r.status_code == 422
    r = client.post("/v1/repair-plan", json={**body, "states": {"a": True}})
    assert r.status_code == 422
    r = client.post(
        "/v1/repair-plan",
        json={**body, "repair_costs": {"a": 1}})
    assert r.status_code == 422


@pytest.mark.parametrize("bad", [0, -1, 1_000_001, -100])
def test_cost_out_of_range(bad):
    r = post_plan({"a": "1/2"}, [], "a",
                  {"a": True}, {"a": bad})
    assert r.status_code == 422
    detail = r.json()["detail"]
    assert "repair_costs" in detail and str(bad) in detail


@pytest.mark.parametrize("bad", [1.5, "1", True, None])
def test_cost_wrong_type(bad):
    r = client.post("/v1/repair-plan", json={
        "events": make_events({"a": "1/2"}),
        "gates": [], "top": "a",
        "states": {"a": True},
        "repair_costs": {"a": bad}})
    assert r.status_code == 422


@pytest.mark.parametrize("bad", [1, 0, "true", None, 1.0])
def test_state_wrong_type(bad):
    r = client.post("/v1/repair-plan", json={
        "events": make_events({"a": "1/2"}),
        "gates": [], "top": "a",
        "states": {"a": bad},
        "repair_costs": {"a": 1}})
    assert r.status_code == 422


def test_extra_field_forbidden():
    r = client.post("/v1/repair-plan", json={
        "events": make_events({"a": "1/2"}),
        "gates": [], "top": "a",
        "states": {"a": True},
        "repair_costs": {"a": 1},
        "note": "x"})
    assert r.status_code == 422


# ---------- 沿用 events / gates / top 校验 ----------

def test_probability_still_validated():
    r = post_plan({"a": "3/2"}, [], "a",
                  {"a": True}, {"a": 1})
    assert r.status_code == 422 and "概率" in r.json()["detail"]


def test_unknown_reference_and_top():
    r = post_plan({"a": "1/2"}, [("g", "OR", ["a", "x"])], "g",
                  {"a": True}, {"a": 1})
    assert r.status_code == 422 and "未知" in r.json()["detail"]
    r = post_plan({"a": "1/2"}, [], "nope",
                  {"a": True}, {"a": 1})
    assert r.status_code == 422 and "顶层" in r.json()["detail"]


def test_cycle_rejected():
    r = post_plan({"a": "1/2"}, [("g", "OR", ["a", "g"])], "g",
                  {"a": True}, {"a": 1})
    assert r.status_code == 422 and "环" in r.json()["detail"]


def test_bad_gate_arity_rejected():
    # NOT 有 2 个输入
    r = post_plan({"a": "1/2", "b": "1/2"},
                  [("g", "NOT", ["a", "b"])], "g",
                  default_states({"a": "1/2", "b": "1/2"}),
                  default_costs({"a": "1/2", "b": "1/2"}))
    assert r.status_code == 422 and "NOT" in r.json()["detail"]


def test_not_gate_is_allowed_here():
    # 与 /v1/cut-sets 不同，NOT 在修复方案接口中正常求值
    r = post_plan({"a": "1/2"}, [("g", "NOT", ["a"])], "g",
                  {"a": True}, {"a": 1})
    assert r.status_code == 200


def test_node_limit_applied(monkeypatch):
    # 生产上限为 50000，与 /v1/analyze 共用
    import app.main as main_mod
    assert main_mod.MAX_NONTERMINAL_NODES == 50_000

    monkeypatch.setattr(main_mod, "MAX_NONTERMINAL_NODES", 5)
    ev = {f"e{i}": "1/2" for i in range(8)}
    gates = []
    prev = "e0"
    for i in range(1, 8):
        o, a, n, x = f"o{i}", f"a{i}", f"n{i}", f"x{i}"
        gates.append((o, "OR", [prev, f"e{i}"]))
        gates.append((a, "AND", [prev, f"e{i}"]))
        gates.append((n, "NOT", [a]))
        gates.append((x, "AND", [o, n]))
        prev = x
    r = post_plan(ev, gates, prev,
                  default_states(ev), default_costs(ev))
    assert r.status_code == 422
    detail = r.json()["detail"]
    assert "非终端节点" in detail and "5" in detail


# ---------- 随机电路（含 NOT）对穷举真值表 ----------

def test_random_circuits_against_brute_force():
    rng = random.Random(20260926)
    for _ in range(120):
        n = rng.randint(1, 9)
        ev_ids = [f"e{i}" for i in range(n)]
        ev_specs = {e: f"{rng.randint(0, 9)}/10" for e in ev_ids}
        gates = []
        available = list(ev_ids)
        for j in range(rng.randint(0, 14)):
            gid = f"g{j}"
            if len(available) < 2 or rng.random() < 0.3:
                op, inputs = "NOT", [rng.choice(available)]
            else:
                op = rng.choice(["AND", "OR"])
                k = rng.randint(2, min(8, len(available)))
                inputs = rng.sample(available, k)
            gates.append((gid, op, inputs))
            available.append(gid)
        top = rng.choice(available)
        states = {e: rng.random() < 0.6 for e in ev_ids}
        costs = {e: rng.randint(1, 20) for e in ev_ids}

        r = post_plan(ev_specs, gates, top, states, costs)
        assert r.status_code == 200, (r.text, ev_specs, gates)
        body = r.json()
        expected = brute_repair(ev_specs, gates, top, states, costs)
        if expected is None:
            assert body == {
                "possible": False, "repair_events": [], "total_cost": None
            }, (body, ev_specs, gates, top, states, costs)
        else:
            assert body == {
                "possible": True,
                "repair_events": expected[0],
                "total_cost": expected[1],
            }, (body, expected, ev_specs, gates, top, states, costs)
