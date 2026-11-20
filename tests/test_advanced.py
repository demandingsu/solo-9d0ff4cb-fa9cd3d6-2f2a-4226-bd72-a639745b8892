"""进阶测试：事务原子性、季度隔离、两种互持的 API 级验证。"""

import pytest

from app import services
from tests.conftest import sec_map, submit, unp_map


def test_transaction_rollback_keeps_old_result_queryable(client, monkeypatch):
    submit(client, "2026Q3", "G", [
        {"target_type": "security", "target_code": "OLD", "weight_bp": 10000},
    ])
    before = client.get("/quarters/2026Q3/funds/G/penetration").json()
    reports_before = client.get("/quarters/2026Q3/reports").json()

    def boom(_holdings):
        raise RuntimeError("simulated compute failure")

    monkeypatch.setattr(services, "penetrate_quarter", boom)

    r = submit(client, "2026Q3", "G", [
        {"target_type": "security", "target_code": "NEW", "weight_bp": 10000},
    ], replace=True)
    assert r.status_code == 500

    # 原结果仍可查，报送清单不变，新表未留下任何残片
    after = client.get("/quarters/2026Q3/funds/G/penetration").json()
    assert after == before
    assert client.get("/quarters/2026Q3/reports").json() == reports_before
    held = client.get(
        "/quarters/2026Q3/funds/G/securities/NEW"
    ).json()
    assert held["weight_bp"] == 0.0


def test_quarters_are_isolated(client):
    submit(client, "2026Q1", "A", [
        {"target_type": "security", "target_code": "S1", "weight_bp": 10000},
    ])
    submit(client, "2026Q2", "A", [
        {"target_type": "security", "target_code": "S2", "weight_bp": 10000},
    ])
    # 替换 Q1 的 A，不影响 Q2
    r = submit(client, "2026Q1", "A", [
        {"target_type": "security", "target_code": "S1X", "weight_bp": 10000},
    ], replace=True)
    assert r.status_code == 201
    assert r.json()["recomputed_funds"] == ["A"]

    q1 = client.get("/quarters/2026Q1/funds/A/penetration").json()
    q2 = client.get("/quarters/2026Q2/funds/A/penetration").json()
    assert set(sec_map(q1)) == {"S1X"}
    assert set(sec_map(q2)) == {"S2"}


def test_mutual_holdings_with_exit_via_api(client):
    submit(client, "2026Q3", "A", [
        {"target_type": "fund", "target_code": "B", "weight_bp": 8000},
        {"target_type": "security", "target_code": "S1", "weight_bp": 2000},
    ])
    submit(client, "2026Q3", "B", [
        {"target_type": "fund", "target_code": "A", "weight_bp": 8000},
        {"target_type": "security", "target_code": "S2", "weight_bp": 2000},
    ])
    p = client.get("/quarters/2026Q3/funds/A/penetration").json()
    s = sec_map(p)
    # 有出口互持：按比例分摊，份额全部流出，无未穿透
    assert s["S1"]["weight_bp_exact"] == "50000/9"
    assert s["S2"]["weight_bp_exact"] == "40000/9"
    assert p["unpenetrated"] == []
    assert p["total_bp"] == 10000.0


def test_closed_mutual_group_with_inflow_via_api(client):
    # A<->B 完全封闭；C 持有 A 50% + S3 50%
    submit(client, "2026Q3", "A", [
        {"target_type": "fund", "target_code": "B", "weight_bp": 10000},
    ])
    submit(client, "2026Q3", "B", [
        {"target_type": "fund", "target_code": "A", "weight_bp": 10000},
    ])
    submit(client, "2026Q3", "C", [
        {"target_type": "fund", "target_code": "A", "weight_bp": 5000},
        {"target_type": "security", "target_code": "S3", "weight_bp": 5000},
    ])
    c = client.get("/quarters/2026Q3/funds/C/penetration").json()
    u = unp_map(c)
    assert sec_map(c)["S3"]["weight_bp"] == 5000.0
    assert u[("closed_group", "A,B")]["weight_bp"] == 5000.0
    assert c["total_bp"] == 10000.0

    for member in ("A", "B"):
        m = client.get(f"/quarters/2026Q3/funds/{member}/penetration").json()
        assert m["securities"] == []
        assert m["unpenetrated_total"]["weight_bp"] == 10000.0


def test_closed_group_opens_after_supplement_adds_exit(client):
    # 先封闭，补报替换为带证券出口后应转为正常分摊
    submit(client, "2026Q3", "A", [
        {"target_type": "fund", "target_code": "B", "weight_bp": 10000},
    ])
    submit(client, "2026Q3", "B", [
        {"target_type": "fund", "target_code": "A", "weight_bp": 10000},
    ])
    assert client.get(
        "/quarters/2026Q3/funds/A/penetration"
    ).json()["unpenetrated_total"]["weight_bp"] == 10000.0

    submit(client, "2026Q3", "B", [
        {"target_type": "fund", "target_code": "A", "weight_bp": 5000},
        {"target_type": "security", "target_code": "S9", "weight_bp": 5000},
    ], replace=True)

    p = client.get("/quarters/2026Q3/funds/A/penetration").json()
    # A: 100% -> B；B: 50% -> A, 50% -> S9。解 x = .5x + .5 => A 全落 S9
    assert sec_map(p)["S9"]["weight_bp_exact"] == "10000/1"
    assert p["unpenetrated"] == []


def test_empty_submission_is_all_residual(client):
    r = submit(client, "2026Q3", "E", [])
    assert r.status_code == 201
    p = client.get("/quarters/2026Q3/funds/E/penetration").json()
    u = unp_map(p)
    assert u[("residual", "unallocated_residual")]["weight_bp"] == 10000.0
    assert p["total_bp"] == 10000.0


def test_reported_fund_fully_in_securities_keeps_reported_status(client):
    # 全部落到证券、没有任何未穿透行时，状态仍必须是 reported
    submit(client, "2026Q3", "F", [
        {"target_type": "security", "target_code": "S1", "weight_bp": 10000},
    ])
    p = client.get("/quarters/2026Q3/funds/F/penetration").json()
    assert p["status"] == "reported"
    assert p["unpenetrated"] == []
    t = client.get("/quarters/2026Q3/unpenetrated",
                   params={"fund_code": "F"}).json()
    assert t["total"]["weight_bp"] == 0.0
    assert t["by_reason"] == []
