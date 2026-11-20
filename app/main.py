"""FastAPI 入口：报送与查询接口，启动 /docs 可交互调试。"""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, HTTPException, Query, status
from fastapi.responses import JSONResponse

from . import db as dbmod
from . import services
from .schemas import (
    PenetrationOut,
    ReportMetaOut,
    SecurityWeightOut,
    SubmissionAckOut,
    SubmissionIn,
    UnpenetratedTotalOut,
)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    dbmod.init_db()
    yield


app = FastAPI(
    title="基金多层持仓穿透服务",
    version="1.0.0",
    description=(
        "按季度接收基金持仓表（基点权重，整表 ≤ 10000），"
        "穿透基金互持后合并各路径落到证券的权重；"
        "缺报、未列余额、流入无出口互持组的份额计入未穿透。"
    ),
    lifespan=lifespan,
)


def get_conn():
    conn = dbmod.connect()
    try:
        # 只读查询用 WAL 快照，不阻塞写入，也不会读到未提交数据。
        yield conn
    finally:
        conn.close()


@app.exception_handler(services.ConflictError)
async def _conflict_handler(_request, exc: services.ConflictError):
    return JSONResponse(
        status_code=status.HTTP_409_CONFLICT,
        content={"detail": str(exc)},
    )


@app.exception_handler(services.NotFoundError)
async def _not_found_handler(_request, exc: services.NotFoundError):
    return JSONResponse(
        status_code=status.HTTP_404_NOT_FOUND,
        content={"detail": str(exc)},
    )


@app.get("/health", tags=["运维"])
def health() -> dict:
    return {"status": "ok"}


@app.post(
    "/funds/holdings",
    response_model=SubmissionAckOut,
    status_code=status.HTTP_201_CREATED,
    tags=["报送"],
    summary="报送或替换一张季度持仓表",
)
def submit_holdings(
    payload: SubmissionIn, conn=Depends(get_conn)
) -> dict:
    try:
        return services.submit_table(conn, payload)
    except services.ConflictError:
        raise
    except services.ServiceError as exc:  # pragma: no cover - 防御
        raise HTTPException(status_code=500, detail=str(exc))


@app.get(
    "/quarters/{quarter}/funds/{fund_code}/penetration",
    response_model=PenetrationOut,
    tags=["查询"],
    summary="查询单只基金的完整穿透结果（证券 + 未穿透）",
)
def penetration(
    quarter: str, fund_code: str, conn=Depends(get_conn)
) -> dict:
    return services.get_penetration(conn, quarter, fund_code)


@app.get(
    "/quarters/{quarter}/funds/{fund_code}/securities/{security_code}",
    response_model=SecurityWeightOut,
    tags=["查询"],
    summary="查询单只基金落到某只证券的合并权重",
)
def security_weight(
    quarter: str,
    fund_code: str,
    security_code: str,
    conn=Depends(get_conn),
) -> dict:
    return services.get_security_weight(
        conn, quarter, fund_code, security_code
    )


@app.get(
    "/quarters/{quarter}/unpenetrated",
    response_model=UnpenetratedTotalOut,
    tags=["查询"],
    summary="未穿透总计（可按基金过滤；缺省为全季度总计）",
)
def unpenetrated_total(
    quarter: str,
    fund_code: str | None = Query(
        None, description="基金代码；省略则返回全季度所有报送基金的总计"
    ),
    conn=Depends(get_conn),
) -> dict:
    return services.get_unpenetrated_total(conn, quarter, fund_code)


@app.get(
    "/quarters/{quarter}/reports",
    response_model=list[ReportMetaOut],
    tags=["查询"],
    summary="列出某季度已完整提交的报送表",
)
def list_reports(quarter: str, conn=Depends(get_conn)) -> list[dict]:
    return services.list_reports(conn, quarter)
