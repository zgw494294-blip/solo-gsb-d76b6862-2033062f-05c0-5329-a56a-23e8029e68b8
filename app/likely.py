"""最可能成因赋值：在已构建的 ROBDD 上求满足条件的最大先验乘积赋值。

条件 C（顶事件发生且全部观测读数成立）已合取为同一 ROBDD 的根。
在满足 C 的完整事件赋值 A 中，最大化独立先验乘积

    P(A) = Π_{A[i]=true} p_i · Π_{A[i]=false} (1 − p_i)

并列时按事件 id 字典序比较真假序列，false 优先（即序列字典序更小者胜）。

BDD 路径上跳过的变量（以及完全不出现在条件中的事件）对 C 是否成立
无影响，各自独立地取权重较大的一侧：p_i > 1/2 取 true，p_i < 1/2 取
false，p_i = 1/2 时两侧权重相等，按 false 优先取 false。

变量顺序即事件 id 字典序：自底向上求每个节点子图取 1 的最大权重
（含路径跳过的自由变量权重）后，从根开始逐节点在并列时优先低分支
（false），即得真假序列字典序最小的最优赋值，无需枚举全部状态。
子节点 id 总是小于父节点 id，按 id 递增即为后序遍历。
"""

from __future__ import annotations

from fractions import Fraction
from typing import List, Optional, Tuple

from .bdd import ROBDD


def most_likely_assignment(
    bdd: ROBDD, root: int, probs: List[Fraction]
) -> Tuple[Fraction, List[bool]]:
    """求使 root 取值为 1 的最大先验乘积完整赋值。

    返回 (P(A), 按变量下标排列的布尔赋值)。调用前需保证 root 不是
    终端 0（P(C)=0 的情形已由调用方拦截并返回 422）。
    """
    n = bdd.n_vars
    # 自由变量：取权重较大的一侧，p = 1/2 并列时 false 优先
    free_w = [max(p, 1 - p) for p in probs]
    free_v = [p > (1 - p) for p in probs]
    # 前缀乘积 pre[i] = Π_{j<i} free_w[j]；下标区间 (a, b) 内被跳过
    # 变量的自由因子为 pre[b] / pre[a+1]
    pre = [Fraction(1)]
    for w in free_w:
        pre.append(pre[-1] * w)

    def var_of(nid: int) -> int:
        return bdd.nodes[nid][0] if nid >= 2 else n  # type: ignore[index]

    # 自底向上：best[nid] 为子图取 1 的最大权重（含区间内自由变量），
    # 终端 0 不可行记 None；take_low[nid] 记录并列时 false 优先的分支
    best: List[Optional[Fraction]] = [None, Fraction(1)] + [None] * (
        len(bdd.nodes) - 2
    )
    take_low = [False] * len(bdd.nodes)

    for nid in range(2, len(bdd.nodes)):
        node = bdd.nodes[nid]
        assert node is not None
        var, low, high = node
        w_low: Optional[Fraction] = None
        if best[low] is not None:
            w_low = (
                (1 - probs[var])
                * pre[var_of(low)]
                / pre[var + 1]
                * best[low]
            )
        w_high: Optional[Fraction] = None
        if best[high] is not None:
            w_high = (
                probs[var] * pre[var_of(high)] / pre[var + 1] * best[high]
            )
        if w_low is None:
            best[nid] = w_high
        elif w_high is None or w_low >= w_high:
            # 低分支可行且不比高分支差：false 优先
            best[nid] = w_low
            take_low[nid] = True
        else:
            best[nid] = w_high

    # 根之前的变量全部自由；best[root] 必非 None（root 非终端 0）
    total = pre[var_of(root)] * best[root]  # type: ignore[operator]

    # 自顶向下沿记录的分支走出赋值；跳过的变量保持自由取值
    assignment = list(free_v)
    nid = root
    while nid >= 2:
        node = bdd.nodes[nid]
        assert node is not None
        var, low, high = node
        if take_low[nid]:
            assignment[var] = False
            nid = low
        else:
            assignment[var] = True
            nid = high
    return total, assignment
