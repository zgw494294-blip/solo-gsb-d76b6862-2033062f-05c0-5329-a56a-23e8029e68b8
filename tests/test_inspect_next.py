"""POST /v1/inspect-next：候选观测收益评估的语义、精确性与 422 校验。

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

def brute_inspect(ev_specs, gate_specs, top, observations, candidates):
    """枚举全部事件赋值，返回 (P(E), P(top|E), {候选: 分支与收益})。

    observations / candidates 的键可以是事件或门；门按结构求值。
    P(E)=0 时返回 None。
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

    w_e = Fraction(0)
    w_e_top = Fraction(0)
    w_ex = {c: Fraction(0) for c in candidates}
    w_ext = {c: Fraction(0) for c in candidates}
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
        w_e += weight
        top_val = evaluate(state, top)
        if top_val:
            w_e_top += weight
        for c in candidates:
            if evaluate(state, c):
                w_ex[c] += weight
                if top_val:
                    w_ext[c] += weight

    if w_e == 0:
        return None

    def g(q):
        return 2 * q * (1 - q)

    q_e = w_e_top / w_e
    per_cand = {}
    for c in candidates:
        p_true = w_ex[c] / w_e
        p_false = 1 - p_true
        q_true = (w_ext[c] / w_ex[c]) if w_ex[c] > 0 else None
        rest = w_e - w_ex[c]
        q_false = ((w_e_top - w_ext[c]) / rest) if rest > 0 else None
        gain = g(q_e)
        if q_true is not None:
            gain -= p_true * g(q_true)
        if q_false is not None:
            gain -= p_false * g(q_false)
        per_cand[c] = (p_true, q_true, p_false, q_false, gain)
    return w_e, q_e, per_cand


def f2s(f):
    return f"{f.numerator}/{f.denominator}"


def make_body(ev_specs, gate_specs, top, observations, candidates):
    return {
        "events": [{"id": k, "p": v} for k, v in ev_specs.items()],
        "gates": [
            {"id": i, "op": op, "inputs": ins}
            for i, op, ins in gate_specs
        ],
        "top": top,
        "observations": observations,
        "candidates": candidates,
    }


def check_against_brute(ev_specs, gate_specs, top, observations, candidates):
    """对照参考实现检查一次请求的全部字段。"""
    body = make_body(ev_specs, gate_specs, top, observations, candidates)
    r = client.post("/v1/inspect-next", json=body)
    exp = brute_inspect(ev_specs, gate_specs, top, observations, candidates)
    if exp is None:
        assert r.status_code == 422, (r.text, body)
        return None
    assert r.status_code == 200, (r.text, body)
    w_e, q_e, per_cand = exp
    data = r.json()
    assert data["evidence_probability"] == f2s(w_e)
    assert data["top_posterior"] == f2s(q_e)
    # 候选按 id 升序
    assert [c["id"] for c in data["candidates"]] == sorted(set(candidates))
    best_id, best_gain = None, None
    for item in data["candidates"]:
        p_true, q_true, p_false, q_false, gain = per_cand[item["id"]]
        assert item["branches"]["true"]["probability"] == f2s(p_true)
        assert item["branches"]["false"]["probability"] == f2s(p_false)
        assert item["branches"]["true"]["q"] == (
            f2s(q_true) if q_true is not None else None
        )
        assert item["branches"]["false"]["q"] == (
            f2s(q_false) if q_false is not None else None
        )
        assert item["gain"] == f2s(gain)
        assert Fraction(item["gain"]) >= 0  # 信息收益非负
        if best_gain is None or gain > best_gain:
            best_id, best_gain = item["id"], gain
    assert data["recommended_id"] == best_id
    return data


# ---------- 基本语义 ----------

MOTOR_EV = {
    "motor_overheat": "1/100",
    "coolant_loss": "2/100",
    "pump_fail": "1/200",
}
MOTOR_GATES = [
    ("cooling_fail", "OR", ["coolant_loss", "pump_fail"]),
    ("top", "OR", ["motor_overheat", "cooling_fail"]),
]


def test_readme_example():
    # 观测电机未过热后，最有价值的是直接读冷却侧门 cooling_fail
    body = make_body(
        MOTOR_EV, MOTOR_GATES, "top",
        {"motor_overheat": False},
        ["pump_fail", "cooling_fail", "coolant_loss"],
    )
    r = client.post("/v1/inspect-next", json=body)
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["evidence_probability"] == "99/100"
    assert data["top_posterior"] == "249/10000"
    assert [c["id"] for c in data["candidates"]] == [
        "coolant_loss", "cooling_fail", "pump_fail",
    ]
    assert data["candidates"][0] == {
        "id": "coolant_loss",
        "branches": {
            "true": {"probability": "1/50", "q": "1/1"},
            "false": {"probability": "49/50", "q": "1/200"},
        },
        "gain": "1940449/50000000",
    }
    assert data["candidates"][1] == {
        "id": "cooling_fail",
        "branches": {
            "true": {"probability": "249/10000", "q": "1/1"},
            "false": {"probability": "9751/10000", "q": "0/1"},
        },
        "gain": "2427999/50000000",
    }
    assert data["candidates"][2] == {
        "id": "pump_fail",
        "branches": {
            "true": {"probability": "1/200", "q": "1/1"},
            "false": {"probability": "199/200", "q": "1/50"},
        },
        "gain": "477799/50000000",
    }
    assert data["recommended_id"] == "cooling_fail"


def test_empty_observations_allowed():
    # 无观测时 E 为恒真，P(E)=1，分支概率即先验
    body = make_body(
        {"a": "1/2", "b": "1/3"},
        [("top", "OR", ["a", "b"])], "top",
        {}, ["a", "b"],
    )
    r = client.post("/v1/inspect-next", json=body)
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["evidence_probability"] == "1/1"
    assert data["top_posterior"] == "2/3"
    by_id = {c["id"]: c for c in data["candidates"]}
    assert by_id["a"]["gain"] == "2/9"
    assert by_id["b"]["gain"] == "1/9"
    assert data["recommended_id"] == "a"


def test_observations_may_be_omitted():
    body = make_body(
        {"a": "1/2", "b": "1/3"},
        [("top", "OR", ["a", "b"])], "top",
        {}, ["a", "b"],
    )
    del body["observations"]
    r = client.post("/v1/inspect-next", json=body)
    assert r.status_code == 200, r.text
    assert r.json()["evidence_probability"] == "1/1"


def test_zero_probability_branch_q_is_null():
    # p=0 的事件作候选：true 分支概率为 0，q 为 null，收益为 0
    body = make_body(
        {"a": "0/1", "b": "1/2"},
        [("top", "OR", ["a", "b"])], "top",
        {}, ["a", "b"],
    )
    r = client.post("/v1/inspect-next", json=body)
    assert r.status_code == 200, r.text
    by_id = {c["id"]: c for c in r.json()["candidates"]}
    assert by_id["a"]["branches"]["true"] == {
        "probability": "0/1", "q": None,
    }
    assert by_id["a"]["branches"]["false"] == {
        "probability": "1/1", "q": "1/2",
    }
    assert by_id["a"]["gain"] == "0/1"
    assert r.json()["recommended_id"] == "b"


def test_deterministic_candidate_under_evidence():
    # 观测已确定候选取值：一个分支概率为 0，收益为 0
    body = make_body(
        {"a": "1/2", "b": "1/2"},
        [("g", "AND", ["a", "b"])], "g",
        {"a": True}, ["b", "g"],
    )
    r = client.post("/v1/inspect-next", json=body)
    assert r.status_code == 200, r.text
    by_id = {c["id"]: c for c in r.json()["candidates"]}
    # E 下 g 等价于 b：两者收益相同，按 id 最小推荐 b
    assert by_id["b"]["gain"] == by_id["g"]["gain"] == "1/2"
    assert r.json()["recommended_id"] == "b"


def test_tie_break_by_smallest_id():
    # top 与 NOT top 互为确定性取反，收益相同且最大；id 最小者胜出
    body = make_body(
        {"a": "1/2"},
        [("top", "NOT", ["a"])], "top",
        {}, ["top", "a"],
    )
    r = client.post("/v1/inspect-next", json=body)
    assert r.status_code == 200, r.text
    data = r.json()
    by_id = {c["id"]: c for c in data["candidates"]}
    assert by_id["a"]["gain"] == by_id["top"]["gain"] == "1/2"
    assert data["recommended_id"] == "a"


def test_candidate_gate_not_reachable_from_top():
    # top 不引用 g，但 g 与 top 共享事件 a：观测 g 仍能提供 a 的信息
    body = make_body(
        {"a": "1/2", "b": "1/3"},
        [("g", "AND", ["a", "b"])], "a",
        {}, ["g"],
    )
    r = client.post("/v1/inspect-next", json=body)
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["evidence_probability"] == "1/1"
    assert data["top_posterior"] == "1/2"
    assert data["candidates"][0]["branches"]["true"] == {
        "probability": "1/6", "q": "1/1",
    }
    assert data["candidates"][0]["branches"]["false"] == {
        "probability": "5/6", "q": "2/5",
    }
    assert data["candidates"][0]["gain"] == "1/10"
    assert data["recommended_id"] == "g"


def test_fractions_are_reduced():
    body = make_body(
        {"a": "2/4", "b": "4/8"},
        [("g", "OR", ["a", "b"])], "g",
        {}, ["a"],
    )
    r = client.post("/v1/inspect-next", json=body)
    assert r.status_code == 200
    data = r.json()
    assert data["top_posterior"] == "3/4"
    item = data["candidates"][0]
    assert item["branches"]["true"]["probability"] == "1/2"
    assert item["branches"]["false"]["q"] == "1/2"
    assert item["gain"] == "1/8"


# ---------- 随机电路穷举对照 ----------

def test_random_circuits_against_truth_table():
    rng = random.Random(20260928)
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

        # 0~16 个观测，键可取自事件或门
        all_ids = list(ev_ids) + [g[0] for g in gates]
        k = min(len(all_ids), rng.randint(0, 16))
        chosen = rng.sample(all_ids, k)
        observations = {oid: rng.choice([True, False]) for oid in chosen}

        # 1~16 个互异、已声明且未观测的候选
        pool = [i for i in all_ids if i not in observations]
        if not pool:
            continue
        k = min(len(pool), rng.randint(1, 16))
        candidates = rng.sample(pool, k)

        data = check_against_brute(
            ev_specs, gates, top, observations, candidates
        )
        if data is not None:
            checked += 1


# ---------- P(E)=0 ----------

def test_structural_contradiction_returns_422():
    # a=true 且 NOT a=true 永远不可同时成立
    body = make_body(
        {"a": "1/2", "b": "1/3"},
        [("na", "NOT", ["a"])], "na",
        {"a": True, "na": True}, ["b"],
    )
    r = client.post("/v1/inspect-next", json=body)
    assert r.status_code == 422
    detail = r.json()["detail"]
    assert "evidence_probability=0" in detail and "矛盾" in detail


def test_zero_prior_joint_probability_returns_422():
    # 结构可满足（a=1），但 P(a)=0，先验下无正概率支持
    body = make_body(
        {"a": "0/1", "b": "1/2"},
        [("g", "OR", ["a", "b"])], "g",
        {"a": True}, ["b"],
    )
    r = client.post("/v1/inspect-next", json=body)
    assert r.status_code == 422
    detail = r.json()["detail"]
    assert "evidence_probability=0" in detail and "先验" in detail


# ---------- 候选与观测的 422 校验 ----------

def test_empty_candidates_rejected():
    body = make_body({"a": "1/2"}, [], "a", {}, [])
    r = client.post("/v1/inspect-next", json=body)
    assert r.status_code == 422


def test_too_many_candidates_rejected():
    ev_specs = {f"e{i}": "1/2" for i in range(17)}
    body = make_body(ev_specs, [], "e0", {}, [f"e{i}" for i in range(17)])
    r = client.post("/v1/inspect-next", json=body)
    assert r.status_code == 422


def test_sixteen_candidates_allowed():
    ev_specs = {f"e{i}": "1/2" for i in range(16)}
    body = make_body(ev_specs, [], "e0", {}, [f"e{i}" for i in range(16)])
    r = client.post("/v1/inspect-next", json=body)
    assert r.status_code == 200, r.text


def test_duplicate_candidates_rejected():
    body = make_body({"a": "1/2", "b": "1/3"}, [], "a", {}, ["a", "b", "a"])
    r = client.post("/v1/inspect-next", json=body)
    assert r.status_code == 422
    assert "重复" in r.json()["detail"] and "'a'" in r.json()["detail"]


def test_unknown_candidate_rejected():
    body = make_body({"a": "1/2"}, [], "a", {}, ["a", "nope"])
    r = client.post("/v1/inspect-next", json=body)
    assert r.status_code == 422
    assert "candidates" in r.json()["detail"] and "nope" in r.json()["detail"]


def test_observed_candidate_rejected():
    body = make_body(
        {"a": "1/2", "b": "1/3"}, [], "a", {"a": True}, ["a", "b"]
    )
    r = client.post("/v1/inspect-next", json=body)
    assert r.status_code == 422
    assert "观测" in r.json()["detail"] and "'a'" in r.json()["detail"]


def test_multiple_candidate_errors_reported_together():
    body = make_body(
        {"a": "1/2", "b": "1/3"}, [], "a",
        {"a": True}, ["a", "a", "nope"],
    )
    r = client.post("/v1/inspect-next", json=body)
    assert r.status_code == 422
    detail = r.json()["detail"]
    assert "重复" in detail and "nope" in detail and "观测" in detail


def test_too_many_observations_rejected():
    ev_specs = {f"e{i}": "1/2" for i in range(18)}
    body = make_body(
        ev_specs, [], "e0",
        {f"e{i}": True for i in range(17)}, ["e17"],
    )
    r = client.post("/v1/inspect-next", json=body)
    assert r.status_code == 422


def test_unknown_observation_key_rejected():
    body = make_body({"a": "1/2", "b": "1/3"}, [], "a",
                     {"nope": False}, ["b"])
    r = client.post("/v1/inspect-next", json=body)
    assert r.status_code == 422
    assert "observations" in r.json()["detail"]


@pytest.mark.parametrize("bad_value", [1, 0, "true", None, 1.0])
def test_non_boolean_observation_value_rejected(bad_value):
    body = make_body({"a": "1/2", "b": "1/3"}, [], "a",
                     {"a": bad_value}, ["b"])
    r = client.post("/v1/inspect-next", json=body)
    assert r.status_code == 422


def test_candidates_missing_rejected():
    r = client.post("/v1/inspect-next", json={
        "events": [{"id": "a", "p": "1/2"}], "gates": [], "top": "a"})
    assert r.status_code == 422


def test_extra_field_rejected():
    body = make_body({"a": "1/2"}, [], "a", {}, ["a"])
    body["extra"] = 1
    r = client.post("/v1/inspect-next", json=body)
    assert r.status_code == 422


def test_original_validation_still_applies():
    # 未知 top
    body = make_body({"a": "1/2"}, [], "nope", {}, ["a"])
    r = client.post("/v1/inspect-next", json=body)
    assert r.status_code == 422 and "顶层" in r.json()["detail"]
    # 环
    body = make_body(
        {"a": "1/2"},
        [("g", "OR", ["a", "g"])], "g", {}, ["a"])
    r = client.post("/v1/inspect-next", json=body)
    assert r.status_code == 422 and "环" in r.json()["detail"]
    # 非法概率
    body = make_body({"a": "3/2"}, [], "a", {}, ["a"])
    r = client.post("/v1/inspect-next", json=body)
    assert r.status_code == 422 and "概率" in r.json()["detail"]
    # 非 ASCII id
    body = make_body({"事件": "1/2"}, [], "事件", {}, ["事件"])
    r = client.post("/v1/inspect-next", json=body)
    assert r.status_code == 422


def test_node_limit_in_branch_conjunction(monkeypatch):
    # 合取观测约束与候选分支（ITE 展开）可能新增节点；
    # 调小上限后应返回 422
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
    body = make_body(evs, gates, prev, {prev: True}, ["e1"])
    r = client.post("/v1/inspect-next", json=body)
    assert r.status_code == 422
    assert "非终端节点" in r.json()["detail"]
