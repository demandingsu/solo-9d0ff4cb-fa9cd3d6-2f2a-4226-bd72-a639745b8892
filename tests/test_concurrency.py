"""并发写入测试：多连接同时报送，写锁串行化后结果必须完整且自洽。

直接走服务层并为每个线程开独立 SQLite 连接（Web 部署中每个请求
本就是独立连接），避免 TestClient 单连接跨线程的限制。
"""

import threading

import pytest

from app import db as dbmod
from app import services
from app.schemas import SubmissionIn


@pytest.fixture()
def temp_db(monkeypatch):
    tmp = __import__("tempfile").TemporaryDirectory()
    path = f"{tmp.name}/concurrency.db"
    monkeypatch.setattr(dbmod, "DEFAULT_DB_PATH", path)
    dbmod.init_db(path)
    yield path
    tmp.cleanup()


def test_concurrent_submissions_are_serialized(temp_db):
    errors = []

    def worker(i):
        conn = dbmod.connect()
        try:
            if i == 0:
                rows = [
                    {"target_type": "security", "target_code": "S0",
                     "weight_bp": 9000},
                ]
            else:
                rows = [
                    {"target_type": "security",
                     "target_code": f"S{i % 3}", "weight_bp": 7000},
                    {"target_type": "fund", "target_code": "F0",
                     "weight_bp": 3000},
                ]
            ack = services.submit_table(
                conn,
                SubmissionIn(quarter="2026Q3", fund_code=f"F{i}", rows=rows),
            )
            assert ack["quarter"] == "2026Q3"
        except Exception as exc:  # noqa: BLE001
            errors.append((i, repr(exc)))
        finally:
            conn.close()

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors

    conn = dbmod.connect()
    try:
        reports = services.list_reports(conn, "2026Q3")
        assert len(reports) == 8
        # 每张结果表合计仍严格 10000，无半提交残片
        for meta in reports:
            p = services.get_penetration(
                conn, "2026Q3", meta["fund_code"]
            )
            assert p["total_bp"] == 10000.0
    finally:
        conn.close()
