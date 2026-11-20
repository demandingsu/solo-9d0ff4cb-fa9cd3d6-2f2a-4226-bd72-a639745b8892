"""穿透核心单元测试：多层、两种互持、缺报、余额、路径合并。"""

from fractions import Fraction

from app.solver import (
    REASON_CLOSED_GROUP,
    REASON_MISSING,
    REASON_RESIDUAL,
    RESIDUAL_KEY,
    TOTAL_BP,
    HoldingRow,
    penetrate_quarter,
)


def row(t, code, bp):
    return HoldingRow(t, code, bp)


def test_multi_layer_and_path_merge():
    # A -> B 6000, S1 4000；B -> C 5000, S1 5000；C -> S2 10000
    holdings = {
        "A": [row("fund", "B", 6000), row("security", "S1", 4000)],
        "B": [row("fund", "C", 5000), row("security", "S1", 5000)],
        "C": [row("security", "S2", TOTAL_BP)],
    }
    res = penetrate_quarter(holdings)
    a = res["A"]
    # S1: 4000 直持 + 6000*0.5 = 7000；S2: 6000*0.5 = 3000
    assert a.securities["S1"] == Fraction(7000)
    assert a.securities["S2"] == Fraction(3000)
    assert a.unpenetrated_total == 0
    assert a.total == TOTAL_BP
    # 同证券两条路径已合并为一条
    assert set(a.securities) == {"S1", "S2"}


def test_mutual_holdings_with_exit_distributes_proportionally():
    # A <-> B 互持，但各有 20% 证券出口
    holdings = {
        "A": [row("fund", "B", 8000), row("security", "S1", 2000)],
        "B": [row("fund", "A", 8000), row("security", "S2", 2000)],
    }
    res = penetrate_quarter(holdings)
    a, b = res["A"], res["B"]
    # 解：x_A = 0.8*x_B + 0.2(S1), x_B = 0.8*x_A + 0.2(S2)
    # A 落 S1 = 5/9 份额 = 50000/9 基点；落 S2 = 4/9 份额 = 40000/9 基点
    assert a.securities["S1"] == Fraction(50000, 9)
    assert a.securities["S2"] == Fraction(40000, 9)
    assert b.securities["S2"] == Fraction(50000, 9)
    assert b.securities["S1"] == Fraction(40000, 9)
    assert a.unpenetrated_total == 0
    assert a.total == TOTAL_BP and b.total == TOTAL_BP


def test_mutual_holdings_without_exit_is_fully_unpenetrated():
    # A 与 B 完全互持，无任何证券/余额出口
    holdings = {
        "A": [row("fund", "B", TOTAL_BP)],
        "B": [row("fund", "A", TOTAL_BP)],
    }
    res = penetrate_quarter(holdings)
    key = "A,B"
    for f in ("A", "B"):
        assert res[f].securities == {}
        assert res[f].unpenetrated[(REASON_CLOSED_GROUP, key)] == Fraction(TOTAL_BP)
        assert res[f].total == TOTAL_BP


def test_inflow_into_closed_group_is_unpenetrated_and_shared():
    # C -> A 5000、S3 5000；A<->B 封闭互持
    holdings = {
        "C": [row("fund", "A", 5000), row("security", "S3", 5000)],
        "A": [row("fund", "B", TOTAL_BP)],
        "B": [row("fund", "A", TOTAL_BP)],
    }
    res = penetrate_quarter(holdings)
    c = res["C"]
    assert c.securities == {"S3": Fraction(5000)}
    # 流入封闭组的 5000 全额计入 C 的未穿透（只计一次）
    assert c.unpenetrated[(REASON_CLOSED_GROUP, "A,B")] == Fraction(5000)
    assert c.total == TOTAL_BP
    # 组内成员各自自身 10000 未穿透，不因外部流入而重复增加
    assert res["A"].unpenetrated_total == TOTAL_BP
    assert res["B"].unpenetrated_total == TOTAL_BP


def test_missing_report_is_unpenetrated():
    # A -> 未报送基金 X 3000，S1 7000
    holdings = {"A": [row("fund", "X", 3000), row("security", "S1", 7000)]}
    res = penetrate_quarter(holdings)["A"]
    assert res.securities == {"S1": Fraction(7000)}
    assert res.unpenetrated[(REASON_MISSING, "X")] == Fraction(3000)
    assert res.total == TOTAL_BP


def test_residual_balance_is_unpenetrated():
    # 只报送 8000，剩余 2000 为表中未列余额
    holdings = {"A": [row("security", "S1", 8000)]}
    res = penetrate_quarter(holdings)["A"]
    assert res.securities == {"S1": Fraction(8000)}
    assert res.unpenetrated[(REASON_RESIDUAL, RESIDUAL_KEY)] == Fraction(2000)
    assert res.total == TOTAL_BP


def test_empty_table_is_fully_residual():
    res = penetrate_quarter({"A": []})["A"]
    assert res.securities == {}
    assert res.unpenetrated[(REASON_RESIDUAL, RESIDUAL_KEY)] == Fraction(TOTAL_BP)


def test_self_loop_with_security_exit_is_solved():
    # A 自持 50% + S1 50%，有出口，不属于封闭组
    holdings = {"A": [row("fund", "A", 5000), row("security", "S1", 5000)]}
    res = penetrate_quarter(holdings)["A"]
    assert res.securities["S1"] == Fraction(TOTAL_BP)
    assert res.unpenetrated_total == 0


def test_self_loop_without_exit_is_closed():
    holdings = {"A": [row("fund", "A", TOTAL_BP)]}
    res = penetrate_quarter(holdings)["A"]
    assert res.securities == {}
    assert res.unpenetrated[(REASON_CLOSED_GROUP, "A")] == Fraction(TOTAL_BP)


def test_diamond_aggregation_exact_fractions():
    # A -> B、C 各 5000；B -> S1 10000；C -> S1 6000 + S2 4000
    holdings = {
        "A": [row("fund", "B", 5000), row("fund", "C", 5000)],
        "B": [row("security", "S1", TOTAL_BP)],
        "C": [row("security", "S1", 6000), row("security", "S2", 4000)],
    }
    a = penetrate_quarter(holdings)["A"]
    assert a.securities["S1"] == Fraction(8000)
    assert a.securities["S2"] == Fraction(2000)
    assert a.total == TOTAL_BP
