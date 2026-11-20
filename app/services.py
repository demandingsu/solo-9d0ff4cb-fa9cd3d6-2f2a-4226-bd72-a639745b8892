"""业务服务层：报送、增量重算、查询。

增量重算范围（补报/替换一张表时）：同季度该基金本身，以及在基金持有
图上直接或间接持有它的所有基金（祖先集合）。整次操作在一个 SQLite
事务内完成，失败回滚，旧结果保持可读。
"""

from __future__ import annotations

import threading
from datetime import datetime, timezone
from fractions import Fraction

from . import db as dbmod
from .schemas import SubmissionIn
from .solver import (
    FUND,
    REASON_CLOSED_GROUP,
    REASON_LABELS,
    REASON_MISSING,
    REASON_RESIDUAL,
    RESIDUAL_KEY,
    TOTAL_BP,
    FundResult,
    HoldingRow,
    penetrate_quarter,
)

# SQLite 单文件写入需串行化；读连接仍可并发（WAL）。
_write_lock = threading.RLock()


class ServiceError(Exception):
    """业务错误基类。"""


class NotFoundError(ServiceError):
    """该季度没有任何报送涉及此基金代码。"""


class ConflictError(ServiceError):
    """报送表已存在且未声明 replace。"""


# ---- 装载 ---------------------------------------------------------------

def load_quarter_holdings(conn, quarter: str) -> dict[str, list[HoldingRow]]:
    # 以 reports 为主表 LEFT JOIN：空表报送（row_count=0）的基金也要
    # 进入穿透，其 10000 基点全部计为未列余额。
    rows = conn.execute(
        "SELECT r.fund_code AS fund_code, h.target_type AS target_type, "
        "h.target_code AS target_code, h.weight_bp AS weight_bp "
        "FROM reports r LEFT JOIN holdings h "
        "ON h.quarter = r.quarter AND h.fund_code = r.fund_code "
        "WHERE r.quarter = ? ORDER BY r.fund_code, h.target_type, h.target_code",
        (quarter,),
    ).fetchall()
    holdings: dict[str, list[HoldingRow]] = {}
    for r in rows:
        rows_for_fund = holdings.setdefault(r["fund_code"], [])
        if r["target_type"] is None:
            continue  # 空报送表
        rows_for_fund.append(
            HoldingRow(
                target_type=r["target_type"],
                target_code=r["target_code"],
                weight_bp=r["weight_bp"],
            )
        )
    return holdings


def _fund_ancestors(
    holdings: dict[str, list[HoldingRow]], target: str
) -> set[str]:
    """同季度直接或间接持有 target 的所有报送基金（不含 target 自身）。"""
    reverse: dict[str, list[str]] = {}
    for fund, rows in holdings.items():
        for row in rows:
            if row.target_type == FUND:
                reverse.setdefault(row.target_code, []).append(fund)
    seen: set[str] = set()
    stack = list(reverse.get(target, ()))
    while stack:
        cur = stack.pop()
        if cur in seen:
            continue
        seen.add(cur)
        stack.extend(reverse.get(cur, ()))
    return seen


def _persist_results(
    conn, quarter: str, results: dict[str, FundResult], affected: set[str]
) -> None:
    for fund in affected:
        conn.execute(
            "DELETE FROM fund_results WHERE quarter = ? AND fund_code = ?",
            (quarter, fund),
        )
        conn.execute(
            "DELETE FROM unpenetrated_results WHERE quarter = ? AND fund_code = ?",
            (quarter, fund),
        )
        res = results.get(fund)
        if res is None:
            # 基金不再报送（整表被清空删除的极端情形），结果删除即可。
            continue
        for code, w in sorted(res.securities.items()):
            conn.execute(
                "INSERT INTO fund_results (quarter, fund_code, security_code, "
                "weight_bp_exact) VALUES (?, ?, ?, ?)",
                (quarter, fund, code, dbmod.dump_fraction(w)),
            )
        for (reason, detail), w in sorted(res.unpenetrated.items()):
            conn.execute(
                "INSERT INTO unpenetrated_results (quarter, fund_code, reason, "
                "detail, weight_bp_exact) VALUES (?, ?, ?, ?, ?)",
                (quarter, fund, reason, detail, dbmod.dump_fraction(w)),
            )


# ---- 报送 / 替换 --------------------------------------------------------

def submit_table(
    conn, payload: SubmissionIn
) -> dict:
    quarter = payload.quarter
    fund = payload.fund_code
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")

    with _write_lock, dbmod.transaction(conn):
        existing = conn.execute(
            "SELECT 1 FROM reports WHERE quarter = ? AND fund_code = ?",
            (quarter, fund),
        ).fetchone()
        if existing and not payload.replace:
            raise ConflictError(
                f"{quarter} 基金 {fund} 已报送；如需替换请置 replace=true"
            )

        # 重算范围（替换前图）：旧图中的祖先
        before = load_quarter_holdings(conn, quarter)
        affected = _fund_ancestors(before, fund) | {fund}

        # 替换报送表
        conn.execute(
            "DELETE FROM holdings WHERE quarter = ? AND fund_code = ?",
            (quarter, fund),
        )
        conn.execute(
            "DELETE FROM reports WHERE quarter = ? AND fund_code = ?",
            (quarter, fund),
        )
        total_bp = sum(r.weight_bp for r in payload.rows)
        conn.execute(
            "INSERT INTO reports (quarter, fund_code, total_bp, row_count, "
            "submitted_at) VALUES (?, ?, ?, ?, ?)",
            (quarter, fund, total_bp, len(payload.rows), now),
        )
        for r in payload.rows:
            conn.execute(
                "INSERT INTO holdings (quarter, fund_code, target_type, "
                "target_code, weight_bp) VALUES (?, ?, ?, ?, ?)",
                (quarter, fund, r.target_type, r.target_code, r.weight_bp),
            )

        # 重算范围（新图）：新图中的祖先，取并集
        after = load_quarter_holdings(conn, quarter)
        affected |= _fund_ancestors(after, fund)
        affected &= set(after.keys())  # 仅仍在报送的基金需要落结果

        results = penetrate_quarter(after)
        missing = affected - set(results)
        if missing:  # pragma: no cover - 防御性检查
            raise ServiceError(f"重算缺失结果: {sorted(missing)}")
        _persist_results(conn, quarter, results, affected)

    return {
        "quarter": quarter,
        "fund_code": fund,
        "row_count": len(payload.rows),
        "total_bp": total_bp,
        "replaced": bool(existing),
        "recomputed_funds": sorted(affected),
        "submitted_at": now,
    }


# ---- 查询 ---------------------------------------------------------------

def _is_reported(conn, quarter: str, fund: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM reports WHERE quarter = ? AND fund_code = ?",
        (quarter, fund),
    ).fetchone() is not None


def _known_fund(conn, quarter: str, fund: str) -> bool:
    """该基金代码在本季度是否出现过（自己报送或被他人持有）。"""
    if _is_reported(conn, quarter, fund):
        return True
    row = conn.execute(
        "SELECT 1 FROM holdings WHERE quarter = ? AND target_type = 'fund' "
        "AND target_code = ? LIMIT 1",
        (quarter, fund),
    ).fetchone()
    return row is not None


def _derive_unreported(
    conn, quarter: str, fund: str
) -> FundResult:
    """未报送基金被查询时，按当前数据现场推导（只读，不落库）。

    用一个“持有该未报送基金 100%”的虚拟报送基金承接其全部份额，
    穿透后该虚拟基金的结果即为未报送基金的未穿透构成。
    """
    holdings = load_quarter_holdings(conn, quarter)
    wrapper = {"__query__": [HoldingRow(FUND, fund, TOTAL_BP)]}
    wrapper.update(holdings)
    return penetrate_quarter(wrapper)["__query__"]


def _bp(frac: Fraction) -> float:
    return round(float(frac), 6)


def _weight_obj(frac: Fraction) -> dict:
    return {"weight_bp": _bp(frac), "weight_bp_exact": dbmod.dump_fraction(frac)}


def _detail_display(reason: str, detail: str) -> str:
    if reason == REASON_RESIDUAL and detail == RESIDUAL_KEY:
        return "unallocated_residual"
    return detail


def _unp_item(reason: str, detail: str, frac: Fraction) -> dict:
    return {
        "reason": reason,
        "reason_label": REASON_LABELS[reason],
        "detail": _detail_display(reason, detail),
        **_weight_obj(frac),
    }


def get_penetration(conn, quarter: str, fund: str) -> dict:
    if not _known_fund(conn, quarter, fund):
        raise NotFoundError(f"{quarter} 未找到基金 {fund} 的任何报送或持有记录")

    if not _is_reported(conn, quarter, fund):
        # 未报送但被他人持有：按当前数据现场推导（只读，不落库）
        derived = _derive_unreported(conn, quarter, fund)
        return {
            "quarter": quarter,
            "fund_code": fund,
            "status": "derived_unreported",
            "securities": [
                {"security_code": code, **_weight_obj(w)}
                for code, w in sorted(derived.securities.items())
            ],
            "unpenetrated": [
                _unp_item(reason, detail, w)
                for (reason, detail), w in sorted(derived.unpenetrated.items())
            ],
            "unpenetrated_total": _weight_obj(derived.unpenetrated_total),
            "total_bp": _bp(derived.total),
        }

    sec_rows = conn.execute(
        "SELECT security_code, weight_bp_exact FROM fund_results "
        "WHERE quarter = ? AND fund_code = ? ORDER BY security_code",
        (quarter, fund),
    ).fetchall()
    unp_rows = conn.execute(
        "SELECT reason, detail, weight_bp_exact FROM unpenetrated_results "
        "WHERE quarter = ? AND fund_code = ? ORDER BY reason, detail",
        (quarter, fund),
    ).fetchall()

    securities = [
        {
            "security_code": r["security_code"],
            **_weight_obj(dbmod.load_fraction(r["weight_bp_exact"])),
        }
        for r in sec_rows
    ]
    unpenetrated = [
        _unp_item(
            r["reason"],
            r["detail"],
            dbmod.load_fraction(r["weight_bp_exact"]),
        )
        for r in unp_rows
    ]
    unp_total = sum(
        (dbmod.load_fraction(r["weight_bp_exact"]) for r in unp_rows),
        Fraction(0),
    )
    total = sum(
        (dbmod.load_fraction(r["weight_bp_exact"]) for r in sec_rows),
        unp_total,
    )

    return {
        "quarter": quarter,
        "fund_code": fund,
        "status": "reported",
        "securities": securities,
        "unpenetrated": unpenetrated,
        "unpenetrated_total": _weight_obj(unp_total),
        "total_bp": _bp(total),
    }


def get_security_weight(
    conn, quarter: str, fund: str, security: str
) -> dict:
    p = get_penetration(conn, quarter, fund)
    match = [s for s in p["securities"] if s["security_code"] == security]
    if not match:
        weight = {"weight_bp": 0.0, "weight_bp_exact": "0/1"}
    else:
        weight = {
            "weight_bp": match[0]["weight_bp"],
            "weight_bp_exact": match[0]["weight_bp_exact"],
        }
    return {
        "quarter": quarter,
        "fund_code": fund,
        "security_code": security,
        **weight,
    }


def get_unpenetrated_total(
    conn, quarter: str, fund: str | None = None
) -> dict:
    if fund is not None and not _known_fund(conn, quarter, fund):
        raise NotFoundError(f"{quarter} 未找到基金 {fund} 的任何报送或持有记录")

    where = "quarter = ?"
    params: list = [quarter]
    if fund is not None:
        where += " AND fund_code = ?"
        params.append(fund)
    rows = conn.execute(
        f"SELECT reason, weight_bp_exact FROM unpenetrated_results "
        f"WHERE {where}",
        params,
    ).fetchall()
    by_reason: dict[str, Fraction] = {}
    for r in rows:
        by_reason[r["reason"]] = by_reason.get(
            r["reason"], Fraction(0)
        ) + dbmod.load_fraction(r["weight_bp_exact"])

    if fund is not None and not rows and not _is_reported(conn, quarter, fund):
        # 未报送但被他人持有的基金：现场推导
        derived = _derive_unreported(conn, quarter, fund)
        for (reason, _detail), w in derived.unpenetrated.items():
            by_reason[reason] = by_reason.get(reason, Fraction(0)) + w

    total = sum(by_reason.values(), Fraction(0))
    ordered = [REASON_MISSING, REASON_RESIDUAL, REASON_CLOSED_GROUP]
    return {
        "quarter": quarter,
        "fund_code": fund,
        "by_reason": [
            {
                "reason": reason,
                "reason_label": REASON_LABELS[reason],
                **_weight_obj(by_reason[reason]),
            }
            for reason in ordered
            if reason in by_reason
        ],
        "total": _weight_obj(total),
    }


def list_reports(conn, quarter: str) -> list[dict]:
    rows = conn.execute(
        "SELECT quarter, fund_code, total_bp, row_count, submitted_at "
        "FROM reports WHERE quarter = ? ORDER BY fund_code",
        (quarter,),
    ).fetchall()
    return [dict(r) for r in rows]
