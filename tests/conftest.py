"""pytest 公共夹具：每个测试用独立临时 SQLite + TestClient。"""

import os
import sys
import tempfile

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app import db as dbmod  # noqa: E402
from app.main import app  # noqa: E402


@pytest.fixture()
def client(monkeypatch):
    tmp = tempfile.TemporaryDirectory()
    db_path = os.path.join(tmp.name, "test.db")
    monkeypatch.setattr(dbmod, "DEFAULT_DB_PATH", db_path)
    dbmod.init_db(db_path)
    with TestClient(app, raise_server_exceptions=False) as c:
        c.db_path = db_path
        yield c
    tmp.cleanup()


def submit(client, quarter, fund, rows, replace=False):
    return client.post(
        "/funds/holdings",
        json={
            "quarter": quarter,
            "fund_code": fund,
            "replace": replace,
            "rows": rows,
        },
    )


def sec_map(payload):
    return {s["security_code"]: s for s in payload["securities"]}


def unp_map(payload):
    return {(u["reason"], u["detail"]): u for u in payload["unpenetrated"]}
