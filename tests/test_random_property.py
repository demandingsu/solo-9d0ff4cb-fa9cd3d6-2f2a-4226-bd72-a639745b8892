"""随机图性质测试：用独立的蒙特卡洛路径模拟交叉验证精确求解器。

对每个随机生成的季度数据：
1. 每只报送基金的穿透合计必须严格等于 10000（证券 + 三类未穿透）；
2. 所有权重非负；
3. 蒙特卡洛模拟大量随机游走得到的落点频率，必须与精确解在容差内一致。
"""

import random

from fractions import Fraction

from app.solver import (
    FUND,
    REASON_CLOSED_GROUP,
    REASON_MISSING,
    REASON_RESIDUAL,
    SECURITY,
    TOTAL_BP,
    HoldingRow,
    penetrate_quarter,
)


def _build_case(rng: random.Random):
    n = rng.randint(2, 15)
    funds = [f"F{i}" for i in range(n)]
    extra = ["M1", "M2"]  # 永不报送的缺报基金
    holdings = {}
    for f in funds:
        rows = []
        remaining = TOTAL_BP
        # 随机挑若干对象（可能挑到自己制造自环、挑到同侪制造环）
        targets = rng.sample(
            funds + extra + [f"S{k}" for k in range(4)],
            k=rng.randint(0, 5),
        )
        for t in targets:
            if remaining <= 0:
                break
            w = rng.randint(1, max(1, remaining))
            remaining -= w
            if t.startswith("S"):
                rows.append(HoldingRow(SECURITY, t, w))
            else:
                rows.append(HoldingRow(FUND, t, w))
        holdings[f] = rows  # remaining 即随机余额（可能为 0）
    return funds, holdings


def _monte_carlo(holdings, start_fund, walks=4000, max_steps=200):
    """从 start_fund 出发随机游走，统计落点（出口类型, 键）频率（基点）。"""
    rng = random.Random(123456789)
    counts = {}
    for _ in range(walks):
        cur = start_fund
        for _step in range(max_steps):
            rows = holdings.get(cur)
            if rows is None:  # 缺报
                counts[(REASON_MISSING, cur)] = counts.get(
                    (REASON_MISSING, cur), 0) + 1
                break
            total = sum(r.weight_bp for r in rows)
            roll = rng.randint(1, TOTAL_BP)
            if roll > total:  # 落在未列余额
                counts[(REASON_RESIDUAL, "__residual__")] = counts.get(
                    (REASON_RESIDUAL, "__residual__"), 0) + 1
                break
            acc = 0
            chosen = rows[-1]
            for r in rows:
                acc += r.weight_bp
                if roll <= acc:
                    chosen = r
                    break
            if chosen.target_type == SECURITY:
                counts[("security", chosen.target_code)] = counts.get(
                    ("security", chosen.target_code), 0) + 1
                break
            cur = chosen.target_code
        else:
            # 走不出互持环 => 无出口互持组
            counts[(REASON_CLOSED_GROUP, "walk")] = counts.get(
                (REASON_CLOSED_GROUP, "walk"), 0) + 1
    return {k: v * TOTAL_BP / walks for k, v in counts.items()}


def test_random_graph_invariants_and_monte_carlo():
    rng = random.Random(20261120)
    checked = 0
    for seed in range(30):
        rng.seed(20261120 + seed)
        funds, holdings = _build_case(rng)
        results = penetrate_quarter(holdings)

        assert set(results) == set(funds)
        for f, res in results.items():
            # 不变量 1：合计严格 10000
            assert res.total == Fraction(TOTAL_BP), (seed, f, res.total)
            # 不变量 2：全部非负
            assert all(w >= 0 for w in res.securities.values())
            assert all(w >= 0 for w in res.unpenetrated.values())

        # 交叉验证：抽 3 只基金做蒙特卡洛
        for f in rng.sample(funds, k=min(3, len(funds))):
            mc = _monte_carlo(holdings, f)
            res = results[f]
            exact = {("security", c): float(w) for c, w in res.securities.items()}
            closed = sum(
                float(w) for (reason, _), w in res.unpenetrated.items()
                if reason == REASON_CLOSED_GROUP
            )
            for (kind, key), freq in mc.items():
                if kind == "security":
                    exact_v = exact.get(("security", key), 0.0)
                elif kind == REASON_CLOSED_GROUP:
                    exact_v = closed
                else:
                    exact_v = float(
                        res.unpenetrated.get((kind, key), Fraction(0))
                    )
                # 4000 次游走的标准误差约 50bp，放宽到 450bp
                assert abs(freq - exact_v) < 450, (seed, f, kind, key, freq, exact_v)
            checked += 1
    assert checked >= 30
