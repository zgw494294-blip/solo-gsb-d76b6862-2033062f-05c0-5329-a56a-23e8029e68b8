"""POST /v1/first-failure-time：顶事件首次发生时间分布的语义、精确性与 422 校验。

用独立的 Fraction 状态空间动态规划（在全部状态上传播，不做吸收）作为
参考实现，与“仅在未触发状态上做整数动态规划”的接口结果交叉验证。
"""

import itertools
import random
from fractions import Fraction

import pytest
from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


# ---------- 独立参考实现（Fraction 状态空间动态规划）----------

def brute_first_failure(ev_specs, gate_specs, top_id, steps, fail_rec):
    """在全部 2^n 个状态上做 Fraction 动态规划，返回 ([first_hit t=0..steps], no_hit)。

    fail_rec: {event_id: ("fail n/d", "recover n/d")}。
    """
    ev_ids = sorted(ev_specs)
    n_ev = len(ev_ids)
    p0 = {e: Fraction(ev_specs[e]) for e in ev_ids}
    fail = {e: Fraction(fail_rec[e][0]) for e in ev_ids}
    recover = {e: Fraction(fail_rec[e][1]) for e in ev_ids}
    gates = {g[0]: (g[1], g[2]) for g in gate_specs}

    def evaluate(bits):
        state = dict(zip(ev_ids, (bool(b) for b in bits)))

        def eval_node(node):
            if node in state:
                return state[node]
            op, inputs = gates[node]
            vals = [eval_node(i) for i in inputs]
            if op == "AND":
                return all(vals)
            if op == "OR":
                return any(vals)
            return not vals[0]

        return eval_node(top_id)

    size = 1 << n_ev

    # t=0：各事件按 p 独立生成
    dist = [Fraction(0)] * size
    for bits in itertools.product((0, 1), repeat=n_ev):
        mask = sum(b << i for i, b in enumerate(bits))
        w = Fraction(1)
        for i, b in enumerate(bits):
            w *= p0[ev_ids[i]] if b else (Fraction(1) - p0[ev_ids[i]])
        dist[mask] = w

    # 注意：dist 按下标 mask 索引（第 i 位对应 ev_ids[i]），故 is_hit
    # 也必须以同一 mask 为下标，不能用 product 的枚举序号
    is_hit = [False] * size
    for bits in itertools.product((0, 1), repeat=n_ev):
        mask = sum(b << i for i, b in enumerate(bits))
        is_hit[mask] = evaluate(bits)

    def absorb(d):
        total = Fraction(0)
        for mask in range(size):
            if is_hit[mask]:
                total += d[mask]
                d[mask] = Fraction(0)
        return total

    first_hit = [absorb(dist)]

    # 逐步转移：给定上一步状态后各事件独立转移（与实现无关的完整枚举）
    for _ in range(steps):
        nxt = [Fraction(0)] * size
        for mask, weight in enumerate(dist):
            if weight == 0:
                continue
            # 枚举每个事件的下一步取值，事件间条件独立
            for next_bits in itertools.product((0, 1), repeat=n_ev):
                prob = Fraction(1)
                for i in range(n_ev):
                    was = (mask >> i) & 1
                    now = next_bits[i]
                    if was:
                        q = recover[ev_ids[i]]
                        prob *= q if now == 0 else (Fraction(1) - q)
                    else:
                        q = fail[ev_ids[i]]
                        prob *= q if now == 1 else (Fraction(1) - q)
                nm = sum(b << i for i, b in enumerate(next_bits))
                nxt[nm] += weight * prob
        dist = nxt
        first_hit.append(absorb(dist))

    no_hit = sum(dist, Fraction(0))
    return first_hit, no_hit


def f2s(f):
    return f"{f.numerator}/{f.denominator}"


def make_body(ev_specs, gate_specs, top, steps, fail_rec):
    return {
        "events": [{"id": k, "p": v} for k, v in ev_specs.items()],
        "gates": [
            {"id": i, "op": op, "inputs": ins}
            for i, op, ins in gate_specs
        ],
        "top": top,
        "steps": steps,
        "transitions": {
            e: {"fail": fail_rec[e][0], "recover": fail_rec[e][1]}
            for e in sorted(ev_specs)
        },
    }


def check_against_brute(ev_specs, gate_specs, top, steps, fail_rec):
    """对照参考实现检查一次请求的全部字段，返回响应 JSON。"""
    body = make_body(ev_specs, gate_specs, top, steps, fail_rec)
    r = client.post("/v1/first-failure-time", json=body)
    assert r.status_code == 200, (r.text, body)
    data = r.json()
    exp_hit, exp_no = brute_first_failure(
        ev_specs, gate_specs, top, steps, fail_rec
    )
    assert len(data["first_hit_distribution"]) == steps + 1
    assert data["first_hit_distribution"] == [f2s(v) for v in exp_hit]
    assert data["no_hit_probability"] == f2s(exp_no)
    # 概率质量守恒：Σ first_hit + no_hit = 1
    total = sum(Fraction(s) for s in data["first_hit_distribution"])
    total += Fraction(data["no_hit_probability"])
    assert total == 1
    return data


def random_transition(rng):
    """生成 (fail, recover) 两个 n/d 概率，分母 1~8，含 0 与 1 的边界。"""
    def q():
        d = rng.randint(1, 8)
        return f"{rng.randint(0, d)}/{d}"

    return (q(), q())


# ---------- 手算基本语义 ----------

def test_single_event_no_recovery():
    # top=a, p=1/2, fail=1/2, recover=0：几何分布
    # first_hit(t) = 2^-(t+1)，no_hit(steps) = 2^-(steps+1)
    check_against_brute(
        {"a": "1/2"}, [], "a", 3, {"a": ("1/2", "0/1")}
    )
    body = make_body({"a": "1/2"}, [], "a", 3, {"a": ("1/2", "0/1")})
    data = client.post("/v1/first-failure-time", json=body).json()
    assert data["first_hit_distribution"] == [
        "1/2", "1/4", "1/8", "1/16"
    ]
    assert data["no_hit_probability"] == "1/16"


def test_single_event_with_recovery():
    # p=1/2, fail=1/2, recover=1/3：t=1 时
    # hit = P(初始 false)·fail = 1/2·1/2 = 1/4；
    # 仅 false 存活；t=2 hit = 1/4·1/2 = 1/8，no_hit = 1/8
    body = make_body(
        {"a": "1/2"}, [], "a", 2, {"a": ("1/2", "1/3")}
    )
    r = client.post("/v1/first-failure-time", json=body)
    assert r.status_code == 200
    assert r.json() == {
        "first_hit_distribution": ["1/2", "1/4", "1/8"],
        "no_hit_probability": "1/8",
    }


def test_constant_false_top():
    # a AND NOT a 恒为假：所有时刻 first_hit=0/1，no_hit=1/1
    gates = [
        ("na", "NOT", ["a"]),
        ("g", "AND", ["a", "na"]),
    ]
    body = make_body(
        {"a": "1/2"}, gates, "g", 3, {"a": ("1/2", "1/3")}
    )
    data = client.post("/v1/first-failure-time", json=body).json()
    assert data["first_hit_distribution"] == ["0/1"] * 4
    assert data["no_hit_probability"] == "1/1"


def test_constant_true_top():
    # a OR NOT a 恒为真：t=0 必然首次发生，之后全 0
    gates = [
        ("na", "NOT", ["a"]),
        ("g", "OR", ["a", "na"]),
    ]
    body = make_body(
        {"a": "1/2"}, gates, "g", 2, {"a": ("3/7", "2/5")}
    )
    data = client.post("/v1/first-failure-time", json=body).json()
    assert data["first_hit_distribution"] == ["1/1", "0/1", "0/1"]
    assert data["no_hit_probability"] == "0/1"


def test_unreferenced_event_still_evolves():
    # top=a；声明但未被引用的 b 仍按自身 p/fail/recover 演化，
    # 但不影响顶事件分布（独立先验与独立转移）
    ev = {"a": "1/3", "b": "4/9"}
    fr = {"a": ("1/5", "2/7"), "b": ("6/7", "1/8")}
    data = check_against_brute(ev, [], "a", 4, fr)
    # 与只含 a 的结果一致
    only_a = make_body({"a": "1/3"}, [], "a", 4, {"a": ("1/5", "2/7")})
    data_a = client.post("/v1/first-failure-time", json=only_a).json()
    assert data == data_a


def test_motor_example_steps_two():
    # 与 README 同一棵电机冷却树，steps=2，结果由参考实现复核
    ev = {
        "motor_overheat": "1/100",
        "coolant_loss": "2/100",
        "pump_fail": "1/200",
    }
    gates = [
        ("cooling_fail", "OR", ["coolant_loss", "pump_fail"]),
        ("top", "OR", ["motor_overheat", "cooling_fail"]),
    ]
    fr = {
        "motor_overheat": ("1/1000", "1/4"),
        "coolant_loss": ("3/1000", "1/3"),
        "pump_fail": ("2/1000", "1/2"),
    }
    data = check_against_brute(ev, gates, "top", 2, fr)
    # t=0 的首次命中即静态顶事件概率 34651/1000000
    assert data["first_hit_distribution"][0] == "34651/1000000"


def test_zero_fail_zero_recover_static():
    # fail=recover=0：状态永不改变，first_hit 只可能在 t=0，
    # no_hit = 1 - P(top)
    ev = {"a": "1/2", "b": "1/3"}
    gates = [("g", "OR", ["a", "b"])]
    fr = {"a": ("0/1", "0/1"), "b": ("0/1", "0/1")}
    body = make_body(ev, gates, "g", 5, fr)
    data = client.post("/v1/first-failure-time", json=body).json()
    # P(a∨b) = 1/2+1/3-1/6 = 2/3
    assert data["first_hit_distribution"][0] == "2/3"
    assert data["first_hit_distribution"][1:] == ["0/1"] * 5
    assert data["no_hit_probability"] == "1/3"


def test_certain_fail_eventually_hits_when_top_can_hold():
    # 单事件 fail=1/1、recover=0：未发生时下一步必故障
    body = make_body(
        {"a": "0/1"}, [], "a", 1, {"a": ("1/1", "0/1")}
    )
    data = client.post("/v1/first-failure-time", json=body).json()
    assert data == {
        "first_hit_distribution": ["0/1", "1/1"],
        "no_hit_probability": "0/1",
    }


# ---------- 随机电路交叉验证 ----------

def test_random_small_circuits():
    """随机生成 ≤8 事件、≤12 门、steps 1~10 的故障树，逐例与参考实现对照。"""
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
        for j in range(rng.randint(0, 12)):
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
            available.append(gid)
        top = rng.choice(available)
        steps = rng.randint(1, 10)
        fr = {e: random_transition(rng) for e in ev_ids}
        check_against_brute(ev_specs, gates, top, steps, fr)


def test_extreme_probabilities_and_8_events():
    """8 个事件、p/fail/recover 取 0 与 1 边界时仍精确且守恒。"""
    rng = random.Random(4242)
    for _ in range(6):
        ev_ids = [f"e{i}" for i in range(8)]
        ev_specs = {}
        fr = {}
        for e in ev_ids:
            ev_specs[e] = rng.choice(["0/1", "1/1", "1/2"])
            fr[e] = (
                rng.choice(["0/1", "1/1", "1/3", "2/3"]),
                rng.choice(["0/1", "1/1", "1/4", "3/4"]),
            )
        gates = []
        available = list(ev_ids)
        for j in range(6):
            gid = f"g{j}"
            op = rng.choice(["AND", "OR", "NOT"])
            if op == "NOT":
                inputs = [rng.choice(available)]
            else:
                k = rng.randint(2, min(8, len(available)))
                inputs = rng.sample(available, k)
            gates.append((gid, op, inputs))
            available.append(gid)
        top = rng.choice(available)
        check_against_brute(ev_specs, gates, top, 3, fr)


# ---------- 422 错误路径 ----------

def base_body(**overrides):
    body = make_body(
        {"a": "1/2", "b": "1/3"},
        [("g", "OR", ["a", "b"])],
        "g",
        2,
        {"a": ("1/4", "1/5"), "b": ("2/7", "3/8")},
    )
    body.update(overrides)
    return body


def test_events_limit_8():
    # 9 个事件被拒
    evs = [{"id": f"e{i}", "p": "1/2"} for i in range(9)]
    body = {
        "events": evs,
        "gates": [],
        "top": "e0",
        "steps": 1,
        "transitions": {
            f"e{i}": {"fail": "1/2", "recover": "1/2"} for i in range(9)
        },
    }
    r = client.post("/v1/first-failure-time", json=body)
    assert r.status_code == 422


def test_empty_events_rejected():
    r = client.post("/v1/first-failure-time", json=base_body(events=[]))
    assert r.status_code == 422


@pytest.mark.parametrize("bad_steps", [0, 11, -1, 1.5, True, "2", None])
def test_steps_out_of_range_or_bad_type(bad_steps):
    r = client.post(
        "/v1/first-failure-time", json=base_body(steps=bad_steps)
    )
    assert r.status_code == 422


def test_steps_boundaries_ok():
    for s in (1, 10):
        r = client.post(
            "/v1/first-failure-time", json=base_body(steps=s)
        )
        assert r.status_code == 200, r.text
        assert len(r.json()["first_hit_distribution"]) == s + 1


def test_transitions_missing_item():
    body = base_body()
    del body["transitions"]["b"]
    r = client.post("/v1/first-failure-time", json=body)
    assert r.status_code == 422
    assert "transitions" in r.json()["detail"] and "缺少" in r.json()["detail"]
    assert "'b'" in r.json()["detail"]


def test_transitions_extra_item():
    body = base_body()
    body["transitions"]["x"] = {"fail": "1/2", "recover": "1/2"}
    r = client.post("/v1/first-failure-time", json=body)
    assert r.status_code == 422
    assert "transitions" in r.json()["detail"] and "不属于任何事件" in r.json()["detail"]
    assert "'x'" in r.json()["detail"]


def test_transitions_missing_and_extra_reported_together():
    body = base_body()
    del body["transitions"]["a"]
    body["transitions"]["z"] = {"fail": "1/2", "recover": "1/2"}
    r = client.post("/v1/first-failure-time", json=body)
    assert r.status_code == 422
    detail = r.json()["detail"]
    assert "'a'" in detail and "'z'" in detail


@pytest.mark.parametrize("field", ["fail", "recover"])
@pytest.mark.parametrize(
    "bad", ["1/2/3", "2", "1/0", "3/2", "1/-1", "abc",
            "1/3.0", "+1/2", " 1/2", "1000001/1000001"]
)
def test_transition_bad_probability(field, bad):
    body = base_body()
    body["transitions"]["a"][field] = bad
    r = client.post("/v1/first-failure-time", json=body)
    assert r.status_code == 422
    detail = r.json()["detail"]
    assert "transitions." + field in detail
    assert "概率" in detail


def test_transition_probability_boundary_1e6_ok():
    body = base_body()
    body["transitions"]["a"] = {
        "fail": "999999/1000000",
        "recover": "1000000/1000000",
    }
    r = client.post("/v1/first-failure-time", json=body)
    assert r.status_code == 200, r.text


def test_transition_entry_extra_field_rejected():
    body = base_body()
    body["transitions"]["a"]["other"] = 1
    r = client.post("/v1/first-failure-time", json=body)
    assert r.status_code == 422


def test_transition_entry_missing_field_rejected():
    body = base_body()
    del body["transitions"]["a"]["recover"]
    r = client.post("/v1/first-failure-time", json=body)
    assert r.status_code == 422


def test_transition_value_must_be_strict_string():
    body = base_body()
    body["transitions"]["a"]["fail"] = 0.5
    r = client.post("/v1/first-failure-time", json=body)
    assert r.status_code == 422


def test_request_extra_field_rejected():
    body = base_body()
    body["extra"] = 1
    r = client.post("/v1/first-failure-time", json=body)
    assert r.status_code == 422


def test_inherited_gate_validation():
    # 未知引用、环、非法门元数等沿用 gates/top 校验
    body = make_body(
        {"a": "1/2"},
        [("g", "AND", ["a", "x"])],
        "g",
        1,
        {"a": ("1/2", "1/2")},
    )
    r = client.post("/v1/first-failure-time", json=body)
    assert r.status_code == 422 and "未知" in r.json()["detail"]

    body = make_body(
        {"a": "1/2"},
        [("g", "OR", ["a", "g"])],
        "g",
        1,
        {"a": ("1/2", "1/2")},
    )
    r = client.post("/v1/first-failure-time", json=body)
    assert r.status_code == 422 and "环" in r.json()["detail"]


def test_inherited_bad_initial_probability():
    body = make_body(
        {"a": "2/1"}, [], "a", 1, {"a": ("1/2", "1/2")}
    )
    r = client.post("/v1/first-failure-time", json=body)
    assert r.status_code == 422 and "概率" in r.json()["detail"]


def test_node_limit(monkeypatch):
    # 限额同样为 50000；调小后级联异或链在建图时超限
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
    body = make_body(
        evs, gates, prev, 1,
        {e: ("1/2", "1/2") for e in evs},
    )
    r = client.post("/v1/first-failure-time", json=body)
    assert r.status_code == 422
    assert "非终端节点" in r.json()["detail"]


def test_old_endpoints_still_available():
    # 旧接口保持可用
    r = client.post("/v1/analyze", json={
        "events": [{"id": "a", "p": "1/2"}],
        "gates": [], "top": "a",
    })
    assert r.status_code == 200
    assert r.json()["top_probability"] == "1/2"
    assert client.get("/health").status_code == 200
