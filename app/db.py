"""SQLite 持久化层。

写入（报送/替换）在单个事务内完成「替换报送表 -> 重算受影响基金 ->
刷新结果」，任何一步失败整个事务回滚，旧结果继续可读。结果表只在
提交后对外可见，天然满足「查询只读到完整提交的结果」。
"""

from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager
from fractions import Fraction
from typing import Iterator

DEFAULT_DB_PATH = os.environ.get(
    "FUND_DB_PATH", "/data/fund_holdings.db"
)

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;
PRAGMA busy_timeout=5000;

CREATE TABLE IF NOT EXISTS reports (
    quarter      TEXT NOT NULL,
    fund_code    TEXT NOT NULL,
    total_bp     INTEGER NOT NULL,
    row_count    INTEGER NOT NULL,
    submitted_at TEXT NOT NULL,
    PRIMARY KEY (quarter, fund_code)
);

CREATE TABLE IF NOT EXISTS holdings (
    quarter      TEXT NOT NULL,
    fund_code    TEXT NOT NULL,
    target_type  TEXT NOT NULL CHECK (target_type IN ('fund', 'security')),
    target_code  TEXT NOT NULL,
    weight_bp    INTEGER NOT NULL CHECK (weight_bp > 0),
    PRIMARY KEY (quarter, fund_code, target_type, target_code),
    FOREIGN KEY (quarter, fund_code) REFERENCES reports (quarter, fund_code)
        ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS fund_results (
    quarter           TEXT NOT NULL,
    fund_code         TEXT NOT NULL,
    security_code     TEXT NOT NULL,
    weight_bp_exact   TEXT NOT NULL,
    PRIMARY KEY (quarter, fund_code, security_code)
);

CREATE TABLE IF NOT EXISTS unpenetrated_results (
    quarter           TEXT NOT NULL,
    fund_code         TEXT NOT NULL,
    reason            TEXT NOT NULL,
    detail            TEXT NOT NULL,
    weight_bp_exact   TEXT NOT NULL,
    PRIMARY KEY (quarter, fund_code, reason, detail)
);

CREATE INDEX IF NOT EXISTS idx_holdings_quarter
    ON holdings (quarter, fund_code);
CREATE INDEX IF NOT EXISTS idx_results_security
    ON fund_results (quarter, security_code);
"""


def connect(db_path: str | None = None) -> sqlite3.Connection:
    path = db_path or DEFAULT_DB_PATH
    parent = os.path.dirname(os.path.abspath(path))
    os.makedirs(parent, exist_ok=True)
    conn = sqlite3.connect(path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def init_db(db_path: str | None = None) -> None:
    conn = connect(db_path)
    try:
        conn.executescript(SCHEMA)
        conn.commit()
    finally:
        conn.close()


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise


# ---- 精确分数的文本序列化 ----------------------------------------------

def dump_fraction(value: Fraction) -> str:
    return f"{value.numerator}/{value.denominator}"


def load_fraction(text: str) -> Fraction:
    a, b = text.split("/")
    return Fraction(int(a), int(b))
