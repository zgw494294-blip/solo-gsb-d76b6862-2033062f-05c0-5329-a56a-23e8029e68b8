"""修复方案优选：在已构建的 ROBDD 上做受限赋值的自底向上动态规划。

当前状态下为 false 的事件固定取 0（不允许改动）；当前为 true 的事件
有两种选择：
  - 修复：取 0，付出一次该事件的修复费用；
  - 不修复：保持 1，费用为 0。
同一事件在故障树中被多处共享引用时只对应一个 BDD 变量，因此一次修复
即同时改变全部引用点，费用也只计一次。

目标是令顶事件（BDD 根节点）取值为 0，并依次按
  (1) 总修复成本最小；
  (2) 修复事件数最少；
  (3) 按事件 id 字典序排序后的事件序列字典序最小
取唯一最优方案。

修复集合用位掩码表示（变量顺序即事件 id 字典序，第 i 位置 1 表示修复
第 i 个事件）。子节点 id 总是小于父节点 id，按 id 递增即为后序遍历。
"""

from __future__ import annotations

from typing import List, Optional, Tuple

from .bdd import ROBDD

# (总成本, 修复事件位掩码)；不可行时为 None
Plan = Optional[Tuple[int, int]]


def _is_better(cost_a: int, mask_a: int, cost_b: int, mask_b: int) -> bool:
    """方案 a 是否严格优于方案 b：成本、修复数、排序后 id 序列依次比较。"""
    if cost_a != cost_b:
        return cost_a < cost_b
    count_a, count_b = mask_a.bit_count(), mask_b.bit_count()
    if count_a != count_b:
        return count_a < count_b
    if mask_a == mask_b:
        return False
    # 序列按变量下标升序（即事件 id 字典序）；两个等势集合在最低分歧位
    # i 以下的成员完全相同，含有 i 的一方其有序序列第 k 个元素即为 i，
    # 另一方则大于 i，因此在最低分歧位上置 1 的方案序列字典序更小
    first_diff = (mask_a ^ mask_b) & -(mask_a ^ mask_b)
    return (mask_a & first_diff) != 0


def best_repair(
    bdd: ROBDD,
    root: int,
    fixed_false_mask: int,
    costs: List[int],
) -> Plan:
    """求令 root 取值为 0 的最优修复方案。

    fixed_false_mask：当前为 false、必须保持 0 的事件位掩码；其余事件
    当前为 true，可修复为 0（费用 costs[i]）或保持 1。
    返回 (总成本, 修复位掩码)；不存在可行方案时返回 None。
    """
    # 终端 0：已满足，空方案零成本；终端 1：恒不满足，不可行
    if root == 0:
        return 0, 0
    if root == 1:
        return None

    dp_cost: List[Optional[int]] = [0, None] + [None] * (
        len(bdd.nodes) - 2
    )
    dp_mask: List[int] = [0] * len(bdd.nodes)

    for nid in range(2, len(bdd.nodes)):
        node = bdd.nodes[nid]
        assert node is not None
        var, low, high = node

        if (fixed_false_mask >> var) & 1:
            # 当前为 false 的事件只能保持 0：走低分支，无费用
            dp_cost[nid] = dp_cost[low]
            dp_mask[nid] = dp_mask[low]
            continue

        # 当前为 true 的事件：
        #   低分支 = 修复（取 0，加 costs[var]）；
        #   高分支 = 不修复（保持 1，无费用）。
        best: Plan = None
        if dp_cost[low] is not None:
            best = (
                dp_cost[low] + costs[var],  # type: ignore[operator]
                dp_mask[low] | (1 << var),
            )
        if dp_cost[high] is not None:
            candidate = (dp_cost[high], dp_mask[high])  # type: ignore[arg-type]
            if best is None or _is_better(
                candidate[0], candidate[1], best[0], best[1]
            ):
                best = candidate
        if best is not None:
            dp_cost[nid], dp_mask[nid] = best

    if dp_cost[root] is None:
        return None
    return dp_cost[root], dp_mask[root]  # type: ignore[return-value]
