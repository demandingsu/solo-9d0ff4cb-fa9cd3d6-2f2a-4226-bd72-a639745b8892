"""Pydantic 请求/响应模型与输入校验。"""

from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator

QUARTER_RE = re.compile(r"^\d{4}Q[1-4]$")
TARGET_FUND = "fund"
TARGET_SECURITY = "security"


class HoldingIn(BaseModel):
    target_type: Literal["fund", "security"] = Field(
        ..., description="对象类别：fund 基金 / security 证券"
    )
    target_code: str = Field(..., min_length=1, max_length=64, description="对象代码")
    weight_bp: int = Field(..., gt=0, description="整数基点权重，必须为正数")

    @field_validator("target_code")
    @classmethod
    def _strip_code(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("代码不能为空")
        return v


class SubmissionIn(BaseModel):
    quarter: str = Field(..., description="季度，格式 YYYYQn，例如 2026Q3")
    fund_code: str = Field(..., min_length=1, max_length=64, description="报送基金代码")
    replace: bool = Field(
        False, description="true 表示替换该基金本季已报送的表，否则重复报送报 409"
    )
    rows: list[HoldingIn] = Field(
        ..., description="持仓行；整表合计至多 10000 基点（允许空表=全未穿透）"
    )

    @field_validator("quarter")
    @classmethod
    def _check_quarter(cls, v: str) -> str:
        if not QUARTER_RE.match(v):
            raise ValueError("季度格式应为 YYYYQn，例如 2026Q3")
        return v

    @field_validator("fund_code")
    @classmethod
    def _strip_fund(cls, v: str) -> str:
        return v.strip()

    @model_validator(mode="after")
    def _check_rows(self) -> "SubmissionIn":
        total = sum(r.weight_bp for r in self.rows)
        if total > 10_000:
            raise ValueError(f"整表合计 {total} 基点，超过上限 10000")
        seen: set[tuple[str, str]] = set()
        for r in self.rows:
            key = (r.target_type, r.target_code)
            if key in seen:
                raise ValueError(
                    f"同一对象重复出现: {r.target_type}:{r.target_code}"
                )
            seen.add(key)
        return self


class WeightOut(BaseModel):
    weight_bp: float = Field(..., description="基点权重（保留 6 位小数）")
    weight_bp_exact: str = Field(..., description="精确分数，如 2500/3")


class SecurityWeightOut(WeightOut):
    security_code: str


class UnpenetratedItemOut(WeightOut):
    reason: Literal["missing", "residual", "closed_group"]
    reason_label: str
    detail: str = Field(..., description="缺报基金代码 / 封闭组成员 / 余额标记")


class ReasonTotalOut(WeightOut):
    reason: str
    reason_label: str


class PenetrationOut(BaseModel):
    quarter: str
    fund_code: str
    status: Literal["reported", "derived_unreported"]
    securities: list[SecurityWeightOut]
    unpenetrated: list[UnpenetratedItemOut]
    unpenetrated_total: WeightOut | None = None
    total_bp: float = Field(..., description="证券+未穿透合计，恒为 10000")


class UnpenetratedTotalOut(BaseModel):
    quarter: str
    fund_code: str | None = Field(
        None, description="为空表示全季度所有已报送基金的总计"
    )
    by_reason: list[ReasonTotalOut]
    total: WeightOut


class SubmissionAckOut(BaseModel):
    quarter: str
    fund_code: str
    row_count: int
    total_bp: int
    replaced: bool
    recomputed_funds: list[str]
    submitted_at: str


class ReportMetaOut(BaseModel):
    quarter: str
    fund_code: str
    total_bp: int
    row_count: int
    submitted_at: str
