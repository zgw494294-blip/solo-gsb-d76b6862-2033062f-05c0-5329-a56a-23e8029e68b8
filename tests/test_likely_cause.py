"""POST /v1/likely-cause：最大先验乘积赋值的语义、并列裁决、精确性与 422 校验。

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

def brute_likely(ev_specs, gate_specs, top, observations):
    """枚举全部事件赋值，返回 (P(C), 最优赋值 dict, P(A)/P(C))。

    observations 的键可以是事件或门；门读数按结构求值。最优赋值在满足
    C（顶事件发生且全部读数成立）的完整赋值中使独立先验乘积 P(A)
    最大；并列时按事件 id 字典序比较真假序列、false 优先——
    itertools.product 按 0(false) 在前枚举，严格更大才替换即得。
    P(C)=0 时返回 None。
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

    cond_prob = Fraction(0)
    best_w = None
    best_state = None
    for bits in itertools.product((0, 1), repeat=len(ev_ids)):
        state = dict(zip(ev_ids, (bool(b) for b in bits)))
        if not evaluate(state, top):
            continue
        if any(
            evaluate(state, oid) != value
            for oid, value in observations.items()
        ):
            continue
        weight = Fraction(1)
        for e in ev_ids:
            weight *= probs[e] if state[e] else (Fraction(1) - probs[e])
        cond_prob += weight
        if best_w is None or weight > best_w:
            best_w = weight
            best_state = state

    if cond_prob == 0:
        return None
    return cond_prob, best_state, best_w / cond_prob


def f2s(f):
    return f"{f.numerator}/{f.denominator}"


def make_body(ev_specs, gate_specs, top, observations=None):
    body = {
        "events": [{"id": k, "p": v} for k, v in ev_specs.items()],
        "gates": [
            {"id": i, "op": op, "inputs": ins}
            for i, op, ins in gate_specs
        ],
        "top": top,
    }
    if observations is not None:
        body["observations"] = observations
    return body


def check_against_brute(ev_specs, gate_specs, top, observations=None):
    """对照参考实现检查一次请求的全部字段，返回响应 JSON（P(C)=0 时为 None）。"""
    obs = observations or {}
    body = make_body(ev_specs, gate_specs, top, obs)
    r = client.post("/v1/likely-cause", json=body)
    exp = brute_likely(ev_specs, gate_specs, top, obs)
    if exp is None:
        assert r.status_code == 422, (r.text, body)
        return None
    assert r.status_code == 200, (r.text, body)
    cond_prob, best_state, posterior = exp
    data = r.json()
    assert data["condition_probability"] == f2s(cond_prob)
    assert data["assignment_posterior"] == f2s(posterior)
    assert data["assignment"] == best_state
    # assignment 按事件 id 字典序排列
    assert list(data["assignment"]) == sorted(ev_specs)
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


def test_no_observations_picks_most_likely_failure():
    # 无观测时 C = top；最可能的顶事件成因是单独 coolant_loss
    # （P = 99/100·2/100·199/200 = 19701/1000000），优于单独
    # motor_overheat（9751/1000000）或单独 pump_fail（4851/1000000）
    data = check_against_brute(MOTOR_EV, MOTOR_GATES, "top")
    assert data["assignment"] == {
        "motor_overheat": False,
        "coolant_loss": True,
        "pump_fail": False,
    }
    assert data["condition_probability"] == "34651/1000000"
    assert data["assignment_posterior"] == "19701/34651"


def test_observations_narrow_the_cause():
    # 观测“电机未过热但冷却失效”：最可能成因变为单独 coolant_loss
    obs = {"motor_overheat": False, "cooling_fail": True}
    data = check_against_brute(MOTOR_EV, MOTOR_GATES, "top", obs)
    assert data["assignment"] == {
        "motor_overheat": False,
        "coolant_loss": True,
        "pump_fail": False,
    }
    # P(C) = 99/100 · 249/10000 = 24651/1000000
    assert data["condition_probability"] == "24651/1000000"
    # P(A) = 99/100 · 1/50 · 199/200 = 19701/1000000
    assert data["assignment_posterior"] == "199/249"


def test_empty_observations_allowed():
    body = make_body(MOTOR_EV, MOTOR_GATES, "top", {})
    r = client.post("/v1/likely-cause", json=body)
    assert r.status_code == 200, r.text
    assert r.json()["assignment"]["coolant_loss"] is True


def test_observations_omitted_allowed():
    body = make_body({"a": "1/2"}, [], "a")
    r = client.post("/v1/likely-cause", json=body)
    assert r.status_code == 200, r.text
    assert r.json() == {
        "assignment": {"a": True},
        "condition_probability": "1/2",
        "assignment_posterior": "1/1",
    }


def test_tie_break_prefers_false():
    # top = a OR b，p 均为 1/2：(F,T)、(T,F)、(T,T) 并列 1/4，
    # 按事件 id 字典序比较真假序列，false 优先 => a=false, b=true
    data = check_against_brute(
        {"a": "1/2", "b": "1/2"},
        [("g", "OR", ["a", "b"])],
        "g",
    )
    assert data["assignment"] == {"a": False, "b": True}
    assert data["condition_probability"] == "3/4"
    assert data["assignment_posterior"] == "1/3"


def test_free_variables_take_more_likely_side():
    # b 与顶事件无关：p=1/3 < 1/2 取 false；c 恒真（p=1/1）取 true
    ev = {"a": "3/4", "b": "1/3", "c": "1/1"}
    data = check_against_brute(ev, [], "a")
    assert data["assignment"] == {"a": True, "b": False, "c": True}
    assert data["condition_probability"] == "3/4"
    # P(A) = 3/4 · 2/3 · 1 = 1/2
    assert data["assignment_posterior"] == "2/3"


def test_free_variable_with_half_probability_prefers_false():
    # p=1/2 的自由变量两侧权重相等，false 优先
    data = check_against_brute({"a": "1/4", "b": "1/2"}, [], "a")
    assert data["assignment"] == {"a": True, "b": False}
    # P(A) = 1/4 · 1/2 = 1/8，P(C) = 1/4
    assert data["assignment_posterior"] == "1/2"


def test_not_gate_condition():
    # top = NOT a：满足 C 的唯一赋值是 a=false
    data = check_against_brute({"a": "3/7"}, [("g", "NOT", ["a"])], "g")
    assert data["assignment"] == {"a": False}
    assert data["condition_probability"] == "4/7"
    assert data["assignment_posterior"] == "1/1"


def test_shared_references_single_assignment():
    # (a AND b) OR (a AND c)，观测 g2=false：a=b=true、c=false 成为
    # 唯一可能的顶事件成因之一，共享的 a 只取一个值
    ev = {"a": "1/2", "b": "1/3", "c": "1/5"}
    gates = [
        ("g1", "AND", ["a", "b"]),
        ("g2", "AND", ["a", "c"]),
        ("top", "OR", ["g1", "g2"]),
    ]
    data = check_against_brute(ev, gates, "top", {"g2": False})
    assert data["assignment"] == {"a": True, "b": True, "c": False}
    assert data["condition_probability"] == "2/15"
    assert data["assignment_posterior"] == "1/1"


def test_observed_unreachable_gate_still_built():
    # top 不经过 g，但观测了 g=true：条件传导到 a、b
    data = check_against_brute(
        {"a": "1/2", "b": "1/3"},
        [("g", "AND", ["a", "b"])],
        "a",
        {"g": True},
    )
    assert data["assignment"] == {"a": True, "b": True}
    assert data["condition_probability"] == "1/6"
    assert data["assignment_posterior"] == "1/1"


def test_gate_observation_false():
    # 观测与 top 无关的 OR 门为 false => 两个输入都必须为 false
    data = check_against_brute(
        {"a": "2/3", "b": "3/5", "c": "1/2"},
        [("g", "OR", ["a", "b"])],
        "c",
        {"g": False},
    )
    assert data["assignment"] == {"a": False, "b": False, "c": True}
    assert data["condition_probability"] == "1/15"
    assert data["assignment_posterior"] == "1/1"


def test_constant_true_top():
    # top = a OR NOT a 恒真：C 退化为全自由，取各事件较可能一侧
    data = check_against_brute(
        {"a": "2/3", "b": "1/2"},
        [("na", "NOT", ["a"]), ("top", "OR", ["a", "na"])],
        "top",
    )
    assert data["assignment"] == {"a": True, "b": False}
    assert data["condition_probability"] == "1/1"
    # P(A) = 2/3 · 1/2 = 1/3
    assert data["assignment_posterior"] == "1/3"


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
        candidates = list(ev_ids) + [g[0] for g in gates]
        k = min(len(candidates), rng.randint(0, 16))
        chosen = rng.sample(candidates, k)
        observations = {oid: rng.choice([True, False]) for oid in chosen}

        data = check_against_brute(ev_specs, gates, top, observations)
        if data is None:
            continue
        # 后验必须是合法概率
        num, den = data["assignment_posterior"].split("/")
        assert 0 < int(num) <= int(den)
        checked += 1


# ---------- P(C)=0 ----------

def test_contradictory_observations_return_422():
    # a=true 且 NOT a=true 永远不可同时成立
    body = make_body(
        {"a": "1/2"},
        [("na", "NOT", ["a"])], "na",
        {"a": True, "na": True},
    )
    r = client.post("/v1/likely-cause", json=body)
    assert r.status_code == 422
    detail = r.json()["detail"]
    assert "condition_probability=0" in detail and "矛盾" in detail


def test_structurally_false_top_returns_422():
    # top = a AND NOT a 恒假：顶事件不可能发生
    body = make_body(
        {"a": "1/2"},
        [("na", "NOT", ["a"]), ("top", "AND", ["a", "na"])],
        "top",
    )
    r = client.post("/v1/likely-cause", json=body)
    assert r.status_code == 422
    detail = r.json()["detail"]
    assert "condition_probability=0" in detail and "矛盾" in detail


def test_zero_prior_event_required_true_returns_422():
    # 结构可满足（a=1 使 top 成立），但 P(a)=0
    body = make_body({"a": "0/1"}, [], "a")
    r = client.post("/v1/likely-cause", json=body)
    assert r.status_code == 422
    detail = r.json()["detail"]
    assert "condition_probability=0" in detail and "先验" in detail


def test_zero_prior_event_required_true_by_observation_returns_422():
    body = make_body({"a": "0/1", "b": "1/2"}, [], "b", {"a": True})
    r = client.post("/v1/likely-cause", json=body)
    assert r.status_code == 422
    assert "condition_probability=0" in r.json()["detail"]


# ---------- 422 校验路径 ----------

def test_unknown_observation_key():
    body = make_body({"a": "1/2"}, [], "a", {"a": True, "nope": False})
    r = client.post("/v1/likely-cause", json=body)
    assert r.status_code == 422
    assert "observations" in r.json()["detail"] and "nope" in r.json()["detail"]


@pytest.mark.parametrize("bad_value", [1, 0, "true", None, 1.0])
def test_non_boolean_observation_value_rejected(bad_value):
    body = make_body({"a": "1/2"}, [], "a", {"a": bad_value})
    r = client.post("/v1/likely-cause", json=body)
    assert r.status_code == 422


def test_too_many_observations_rejected():
    ev_specs = {f"e{i}": "1/2" for i in range(17)}
    body = make_body(
        ev_specs, [], "e0",
        {f"e{i}": True for i in range(17)},
    )
    r = client.post("/v1/likely-cause", json=body)
    assert r.status_code == 422


def test_sixteen_observations_allowed():
    ev_specs = {f"e{i}": "1/2" for i in range(16)}
    body = make_body(
        ev_specs, [], "e0",
        {f"e{i}": True for i in range(16)},
    )
    r = client.post("/v1/likely-cause", json=body)
    assert r.status_code == 200, r.text
    data = r.json()
    assert all(v is True for v in data["assignment"].values())
    assert data["assignment_posterior"] == "1/1"


def test_extra_field_rejected():
    r = client.post("/v1/likely-cause", json={
        "events": [{"id": "a", "p": "1/2"}],
        "gates": [], "top": "a",
        "observations": {"a": True},
        "extra": 1})
    assert r.status_code == 422


def test_original_validation_still_applies():
    # 未知 top
    body = make_body({"a": "1/2"}, [], "nope")
    r = client.post("/v1/likely-cause", json=body)
    assert r.status_code == 422 and "顶层" in r.json()["detail"]
    # 环
    body = make_body({"a": "1/2"}, [("g", "OR", ["a", "g"])], "g")
    r = client.post("/v1/likely-cause", json=body)
    assert r.status_code == 422 and "环" in r.json()["detail"]
    # 非法概率
    body = make_body({"a": "3/2"}, [], "a")
    r = client.post("/v1/likely-cause", json=body)
    assert r.status_code == 422 and "概率" in r.json()["detail"]
    # 非 ASCII id
    body = make_body({"事件": "1/2"}, [], "事件")
    r = client.post("/v1/likely-cause", json=body)
    assert r.status_code == 422


def test_node_limit_in_condition_conjunction(monkeypatch):
    # 树本身节点不多，但合取观测约束可能新增节点；调小上限后应返回 422
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
    r = client.post("/v1/likely-cause", json=body)
    assert r.status_code == 422
    assert "非终端节点" in r.json()["detail"]
