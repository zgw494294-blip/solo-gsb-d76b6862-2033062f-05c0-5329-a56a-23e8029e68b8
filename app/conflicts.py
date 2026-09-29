"""最小零概率观测组枚举：在共享 ROBDD 上按观测子集精确判定，不枚举事件赋值。

每个观测（已声明事件或门 id → 严格布尔读数）对应同一 ROBDD 上的一个
约束节点：读 true 取该节点本身，读 false 取其 BDD 取反。一组观测“同时
成立”即各约束节点的合取，其先验概率为 0 有两种来源：
  - 结构上不可能同时成立（合取恒为假，如同时读 a=true 与 NOT a=true）；
  - 结构可满足，但每个满足赋值都把某个 p=0/1 或 p=1/1 的事件取在零概率
    一侧（如把 p=0/1 的事件读成 true）。

两种来源统一折叠为结构判定：令 G 为正概率赋值子空间的合取（p=1 的事件
取 x、p=0 的事件取 ¬x），完整赋值 A 的概率大于 0 当且仅当 A 满足 G，故
P(∧S)=0 ⟺ (∧S)∧G ≡ 0。把 G 并入每个约束（ĉ_i = c_i ∧ G；合取幂等，
子集合取 ∧ĉ_i = (∧S)∧G），零概率组即“折叠后约束的合取为终端 0”的组。

“合取为假”关于组的包含关系单调（S ⊆ T 时 ∧T ⇒ ∧S），全部最小零概率组
构成反链。观测至多 16 个，按位掩码数值升序枚举非空子集：mask 的真子集
数值必小于 mask，处理 mask 时其全部真子集的判定已完成；若 mask 删去任
一元素后已知零概率，则 mask 也零概率但非最小，直接跳过（不再建节点）。
否则由删去最低位的前驱合取节点经一次 and_ 得到本子集合取（前驱未被跳
过、其节点必已就绪），为终端 0 即找到一个最小组。

先合取全部约束做一次短路：完全合取非零（全部读数可同时成立）时由单调
性不存在任何零概率组，直接返回空组列，不为无冲突请求展开子集合取。
"""

from __future__ import annotations

from fractions import Fraction
from typing import List

from .bdd import ROBDD

# 允许返回的最小零概率观测组数上限
MAX_CONFLICT_GROUPS = 5000


class ConflictGroupLimitExceeded(ValueError):
    """最小零概率观测组数超过上限。"""


def minimal_zero_prob_groups(
    bdd: ROBDD, constraints: List[int], probs: List[Fraction]
) -> List[int]:
    """返回全部最小零概率观测组（观测下标的位掩码列表，顺序未排序）。

    constraints[i] 为第 i 个观测在共享 ROBDD 上的约束节点（读 false 的
    观测已取反）；probs 按变量下标给出各事件先验。零概率判定通过折叠
    p∈{0,1} 事件的强制文字化为结构判定（见模块说明）。组数超过
    MAX_CONFLICT_GROUPS 时抛 ConflictGroupLimitExceeded；合取构建累计
    非终端节点超限由 ROBDD.mk 抛 NodeLimitExceeded。
    """
    m = len(constraints)

    # G：正概率赋值子空间，p=1 强制 x、p=0 强制 ¬x（均为单个文字）
    g = 1
    for var, p in enumerate(probs):
        if p == 1:
            g = bdd.and_(g, bdd.mk(var, 0, 1))
        elif p == 0:
            g = bdd.and_(g, bdd.mk(var, 1, 0))

    # 折叠：ĉ_i = c_i ∧ G；子集合取 ∧ĉ_i = (∧S)∧G（G 幂等）
    folded = [bdd.and_(c, g) for c in constraints]

    # 短路：全部读数可同时成立（完全合取非零）时不存在任何零概率组
    full = 1
    for c in folded:
        full = bdd.and_(full, c)
        if full == 0:
            break
    if full != 0:
        return []

    # 自底向上枚举非空子集。nodes[mask] 为对应合取节点，仅处理过的
    # mask 有效：mask 被处理时其任一删单元素前驱都未被跳过（否则 mask
    # 已因前驱已知零概率而跳过），故 nodes[mask ^ lowbit] 必已就绪。
    nodes = [0] * (1 << m)
    nodes[0] = 1
    known_zero = bytearray(1 << m)
    groups: List[int] = []
    for mask in range(1, 1 << m):
        # 删去任一元素后已知零概率 => mask 零概率但非最小，跳过
        rest = mask
        while rest:
            bit = rest & -rest
            if known_zero[mask ^ bit]:
                known_zero[mask] = 1
                break
            rest ^= bit
        if known_zero[mask]:
            continue
        bit = mask & -mask
        idx = bit.bit_length() - 1
        node = bdd.and_(nodes[mask ^ bit], folded[idx])
        nodes[mask] = node
        if node == 0:
            known_zero[mask] = 1
            groups.append(mask)
            if len(groups) > MAX_CONFLICT_GROUPS:
                raise ConflictGroupLimitExceeded(
                    f"最小零概率观测组超过 {MAX_CONFLICT_GROUPS} 组的限制"
                )
    return groups
