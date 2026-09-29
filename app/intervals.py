"""概率区间风险判定：在已构建的 ROBDD 上求顶事件概率关于区间事件的多线性多项式。

设请求指定 k≤8 个“区间事件”，其发生概率不再固定，而可在 [low, high]
内独立取值；其余事件保持原先验。顶事件概率 P(top) 是这 k 个区间变量
t_0,…,t_{k-1} 的多线性多项式（事件独立、NOT 门照常，重复引用共享同一
变量）：

    F(t_0, …, t_{k-1}) = Σ_{S ⊆ {0..k-1}} c_S · Π_{i∈S} t_i

多线性函数在超盒 [low_i, high_i]^k 上的最值必在端点组合（每个 t_i 取
low_i 或 high_i）处取得，故只需对 2^k≤256 个端点组合求值，无需枚举
事件状态，也不新增任何 BDD 节点。

多项式按 ROBDD 节点自底向上动态规划：
  固定事件 v（概率 p_v）：poly(n) = (1−p_v)·poly(low) + p_v·poly(high)；
  区间事件 i（符号 t_i）：
      poly(n) = (1−t_i)·poly(low) + t_i·poly(high)
              = poly(low) + t_i·(poly(high) − poly(low))。
BDD 边跳过的变量（含区间事件）天然不出现于子图多项式，无需补因子；
终端 1 为常数 1，终端 0 为零多项式。

多项式以支撑集（出现的区间变量位掩码）上的稠密系数表存储，系数按子集
掩码数值升序排列；k≤8 时每项至多 256 个分数。子节点多项式在被全部父
节点用完后即释放。

端点求值用类 zeta 变换在 O(k·2^k) 次分数运算内完成全部 2^k 个端点；
最值并列时按区间变量下标（事件 id 字典序）升序比较端点向量，low 优先，
即按下标 0 为最高位、0 端（low）优先的字典序取第一个顶点（等价于把
端点位掩码的位序反转后取数值最小者）。
"""

from __future__ import annotations

from fractions import Fraction
from typing import Dict, List, Optional, Tuple

from .bdd import ROBDD

# 多项式：(支撑集位掩码, 按子集掩码升序排列的系数表)
# 支撑集仅保留系数非零的变量位（取最高非零系数的子集掩码），保证系数表
# 长度恰为 2^popcount(support)，且不会出现“support 非空但全零”的情形。
Poly = Tuple[int, List[Fraction]]


def _compact(tables: "_Tables", mask: int, out: List[Fraction]) -> Poly:
    """按非零系数收缩支撑集：support = 最高非零系数的子集掩码。

    系数表 out 以 mask 的子掩码稠密排列（见 _Tables.submasks）。常见情形
    最高项非零（支撑集不变），直接返回；否则才扫描并收缩。
    """
    if out[-1] != 0:
        return mask, out
    subs = tables.submasks(mask)
    support = 0
    for s, c in zip(subs, out):
        if c != 0:
            support = s  # 子集掩码按数值升序，末个非零即最高非零掩码
    if support == 0:
        return 0, ([] if out[0] == 0 else [out[0]])
    return support, [c for s, c in zip(subs, out) if (s & ~support) == 0]


class _Tables:
    """支撑集相关的位掩码索引表，按支撑集记忆化（k≤8，至多 2^k 种）。"""

    def __init__(self, k: int) -> None:
        self._subs: Dict[int, List[int]] = {}
        self._gather: Dict[Tuple[int, int], List[int]] = {}
        self._bit_rank: Dict[Tuple[int, int], int] = {}

    def submasks(self, mask: int) -> List[int]:
        """mask 的全部子掩码，按数值升序（索引与稠密系数表一致）。"""
        got = self._subs.get(mask)
        if got is None:
            got = []
            sub = mask
            while True:
                got.append(sub)
                if sub == 0:
                    break
                sub = (sub - 1) & mask
            got.reverse()
            self._subs[mask] = got
        return got

    def gather(self, union: int, support: int) -> List[int]:
        """support 子掩码序列在 union 稠密系数表中的输出下标序列。

        与 submasks(support) 对齐，供把子系数表散布到输出表时直接枚举，
        免去逐元素的字典查找。
        """
        key = (union, support)
        got = self._gather.get(key)
        if got is None:
            pos = {s: i for i, s in enumerate(self.submasks(union))}
            got = [pos[s] for s in self.submasks(support)]
            self._gather[key] = got
        return got

    def bit_rank(self, union: int, bit: int) -> int:
        """bit 在 union 有序位中的下标：稠密表中翻转该位跨 2^rank 项。"""
        key = (union, bit)
        got = self._bit_rank.get(key)
        if got is None:
            got = (union & (bit - 1)).bit_count()
            self._bit_rank[key] = got
        return got


def _combine_fixed(
    tables: _Tables,
    low_poly: Poly,
    high_poly: Poly,
    p_var: Fraction,
) -> Poly:
    """固定概率事件节点：poly = (1−p)·poly(low) + p·poly(high)。"""
    sl, cl = low_poly
    sh, ch = high_poly
    # 零多项式（空表，仅终端 0）不贡献
    if not cl and not ch:
        return 0, []
    if not cl:
        return _compact(tables, sh, [p_var * c for c in ch])
    if not ch:
        q = Fraction(1) - p_var
        return _compact(tables, sl, [q * c for c in cl])
    if sl == sh:
        # 两子多项式同支撑、同排列：逐系数线性组合
        q = Fraction(1) - p_var
        return sl, [q * a + p_var * b for a, b in zip(cl, ch)]
    union = sl | sh
    out = [Fraction(0)] * (1 << union.bit_count())
    q = Fraction(1) - p_var
    for j, idx in enumerate(tables.gather(union, sl)):
        out[idx] += q * cl[j]
    for j, idx in enumerate(tables.gather(union, sh)):
        out[idx] += p_var * ch[j]
    return _compact(tables, union, out)


def _combine_interval(
    tables: _Tables,
    low_poly: Poly,
    high_poly: Poly,
    bit: int,
) -> Poly:
    """区间事件节点：poly = poly(low) + t_i·(poly(high) − poly(low))。"""
    sl, cl = low_poly
    sh, ch = high_poly
    if not cl and not ch:
        return 0, []
    union = sl | sh | bit
    out = [Fraction(0)] * (1 << union.bit_count())
    # 稠密表按 union 各位在子掩码中的位序排列，bit 的秩决定下标跨度
    flip = 1 << tables.bit_rank(union, bit)
    # (1−t_i)·poly(low)：poly(low) 与 −t_i·poly(low)（low 为终端 0 时跳过）
    if cl:
        for j, idx in enumerate(tables.gather(union, sl)):
            c = cl[j]
            out[idx] += c
            out[idx + flip] -= c
    # t_i·poly(high)（high 为终端 0 时跳过）
    if ch:
        for j, idx in enumerate(tables.gather(union, sh)):
            out[idx + flip] += ch[j]
    return _compact(tables, union, out)


def top_probability_polynomial(
    bdd: ROBDD,
    root: int,
    probs: List[Fraction],
    interval_vars: List[int],
) -> Poly:
    """求顶事件概率关于区间事件（变量下标列表 interval_vars）的多线性多项式。

    probs 为全部事件的原概率（仅固定事件参与常数项加权；区间事件的原
    概率不参与，只用于请求校验）。区间变量个数须在 1~8 之间。
    """
    ivar_of_var: List[int] = [-1] * bdd.n_vars
    for ivar, var in enumerate(interval_vars):
        ivar_of_var[var] = ivar
    tables = _Tables(len(interval_vars))

    zero: Poly = (0, [])
    one: Poly = (0, [Fraction(1)])
    polys: List[Optional[Poly]] = [None] * len(bdd.nodes)
    polys[0] = zero
    polys[1] = one

    # 引用计数只统计从 root 可达的边：建图过程中唯一表里可能留下本次
    # 顶层不可达的节点（门求值顺序早于 root 最终引用），若把它们的出边
    # 也计入 uses，共享子节点会因计数虚高而在真正的父节点用完前不释放，
    # 或被不可达节点的计数干扰。先从 root 标记可达节点（父 id > 子 id）。
    reachable = [False] * len(bdd.nodes)
    stack = [root]
    while stack:
        nid = stack.pop()
        if nid < 2 or reachable[nid]:
            continue
        reachable[nid] = True
        node = bdd.nodes[nid]
        assert node is not None
        stack.extend((node[1], node[2]))

    uses = [0] * len(bdd.nodes)
    for nid in range(2, len(bdd.nodes)):
        if not reachable[nid]:
            continue
        node = bdd.nodes[nid]
        assert node is not None
        uses[node[1]] += 1
        uses[node[2]] += 1
    uses[root] += 1

    for nid in range(2, len(bdd.nodes)):
        if not reachable[nid]:
            continue
        node = bdd.nodes[nid]
        assert node is not None
        var, low, high = node
        low_poly = polys[low]
        high_poly = polys[high]
        assert low_poly is not None and high_poly is not None
        ivar = ivar_of_var[var]
        if ivar < 0:
            polys[nid] = _combine_fixed(
                tables, low_poly, high_poly, probs[var]
            )
        else:
            polys[nid] = _combine_interval(
                tables, low_poly, high_poly, 1 << ivar
            )
        for child in (low, high):
            if child >= 2:
                uses[child] -= 1
                if uses[child] == 0:
                    polys[child] = None

    result = polys[root]
    assert result is not None
    return result


def _endpoint_values(
    poly: Poly, lows: List[Fraction], highs: List[Fraction]
) -> List[Fraction]:
    """求全部 2^k 个端点组合上的 F 值，下标 m 的置位位取 high、其余取 low。

    逐变量代入的类 zeta 变换：处理变量 i 时，把表中 (不含 bit 的 x,
    x|bit) 一对（分别代表 t_i 的系数 B 与常数部分 A，即 F = A + t_i·B）
    替换为 (A + low_i·B, A + high_i·B)；各对互不相交，可原地更新。
    处理完全部出现于支撑集的变量后，表项 s 即 F(顶点 s)；未出现在
    支撑集中的区间变量与 F 无关，最后按 m & support 展开到全部顶点。
    """
    support, coeff = poly
    k = len(lows)
    if not coeff:
        # 零多项式（顶事件结构上恒假）：所有端点取值均为 0
        return [Fraction(0)] * (1 << k)
    subs = [s for s in range(1 << k) if (s & ~support) == 0]
    pos = {s: i for i, s in enumerate(subs)}
    vals = list(coeff)

    for i, lo in enumerate(lows):
        bit = 1 << i
        if not support & bit:
            continue  # F 与该变量无关：顶点取值不随其端点变化
        hi = highs[i]
        for x in subs:
            if x & bit:
                continue
            # 对 (x, x|bit) 同时取旧值后原地替换
            a = vals[pos[x]]
            b = vals[pos[x | bit]]
            vals[pos[x]] = a + lo * b
            vals[pos[x | bit]] = a + hi * b

    full = [Fraction(0)] * (1 << k)
    for m in range(1 << k):
        full[m] = vals[pos[m & support]]
    return full


def probability_interval_extrema(
    bdd: ROBDD,
    root: int,
    probs: List[Fraction],
    interval_vars: List[int],
    lows: List[Fraction],
    highs: List[Fraction],
) -> Tuple[Fraction, int, Fraction, int]:
    """返回 (最小概率, 取最小的端点掩码, 最大概率, 取最大的端点掩码)。

    端点掩码按区间变量下标（事件 id 字典序）排列：置位取 high，否则取
    low。并列时按事件 id 字典序比较端点、low 优先——下标 0 是字典序最
    高位，故按“位反转后的数值”升序枚举顶点，取第一个取得最值的掩码
    （最小与最大都适用同一并列规则）。
    """
    poly = top_probability_polynomial(bdd, root, probs, interval_vars)
    values = _endpoint_values(poly, lows, highs)
    k = len(lows)

    def bit_reverse(m: int) -> int:
        r = 0
        for i in range(k):
            if m >> i & 1:
                r |= 1 << (k - 1 - i)
        return r

    min_mask = max_mask = 0
    min_value = max_value = values[0]
    for m in range(1, len(values)):
        v = values[m]
        if v < min_value or (v == min_value and bit_reverse(m) < bit_reverse(min_mask)):
            min_value, min_mask = v, m
        if v > max_value or (v == max_value and bit_reverse(m) < bit_reverse(max_mask)):
            max_value, max_mask = v, m
    return min_value, min_mask, max_value, max_mask
