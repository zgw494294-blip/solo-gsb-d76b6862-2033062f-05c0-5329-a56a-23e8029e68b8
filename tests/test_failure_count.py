"""POST /v1/failure-count：顶事件条件下取 true 事件数分布的语义、精确性与 422 校验。

用穷举真值表（枚举全部事件状态）作为参考实现做交叉验证。
"""

import itertools
import math
import random
from fractions import Fraction

from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


# ---------- 穷举真值表参考实现 ----------

def brute_failure_count(ev_specs, gate_specs, top):
    """枚举全部事件赋值，返回 (P(T), [P(K=k|T) for k in 0..n])。

    K 为全部已声明事件中取 true 的个数（包括顶层未引用的事件；同一 id
    被多处引用也只计一次）。P(T)=0 时返回 None。
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

    n = len(ev_ids)
    joint = [Fraction(0)] * (n + 1)
    for bits in itertools.product((0, 1), repeat=n):
        state = dict(zip(ev_ids, (bool(b) for b in bits)))
        if not evaluate(state, top):
            continue
        weight = Fraction(1)
        for e in ev_ids:
            weight *= probs[e] if state[e] else (Fraction(1) - probs[e])
        joint[sum(bits)] += weight

    top_prob = sum(joint, Fraction(0))
    if top_prob == 0:
        return None
    return top_prob, [c / top_prob for c in joint]


def f2s(f):
    return f"{f.numerator}/{f.denominator}"


def make_body(ev_specs, gate_specs, top):
    return {
        "events": [{"id": k, "p": v} for k, v in ev_specs.items()],
        "gates": [
            {"id": i, "op": op, "inputs": ins}
            for i, op, ins in gate_specs
        ],
        "top": top,
    }


def check_against_brute(ev_specs, gate_specs, top):
    """对照参考实现检查一次请求的全部字段，返回响应 JSON（P(T)=0 时为 None）。"""
    body = make_body(ev_specs, gate_specs, top)
    r = client.post("/v1/failure-count", json=body)
    exp = brute_failure_count(ev_specs, gate_specs, top)
    if exp is None:
        assert r.status_code == 422, (r.text, body)
        return None
    assert r.status_code == 200, (r.text, body)
    top_prob, dist = exp
    data = r.json()
    assert data["top_probability"] == f2s(top_prob)
    # 覆盖 k=0 到事件总数，逐项与穷举一致
    assert len(data["count_distribution"]) == len(ev_specs) + 1
    assert data["count_distribution"] == [f2s(c) for c in dist]
    # 条件分布之和为 1
    assert sum(Fraction(s) for s in data["count_distribution"]) == 1
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


def test_motor_example_distribution():
    # top = motor_overheat OR coolant_loss OR pump_fail，
    # 顶事件条件下的失效事件数分布（手算联合权重，分母统一为 2_000_000）：
    #   恰好 1 个：68606/2_000_000；恰好 2 个：694/2_000_000；
    #   3 个：2/2_000_000；0 个时顶事件必不发生
    data = check_against_brute(MOTOR_EV, MOTOR_GATES, "top")
    assert data["top_probability"] == "34651/1000000"
    assert data["count_distribution"] == [
        "0/1", "34303/34651", "347/34651", "1/34651",
    ]


def test_unreferenced_events_are_counted():
    # b 不被 top 引用，仍计入 K：top=a 时 K=1 与 K=2 的概率按 b 的先验分配
    data = check_against_brute(
        {"a": "1/2", "b": "1/3"}, [], "a"
    )
    assert data["top_probability"] == "1/2"
    # P(K=1|T)=P(b=0)=2/3；P(K=2|T)=P(b=1)=1/3
    assert data["count_distribution"] == ["0/1", "2/3", "1/3"]


def test_many_unreferenced_events_complete_distribution():
    # 全部 4 个事件均与 top 无关：top=NOT e（恒可发生），K 服从先验独立分布
    ev = {"a": "1/2", "b": "1/3", "c": "2/5", "e": "3/7"}
    gates = [("top", "NOT", ["e"])]
    data = check_against_brute(ev, gates, "top")
    # NOT e 与 e 相关，但 K 含全部 4 个事件；穷举核对即可
    assert len(data["count_distribution"]) == 5
    assert data["top_probability"] == "4/7"


def test_duplicate_reference_counts_once():
    # OR 门重复引用 a：共享同一变量，K 中 a 只计一次
    data = check_against_brute(
        {"a": "1/2", "b": "1/3"},
        [("g", "OR", ["a", "a", "b"])],
        "g",
    )
    assert data["top_probability"] == "2/3"
    assert data["count_distribution"] == ["0/1", "3/4", "1/4"]


def test_not_gate_top():
    # top = NOT a：满足 T 时 a 必为 false
    data = check_against_brute(
        {"a": "3/7"}, [("g", "NOT", ["a"])], "g"
    )
    assert data["top_probability"] == "4/7"
    assert data["count_distribution"] == ["1/1", "0/1"]


def test_constant_true_top_distribution():
    # top = a OR NOT a 恒真：分布退化为 K 的先验分布
    data = check_against_brute(
        {"a": "2/3", "b": "1/2"},
        [("na", "NOT", ["a"]), ("top", "OR", ["a", "na"])],
        "top",
    )
    assert data["top_probability"] == "1/1"
    # P(K=0)=1/3·1/2=1/6；P(K=1)=2/3·1/2+1/3·1/2=1/2；P(K=2)=2/3·1/2=1/3
    assert data["count_distribution"] == ["1/6", "1/2", "1/3"]


def test_single_event_top():
    data = check_against_brute({"a": "1/2"}, [], "a")
    assert data["top_probability"] == "1/2"
    assert data["count_distribution"] == ["0/1", "1/1"]


def test_and_gate_counts_both_events():
    # top = a AND b：条件分布只在 k=2 有质量
    data = check_against_brute(
        {"a": "2/3", "b": "3/5"}, [("g", "AND", ["a", "b"])], "g"
    )
    assert data["top_probability"] == "2/5"
    assert data["count_distribution"] == ["0/1", "0/1", "1/1"]


def test_p_zero_and_p_one_boundaries():
    # a 恒不发生、b 恒发生：top = a OR b 恒由 b 导致，K 恒为 1
    data = check_against_brute(
        {"a": "0/1", "b": "1/1"},
        [("g", "OR", ["a", "b"])], "g",
    )
    assert data["top_probability"] == "1/1"
    assert data["count_distribution"] == ["0/1", "1/1", "0/1"]


def test_reduced_fraction_input():
    # 2/4 与 4/8 均先约分；top = a AND b
    data = check_against_brute(
        {"a": "2/4", "b": "4/8"},
        [("g", "AND", ["a", "b"])], "g",
    )
    assert data["top_probability"] == "1/4"
    assert data["count_distribution"] == ["0/1", "0/1", "1/1"]


def test_thirty_two_events_condition_on_one():
    # 32 个事件、top=e0：其余 31 个全是被约简跳过的自由变量，
    # 不枚举 2^32 种赋值，分布必须精确为 C(31,k-1)/2^31
    ev_specs = {f"e{i:02d}": "1/2" for i in range(32)}
    body = make_body(ev_specs, [], "e00")
    r = client.post("/v1/failure-count", json=body)
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["top_probability"] == "1/2"
    dist = data["count_distribution"]
    assert len(dist) == 33
    total = 2 ** 31
    for k, s in enumerate(dist):
        expected = Fraction(math.comb(31, k - 1), total) if k >= 1 else Fraction(0)
        assert s == f2s(expected), (k, s)


def test_free_variable_after_terminal_reaches_end():
    # top = a（事件顺序 a < z）：z 到达终端后仍是自由变量，两个计数都有质量
    data = check_against_brute(
        {"a": "1/4", "z": "2/3"}, [], "a"
    )
    # P(T,K=1) = 1/4·1/3 = 1/12；P(T,K=2) = 1/4·2/3 = 1/6；P(T)=1/4
    assert data["top_probability"] == "1/4"
    assert data["count_distribution"] == ["0/1", "1/3", "2/3"]


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

        data = check_against_brute(ev_specs, gates, top)
        if data is None:
            continue
        for s in data["count_distribution"]:
            num, den = s.split("/")
            assert 0 <= int(num) <= int(den)
        checked += 1


# ---------- P(T)=0 ----------

def test_structurally_impossible_top_returns_422():
    # top = a AND NOT a 恒假
    body = make_body(
        {"a": "1/2"},
        [("na", "NOT", ["a"]), ("top", "AND", ["a", "na"])],
        "top",
    )
    r = client.post("/v1/failure-count", json=body)
    assert r.status_code == 422
    detail = r.json()["detail"]
    assert "top_probability=0" in detail and "恒为假" in detail


def test_zero_prior_top_returns_422():
    # 结构上 a=true 即可使顶事件发生，但 P(a)=0
    body = make_body({"a": "0/1"}, [], "a")
    r = client.post("/v1/failure-count", json=body)
    assert r.status_code == 422
    detail = r.json()["detail"]
    assert "top_probability=0" in detail and "先验" in detail


def test_zero_prior_through_gate_returns_422():
    # top = a OR b，两者先验都为 0
    body = make_body(
        {"a": "0/1", "b": "0/2"},
        [("g", "OR", ["a", "b"])], "g",
    )
    r = client.post("/v1/failure-count", json=body)
    assert r.status_code == 422
    assert "top_probability=0" in r.json()["detail"]


def test_certain_false_event_with_constant_true_top_still_ok():
    # 顶事件恒真（a OR NOT a），即便 a 先验为 0 也能正常计数
    body = make_body(
        {"a": "0/1", "b": "1/2"},
        [("na", "NOT", ["a"]), ("top", "OR", ["a", "na"])],
        "top",
    )
    r = client.post("/v1/failure-count", json=body)
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["top_probability"] == "1/1"
    # a 恒 false，K 完全由 b 决定
    assert data["count_distribution"] == ["1/2", "1/2", "0/1"]


# ---------- 422 校验路径（沿用 /v1/analyze）----------

def test_unknown_top():
    r = client.post("/v1/failure-count",
                    json=make_body({"a": "1/2"}, [], "nope"))
    assert r.status_code == 422 and "顶层" in r.json()["detail"]


def test_unknown_reference_in_gate():
    body = make_body(
        {"a": "1/2"}, [("g", "AND", ["a", "x"])], "g")
    r = client.post("/v1/failure-count", json=body)
    assert r.status_code == 422 and "未知" in r.json()["detail"]


def test_cycle_rejected():
    body = make_body(
        {"a": "1/2"}, [("g", "OR", ["a", "g"])], "g")
    r = client.post("/v1/failure-count", json=body)
    assert r.status_code == 422 and "环" in r.json()["detail"]


def test_bad_probability_rejected():
    body = make_body({"a": "3/2"}, [], "a")
    r = client.post("/v1/failure-count", json=body)
    assert r.status_code == 422 and "概率" in r.json()["detail"]


def test_extra_field_rejected():
    body = make_body({"a": "1/2"}, [], "a")
    body["extra"] = 1
    r = client.post("/v1/failure-count", json=body)
    assert r.status_code == 422


def test_empty_events_rejected():
    r = client.post("/v1/failure-count",
                    json={"events": [], "gates": [], "top": "x"})
    assert r.status_code == 422


def test_node_limit(monkeypatch):
    # 沿用 50000 非终端节点上限：调小后级联异或构建即超限
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
    r = client.post("/v1/failure-count", json=make_body(evs, gates, prev))
    assert r.status_code == 422
    assert "非终端节点" in r.json()["detail"]
