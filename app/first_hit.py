"""首次故障时间分布：仅在未触发（顶事件尚未发生）状态上做概率动态规划。

事件数 n ≤ 8，全部事件状态只有 2^n ≤ 256 种，用一个 bit 表示一个事件
（位序与 ROBDD 变量顺序一致：事件 id 字典序），状态概率以“公分母下的
整数质量”表示：alive[mask] = 该状态的概率分子，basis 为当前公分母，
即 P(mask) = alive[mask] / basis。转移分母逐事件并入公分母，全程只做
整数乘法与加法，直到最终输出才统一经 Fraction 约分，避免传播过程中
反复求 gcd。

顶事件一旦发生即被“吸收”：该状态的质量计入当步 first_hit 后从活跃
分布中删除，不再参与后续转移，故动态规划始终只在未触发状态上进行。
"""

from __future__ import annotations

from fractions import Fraction
from math import lcm
from typing import List, Sequence, Tuple

from .bdd import ROBDD


def top_hit_masks(bdd: ROBDD, top_node: int, n: int) -> int:
    """枚举 2^n 个事件状态，求顶事件发生的状态集合（位掩码）。

    第 mask 位为 1 当且仅当该完整事件赋值下顶事件为真。n ≤ 8 时枚举
    成本可忽略；每个状态沿 ROBDD 自顶向下走一遍即可求值。
    """
    hit = 0
    for mask in range(1 << n):
        node = top_node
        while node >= 2:
            var, low, high = bdd.nodes[node]  # type: ignore[index]
            node = high if (mask >> var) & 1 else low
        if node == 1:
            hit |= 1 << mask
    return hit


def first_failure_distribution(
    init_probs: Sequence[Fraction],
    fail_probs: Sequence[Fraction],
    recover_probs: Sequence[Fraction],
    hit_masks: int,
    steps: int,
) -> Tuple[List[Fraction], Fraction]:
    """求顶事件首次发生时间的精确分布。

    init_probs[i]：t=0 各事件独立生成时事件 i 取 true 的先验 p；
    fail_probs[i] / recover_probs[i]：每步事件 i 的故障 / 恢复概率，
      给定上一步状态后各事件独立转移。
    hit_masks：顶事件发生的完整事件状态集合（见 top_hit_masks）。
    steps：转移步数（1~10）。

    返回 (first_hit, no_hit)：
      first_hit[t] = P(顶事件首次在第 t 步发生)，t=0..steps；
      no_hit = P(直到第 steps 步顶事件始终未发生)。
    """
    n = len(init_probs)
    size = 1 << n

    # ---- t=0：按 p 独立生成初始事件状态 ----
    # 公分母 basis0 = Π b_i（p_i = a_i/b_i），状态 mask 的整数质量为
    # 各事件因子之积：取 true 乘 a_i，取 false 乘 (b_i-a_i)。
    alive = [0] * size
    basis = 1
    for p in init_probs:
        basis *= p.denominator
    init_num = [p.numerator for p in init_probs]
    init_den = [p.denominator for p in init_probs]
    for mask in range(size):
        mass = 1
        for i in range(n):
            if (mask >> i) & 1:
                mass *= init_num[i]
            else:
                mass *= init_den[i] - init_num[i]
        alive[mask] = mass

    # first_hit[t] = (整数质量, 当步公分母)，最终再转约分分数
    hit_terms: List[Tuple[int, int]] = []

    def absorb() -> None:
        """把顶事件发生状态的质量计入当步首次命中，并移出活跃分布。"""
        total = 0
        remaining = hit_masks
        while remaining:
            bit = remaining & -remaining
            idx = bit.bit_length() - 1
            total += alive[idx]
            alive[idx] = 0
            remaining ^= bit
        hit_terms.append((total, basis))

    absorb()  # t=0

    # ---- 逐步转移：每步按事件序逐个施加二态转移核 ----
    # 给定上一步状态后各事件独立转移，逐事件核的复合即联合转移：
    #   上步 false：以 fail 变 true、以 1-fail 保持 false；
    #   上步 true ：以 recover 变 false、以 1-recover 保持 true。
    # 事件 i 的两个分母以 lcm 并入公分母。
    f_num = [p.numerator for p in fail_probs]
    f_den = [p.denominator for p in fail_probs]
    r_num = [p.numerator for p in recover_probs]
    r_den = [p.denominator for p in recover_probs]

    for _ in range(steps):
        for i in range(n):
            # scale=lcm(f_d, r_d) 为新公分母相对旧公分母的倍数。
            # 旧质量 weight 折算到新公分母为 weight·scale；再乘转移概率
            # a/b 时应除以 b，等价于乘 weight·(scale/b)·a。
            scale = lcm(f_den[i], r_den[i])
            fn, fd = f_num[i], f_den[i]
            rn, rd = r_num[i], r_den[i]
            k_fail = scale // fd
            k_rec = scale // rd
            bit = 1 << i
            nxt = [0] * size
            for mask, weight in enumerate(alive):
                if weight == 0:
                    continue
                if mask & bit:
                    # 已故障：保持 true 的概率 1-recover，恢复为 false 的概率 recover
                    nxt[mask] += weight * k_rec * (rd - rn)
                    nxt[mask ^ bit] += weight * k_rec * rn
                else:
                    # 正常：保持 false 的概率 1-fail，故障为 true 的概率 fail
                    nxt[mask] += weight * k_fail * (fd - fn)
                    nxt[mask | bit] += weight * k_fail * fn
            alive = nxt
            basis *= scale
        absorb()

    first_hit = [Fraction(num, den) for num, den in hit_terms]
    no_hit = Fraction(sum(alive), basis)
    return first_hit, no_hit
