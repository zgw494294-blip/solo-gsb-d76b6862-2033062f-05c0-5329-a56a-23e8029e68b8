"""POST /v1/expand-model：模板递归实例化展开的语义与 422 校验。

展开结果直接提交给 /v1/analyze，与手工展开的故障树及穷举真值表交叉验证。
"""

import itertools
import random
import time
from fractions import Fraction

from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


# ---------- 穷举真值表参考实现 ----------

def brute_top_prob(ev_specs, gates, top):
    """枚举全部事件赋值，返回 top 发生概率（Fraction）。"""
    ev_ids = sorted(ev_specs)
    probs = {e: Fraction(ev_specs[e]) for e in ev_ids}
    gate = {g["id"]: (g["op"], g["inputs"]) for g in gates}

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

    total = Fraction(0)
    for bits in itertools.product((0, 1), repeat=len(ev_ids)):
        state = dict(zip(ev_ids, bits))
        if evaluate(state, top):
            w = Fraction(1)
            for e in ev_ids:
                w *= probs[e] if state[e] else 1 - probs[e]
            total += w
    return total


def f2s(f):
    return f"{f.numerator}/{f.denominator}"


def post(payload):
    return client.post("/v1/expand-model", json=payload)


def pair_templates(ev_specs):
    """基础事件 + Pair(OR 两参数) 与 System（AND 两 Pair 子实例）。"""
    events = [{"id": k, "p": v} for k, v in ev_specs.items()]
    templates = {
        "Pair": {
            "params": ["a", "b"],
            "gates": [{"id": "g", "op": "OR", "inputs": ["a", "b"]}],
            "instances": [],
            "top": "g",
        },
        "System": {
            "params": ["x", "y", "z"],
            "gates": [
                {"id": "all", "op": "AND", "inputs": ["x", "p1", "p2"]}
            ],
            "instances": [
                {"id": "p1", "template": "Pair",
                 "bindings": {"a": "x", "b": "y"}},
                {"id": "p2", "template": "Pair",
                 "bindings": {"a": "y", "b": "z"}},
            ],
            "top": "all",
        },
    }
    root = {
        "template": "System",
        "bindings": {"x": "e1", "y": "e2", "z": "e3"},
    }
    return events, templates, root


# ---------- 基本展开 ----------

def test_basic_expansion():
    events, templates, root = pair_templates(
        {"e1": "1/2", "e2": "1/3", "e3": "1/5"}
    )
    r = post({"events": events, "templates": templates, "root": root})
    assert r.status_code == 200, r.text
    body = r.json()

    # 原 events 原样返回
    assert body["events"] == events
    assert body["top"] == "root/all"
    gate_map = {g["id"]: g for g in body["gates"]}
    # 门路径：root/门名、root/实例/门名
    assert gate_map["root/all"]["op"] == "AND"
    assert gate_map["root/all"]["inputs"] == [
        "e1", "root/p1/g", "root/p2/g"
    ]
    assert gate_map["root/p1/g"]["inputs"] == ["e1", "e2"]
    assert gate_map["root/p2/g"]["inputs"] == ["e2", "e3"]
    # 前序：本模板门在前，子实例门按 instances 顺序在后
    assert [g["id"] for g in body["gates"]] == [
        "root/all", "root/p1/g", "root/p2/g"
    ]

    # 展开结果可直接用于 /v1/analyze，且等价于手工展开
    r2 = client.post("/v1/analyze", json=body)
    assert r2.status_code == 200, r2.text
    # e1 AND (e1 OR e2) AND (e2 OR e3) = e1 AND (e2 OR e3)
    expected = Fraction(1, 2) * (
        Fraction(1, 3) + Fraction(1, 5) - Fraction(1, 15)
    )
    assert r2.json()["top_probability"] == f2s(expected)


def test_top_is_param_resolves_to_event():
    """top 直接引用参数时展开为事件 id，展开后 0 个门。"""
    payload = {
        "events": [{"id": "e1", "p": "1/2"}],
        "templates": {
            "Passthrough": {
                "params": ["x"],
                "gates": [{"id": "g", "op": "NOT", "inputs": ["x"]}],
                "instances": [],
                "top": "x",
            }
        },
        "root": {"template": "Passthrough", "bindings": {"x": "e1"}},
    }
    r = post(payload)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["top"] == "e1"
    # 未被 top 引用的门仍会发射（同 analyze 允许存在未使用门的语义）
    assert body["gates"] == [
        {"id": "root/g", "op": "NOT", "inputs": ["e1"]}
    ]


def test_top_is_instance_output():
    """top 引用子实例时展开为子实例的展开 top。"""
    payload = {
        "events": [{"id": "e1", "p": "1/2"}, {"id": "e2", "p": "1/3"}],
        "templates": {
            "Leaf": {
                "params": ["a", "b"],
                "gates": [{"id": "g", "op": "OR", "inputs": ["a", "b"]}],
                "instances": [],
                "top": "g",
            },
            "Wrap": {
                "params": ["u", "v"],
                "gates": [{"id": "w", "op": "NOT", "inputs": ["u"]}],
                "instances": [
                    {"id": "kid", "template": "Leaf",
                     "bindings": {"a": "u", "b": "v"}}
                ],
                "top": "kid",
            },
        },
        "root": {"template": "Wrap",
                 "bindings": {"u": "e1", "v": "e2"}},
    }
    r = post(payload)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["top"] == "root/kid/g"
    ids = {g["id"] for g in body["gates"]}
    assert ids == {"root/w", "root/kid/g"}

    r2 = client.post("/v1/analyze", json=body)
    assert r2.status_code == 200
    assert r2.json()["top_probability"] == f2s(
        Fraction(1, 3) + Fraction(1, 2) - Fraction(1, 6)
    )


def test_nested_instances():
    """三层嵌套：参数逐层下传，门路径含完整实例链。"""
    payload = {
        "events": [
            {"id": "a", "p": "1/2"},
            {"id": "b", "p": "1/3"},
        ],
        "templates": {
            "L0": {
                "params": ["p"],
                "gates": [{"id": "n", "op": "NOT", "inputs": ["p"]}],
                "instances": [],
                "top": "n",
            },
            "L1": {
                "params": ["q"],
                "gates": [{"id": "g", "op": "OR",
                           "inputs": ["q", "c"]}],
                "instances": [
                    {"id": "c", "template": "L0",
                     "bindings": {"p": "q"}}
                ],
                "top": "g",
            },
            "L2": {
                "params": ["r", "s"],
                "gates": [{"id": "g", "op": "AND",
                           "inputs": ["d", "s"]}],
                "instances": [
                    {"id": "d", "template": "L1",
                     "bindings": {"q": "r"}}
                ],
                "top": "g",
            },
        },
        "root": {"template": "L2",
                 "bindings": {"r": "a", "s": "b"}},
    }
    r = post(payload)
    assert r.status_code == 200, r.text
    body = r.json()
    gate_map = {g["id"]: g for g in body["gates"]}
    assert body["top"] == "root/g"
    assert gate_map["root/g"]["inputs"] == ["root/d/g", "b"]
    assert gate_map["root/d/g"]["inputs"] == ["a", "root/d/c/n"]
    assert gate_map["root/d/c/n"]["inputs"] == ["a"]
    assert [g["id"] for g in body["gates"]] == [
        "root/g", "root/d/g", "root/d/c/n"
    ]

    r2 = client.post("/v1/analyze", json=body)
    assert r2.status_code == 200
    # (a OR NOT a) AND b = b
    assert r2.json()["top_probability"] == "1/3"


def test_shared_events_keep_identity():
    """多个子实例绑定同一事件时共享同一状态（展开后引用同一事件 id）。"""
    payload = {
        "events": [{"id": "e1", "p": "1/2"}],
        "templates": {
            "Not": {
                "params": ["x"],
                "gates": [{"id": "n", "op": "NOT", "inputs": ["x"]}],
                "instances": [],
                "top": "n",
            },
            "Top": {
                "params": ["y"],
                "gates": [{"id": "g", "op": "AND",
                           "inputs": ["i1", "i2"]}],
                "instances": [
                    {"id": "i1", "template": "Not",
                     "bindings": {"x": "y"}},
                    {"id": "i2", "template": "Not",
                     "bindings": {"x": "y"}},
                ],
                "top": "g",
            },
        },
        "root": {"template": "Top", "bindings": {"y": "e1"}},
    }
    r = post(payload)
    assert r.status_code == 200, r.text
    body = r.json()
    gate_map = {g["id"]: g for g in body["gates"]}
    assert gate_map["root/i1/n"]["inputs"] == ["e1"]
    assert gate_map["root/i2/n"]["inputs"] == ["e1"]
    # (NOT e1) AND (NOT e1) = NOT e1
    r2 = client.post("/v1/analyze", json=body)
    assert r2.json()["top_probability"] == "1/2"


def test_unused_params_and_duplicate_bindings():
    """被调模板未使用某参数、多个子参数绑定同一父参数均合法。"""
    payload = {
        "events": [
            {"id": "e1", "p": "1/2"},
            {"id": "e2", "p": "1/3"},
        ],
        "templates": {
            "M": {
                "params": ["a", "b"],
                "gates": [{"id": "g", "op": "NOT", "inputs": ["a"]}],
                "instances": [],
                "top": "g",
            },
            "P": {
                "params": ["z"],
                "gates": [{"id": "g", "op": "NOT", "inputs": ["c"]}],
                "instances": [
                    {"id": "c", "template": "M",
                     "bindings": {"a": "z", "b": "z"}}
                ],
                "top": "g",
            },
        },
        "root": {"template": "P", "bindings": {"z": "e1"}},
    }
    r = post(payload)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["gates"][1]["inputs"] == ["e1"]


def test_random_templates_against_bruteforce():
    """随机生成无环模板树，展开后穷举真值表对照。"""
    rng = random.Random(20260928)
    n_events = 5
    ev_specs = {f"e{i}": f"{rng.randint(1, 5)}/7" for i in range(n_events)}
    events = [{"id": k, "p": v} for k, v in ev_specs.items()]

    # 构造 4 个叶子模板（0~2 参数）与若干上层模板，调用关系严格分层，
    # 天然无递归；参数绑定在父参数中任选，允许多对一。
    templates: dict = {}
    levels = [["T0", "T1", "T2", "T3"]]

    def make_gate_body(name, params, child_outs, rng):
        """返回 (gates, top)，门输入只引用 params/子实例/同门列表中早先的门。"""
        local = list(params) + list(child_outs)
        gates = []
        for j in range(rng.randint(1, 3)):
            gid = f"g{j}"
            pool = local + [g["id"] for g in gates]
            op = rng.choice(["AND", "OR", "NOT"])
            if op == "NOT" or len(pool) < 2:
                inputs = [rng.choice(pool)]
            else:
                k = rng.randint(2, min(4, len(pool)))
                inputs = rng.sample(pool, k)
            gates.append({"id": gid, "op": op, "inputs": inputs})
        top_pool = local + [g["id"] for g in gates]
        return gates, rng.choice(top_pool)

    for i, tname in enumerate(levels[0]):
        n_params = rng.randint(1, 2)
        params = [f"p{j}" for j in range(n_params)]
        gates, top = make_gate_body(tname, params, [], rng)
        templates[tname] = {
            "params": params, "gates": gates,
            "instances": [], "top": top,
        }

    # 两层上层模板
    prev = levels[0]
    for layer in ("M1", "M2"):
        n_params = rng.randint(2, 3)
        params = [f"q{j}" for j in range(n_params)]
        n_children = rng.randint(1, 3)
        child_names = [f"{layer.lower()}{j}" for j in range(n_children)]
        # 允许同一层模板被重复调用（类似共享组件）
        child_templates = [rng.choice(prev) for _ in range(n_children)]
        instances = []
        for cid, ctpl in zip(child_names, child_templates):
            callee_params = templates[ctpl]["params"]
            bindings = {p: rng.choice(params) for p in callee_params}
            instances.append({"id": cid, "template": ctpl,
                              "bindings": bindings})
        gates, top = make_gate_body(layer, params, child_names, rng)
        templates[layer] = {
            "params": params, "gates": gates,
            "instances": instances, "top": top,
        }
        prev = [layer]

    root_tpl = "M2"
    root_params = templates[root_tpl]["params"]
    ev_ids = list(ev_specs)
    root_bindings = {p: rng.choice(ev_ids) for p in root_params}

    payload = {
        "events": events,
        "templates": templates,
        "root": {"template": root_tpl, "bindings": root_bindings},
    }
    r = post(payload)
    assert r.status_code == 200, r.text
    body = r.json()
    assert len(body["gates"]) <= 64

    r2 = client.post("/v1/analyze", json=body)
    assert r2.status_code == 200, r2.text
    expected = brute_top_prob(ev_specs, body["gates"], body["top"])
    assert r2.json()["top_probability"] == f2s(expected)


# ---------- 结构错误（pydantic，422）----------

def test_template_count_bounds():
    # 0 个模板：min_length 拦截
    r = post({
        "events": [{"id": "e1", "p": "1/2"}],
        "templates": {},
        "root": {"template": "X", "bindings": {}},
    })
    assert r.status_code == 422

    # 9 个模板：max_length=8 拦截（在语义校验之前）
    nine = {
        f"T{i}": {
            "params": [],
            "gates": [],
            "instances": [], "top": "g",
        }
        for i in range(9)
    }
    r = post({
        "events": [{"id": "e1", "p": "1/2"}],
        "templates": nine,
        "root": {"template": "T0", "bindings": {}},
    })
    assert r.status_code == 422
    assert "8" in r.json()["detail"]


def test_empty_gates_rejected():
    payload = {
        "events": [{"id": "e1", "p": "1/2"}],
        "templates": {
            "T": {"params": [], "gates": [], "instances": [], "top": "x"},
        },
        "root": {"template": "T", "bindings": {}},
    }
    r = post(payload)
    assert r.status_code == 422


def test_extra_fields_rejected():
    payload = {
        "events": [{"id": "e1", "p": "1/2"}],
        "templates": {
            "T": {
                "params": [],
                "gates": [{"id": "g", "op": "NOT", "inputs": ["e1_"]}],
                "instances": [], "top": "g", "extra": 1,
            }
        },
        "root": {"template": "T", "bindings": {}, "x": 1},
    }
    # 注意：模板门不能引用事件，但 extra 字段应先被 pydantic 拦截
    r = post(payload)
    assert r.status_code == 422


def test_bad_gate_op():
    payload = {
        "events": [{"id": "e1", "p": "1/2"}],
        "templates": {
            "T": {
                "params": ["p"],
                "gates": [{"id": "g", "op": "XOR", "inputs": ["p"]}],
                "instances": [], "top": "g",
            }
        },
        "root": {"template": "T", "bindings": {"p": "e1"}},
    }
    r = post(payload)
    assert r.status_code == 422


# ---------- 命名与互异 ----------

def test_bad_names():
    bad_names = ["1abc", "_abc", "a-b", "a.b", "a b", "a/b", "中文", ""]
    for bad in bad_names:
        payload = {
            "events": [{"id": "e1", "p": "1/2"}],
            "templates": {
                bad: {
                    "params": [],
                    "gates": [{"id": "g", "op": "NOT", "inputs": ["p"]}],
                    "instances": [], "top": "g",
                }
            },
            "root": {"template": bad, "bindings": {}},
        }
        r = post(payload)
        assert r.status_code == 422, bad

    # 参数名 / 门名 / 实例名非法
    payload = {
        "events": [{"id": "e1", "p": "1/2"}],
        "templates": {
            "T": {
                "params": ["1p"],
                "gates": [{"id": "g", "op": "NOT", "inputs": ["1p"]}],
                "instances": [], "top": "g",
            }
        },
        "root": {"template": "T", "bindings": {"1p": "e1"}},
    }
    r = post(payload)
    assert r.status_code == 422 and "字母" in r.json()["detail"]


def test_duplicate_param_names():
    payload = {
        "events": [{"id": "e1", "p": "1/2"}],
        "templates": {
            "T": {
                "params": ["p", "p"],
                "gates": [{"id": "g", "op": "NOT", "inputs": ["p"]}],
                "instances": [], "top": "g",
            }
        },
        "root": {"template": "T", "bindings": {"p": "e1"}},
    }
    r = post(payload)
    assert r.status_code == 422 and "重复" in r.json()["detail"]


def test_duplicate_gate_names():
    payload = {
        "events": [{"id": "e1", "p": "1/2"}],
        "templates": {
            "T": {
                "params": ["p"],
                "gates": [
                    {"id": "g", "op": "NOT", "inputs": ["p"]},
                    {"id": "g", "op": "OR", "inputs": ["p", "p"]},
                ],
                "instances": [], "top": "g",
            }
        },
        "root": {"template": "T", "bindings": {"p": "e1"}},
    }
    r = post(payload)
    assert r.status_code == 422 and "重复" in r.json()["detail"]


def test_duplicate_instance_names():
    payload = {
        "events": [{"id": "e1", "p": "1/2"}],
        "templates": {
            "Leaf": {
                "params": ["x"],
                "gates": [{"id": "g", "op": "NOT", "inputs": ["x"]}],
                "instances": [], "top": "g",
            },
            "T": {
                "params": ["p"],
                "gates": [{"id": "g", "op": "OR",
                           "inputs": ["a", "b"]}],
                "instances": [
                    {"id": "a", "template": "Leaf",
                     "bindings": {"x": "p"}},
                    {"id": "a", "template": "Leaf",
                     "bindings": {"x": "p"}},
                ],
                "top": "g",
            },
        },
        "root": {"template": "T", "bindings": {"p": "e1"}},
    }
    r = post(payload)
    assert r.status_code == 422 and "重复" in r.json()["detail"]


def test_param_gate_instance_names_must_be_distinct():
    def make(clash):
        gate_id = "a" if clash == "gate-inst" else "h0"
        gates = [{"id": gate_id, "op": "NOT", "inputs": ["p"]}]
        if clash == "param-gate":
            gate_id = "a"
            gates = [{"id": gate_id, "op": "NOT", "inputs": ["p"]}]
        top = "h0" if clash == "param-inst" else gate_id
        return {
            "events": [{"id": "e1", "p": "1/2"}],
            "templates": {
                "Leaf": {
                    "params": ["x"],
                    "gates": [{"id": "g", "op": "NOT", "inputs": ["x"]}],
                    "instances": [], "top": "g",
                },
                "T": {
                    "params": ["p"] + (["a"] if clash == "param-gate" else
                                       (["a"] if clash == "param-inst" else [])),
                    "gates": gates,
                    "instances": [
                        {"id": "a", "template": "Leaf",
                         "bindings": {"x": "p"}}
                    ],
                    "top": top,
                },
            },
            "root": {
                "template": "T",
                "bindings": {
                    "p": "e1",
                    **({"a": "e1"} if clash != "gate-inst" else {}),
                },
            },
        }

    for clash in ("param-gate", "gate-inst", "param-inst"):
        payload = make(clash)
        r = post(payload)
        assert r.status_code == 422, clash
        assert "互异" in r.json()["detail"], (clash, r.json()["detail"])


# ---------- 绑定校验 ----------

def test_root_unknown_template():
    payload = {
        "events": [{"id": "e1", "p": "1/2"}],
        "templates": {
            "T": {
                "params": ["p"],
                "gates": [{"id": "g", "op": "NOT", "inputs": ["p"]}],
                "instances": [], "top": "g",
            }
        },
        "root": {"template": "Nope", "bindings": {}},
    }
    r = post(payload)
    assert r.status_code == 422 and "root" in r.json()["detail"]


def test_root_binding_missing_and_extra():
    payload = {
        "events": [{"id": "e1", "p": "1/2"}],
        "templates": {
            "T": {
                "params": ["p"],
                "gates": [{"id": "g", "op": "NOT", "inputs": ["p"]}],
                "instances": [], "top": "g",
            }
        },
        "root": {"template": "T", "bindings": {"q": "e1"}},
    }
    r = post(payload)
    assert r.status_code == 422
    detail = r.json()["detail"]
    assert "缺少" in detail and "多余" in detail or "不属于" in detail


def test_root_binds_unknown_event():
    payload = {
        "events": [{"id": "e1", "p": "1/2"}],
        "templates": {
            "T": {
                "params": ["p"],
                "gates": [{"id": "g", "op": "NOT", "inputs": ["p"]}],
                "instances": [], "top": "g",
            }
        },
        "root": {"template": "T", "bindings": {"p": "nope"}},
    }
    r = post(payload)
    assert r.status_code == 422 and "事件" in r.json()["detail"]


def test_instance_unknown_template():
    payload = {
        "events": [{"id": "e1", "p": "1/2"}],
        "templates": {
            "T": {
                "params": ["p"],
                "gates": [{"id": "g", "op": "NOT", "inputs": ["c"]}],
                "instances": [
                    {"id": "c", "template": "Nope",
                     "bindings": {}}
                ],
                "top": "g",
            }
        },
        "root": {"template": "T", "bindings": {"p": "e1"}},
    }
    r = post(payload)
    assert r.status_code == 422 and "未定义" in r.json()["detail"]


def test_instance_binding_cover():
    payload = {
        "events": [{"id": "e1", "p": "1/2"}],
        "templates": {
            "Leaf": {
                "params": ["a", "b"],
                "gates": [{"id": "g", "op": "OR",
                           "inputs": ["a", "b"]}],
                "instances": [], "top": "g",
            },
            "T": {
                "params": ["p"],
                "gates": [{"id": "g", "op": "NOT", "inputs": ["c"]}],
                "instances": [
                    {"id": "c", "template": "Leaf",
                     "bindings": {"a": "p", "x": "p"}}
                ],
                "top": "g",
            },
        },
        "root": {"template": "T", "bindings": {"p": "e1"}},
    }
    r = post(payload)
    assert r.status_code == 422
    detail = r.json()["detail"]
    assert "缺少" in detail and "不属于" in detail


def test_instance_must_bind_parent_param_only():
    payload = {
        "events": [{"id": "e1", "p": "1/2"}],
        "templates": {
            "Leaf": {
                "params": ["a"],
                "gates": [{"id": "g", "op": "NOT", "inputs": ["a"]}],
                "instances": [], "top": "g",
            },
            "T": {
                "params": ["p"],
                "gates": [{"id": "g", "op": "NOT", "inputs": ["c"]}],
                "instances": [
                    {"id": "c", "template": "Leaf",
                     "bindings": {"a": "e1"}}
                ],
                "top": "g",
            },
        },
        "root": {"template": "T", "bindings": {"p": "e1"}},
    }
    r = post(payload)
    assert r.status_code == 422 and "父模板参数" in r.json()["detail"]


# ---------- 引用、环、递归、门数上限 ----------

def test_gate_unknown_reference():
    payload = {
        "events": [{"id": "e1", "p": "1/2"}],
        "templates": {
            "T": {
                "params": ["p"],
                "gates": [{"id": "g", "op": "OR",
                           "inputs": ["p", "nope"]}],
                "instances": [], "top": "g",
            }
        },
        "root": {"template": "T", "bindings": {"p": "e1"}},
    }
    r = post(payload)
    assert r.status_code == 422 and "未知" in r.json()["detail"]


def test_top_unknown_reference():
    payload = {
        "events": [{"id": "e1", "p": "1/2"}],
        "templates": {
            "T": {
                "params": ["p"],
                "gates": [{"id": "g", "op": "NOT", "inputs": ["p"]}],
                "instances": [], "top": "nope",
            }
        },
        "root": {"template": "T", "bindings": {"p": "e1"}},
    }
    r = post(payload)
    assert r.status_code == 422 and "top" in r.json()["detail"]


def test_gate_cycle_inside_template():
    payload = {
        "events": [{"id": "e1", "p": "1/2"}],
        "templates": {
            "T": {
                "params": ["p"],
                "gates": [
                    {"id": "g1", "op": "OR", "inputs": ["p", "g2"]},
                    {"id": "g2", "op": "OR", "inputs": ["p", "g1"]},
                ],
                "instances": [], "top": "g1",
            }
        },
        "root": {"template": "T", "bindings": {"p": "e1"}},
    }
    r = post(payload)
    assert r.status_code == 422 and "环" in r.json()["detail"]


def test_self_recursive_template():
    payload = {
        "events": [{"id": "e1", "p": "1/2"}],
        "templates": {
            "T": {
                "params": ["p"],
                "gates": [{"id": "g", "op": "OR",
                           "inputs": ["p", "c"]}],
                "instances": [
                    {"id": "c", "template": "T",
                     "bindings": {"p": "p"}}
                ],
                "top": "g",
            }
        },
        "root": {"template": "T", "bindings": {"p": "e1"}},
    }
    r = post(payload)
    assert r.status_code == 422 and "递归" in r.json()["detail"]


def test_mutual_recursive_templates():
    payload = {
        "events": [{"id": "e1", "p": "1/2"}],
        "templates": {
            "A": {
                "params": ["p"],
                "gates": [{"id": "g", "op": "NOT", "inputs": ["c"]}],
                "instances": [
                    {"id": "c", "template": "B",
                     "bindings": {"q": "p"}}
                ],
                "top": "g",
            },
            "B": {
                "params": ["q"],
                "gates": [{"id": "g", "op": "NOT", "inputs": ["d"]}],
                "instances": [
                    {"id": "d", "template": "A",
                     "bindings": {"p": "q"}}
                ],
                "top": "g",
            },
        },
        "root": {"template": "A", "bindings": {"p": "e1"}},
    }
    r = post(payload)
    assert r.status_code == 422 and "递归" in r.json()["detail"]


def test_unreachable_recursive_template_still_rejected():
    """未被 root 触达的模板存在递归也要拒绝（检查全部模板）。"""
    payload = {
        "events": [{"id": "e1", "p": "1/2"}],
        "templates": {
            "Main": {
                "params": ["p"],
                "gates": [{"id": "g", "op": "NOT", "inputs": ["p"]}],
                "instances": [], "top": "g",
            },
            "Loop": {
                "params": ["p"],
                "gates": [{"id": "g", "op": "NOT", "inputs": ["c"]}],
                "instances": [
                    {"id": "c", "template": "Loop",
                     "bindings": {"p": "p"}}
                ],
                "top": "g",
            },
        },
        "root": {"template": "Main", "bindings": {"p": "e1"}},
    }
    r = post(payload)
    assert r.status_code == 422 and "递归" in r.json()["detail"]


def test_bad_arity_in_template():
    payload = {
        "events": [{"id": "e1", "p": "1/2"}],
        "templates": {
            "T": {
                "params": ["p"],
                "gates": [{"id": "g", "op": "NOT",
                           "inputs": ["p", "p"]}],
                "instances": [], "top": "g",
            }
        },
        "root": {"template": "T", "bindings": {"p": "e1"}},
    }
    r = post(payload)
    assert r.status_code == 422 and "NOT" in r.json()["detail"]


def test_expansion_over_64_gates():
    """每个实例发射 2 个门，6 层二叉即 2+4+8+16+32+64=126 > 64。"""
    def tpl(name, child):
        if child is None:
            return {
                "params": ["p"],
                "gates": [
                    {"id": "n", "op": "NOT", "inputs": ["p"]},
                    {"id": "g", "op": "NOT", "inputs": ["n"]},
                ],
                "instances": [], "top": "g",
            }
        return {
            "params": ["p"],
            "gates": [
                {"id": "n", "op": "NOT", "inputs": ["p"]},
                {"id": "g", "op": "AND", "inputs": ["n", "l", "r"]},
            ],
            "instances": [
                {"id": "l", "template": child,
                 "bindings": {"p": "p"}},
                {"id": "r", "template": child,
                 "bindings": {"p": "p"}},
            ],
            "top": "g",
        }

    templates = {}
    child = None
    for i in range(6):
        name = f"L{i}"
        templates[name] = tpl(name, child)
        child = name
    payload = {
        "events": [{"id": "e1", "p": "1/2"}],
        "templates": templates,
        "root": {"template": "L5", "bindings": {"p": "e1"}},
    }
    r = post(payload)
    assert r.status_code == 422
    assert "64" in r.json()["detail"]


def test_expansion_exactly_64_gates_ok():
    """每个模板 8 个 NOT 门 + 一个子实例，8 层恰好 64 门。"""
    templates = {}
    for i in range(8):
        name = f"L{i}"
        if i == 0:
            tpl_def = {
                "params": ["p"],
                "gates": [
                    {"id": f"n{j}", "op": "NOT", "inputs": ["p"]}
                    for j in range(8)
                ],
                "instances": [],
                "top": "n7",
            }
        else:
            tpl_def = {
                "params": ["p"],
                "gates": [
                    {"id": f"n{j}", "op": "NOT", "inputs": ["p"]}
                    for j in range(7)
                ] + [{"id": "n7", "op": "NOT", "inputs": ["c"]}],
                "instances": [
                    {"id": "c", "template": f"L{i-1}",
                     "bindings": {"p": "p"}}
                ],
                "top": "n7",
            }
        templates[name] = tpl_def
    payload = {
        "events": [{"id": "e1", "p": "1/2"}],
        "templates": templates,
        "root": {"template": "L7", "bindings": {"p": "e1"}},
    }
    r = post(payload)
    assert r.status_code == 200, r.text
    assert len(r.json()["gates"]) == 64


def test_dense_8ary_depth8_rejected_without_tree_walk():
    """密集嵌套：8 层、每层 8 个同构子实例、每模板 1 门。

    全树有 1+8+…+8^7=2396745 个实例（门），远超 64；门数判定必须只在
    模板调用图上完成、立即返回 422，不能遍历实例树（修复前需数秒）。
    """
    templates = {}
    for i in range(8):
        if i == 0:
            tpl_def = {
                "params": ["p"],
                "gates": [{"id": "g", "op": "NOT", "inputs": ["p"]}],
                "instances": [],
                "top": "g",
            }
        else:
            tpl_def = {
                "params": ["p"],
                "gates": [{"id": "g", "op": "NOT", "inputs": ["p"]}],
                "instances": [
                    {"id": f"c{j}", "template": f"L{i-1}",
                     "bindings": {"p": "p"}}
                    for j in range(8)
                ],
                "top": "g",
            }
        templates[f"L{i}"] = tpl_def
    payload = {
        "events": [{"id": "e1", "p": "1/2"}],
        "templates": templates,
        "root": {"template": "L7", "bindings": {"p": "e1"}},
    }

    t0 = time.perf_counter()
    r = post(payload)
    elapsed = time.perf_counter() - t0
    assert r.status_code == 422, r.text
    detail = r.json()["detail"]
    assert "64" in detail
    # 不遍历完整实例树：即使实例树有 240 万节点也应瞬时返回
    assert elapsed < 2.0, elapsed


def test_gate_count_via_call_graph_shared_template_memoized():
    """同一叶子模板被实例化多次：门数按调用关系倍数累加。

    Leaf=8 门；Mid 有 7 个自身门 + 2 个 Leaf 实例，共 23 门；
    Root 有 1 个自身门 + 3 个 Mid 实例，共 1 + 3*23 = 70 > 64。
    实际只有 13 个实例，超限完全由同构模板的多次实例化造成。
    """
    payload = {
        "events": [{"id": "e1", "p": "1/2"}],
        "templates": {
            "Leaf": {
                "params": ["p"],
                "gates": [
                    {"id": f"g{j}", "op": "NOT", "inputs": ["p"]}
                    for j in range(8)
                ],
                "instances": [],
                "top": "g7",
            },
            "Mid": {
                "params": ["p"],
                "gates": [
                    {"id": f"m{j}", "op": "NOT", "inputs": ["p"]}
                    for j in range(7)
                ],
                "instances": [
                    {"id": "a", "template": "Leaf",
                     "bindings": {"p": "p"}},
                    {"id": "b", "template": "Leaf",
                     "bindings": {"p": "p"}},
                ],
                "top": "m0",
            },
            "Root": {
                "params": ["p"],
                "gates": [{"id": "r", "op": "NOT", "inputs": ["p"]}],
                "instances": [
                    {"id": f"k{j}", "template": "Mid",
                     "bindings": {"p": "p"}}
                    for j in range(3)
                ],
                "top": "r",
            },
        },
        "root": {"template": "Root", "bindings": {"p": "e1"}},
    }
    r = post(payload)
    assert r.status_code == 422
    assert "64" in r.json()["detail"]


def test_dense_boundary_65_rejected_64_ok():
    """稠密边界：1 个自身门 + n 个 8 门叶子，n=7 得 57、n=8 得 65。

    另构造跨两层恰好 64 门的结构，确认恰好在边界上仍可展开。
    """
    def build(n_kids):
        return {
            "events": [{"id": "e1", "p": "1/2"}],
            "templates": {
                "Leaf": {
                    "params": ["p"],
                    "gates": [
                        {"id": f"g{j}", "op": "NOT", "inputs": ["p"]}
                        for j in range(8)
                    ],
                    "instances": [],
                    "top": "g7",
                },
                "Box": {
                    "params": ["p"],
                    "gates": [{"id": "h", "op": "NOT", "inputs": ["p"]}],
                    "instances": [
                        {"id": f"k{j}", "template": "Leaf",
                         "bindings": {"p": "p"}}
                        for j in range(n_kids)
                    ],
                    "top": "h",
                },
            },
            "root": {"template": "Box", "bindings": {"p": "e1"}},
        }

    # 1 + 8*8 = 65 > 64：超限拒绝
    r_over = post(build(8))
    assert r_over.status_code == 422
    assert "64" in r_over.json()["detail"]

    # 1 + 7*8 = 57 <= 64：正常展开
    r_under = post(build(7))
    assert r_under.status_code == 200, r_under.text
    assert len(r_under.json()["gates"]) == 57

    # 跨两层恰好 64：
    # Mid  = 7 自身门 + 1 个 LeafA(8) + 1 个 LeafB(8) = 23；
    # Root = 2 自身门 + 3 个 Mid(23*3=69) 不对，取 2 个 Mid + 补门：
    # Root = 18 自身门 + 2 个 Mid(46) = 64。
    payload = {
        "events": [{"id": "e1", "p": "1/2"}],
        "templates": {
            "LeafA": {
                "params": ["p"],
                "gates": [
                    {"id": f"a{j}", "op": "NOT", "inputs": ["p"]}
                    for j in range(8)
                ],
                "instances": [],
                "top": "a7",
            },
            "LeafB": {
                "params": ["p"],
                "gates": [
                    {"id": f"b{j}", "op": "NOT", "inputs": ["p"]}
                    for j in range(8)
                ],
                "instances": [],
                "top": "b7",
            },
            "Mid": {
                "params": ["p"],
                "gates": [
                    {"id": f"m{j}", "op": "NOT", "inputs": ["p"]}
                    for j in range(7)
                ],
                "instances": [
                    {"id": "x", "template": "LeafA",
                     "bindings": {"p": "p"}},
                    {"id": "y", "template": "LeafB",
                     "bindings": {"p": "p"}},
                ],
                "top": "m0",
            },
            "Root": {
                "params": ["p"],
                "gates": [
                    {"id": f"r{j}", "op": "NOT", "inputs": ["p"]}
                    for j in range(18)
                ],
                "instances": [
                    {"id": "u", "template": "Mid",
                     "bindings": {"p": "p"}},
                    {"id": "v", "template": "Mid",
                     "bindings": {"p": "p"}},
                ],
                "top": "r0",
            },
        },
        "root": {"template": "Root", "bindings": {"p": "e1"}},
    }
    # 18 + 2*(7 + 8 + 8) = 18 + 46 = 64
    r_exact = post(payload)
    assert r_exact.status_code == 200, r_exact.text
    assert len(r_exact.json()["gates"]) == 64


def test_unreachable_huge_template_ignored_by_limit_but_still_validated():
    """根未触达的模板不参与门数统计，但其静态错误仍被校验。"""
    base_templates = {
        "Main": {
            "params": ["p"],
            "gates": [{"id": "g", "op": "NOT", "inputs": ["p"]}],
            "instances": [],
            "top": "g",
        },
        # 未被 root 触达：即使该模板被实例化会发射远超 64 个门，
        # 也不影响根实例的门数判定。
        "Huge": {
            "params": ["p"],
            "gates": [
                {"id": f"g{j}", "op": "NOT", "inputs": ["p"]}
                for j in range(8)
            ],
            "instances": [
                {"id": f"c{j}", "template": "HugeLeaf",
                 "bindings": {"p": "p"}}
                for j in range(8)
            ],
            "top": "g0",
        },
        "HugeLeaf": {
            "params": ["p"],
            "gates": [
                {"id": f"h{j}", "op": "NOT", "inputs": ["p"]}
                for j in range(8)
            ],
            "instances": [],
            "top": "h7",
        },
    }
    payload = {
        "events": [{"id": "e1", "p": "1/2"}],
        "templates": base_templates,
        "root": {"template": "Main", "bindings": {"p": "e1"}},
    }
    r = post(payload)
    assert r.status_code == 200, r.text
    assert len(r.json()["gates"]) == 1

    # 未触达模板自身有静态错误时仍须拒绝（沿用全模板校验）
    bad = {
        "Main": {
            "params": ["p"],
            "gates": [{"id": "g", "op": "NOT", "inputs": ["p"]}],
            "instances": [],
            "top": "g",
        },
        "Bad": {
            "params": ["p"],
            "gates": [{"id": "g", "op": "NOT",
                       "inputs": ["p", "p"]}],  # NOT 元数错误
            "instances": [],
            "top": "g",
        },
    }
    r2 = post({
        "events": [{"id": "e1", "p": "1/2"}],
        "templates": bad,
        "root": {"template": "Main", "bindings": {"p": "e1"}},
    })
    assert r2.status_code == 422 and "NOT" in r2.json()["detail"]


def test_expanded_gate_id_clashes_with_event():
    """事件 id 恰为展开门路径时由 analyze 门校验兜底拦截。"""
    payload = {
        "events": [
            {"id": "e1", "p": "1/2"},
            {"id": "root/g", "p": "1/3"},
        ],
        "templates": {
            "T": {
                "params": ["p"],
                "gates": [{"id": "g", "op": "NOT", "inputs": ["p"]}],
                "instances": [], "top": "g",
            }
        },
        "root": {"template": "T", "bindings": {"p": "e1"}},
    }
    r = post(payload)
    assert r.status_code == 422 and "冲突" in r.json()["detail"]


def test_bad_event_probability_still_rejected():
    payload = {
        "events": [{"id": "e1", "p": "2/1"}],
        "templates": {
            "T": {
                "params": ["p"],
                "gates": [{"id": "g", "op": "NOT", "inputs": ["p"]}],
                "instances": [], "top": "g",
            }
        },
        "root": {"template": "T", "bindings": {"p": "e1"}},
    }
    r = post(payload)
    assert r.status_code == 422 and "概率" in r.json()["detail"]
