"""POST /v1/diagnose：联合观测证据概率与后验概率的语义、精确性与 422 校验。

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

def brute_diagnose(ev_specs, gate_specs, top, observations):
    """枚举全部事件赋值，返回 (P(E), {事件 id: P(事件=1 且 E)/P(E)})。

    observations 的键可以是事件或门；门读数按结构求值，不作为独立
    事件加权。P(E)=0 时返回 (0, None)。
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

    evidence = Fraction(0)
    joint = {e: Fraction(0) for e in ev_ids}
    for bits in itertools.product((0, 1), repeat=len(ev_ids)):
        state = dict(zip(ev_ids, (bool(b) for b in bits)))
        if any(
            evaluate(state, oid) != value
            for oid, value in observations.items()
        ):
            continue
        weight = Fraction(1)
        for e in ev_ids:
            weight *= probs[e] if state[e] else (Fraction(1) - probs[e])
        evidence += weight
        for e in ev_ids:
            if state[e]:
                joint[e] += weight

    if evidence == 0:
        return Fraction(0), None
    return evidence, {e: joint[e] / evidence for e in ev_ids}


def f2s(f):
    return f"{f.numerator}/{f.denominator}"


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


# ---------- 基本语义：README 联合观测示例 ----------

MOTOR_EV = {
    "motor_overheat": "1/100",
    "coolant_loss": "2/100",
    "pump_fail": "1/200",
}
MOTOR_GATES = [
    ("cooling_fail", "OR", ["coolant_loss", "pump_fail"]),
    ("top", "OR", ["motor_overheat", "cooling_fail"]),
]


def test_joint_observation_example():
    # 观测：电机未过热但顶事件发生 => 必为冷却侧故障（explaining-away）
    body = make_body(
        MOTOR_EV, MOTOR_GATES, "top",
        {"motor_overheat": False, "top": True},
    )
    r = client.post("/v1/diagnose", json=body)
    assert r.status_code == 200, r.text
    data = r.json()
    exp_p, exp_post = brute_diagnose(
        MOTOR_EV, MOTOR_GATES, "top",
        {"motor_overheat": False, "top": True},
    )
    assert data["evidence_probability"] == f2s(exp_p)
    assert data["evidence_probability"] == "24651/1000000"
    assert data["posteriors"] == {k: f2s(v) for k, v in exp_post.items()}
    assert data["posteriors"] == {
        "motor_overheat": "0/1",
        "coolant_loss": "200/249",
        "pump_fail": "50/249",
    }


def test_event_only_observations():
    body = make_body(
        MOTOR_EV, MOTOR_GATES, "top",
        {"motor_overheat": False, "coolant_loss": True},
    )
    r = client.post("/v1/diagnose", json=body)
    assert r.status_code == 200, r.text
    exp_p, exp_post = brute_diagnose(
        MOTOR_EV, MOTOR_GATES, "top",
        {"motor_overheat": False, "coolant_loss": True},
    )
    data = r.json()
    assert data["evidence_probability"] == f2s(exp_p)
    assert data["evidence_probability"] == "99/5000"
    assert data["posteriors"] == {k: f2s(v) for k, v in exp_post.items()}
    # 未观测的 pump_fail 保持先验
    assert data["posteriors"]["pump_fail"] == "1/200"


def test_not_gate_observable():
    # top = NOT a：观测 top=true 等价于 a=false
    ev = {"a": "3/7"}
    gates = [("g", "NOT", ["a"])]
    body = make_body(ev, gates, "g", {"g": True})
    r = client.post("/v1/diagnose", json=body)
    assert r.status_code == 200, r.text
    data = r.json()
    exp_p, exp_post = brute_diagnose(ev, gates, "g", {"g": True})
    assert data["evidence_probability"] == f2s(exp_p) == "4/7"
    assert data["posteriors"]["a"] == "0/1"


def test_gate_reading_is_not_independent_event():
    """门与其输入同时观测时按同一结构约束合取，而非两个独立事件。"""
    # cooling_fail=true 且 pump_fail=false => coolant_loss 必为 true
    body = make_body(
        MOTOR_EV, MOTOR_GATES, "top",
        {"cooling_fail": True, "pump_fail": False},
    )
    r = client.post("/v1/diagnose", json=body)
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["posteriors"]["coolant_loss"] == "1/1"
    assert data["posteriors"]["pump_fail"] == "0/1"
    # motor_overheat 与证据无关，保持先验
    assert data["posteriors"]["motor_overheat"] == "1/100"


def test_shared_event_references_share_state():
    # (a AND b) OR (a AND c)：观测两个 AND 门不同时为真……
    # 观测 g1=true 且 g2=false：要求 a=b=1 且 c=0
    ev = {"a": "1/2", "b": "1/3", "c": "1/5"}
    gates = [
        ("g1", "AND", ["a", "b"]),
        ("g2", "AND", ["a", "c"]),
        ("top", "OR", ["g1", "g2"]),
    ]
    body = make_body(ev, gates, "top", {"g1": True, "g2": False})
    r = client.post("/v1/diagnose", json=body)
    assert r.status_code == 200, r.text
    data = r.json()
    exp_p, exp_post = brute_diagnose(ev, gates, "top",
                                     {"g1": True, "g2": False})
    assert data["evidence_probability"] == f2s(exp_p)
    assert data["posteriors"] == {k: f2s(v) for k, v in exp_post.items()}
    assert data["posteriors"]["a"] == "1/1"
    assert data["posteriors"]["b"] == "1/1"
    assert data["posteriors"]["c"] == "0/1"


def test_unobserved_events_keep_prior_when_independent():
    body = make_body(
        {"a": "2/7", "b": "3/11"},
        [("g", "AND", ["a", "b"])], "g",
        {"a": True},
    )
    r = client.post("/v1/diagnose", json=body)
    assert r.status_code == 200
    data = r.json()
    assert data["evidence_probability"] == "2/7"
    assert data["posteriors"]["a"] == "1/1"
    assert data["posteriors"]["b"] == "3/11"


def test_fractions_are_reduced():
    body = make_body(
        {"a": "2/4", "b": "4/8"},
        [("g", "AND", ["a", "b"])], "g",
        {"g": True},
    )
    r = client.post("/v1/diagnose", json=body)
    assert r.status_code == 200
    data = r.json()
    assert data["evidence_probability"] == "1/4"
    assert data["posteriors"] == {"a": "1/1", "b": "1/1"}


# ---------- 随机电路穷举对照 ----------

def test_random_circuits_against_truth_table():
    rng = random.Random(20260927)
    checked = 0
    while checked < 40:
        n = rng.randint(1, 10)
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

        # 1~16 个观测，键可取自事件或门（门天然无环：只引用之前节点）
        candidates = list(ev_ids) + [g[0] for g in gates]
        k = min(len(candidates), rng.randint(1, 16))
        chosen = rng.sample(candidates, k)
        observations = {oid: rng.choice([True, False]) for oid in chosen}

        body = make_body(ev_specs, gates, top, observations)
        r = client.post("/v1/diagnose", json=body)
        exp_p, exp_post = brute_diagnose(ev_specs, gates, top, observations)
        if exp_p == 0:
            assert r.status_code == 422, (r.text, ev_specs, gates, observations)
            continue
        assert r.status_code == 200, (r.text, ev_specs, gates, observations)
        data = r.json()
        assert data["evidence_probability"] == f2s(exp_p)
        assert data["posteriors"] == {
            e: f2s(v) for e, v in exp_post.items()  # type: ignore[union-attr]
        }
        # 后验必须是合法概率
        for s in data["posteriors"].values():
            num, den = s.split("/")
            assert 0 <= int(num) <= int(den)
        checked += 1


# ---------- P(E)=0 ----------

def test_structural_contradiction_returns_422():
    # a=true 且 NOT a=true 永远不可同时成立
    body = make_body(
        {"a": "1/2"},
        [("na", "NOT", ["a"])], "na",
        {"a": True, "na": True},
    )
    r = client.post("/v1/diagnose", json=body)
    assert r.status_code == 422
    detail = r.json()["detail"]
    assert "evidence_probability=0" in detail and "矛盾" in detail


def test_zero_prior_event_observed_true_returns_422():
    # 结构可满足（单事件 a=1），但 P(a)=0
    body = make_body({"a": "0/1"}, [], "a", {"a": True})
    r = client.post("/v1/diagnose", json=body)
    assert r.status_code == 422
    detail = r.json()["detail"]
    assert "evidence_probability=0" in detail and "先验" in detail


def test_zero_prior_event_observed_false_is_fine():
    body = make_body({"a": "0/1"}, [], "a", {"a": False})
    r = client.post("/v1/diagnose", json=body)
    assert r.status_code == 200
    assert r.json() == {"evidence_probability": "1/1",
                        "posteriors": {"a": "0/1"}}


def test_one_prior_event_observed_false_returns_422():
    body = make_body({"a": "1/1"}, [], "a", {"a": False})
    r = client.post("/v1/diagnose", json=body)
    assert r.status_code == 422
    assert "evidence_probability=0" in r.json()["detail"]


# ---------- 422 校验路径 ----------

def test_unknown_observation_key():
    body = make_body({"a": "1/2"}, [], "a", {"a": True, "nope": False})
    r = client.post("/v1/diagnose", json=body)
    assert r.status_code == 422
    assert "observations" in r.json()["detail"] and "nope" in r.json()["detail"]


@pytest.mark.parametrize("bad_value", [1, 0, "true", None, 1.0])
def test_non_boolean_observation_value_rejected(bad_value):
    body = make_body({"a": "1/2"}, [], "a", {"a": bad_value})
    r = client.post("/v1/diagnose", json=body)
    assert r.status_code == 422


def test_observations_missing():
    r = client.post("/v1/diagnose", json={
        "events": [{"id": "a", "p": "1/2"}], "gates": [], "top": "a"})
    assert r.status_code == 422


def test_empty_observations_rejected():
    body = make_body({"a": "1/2"}, [], "a", {})
    r = client.post("/v1/diagnose", json=body)
    assert r.status_code == 422


def test_too_many_observations_rejected():
    ev_specs = {f"e{i}": "1/2" for i in range(17)}
    body = make_body(
        ev_specs, [], "e0",
        {f"e{i}": True for i in range(17)},
    )
    r = client.post("/v1/diagnose", json=body)
    assert r.status_code == 422


def test_sixteen_observations_allowed():
    ev_specs = {f"e{i}": "1/2" for i in range(16)}
    body = make_body(
        ev_specs, [], "e0",
        {f"e{i}": True for i in range(16)},
    )
    r = client.post("/v1/diagnose", json=body)
    assert r.status_code == 200, r.text


def test_extra_field_rejected():
    r = client.post("/v1/diagnose", json={
        "events": [{"id": "a", "p": "1/2"}],
        "gates": [], "top": "a",
        "observations": {"a": True},
        "extra": 1})
    assert r.status_code == 422


def test_original_validation_still_applies():
    # 未知 top
    body = make_body({"a": "1/2"}, [], "nope", {"a": True})
    r = client.post("/v1/diagnose", json=body)
    assert r.status_code == 422 and "顶层" in r.json()["detail"]
    # 环
    body = make_body(
        {"a": "1/2"},
        [("g", "OR", ["a", "g"])], "g", {"g": True})
    r = client.post("/v1/diagnose", json=body)
    assert r.status_code == 422 and "环" in r.json()["detail"]
    # 非法概率
    body = make_body({"a": "3/2"}, [], "a", {"a": True})
    r = client.post("/v1/diagnose", json=body)
    assert r.status_code == 422 and "概率" in r.json()["detail"]
    # 非 ASCII id
    body = make_body({"事件": "1/2"}, [], "事件", {"事件": True})
    r = client.post("/v1/diagnose", json=body)
    assert r.status_code == 422


def test_node_limit_in_evidence_conjunction(monkeypatch):
    # 树本身节点不多，但合取观测约束（NOT + AND 的 ITE 展开）可能
    # 新增节点；调小上限后应返回 422
    import app.main as main_mod
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
    body = make_body(evs, gates, prev, {prev: True})
    r = client.post("/v1/diagnose", json=body)
    assert r.status_code == 422
    assert "非终端节点" in r.json()["detail"]


def test_observed_unreachable_gate_still_built():
    # top 不经过 g，但观测了 g：必须正常求值而非报未知引用
    body = make_body(
        {"a": "1/2", "b": "1/3"},
        [("g", "AND", ["a", "b"])], "a",
        {"g": True},
    )
    r = client.post("/v1/diagnose", json=body)
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["evidence_probability"] == "1/6"
    assert data["posteriors"] == {"a": "1/1", "b": "1/1"}
