"""API 端到端测试：报送、校验、增量重算（补报/替换）、原子性、查询。"""

from tests.conftest import sec_map, submit, unp_map


def test_health(client):
    assert client.get("/health").json() == {"status": "ok"}


def test_full_flow_multi_layer(client):
    r = submit(
        client, "2026Q3", "A",
        [
            {"target_type": "fund", "target_code": "B", "weight_bp": 6000},
            {"target_type": "security", "target_code": "S1", "weight_bp": 4000},
        ],
    )
    assert r.status_code == 201, r.text
    assert r.json()["recomputed_funds"] == ["A"]

    submit(
        client, "2026Q3", "B",
        [
            {"target_type": "fund", "target_code": "C", "weight_bp": 5000},
            {"target_type": "security", "target_code": "S1", "weight_bp": 5000},
        ],
    )
    submit(client, "2026Q3", "C",
           [{"target_type": "security", "target_code": "S2", "weight_bp": 10000}])

    p = client.get("/quarters/2026Q3/funds/A/penetration").json()
    s = sec_map(p)
    assert s["S1"]["weight_bp_exact"] == "7000/1"
    assert s["S2"]["weight_bp_exact"] == "3000/1"
    assert p["total_bp"] == 10000.0
    assert p["unpenetrated_total"]["weight_bp"] == 0.0

    # 单证券查询（同一证券多路径已合并）
    w = client.get("/quarters/2026Q3/funds/A/securities/S1").json()
    assert w["weight_bp_exact"] == "7000/1"
    w2 = client.get("/quarters/2026Q3/funds/A/securities/NOPE").json()
    assert w2["weight_bp"] == 0.0


def test_incremental_recompute_scope_on_supplement(client):
    # H -> G -> F；先报上层，后补报 F
    submit(client, "2026Q1", "H",
           [{"target_type": "fund", "target_code": "G", "weight_bp": 10000}])
    submit(client, "2026Q1", "G",
           [{"target_type": "fund", "target_code": "F", "weight_bp": 10000}])

    # F 缺报：H、G 都应记 missing=F
    for f in ("H", "G"):
        p = client.get(f"/quarters/2026Q1/funds/{f}/penetration").json()
        assert ("missing", "F") in unp_map(p)

    # 无关基金 U 不受影响
    submit(client, "2026Q1", "U",
           [{"target_type": "security", "target_code": "SX", "weight_bp": 10000}])
    u_before = client.get("/quarters/2026Q1/funds/U/penetration").json()

    # 补报 F：重算范围必须含 F、G、H，不含 U
    r = submit(client, "2026Q1", "F",
               [{"target_type": "security", "target_code": "SF",
                 "weight_bp": 10000}])
    assert r.status_code == 201
    assert r.json()["recomputed_funds"] == ["F", "G", "H"]

    h = client.get("/quarters/2026Q1/funds/H/penetration").json()
    assert sec_map(h)["SF"]["weight_bp_exact"] == "10000/1"
    assert h["unpenetrated"] == []
    u_after = client.get("/quarters/2026Q1/funds/U/penetration").json()
    assert u_after == u_before


def test_replace_recomputes_ancestors_and_keeps_unrelated(client):
    submit(client, "2026Q2", "H",
           [{"target_type": "fund", "target_code": "G", "weight_bp": 10000}])
    submit(client, "2026Q2", "G",
           [{"target_type": "security", "target_code": "OLD", "weight_bp": 10000}])
    assert sec_map(
        client.get("/quarters/2026Q2/funds/H/penetration").json()
    )["OLD"]["weight_bp"] == 10000.0

    # 未带 replace 应冲突
    conflict = submit(client, "2026Q2", "G",
                      [{"target_type": "security", "target_code": "NEW",
                        "weight_bp": 10000}])
    assert conflict.status_code == 409

    # 替换 G：H（祖先）同步重算
    r = submit(
        client, "2026Q2", "G",
        [{"target_type": "security", "target_code": "NEW", "weight_bp": 10000}],
        replace=True,
    )
    assert r.status_code == 201
    assert r.json()["replaced"] is True
    assert r.json()["recomputed_funds"] == ["G", "H"]
    h = client.get("/quarters/2026Q2/funds/H/penetration").json()
    assert set(sec_map(h)) == {"NEW"}


def test_invalid_submission_does_not_change_old_result(client):
    submit(client, "2026Q3", "G",
           [{"target_type": "security", "target_code": "OLD", "weight_bp": 10000}])
    before = client.get("/quarters/2026Q3/funds/G/penetration").json()

    # 超 10000：422，旧结果仍可查
    bad = submit(
        client, "2026Q3", "G",
        [
            {"target_type": "security", "target_code": "NEW", "weight_bp": 9000},
            {"target_type": "security", "target_code": "NEW2", "weight_bp": 2000},
        ],
        replace=True,
    )
    assert bad.status_code == 422
    after = client.get("/quarters/2026Q3/funds/G/penetration").json()
    assert after == before

    # 重复对象、错误季度、非正权重也应 422
    assert submit(client, "2026Q3", "X", [
        {"target_type": "security", "target_code": "S1", "weight_bp": 100},
        {"target_type": "security", "target_code": "S1", "weight_bp": 200},
    ]).status_code == 422
    assert submit(client, "2026-3", "X", []).status_code == 422
    assert submit(client, "2026Q3", "X", [
        {"target_type": "security", "target_code": "S1", "weight_bp": 0},
    ]).status_code == 422

    # 报送表清单仍是旧的那张
    reports = client.get("/quarters/2026Q3/reports").json()
    assert [r["fund_code"] for r in reports] == ["G"]


def test_unpenetrated_breakdown_and_quarter_total(client):
    # A: 缺报 X 3000 + 余额 2000 + S1 5000
    submit(client, "2026Q3", "A", [
        {"target_type": "fund", "target_code": "X", "weight_bp": 3000},
        {"target_type": "security", "target_code": "S1", "weight_bp": 5000},
    ])
    # B: 封闭互持组 B<->D 全封闭
    submit(client, "2026Q3", "B",
           [{"target_type": "fund", "target_code": "D", "weight_bp": 10000}])
    submit(client, "2026Q3", "D",
           [{"target_type": "fund", "target_code": "B", "weight_bp": 10000}])

    p = client.get("/quarters/2026Q3/funds/A/penetration").json()
    u = unp_map(p)
    assert u[("missing", "X")]["weight_bp"] == 3000.0
    assert u[("residual", "unallocated_residual")]["weight_bp"] == 2000.0
    assert p["unpenetrated_total"]["weight_bp"] == 5000.0

    total = client.get("/quarters/2026Q3/unpenetrated").json()
    by = {r["reason"]: r["weight_bp"] for r in total["by_reason"]}
    # A: missing 3000 + residual 2000；B、D 各 closed 10000
    assert by["missing"] == 3000.0
    assert by["residual"] == 2000.0
    assert by["closed_group"] == 20000.0
    assert total["total"]["weight_bp"] == 25000.0

    fund_total = client.get(
        "/quarters/2026Q3/unpenetrated", params={"fund_code": "B"}
    ).json()
    assert fund_total["total"]["weight_bp"] == 10000.0


def test_unknown_fund_404(client):
    r = client.get("/quarters/2026Q3/funds/GHOST/penetration")
    assert r.status_code == 404


def test_derived_result_for_unreported_but_held_fund(client):
    submit(client, "2026Q3", "A", [
        {"target_type": "fund", "target_code": "X", "weight_bp": 4000},
        {"target_type": "security", "target_code": "S1", "weight_bp": 6000},
    ])
    r = client.get("/quarters/2026Q3/funds/X/penetration")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "derived_unreported"
    assert unp_map(body)[("missing", "X")]["weight_bp"] == 10000.0
