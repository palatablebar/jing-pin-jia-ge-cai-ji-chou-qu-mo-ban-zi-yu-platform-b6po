from __future__ import annotations

from pydantic import BaseModel


class ReportRow(BaseModel):
    site_id: int
    site_name: str
    domain: str
    snapshot_label: str
    captured_at: str
    title: str | None
    list_price: str | None
    promo_price: str | None
    stock: str | None
    f1_score: float | None
    status: str
    run_id: int | None = None
    execution_owner: str | None = None
    result_complete: bool = True
    pending_reason: str | None = None


class ReportOut(BaseModel):
    generated_at: str
    rows: list[ReportRow]
    avg_f1: float | None
    complete_rows: int = 0
    pending_rows: int = 0
