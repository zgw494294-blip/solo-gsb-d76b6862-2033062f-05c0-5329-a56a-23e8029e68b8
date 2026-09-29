"""POST /v1/observation-conflicts：最小零概率观测组的语义、精确性与 422 校验。

用穷举真值表（枚举全部事件赋值与观测子集）作为参考实现做交叉验证。
"""

import itertools
import random
from fractions import Fraction

import pytest
from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


# ---------- 穷举真值表参考实现 ----------

def brute_conflicts(ev_specs, gate_specs, observations):
    """枚举全部事件赋值与观测子集，返回最小零概率观测组（id 列表的列表）。

    零概率：组内读数同时成立的先验概率为 0（含 p=0/1、1/1 造成的零概率）；
    最小：删去组内任一项后概率大于 0。返回按 (长度, id 字典序) 排序的组。
    """
    ev_ids = sorted(ev_specs)
    probs = {e: Fraction(ev_specs[e]) for e in ev_ids}
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

    obs_ids = sorted(observations)
    m = len(obs_ids)
    # 每个完整赋值：先验权重 + 各观测是否成立（位掩码）
    weighted = []
    for bits in itertools.product((0, 1), repeat=len(ev_ids)):
        state = dict(zip(ev_ids, (bool(b) for b in bits)))
        weight = Fraction(1)
        for e in ev_ids:
            weight *= probs[e] if state[e] else (Fraction(1) - probs[e])
        sat = 0
        for j, oid in enumerate(obs_ids):
            if evaluate(state, oid) == observations[oid]:
                sat |= 1 << j
        weighted.append((weight, sat))

    # 子集 S 的联合概率：对满足 S 内全部读数的赋值权重求和
    zero = [False] * (1 << m)
    for mask in range(1, 1 << m):
        total = Fraction(0)
        for weight, sat in weighted:
            if weight and mask & ~sat == 0:
                total += weight
        zero[mask] = total == 0

    groups = []
    for mask in range(1, 1 << m):
        if not zero[mask]:
            continue
        # 最小：删去组内任一项后概率大于 0
        if any(zero[mask ^ (1 << i)] for i in range(m) if mask >> i & 1):
            continue
        groups.append([obs_ids[i] for i in range(m) if mask >> i & 1])
    groups.sort(key=lambda ids: (len(ids), ids))
    return groups


def make_body(ev_specs, gate_specs, top, observations):
    return {
        "events": [{"id": k, "p": v} for k, v in ev_specs.items()],
        "gates": [
            {"id": i, "op": op, "inputs": ins}
            for i, op, ins in gate_specs
        ],
        "top": top,
        "observations": observations,
    }


def post(body):
    return client.post("/v1/observation-conflicts", json=body)


# ---------- 基本语义：README 观测冲突示例 ----------

MOTOR_EV = {
    "motor_overheat": "1/100",
    "coolant_loss": "2/100",
    "pump_fail": "1/200",
}
MOTOR_GATES = [
    ("cooling_fail", "OR", ["coolant_loss", "pump_fail"]),
    ("cooling_ok", "NOT", ["cooling_fail"]),
    ("top", "OR", ["motor_overheat", "cooling_fail"]),
]


def test_readme_example():
    # 冷却指示灯正常（cooling_ok=true）但泵已失效：两者矛盾；
    # 电机未过热与任何读数都不冲突
    body = make_body(
        MOTOR_EV, MOTOR_GATES, "top",
        {"cooling_ok": True, "motor_overheat": False, "pump_fail": True},
    )
    r = post(body)
    assert r.status_code == 200, r.text
    assert r.json() == {"conflicts": [["cooling_ok", "pump_fail"]]}
    assert r.json()["conflicts"] == brute_conflicts(
        MOTOR_EV, MOTOR_GATES,
        {"cooling_ok": True, "motor_overheat": False, "pump_fail": True},
    )


def test_all_consistent_returns_empty():
    body = make_body(
        MOTOR_EV, MOTOR_GATES, "top",
        {"cooling_ok": True, "motor_overheat": False},
    )
    r = post(body)
    assert r.status_code == 200, r.text
    assert r.json() == {"conflicts": []}


def test_single_observation_consistent_returns_empty():
    body = make_body({"a": "1/2"}, [], "a", {"a": True})
    r = post(body)
    assert r.status_code == 200
    assert r.json() == {"conflicts": []}


# ---------- 零概率来源：结构矛盾与 p=0/1、1/1 ----------

def test_structural_contradiction_pair():
    # a=true 与 NOT a=true 结构上不可能同时成立
    body = make_body(
        {"a": "1/2"}, [("na", "NOT", ["a"])], "na",
        {"a": True, "na": True},
    )
    r = post(body)
    assert r.status_code == 200, r.text
    assert r.json() == {"conflicts": [["a", "na"]]}


def test_zero_prior_event_read_true_is_singleton_group():
    # 结构可满足（a=1），但 P(a)=0：单独一项即成组
    body = make_body(
        {"a": "0/1", "b": "1/2"}, [], "b",
        {"a": True, "b": True},
    )
    r = post(body)
    assert r.status_code == 200, r.text
    assert r.json() == {"conflicts": [["a"]]}


def test_one_prior_event_read_false_is_singleton_group():
    body = make_body(
        {"a": "1/1", "b": "1/2"}, [], "b",
        {"a": False, "b": False},
    )
    r = post(body)
    assert r.status_code == 200, r.text
    assert r.json() == {"conflicts": [["a"]]}


def test_zero_prior_joint_only():
    # a、b 各自读数概率都为正，但同时成立要求 a=b=1 而 P(b)=0
    body = make_body(
        {"a": "1/2", "b": "0/1"}, [], "a",
        {"a": True, "b": True},
    )
    r = post(body)
    assert r.status_code == 200, r.text
    assert r.json() == {"conflicts": [["b"]]}


def test_zero_prob_only_jointly():
    # g = a AND b，p(a)=1/1：g=false 与 b=true 联合才零概率（各自都不是）
    body = make_body(
        {"a": "1/1", "b": "1/2"},
        [("g", "AND", ["a", "b"])], "g",
        {"g": False, "b": True},
    )
    r = post(body)
    assert r.status_code == 200, r.text
    assert r.json() == {"conflicts": [["b", "g"]]}


# ---------- 最小性与排序 ----------

def test_superset_of_conflict_not_reported():
    # {a, na} 已是最小组，{a, na, b} 零概率但非最小，不应出现
    body = make_body(
        {"a": "1/2", "b": "1/2"},
        [("na", "NOT", ["a"])], "na",
        {"a": True, "na": True, "b": True},
    )
    r = post(body)
    assert r.status_code == 200, r.text
    assert r.json() == {"conflicts": [["a", "na"]]}


def test_multiple_groups_sorted_by_length_then_ids():
    # c 单独成组（p=0 读 true）；{a, na}、{b, nb} 各为结构矛盾对
    ev = {"a": "1/2", "b": "1/2", "c": "0/1"}
    gates = [("na", "NOT", ["a"]), ("nb", "NOT", ["b"])]
    obs = {"a": True, "na": True, "b": True, "nb": True, "c": True}
    body = make_body(ev, gates, "a", obs)
    r = post(body)
    assert r.status_code == 200, r.text
    assert r.json() == {
        "conflicts": [["c"], ["a", "na"], ["b", "nb"]]
    }
    assert r.json()["conflicts"] == brute_conflicts(ev, gates, obs)


def test_three_way_minimal_conflict():
    # g = a AND b：g=false 与 a=true、b=true 三者联合才矛盾，任两项都可
    body = make_body(
        {"a": "1/2", "b": "1/3"},
        [("g", "AND", ["a", "b"])], "g",
        {"g": False, "a": True, "b": True},
    )
    r = post(body)
    assert r.status_code == 200, r.text
    assert r.json() == {"conflicts": [["a", "b", "g"]]}


def test_shared_event_references_conflict():
    # g1 = a AND b 读 true（要求 a=b=1），g2 = a OR b 读 false（要求 a=b=0）
    body = make_body(
        {"a": "1/2", "b": "1/2"},
        [("g1", "AND", ["a", "b"]), ("g2", "OR", ["a", "b"])], "g2",
        {"g1": True, "g2": False},
    )
    r = post(body)
    assert r.status_code == 200, r.text
    assert r.json() == {"conflicts": [["g1", "g2"]]}


def test_observing_top_itself():
    # top = a OR b：top=false 与 a=true 矛盾
    body = make_body(
        {"a": "1/2", "b": "1/2"},
        [("top", "OR", ["a", "b"])], "top",
        {"top": False, "a": True},
    )
    r = post(body)
    assert r.status_code == 200, r.text
    assert r.json() == {"conflicts": [["a", "top"]]}


def test_observed_unreachable_gate_still_built():
    # top 不经过 g，但 g 被观测：g=true 要求 b=true，与 b=false 矛盾
    body = make_body(
        {"a": "1/2", "b": "1/2"},
        [("g", "AND", ["a", "b"])], "a",
        {"g": True, "b": False},
    )
    r = post(body)
    assert r.status_code == 200, r.text
    assert r.json() == {"conflicts": [["b", "g"]]}


def test_sixteen_observations_all_consistent():
    # 16 个独立事件全部读 true：完全合取非零，短路返回空组列
    ev_specs = {f"e{i}": "1/2" for i in range(16)}
    body = make_body(
        ev_specs, [], "e0", {f"e{i}": True for i in range(16)},
    )
    r = post(body)
    assert r.status_code == 200, r.text
    assert r.json() == {"conflicts": []}


def test_sixteen_observations_with_conflict():
    # 14 个一致读数 + 一对矛盾（e0 与 NOT e0 同读 true）
    ev_specs = {f"e{i}": "1/2" for i in range(15)}
    gates = [("nx", "NOT", ["e0"])]
    obs = {f"e{i}": True for i in range(15)}
    obs["nx"] = True
    body = make_body(ev_specs, gates, "e1", obs)
    r = post(body)
    assert r.status_code == 200, r.text
    assert r.json() == {"conflicts": [["e0", "nx"]]}


# ---------- 随机电路穷举对照 ----------

def test_random_circuits_against_truth_table():
    rng = random.Random(20260927)
    for _ in range(40):
        n = rng.randint(1, 8)
        ev_ids = [f"e{i}" for i in range(n)]
        ev_specs = {}
        for e in ev_ids:
            d = rng.randint(1, 8)
            ev_specs[e] = f"{rng.randint(0, d)}/{d}"
        gates = []
        available = list(ev_ids)
        for j in range(rng.randint(0, min(16, 3 * n))):
            gid = f"g{j}"
            if len(available) < 2:
                op = "NOT"
            else:
                op = rng.choice(["AND", "OR", "NOT"])
            if op == "NOT":
                inputs = [rng.choice(available)]
            else:
                k = rng.randint(2, min(8, len(available)))
                inputs = rng.sample(available, k)
            gates.append((gid, op, inputs))
            available.append(gid)
        top = rng.choice(available)

        # 1~8 个观测，键可取自事件或门（门天然无环：只引用之前节点）
        candidates = list(ev_ids) + [g[0] for g in gates]
        k = min(len(candidates), rng.randint(1, 8))
        chosen = rng.sample(candidates, k)
        observations = {oid: rng.choice([True, False]) for oid in chosen}

        body = make_body(ev_specs, gates, top, observations)
        r = post(body)
        assert r.status_code == 200, (r.text, ev_specs, gates, observations)
        expected = brute_conflicts(ev_specs, gates, observations)
        assert r.json()["conflicts"] == expected, (
            ev_specs, gates, observations, r.json(), expected,
        )


# ---------- 422 校验路径 ----------

def test_unknown_observation_key():
    body = make_body({"a": "1/2"}, [], "a", {"a": True, "nope": False})
    r = post(body)
    assert r.status_code == 422
    assert "observations" in r.json()["detail"] and "nope" in r.json()["detail"]


@pytest.mark.parametrize("bad_value", [1, 0, "true", None, 1.0])
def test_non_boolean_observation_value_rejected(bad_value):
    body = make_body({"a": "1/2"}, [], "a", {"a": bad_value})
    r = post(body)
    assert r.status_code == 422


def test_observations_missing():
    r = client.post("/v1/observation-conflicts", json={
        "events": [{"id": "a", "p": "1/2"}], "gates": [], "top": "a"})
    assert r.status_code == 422


def test_empty_observations_rejected():
    body = make_body({"a": "1/2"}, [], "a", {})
    r = post(body)
    assert r.status_code == 422


def test_too_many_observations_rejected():
    ev_specs = {f"e{i}": "1/2" for i in range(17)}
    body = make_body(
        ev_specs, [], "e0",
        {f"e{i}": True for i in range(17)},
    )
    r = post(body)
    assert r.status_code == 422


def test_extra_field_rejected():
    r = client.post("/v1/observation-conflicts", json={
        "events": [{"id": "a", "p": "1/2"}],
        "gates": [], "top": "a",
        "observations": {"a": True},
        "extra": 1})
    assert r.status_code == 422


def test_original_validation_still_applies():
    # 未知 top
    body = make_body({"a": "1/2"}, [], "nope", {"a": True})
    r = post(body)
    assert r.status_code == 422 and "顶层" in r.json()["detail"]
    # 环
    body = make_body(
        {"a": "1/2"},
        [("g", "OR", ["a", "g"])], "g", {"g": True})
    r = post(body)
    assert r.status_code == 422 and "环" in r.json()["detail"]
    # 非法概率
    body = make_body({"a": "3/2"}, [], "a", {"a": True})
    r = post(body)
    assert r.status_code == 422 and "概率" in r.json()["detail"]
    # 非 ASCII id
    body = make_body({"事件": "1/2"}, [], "事件", {"事件": True})
    r = post(body)
    assert r.status_code == 422


def test_group_limit_returns_422(monkeypatch):
    # 两对独立矛盾产生 2 个最小组；把组数上限调小后应返回 422
    import app.conflicts as conflicts_mod
    monkeypatch.setattr(conflicts_mod, "MAX_CONFLICT_GROUPS", 1)
    ev = {"a": "1/2", "b": "1/2"}
    gates = [("na", "NOT", ["a"]), ("nb", "NOT", ["b"])]
    body = make_body(
        ev, gates, "a",
        {"a": True, "na": True, "b": True, "nb": True},
    )
    r = post(body)
    assert r.status_code == 422
    detail = r.json()["detail"]
    assert "观测组" in detail and "超过" in detail


def test_node_limit_in_subset_enumeration(monkeypatch):
    # 树本身节点不多，但子集合取枚举可能新增节点；调小上限后应返回 422
    import app.main as main_mod
    monkeypatch.setattr(main_mod, "MAX_NONTERMINAL_NODES", 5)
    evs = {f"e{i}": "1/2" for i in range(3)}
    gates = [("nx", "NOT", ["e0"])]
    body = make_body(
        evs, gates, "e0",
        {"e0": True, "nx": True, "e1": True, "e2": True},
    )
    r = post(body)
    assert r.status_code == 422
    assert "非终端节点" in r.json()["detail"]
