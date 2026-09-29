"""最小割集（minimal cut sets）计算，仅适用于 AND/OR 单调故障树。

割集语义：组内事件同时发生时，无论其他事件状态如何，顶事件必然发生；
最小割集：删去组内任一事件后不再保证顶事件发生。

每个割集表示为一个 32 位位掩码：事件按 id 字典序编号，第 i 位置 1
表示该事件属于此割集（共享事件自然只计一次）。门级语义：
  OR  门：割集族 = 各输入割集族的并集；
  AND 门：割集族 = 各输入割集族的笛卡尔并积（各取一个输入割集求并）。
每个门的割集族都去重并做吸收（A ⊆ B 时删去 B，如 A OR (A AND B) 只剩 [A]），
只保留最小割集；去重、吸收后仍超过 MAX_CUT_SETS 组时抛出 CutSetLimitExceeded。
"""

from __future__ import annotations

from array import array
from typing import Dict, List, Optional, Sequence, Set

# 任一门允许的最小割集组数上限（去重、吸收后判定）
MAX_CUT_SETS = 5000

# 事件数上限 32，位掩码始终落在 32 位无符号整数内
_MAX_BITS = 32


class CutSetLimitExceeded(ValueError):
    """任一门去重、吸收后的最小割集组数超过上限。"""


def _has_subset(mask: int, k: int, live: Set[int],
                live_by_size: List[List[int]]) -> bool:
    """判断已有最小割集中是否存在 mask 的子集（必为真子集）。

    live 只含比 mask 更小的割集；子掩码枚举（2^k 个）与线性扫描
    取代价较小者。
    """
    smaller = live_by_size[:k]
    smaller_count = 0
    for lst in smaller:
        smaller_count += len(lst)
    if smaller_count == 0:
        return False
    if (1 << k) <= smaller_count:
        # 活割集比 mask 的子掩码还多：逐个枚举子掩码查表更快
        sub = (mask - 1) & mask
        while sub:
            if sub in live:
                return True
            sub = (sub - 1) & mask
        return False
    for lst in smaller:
        for m in lst:
            if (mask & m) == m:
                return True
    return False


def _reduce(buckets: List[array], limit: Optional[int]) -> Set[int]:
    """对候选割集（已按大小分桶）去重、吸收，返回最小割集集合。

    按割集大小升序处理：同尺寸的不同割集互不吸收，更小的割集不可能
    被更大的吸收，因此幸存者计数单调，一旦超过 limit（非 None）
    即可安全地提前抛出 CutSetLimitExceeded。
    """
    live: Set[int] = set()
    live_by_size: List[List[int]] = [[] for _ in range(_MAX_BITS + 1)]
    for k, bucket in enumerate(buckets):
        for mask in bucket:
            if mask in live:  # 同尺寸重复
                continue
            if _has_subset(mask, k, live, live_by_size):
                continue
            live.add(mask)
            live_by_size[k].append(mask)
        if limit is not None and len(live) > limit:
            raise CutSetLimitExceeded
    return live


def _new_buckets() -> List[array]:
    # 紧凑的 32 位无符号数组，避免大规模候选集时的内存膨胀
    return [array("I") for _ in range(_MAX_BITS + 1)]


def _combine_or(families: Sequence[Set[int]]) -> Set[int]:
    """OR 门：割集族 = 各输入割集族的并集，去重、吸收。"""
    buckets = _new_buckets()
    for fam in families:
        for mask in fam:
            buckets[mask.bit_count()].append(mask)
    return _reduce(buckets, MAX_CUT_SETS)


def _combine_and(families: Sequence[Set[int]]) -> Set[int]:
    """AND 门：割集族 = 各输入割集族的笛卡尔并积，去重、吸收。

    增量合并：每并入一个输入族就立即去重、吸收，避免一次性展开
    len(F1)×…×len(Fk) 的组合爆炸。中间阶段不做数量截断——后续并入
    的输入族可能把大量割集合并成少数几个（例如再 AND 一个所有割集
    共有的事件），只有合并完全部输入后才能判定是否超限。
    """
    ordered = sorted(families, key=len)
    current: Set[int] = set(ordered[0])
    for i, fam in enumerate(ordered[1:], 1):
        last = i == len(ordered) - 1
        buckets = _new_buckets()
        for a in current:
            for b in fam:
                u = a | b
                buckets[u.bit_count()].append(u)
        current = _reduce(buckets, MAX_CUT_SETS if last else None)
    if len(current) > MAX_CUT_SETS:
        raise CutSetLimitExceeded
    return current


def compute_cut_set_families(
    event_ids: List[str],
    gate_ops: Dict[str, str],
    gate_inputs: Dict[str, List[str]],
) -> Dict[str, Set[int]]:
    """计算每个门的输出最小割集族（位掩码集合）。

    event_ids 为按字典序排序的事件 id，第 i 个事件对应第 i 位。
    假定输入引用完整、无环且只含 AND/OR；任一门（包括顶层不可达的
    门）超限都会抛出 CutSetLimitExceeded。
    """
    event_fams: Dict[str, Set[int]] = {
        eid: {1 << i} for i, eid in enumerate(event_ids)
    }
    families: Dict[str, Set[int]] = {}

    def family_of(node: str) -> Set[int]:
        fam = event_fams.get(node)
        if fam is not None:
            return fam
        if node not in families:
            families[node] = _gate_family(node)
        return families[node]

    def _gate_family(gid: str) -> Set[int]:
        fams = [family_of(ref) for ref in gate_inputs[gid]]
        try:
            if gate_ops[gid] == "OR":
                return _combine_or(fams)
            return _combine_and(fams)
        except CutSetLimitExceeded:
            raise CutSetLimitExceeded(
                f"门 {gid!r} 的最小割集在去重、吸收后仍超过 "
                f"{MAX_CUT_SETS} 组的限制"
            ) from None

    for gid in gate_ops:
        family_of(gid)
    return families
