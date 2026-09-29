"""ROBDD（Reduced Ordered Binary Decision Diagram）构建与精确概率计算。

变量顺序：事件 id 字典序（sorted(events)），变量等级越小越靠前。

存储采用全局唯一表（unique table），以 (var, low, high) 为键保证规范化，
自动满足 ROBDD 的两条约简规则：
  1. 删除冗余节点（low == high 时直接复用子节点）；
  2. 合并同构子图（相同三元组复用同一节点）。

0/1 终端用整数 0 / 1 表示，非终端节点 id 从 2 开始。
"""

from __future__ import annotations

from fractions import Fraction
from typing import Dict, List, Optional, Tuple

# 构建过程中允许累计的非终端节点上限
MAX_NONTERMINAL_NODES = 50_000


class NodeLimitExceeded(Exception):
    """累计创建的非终端节点数超过上限。"""


# 节点：var_index, low（0 分支）, high（1 分支）
Node = Tuple[int, int, int]


class ROBDD:
    def __init__(self, n_vars: int, limit: int = MAX_NONTERMINAL_NODES) -> None:
        self.n_vars = n_vars
        self.limit = limit
        # 节点 0 / 1 为终端，占位 None；其余为 Node
        self.nodes: List[Node | None] = [None, None]
        # (var, low, high) -> node_id
        self.unique: Dict[Tuple[int, int, int], int] = {}
        # ITE 计算缓存（computed table）：(f, g, h) -> 结果节点
        self._ite_memo: Dict[Tuple[int, int, int], int] = {}

    # ---- 唯一表 ----
    def mk(self, var: int, low: int, high: int) -> int:
        """获取（或创建）规范化非终端节点，变量 var 必须低于两个子节点。"""
        # 冗余删除：变量取值不影响结果
        if low == high:
            return low
        key = (var, low, high)
        node_id = self.unique.get(key)
        if node_id is not None:
            return node_id
        if len(self.nodes) - 2 >= self.limit:
            raise NodeLimitExceeded(
                f"构建中累计非终端节点数超过 {self.limit} 的限制"
            )
        node_id = len(self.nodes)
        self.nodes.append((var, low, high))
        self.unique[key] = node_id
        return node_id

    # ---- ITE 算子 ----
    def ite(self, f: int, g: int, h: int) -> int:
        """if f then g else h，标准 ROBDD 递归算法。"""
        # 终端情形
        if f == 1:
            return g
        if f == 0:
            return h
        if g == 1 and h == 0:
            return f
        if g == h:
            return g

        key = (f, g, h)
        cached = self._ite_memo.get(key)
        if cached is not None:
            return cached

        vf = self.nodes[f][0]  # type: ignore[index]
        vg = self.nodes[g][0] if g >= 2 else self.n_vars
        vh = self.nodes[h][0] if h >= 2 else self.n_vars
        var = min(vf, vg, vh)

        fx = self.nodes[f] if f >= 2 else None  # type: ignore[assignment]
        gx = self.nodes[g] if g >= 2 else None
        hx = self.nodes[h] if h >= 2 else None

        # 对 var 求余因子（变量等级高于 var 时节点不受影响）
        f_lo = fx[1] if (fx is not None and fx[0] == var) else f
        f_hi = fx[2] if (fx is not None and fx[0] == var) else f
        g_lo = gx[1] if (gx is not None and gx[0] == var) else g
        g_hi = gx[2] if (gx is not None and gx[0] == var) else g
        h_lo = hx[1] if (hx is not None and hx[0] == var) else h
        h_hi = hx[2] if (hx is not None and hx[0] == var) else h

        low = self.ite(f_lo, g_lo, h_lo)
        high = self.ite(f_hi, g_hi, h_hi)
        result = self.mk(var, low, high)
        self._ite_memo[key] = result
        return result

    # ---- 布尔运算（全部经由 ITE，保证结果仍在同一唯一表中规范化）----
    def negate(self, f: int) -> int:
        return self.ite(f, 0, 1)

    def and_(self, f: int, g: int) -> int:
        return self.ite(f, g, 0)

    def or_(self, f: int, g: int) -> int:
        return self.ite(f, 1, g)

    # ---- 精确概率与重要度 ----
    def node_probabilities(self, probs: List[Fraction]) -> List[Fraction]:
        """自底向上求唯一表中每个节点的发生概率，按节点 id 索引返回。

        子节点总是先于父节点创建（id 更小），递增序即为后序。多个根
        （证据合取、各候选分支等）共享同一唯一表时，一次遍历即可同时
        读出全部根的概率，避免对每个根重复传播。
        """
        prob: List[Fraction] = [Fraction(0), Fraction(1)] + [
            Fraction(0)
        ] * (len(self.nodes) - 2)
        for nid in range(2, len(self.nodes)):
            node = self.nodes[nid]
            assert node is not None
            var, low, high = node
            p = prob[high] - prob[low]
            prob[nid] = prob[low] + probs[var] * p
        return prob

    def root_probability(
        self, root: int, probs: List[Fraction]
    ) -> Tuple[Fraction, List[Fraction]]:
        """返回 (顶层发生概率, 每个变量的 Birnbaum 重要度)。

        事件独立，P(node) = (1-p_v)·P(low) + p_v·P(high)。

        重要度定义：
            I(v) = P(top | v 强制为 1) - P(top | v 强制为 0)
        用单遍“路径到达质量”传播精确计算：设 w(n) 为从 root 到达节点 n
        的路径概率质量（其子孙变量的赋值概率乘积之和），r(n) 为 n 的
        high 与 low 子节点概率之差，则
            P(top | v=1) - P(top | v=0) = Σ_{var(n)=v} w(n)·r(n)。
        """
        if root == 0:
            return Fraction(0), [Fraction(0) for _ in range(self.n_vars)]
        if root == 1:
            return Fraction(1), [Fraction(0) for _ in range(self.n_vars)]

        # 第一遍：自底向上求每个节点的发生概率 P(n)。
        # 子节点总是先于父节点创建（id 更小），递增序即为后序。
        prob: List[Fraction] = [Fraction(0), Fraction(1)] + [
            Fraction(0)
        ] * (len(self.nodes) - 2)
        for nid in range(2, len(self.nodes)):
            node = self.nodes[nid]
            assert node is not None
            var, low, high = node
            p = prob[high] - prob[low]
            prob[nid] = prob[low] + probs[var] * p

        # 第二遍：自顶向下传播到达质量 w(n)（父节点 id 更大，用递减序）。
        weight: List[Fraction] = [Fraction(0)] * len(self.nodes)
        weight[root] = Fraction(1)
        importance = [Fraction(0) for _ in range(self.n_vars)]
        for nid in range(len(self.nodes) - 1, 1, -1):
            w = weight[nid]
            if w == 0:
                continue
            node = self.nodes[nid]
            assert node is not None
            var, low, high = node
            # 走 low 分支乘 (1-p_v)，走 high 分支乘 p_v
            weight[low] += w * (Fraction(1) - probs[var])
            weight[high] += w * probs[var]
            importance[var] += w * (prob[high] - prob[low])

        return prob[root], importance

    def joint_probabilities(
        self, root: int, probs: List[Fraction]
    ) -> Tuple[Fraction, List[Fraction]]:
        """返回 (P(root), 每个变量 v 的 P(root 且 v=1))，均为精确分数。

        用于条件证据 E（root 即各观测约束的合取）上的后验计算：
            P(v=1 | E) = P(root 且 v=1) / P(root)
        观测仅作为条件约束，变量仍按 probs 独立加权，不修改先验。

        ROBDD 中每条根到终端 1 的路径都对应一组“被赋值变量”的
        部分指派，跳过的变量在该路径上自由。第一遍自底向上求
        P(n)（同 root_probability）；第二遍自顶向下传播到达质量
        w(n)——从 root 到 n 的路径上（n 的祖先）已赋值变量的概率
        乘积之和，则 P(root) = Σ_{路径终点 1} w。

        P(root 且 v=1) 的全部贡献按 v 在哪里被决定分三类：
          1. v 比 root 变量等级更靠前：root 子图不含 v，整条路径上
             v 都自由，贡献 P(root)·p_v；
          2. 到达 var=v 的节点 n 后走 high 边：w(n)·p_v·P(high)；
          3. 某条边 (父 var=a → 子节点 c) 跳过了 v（a < v < var(c)，
             或 c 为终端）：该边的到达质量上 v 自由，贡献
             w_edge·p_v·P(c)。
        三类互不重叠且覆盖全部满足指派，故无需枚举事件状态。
        """
        if root == 0:
            return Fraction(0), [Fraction(0) for _ in range(self.n_vars)]
        if root == 1:
            return Fraction(1), list(probs)

        # 第一遍：自底向上求每个节点的发生概率 P(n)。
        prob: List[Fraction] = [Fraction(0), Fraction(1)] + [
            Fraction(0)
        ] * (len(self.nodes) - 2)
        for nid in range(2, len(self.nodes)):
            node = self.nodes[nid]
            assert node is not None
            var, low, high = node
            p = prob[high] - prob[low]
            prob[nid] = prob[low] + probs[var] * p

        # 第二遍：自顶向下传播到达质量 w(n)。
        weight: List[Fraction] = [Fraction(0)] * len(self.nodes)
        weight[root] = Fraction(1)
        joint: List[Fraction] = [Fraction(0) for _ in range(self.n_vars)]

        # 情形 1：等级早于 root 变量的变量在整条函数上都自由
        root_var = self.nodes[root][0]  # type: ignore[index]
        for var in range(root_var):
            joint[var] = prob[root] * probs[var]

        for nid in range(len(self.nodes) - 1, 1, -1):
            w = weight[nid]
            if w == 0:
                continue
            node = self.nodes[nid]
            assert node is not None
            var, low, high = node
            # 情形 2：在本节点走 high 分支即显式令 var=1
            joint[var] += w * probs[var] * prob[high]
            w_low = w * (Fraction(1) - probs[var])
            w_high = w * probs[var]
            weight[low] += w_low
            weight[high] += w_high
            # 情形 3：边跳过的变量在该路径上自由
            for edge_mass, child in ((w_low, low), (w_high, high)):
                if edge_mass == 0:
                    continue
                child_var = (
                    self.nodes[child][0]  # type: ignore[index]
                    if child >= 2
                    else self.n_vars
                )
                for skipped in range(var + 1, child_var):
                    joint[skipped] += (
                        edge_mass * probs[skipped] * prob[child]
                    )

        return prob[root], joint

    # ---- 顶事件条件下取 1 变量个数的计数分布 ----
    def count_distribution(
        self, root: int, probs: List[Fraction]
    ) -> List[Fraction]:
        """返回 [P(K=k 且 root=1)]，下标 k = 0..n_vars，K 为全部变量取 1 的个数。

        精确计数，不枚举赋值：自底向上为每个节点求子图取 1 的计数生成
        函数（整系数多项式，x^k 的系数对应“该节点之后变量中恰有 k 个
        取 1”的权重）。ROBDD 约简跳过的变量（父节点直接连到高等级
        子节点）与到达终端后剩余的变量都是自由变量，按各自先验以
        ((1-p_j) + p_j·x) 因子补入；同一变量无论被多少处引用都只
        对应一个因子，天然只计一次。

        全部系数以 Π b_j（各先验约分分母之积）为公分母用整数运算，
        最后逐系数统一约分，避免传播过程中的反复 gcd。子节点 id 总是
        小于父节点 id，按 id 递增即为后序遍历；节点多项式在被全部父
        节点用完后即释放（根可能因规范化共享同时成为其他节点的子
        节点，额外保留一次），内存只与遍历前沿有关。
        """
        n = self.n_vars
        # p_j = a_j / b_j（Fraction 已约分）；自由变量因子 ((b_j-a_j) + a_j·x)
        num = [p.numerator for p in probs]
        den = [p.denominator for p in probs]

        def var_of(nid: int) -> int:
            return self.nodes[nid][0] if nid >= 2 else n  # type: ignore[index]

        def mul_linear(coeffs: List[int], j: int) -> List[int]:
            # 乘以自由变量 j 的因子 ((b_j-a_j) + a_j·x)
            if not coeffs:
                return coeffs
            aj, bj = num[j], den[j]
            out = [0] * (len(coeffs) + 1)
            fj = bj - aj
            for k, c in enumerate(coeffs):
                out[k] += fj * c
                out[k + 1] += aj * c
            return out

        # 节点生成函数，隐含公分母 Π_{j>=var(node)} b_j；
        # 终端 1 为 [1]（空指派），终端 0 为 []（零多项式）
        polys: List[Optional[List[int]]] = [None] * len(self.nodes)
        polys[0] = []
        polys[1] = [1]

        # 引用计数：子节点多项式被全部父节点用完后即可释放
        uses = [0] * len(self.nodes)
        for nid in range(2, len(self.nodes)):
            node = self.nodes[nid]
            assert node is not None
            uses[node[1]] += 1
            uses[node[2]] += 1
        uses[root] += 1

        for nid in range(2, len(self.nodes)):
            node = self.nodes[nid]
            assert node is not None
            var, low, high = node
            av, bv = num[var], den[var]
            acc: List[int] = []
            for child, is_high in ((low, False), (high, True)):
                poly = polys[child]
                assert poly is not None
                coeffs = list(poly)
                # 边上被约简跳过的变量 var+1 .. var(child)-1 逐一补因子
                for j in range(var + 1, var_of(child)):
                    coeffs = mul_linear(coeffs, j)
                # 本变量 var 的取值分支：低分支权重 (b_v-a_v)（取 0，不
                # 计数）；高分支权重 a_v 且计数加一（再乘 x）
                if is_high:
                    if coeffs:
                        coeffs = [0] + [av * c for c in coeffs]
                else:
                    fv = bv - av
                    coeffs = [fv * c for c in coeffs]
                if len(acc) < len(coeffs):
                    acc.extend([0] * (len(coeffs) - len(acc)))
                for k, c in enumerate(coeffs):
                    acc[k] += c
                if child >= 2:
                    uses[child] -= 1
                    if uses[child] == 0:
                        polys[child] = None
            polys[nid] = acc

        # 根之前的变量（根为终端时即全部变量）同样按自由变量补入
        root_poly = polys[root]
        assert root_poly is not None
        coeffs = list(root_poly)
        for j in range(var_of(root)):
            coeffs = mul_linear(coeffs, j)

        # 隐含公分母 D = Π b_j；逐系数约分，右侧补零到 n+1 项
        common = 1
        for bj in den:
            common *= bj
        result = [Fraction(0)] * (n + 1)
        for k, c in enumerate(coeffs):
            if c:
                result[k] = Fraction(c, common)
        return result
