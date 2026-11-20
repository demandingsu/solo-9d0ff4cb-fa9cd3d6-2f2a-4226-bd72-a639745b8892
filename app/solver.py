"""穿透计算核心。

输入某季度所有基金的报送表，输出每只报送基金经多层穿透后落到证券的
权重，以及未穿透份额（三种来源）：

1. ``missing``        —— 缺报：某基金直接持有另一只从未报送过的基金，
   该部分份额无法继续穿透；
2. ``residual``       —— 表中未列的余额：报送权重合计不足 10000 基点
   （整表合计 *至多* 10000），缺口视为未穿透；
3. ``closed_group``   —— 流入无出口互持组的份额：组内基金之间形成封闭
   强连通分量且组内不存在任何证券/缺报/余额出口，进入该组的全部份额
   都无法落到证券。

有出口的互持组不做封闭处理，而是在线性方程组中按比例分摊，流出到
证券（或其他出口）的份额会被算足。

所有计算使用 :class:`fractions.Fraction` 精确有理数，不产生浮点误差。
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from fractions import Fraction
from typing import Iterable

TOTAL_BP = 10_000

REASON_MISSING = "missing"
REASON_RESIDUAL = "residual"
REASON_CLOSED_GROUP = "closed_group"

REASON_LABELS = {
    REASON_MISSING: "缺报",
    REASON_RESIDUAL: "表中未列的余额",
    REASON_CLOSED_GROUP: "无出口互持组",
}

FUND = "fund"
SECURITY = "security"


@dataclass(frozen=True)
class HoldingRow:
    """报送表中的一行：对象类别、代码、整数基点权重。"""

    target_type: str
    target_code: str
    weight_bp: int


@dataclass
class FundResult:
    """单只基金的穿透结果。"""

    fund_code: str
    # 证券代码 -> 合并后的精确权重（各条路径汇入同一证券时自动累加）
    securities: dict[str, Fraction] = field(default_factory=dict)
    # 未穿透明细：(原因, 明细键) -> 精确权重
    #   missing      -> 缺报基金代码
    #   residual     -> 固定键 RESIDUAL_KEY
    #   closed_group -> 封闭组标识（组内基金代码按序拼接）
    unpenetrated: dict[tuple[str, str], Fraction] = field(default_factory=dict)

    @property
    def unpenetrated_total(self) -> Fraction:
        return sum(self.unpenetrated.values(), Fraction(0))

    @property
    def total(self) -> Fraction:
        return sum(self.securities.values(), Fraction(0)) + self.unpenetrated_total


RESIDUAL_KEY = "__residual__"


def _tarjan_scc(nodes: list[str], edges: dict[str, list[str]]) -> list[list[str]]:
    """Tarjan 强连通分量，返回顺序为逆拓扑序（汇在前）。"""
    index = 0
    indices: dict[str, int] = {}
    low: dict[str, int] = {}
    on_stack: set[str] = set()
    stack: list[str] = []
    result: list[list[str]] = []

    def strongconnect(v: str) -> None:
        nonlocal index
        indices[v] = low[v] = index
        index += 1
        stack.append(v)
        on_stack.add(v)
        for w in edges.get(v, ()):
            if w not in indices:
                strongconnect(w)
                low[v] = min(low[v], low[w])
            elif w in on_stack:
                low[v] = min(low[v], indices[w])
        if low[v] == indices[v]:
            component = []
            while True:
                w = stack.pop()
                on_stack.discard(w)
                component.append(w)
                if w == v:
                    break
            result.append(component)

    old_limit = sys.getrecursionlimit()
    sys.setrecursionlimit(max(old_limit, len(nodes) * 4 + 1000))
    try:
        for v in nodes:
            if v not in indices:
                strongconnect(v)
    finally:
        sys.setrecursionlimit(old_limit)
    return result


def _invert(matrix: list[list[Fraction]], n: int) -> list[list[Fraction]]:
    """高斯-若尔当求逆（精确分数）。输入方阵可逆。"""
    aug = [matrix[i][:] + [Fraction(1 if i == j else 0) for j in range(n)]
           for i in range(n)]
    for col in range(n):
        pivot = next(r for r in range(col, n) if aug[r][col] != 0)
        if pivot != col:
            aug[col], aug[pivot] = aug[pivot], aug[col]
        piv = aug[col][col]
        aug[col] = [x / piv for x in aug[col]]
        for r in range(n):
            if r == col:
                continue
            factor = aug[r][col]
            if factor:
                aug[r] = [a - factor * b for a, b in zip(aug[r], aug[col])]
    return [row[n:] for row in aug]


def _group_key(members: Iterable[str]) -> str:
    return ",".join(sorted(members))


def penetrate_quarter(
    holdings: dict[str, list[HoldingRow]],
) -> dict[str, FundResult]:
    """对整个季度的报送数据做穿透。

    :param holdings: 已报送基金代码 -> 该基金的报送行（权重和 ≤ 10000）。
    :return: 每只报送基金一个 :class:`FundResult`，合计严格等于 10000。
    """
    reported = set(holdings)
    # 出边（仅基金之间的边参与 SCC / 反向追溯）
    edges: dict[str, list[str]] = {f: [] for f in reported}
    weights: dict[tuple[str, str], Fraction] = {}
    for fund, rows in holdings.items():
        for row in rows:
            if row.target_type == FUND and row.target_code in reported:
                edges[fund].append(row.target_code)
                weights[(fund, row.target_code)] = (
                    weights.get((fund, row.target_code), Fraction(0))
                    + Fraction(row.weight_bp, TOTAL_BP)
                )

    # ---- 1. 找出无出口互持组：报送基金构成的封闭 SCC -------------------
    # 出口包括：证券行、指向组外基金（缺报基金或其他报送基金）的行、
    # 以及表中未列的余额。只有完全没有这些出口的 SCC 才是无出口互持组。
    closed_members: set[str] = set()
    closed_groups: list[set[str]] = []
    for component in _tarjan_scc(sorted(reported), edges):
        members = set(component)
        has_self_loop = any((f, f) in weights for f in component)
        if len(component) == 1 and not has_self_loop:
            continue  # 单点且无自环，不构成互持组
        has_exit = False
        for f in component:
            stated = 0
            for row in holdings[f]:
                stated += row.weight_bp
                if row.target_type == SECURITY:
                    has_exit = True
                elif row.target_code not in members:
                    has_exit = True  # 指向组外（含缺报基金）
            if stated < TOTAL_BP:
                has_exit = True  # 未列余额也是出口（未穿透出口）
            if has_exit:
                break
        if not has_exit:
            closed_members |= members
            closed_groups.append(members)

    # 封闭组基金：自身 100% 未穿透（无任何出口）
    results: dict[str, FundResult] = {
        f: FundResult(f) for f in reported
    }
    for group in closed_groups:
        key = _group_key(group)
        for member in group:
            results[member].unpenetrated[(REASON_CLOSED_GROUP, key)] = Fraction(
                TOTAL_BP, 1
            )

    # ---- 2. 活跃基金（不在封闭组中）建立线性方程组 ---------------------
    active = sorted(f for f in reported if f not in closed_members)
    idx = {f: i for i, f in enumerate(active)}
    n = len(active)
    matrix: list[list[Fraction]] = [
        [Fraction(0) for _ in range(n)] for _ in range(n)
    ]
    for i, f in enumerate(active):
        matrix[i][i] = Fraction(1)
        for target in edges.get(f, ()):
            if target in idx:  # 流向另一只活跃基金
                matrix[i][idx[target]] -= weights[(f, target)]

    # 收集各活跃基金的“汇”出口
    # fund -> list[(kind, key, weight_fraction)]
    #   kind: 'security' | 'missing' | 'residual' | 'closed'
    sinks: dict[str, list[tuple[str, str, Fraction]]] = {f: [] for f in active}
    for f in active:
        stated = 0
        for row in holdings[f]:
            w = Fraction(row.weight_bp, TOTAL_BP)
            stated += row.weight_bp
            if row.target_type == SECURITY:
                sinks[f].append(("security", row.target_code, w))
            elif row.target_type == FUND:
                if row.target_code in idx:
                    pass  # 基金间边已进入矩阵
                elif row.target_code in closed_members:
                    sinks[f].append(
                        ("closed", _own_group_key(row.target_code, closed_groups), w)
                    )
                else:
                    sinks[f].append(("missing", row.target_code, w))
            else:  # pragma: no cover - 入口已校验
                raise ValueError(f"未知对象类别: {row.target_type}")
        residual_bp = TOTAL_BP - stated
        if residual_bp > 0:
            sinks[f].append(
                ("residual", RESIDUAL_KEY, Fraction(residual_bp, TOTAL_BP))
            )

    # 每个汇的直接出口向量 b（按活跃基金列），一次求逆批量求解
    sink_keys = sorted({(kind, key) for f in active for kind, key, _ in sinks[f]})
    sink_index = {s: k for k, s in enumerate(sink_keys)}
    b_matrix = [
        [Fraction(0) for _ in sink_keys] for _ in range(n)
    ]
    for f in active:
        for kind, key, w in sinks[f]:
            b_matrix[idx[f]][sink_index[(kind, key)]] += w

    if n:
        inv = _invert(matrix, n)
        # flows[i][k] = 活跃基金 i 最终落到汇 k 的份额（占自身的比例）
        flows = [
            [
                sum((inv[i][p] * b_matrix[p][k] for p in range(n)), Fraction(0))
                for k in range(len(sink_keys))
            ]
            for i in range(n)
        ]
    else:
        flows = []

    for i, f in enumerate(active):
        res = results[f]
        for k, (kind, key) in enumerate(sink_keys):
            share = flows[i][k]
            if share == 0:
                continue
            bp = share * TOTAL_BP
            if kind == "security":
                res.securities[key] = res.securities.get(key, Fraction(0)) + bp
            elif kind == "missing":
                res.unpenetrated[(REASON_MISSING, key)] = (
                    res.unpenetrated.get((REASON_MISSING, key), Fraction(0)) + bp
                )
            elif kind == "residual":
                res.unpenetrated[(REASON_RESIDUAL, RESIDUAL_KEY)] = (
                    res.unpenetrated.get((REASON_RESIDUAL, RESIDUAL_KEY),
                                         Fraction(0)) + bp
                )
            elif kind == "closed":
                res.unpenetrated[(REASON_CLOSED_GROUP, key)] = (
                    res.unpenetrated.get((REASON_CLOSED_GROUP, key),
                                         Fraction(0)) + bp
                )

    # ---- 3. 合计校验 ----------------------------------------------------
    # 说明：流入封闭组的份额只在“流入方”的未穿透中体现一次；
    # 组内成员自身的 10000 基点已全额计入其未穿透，不再二次分摊流入。
    for f, res in results.items():
        if res.total != Fraction(TOTAL_BP, 1):
            raise AssertionError(
                f"基金 {f} 穿透合计 {res.total} != {TOTAL_BP}"
            )
    return results


def _own_group_key(fund_code: str, closed_groups: list[set[str]]) -> str:
    for group in closed_groups:
        if fund_code in group:
            return _group_key(group)
    raise KeyError(fund_code)  # pragma: no cover
