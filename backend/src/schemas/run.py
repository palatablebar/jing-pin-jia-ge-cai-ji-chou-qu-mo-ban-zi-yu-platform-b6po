from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel


class RunCreateIn(BaseModel):
    template_id: int
    snapshot_id: int


class TakeoverIn(BaseModel):
    force: bool = False


class FieldResult(BaseModel):
    field: str
    label: str
    type: str
    value: str | None
    raw: str | None
    status: str
    similarity: float
    matched_selector: str | None
    snippet: str | None


class RunAttemptOut(BaseModel):
    id: int
    attempt_no: int
    lease_token: int
    instance_id: str
    status: str
    progress_done: int
    progress_total: int
    resumed_from: int | None
    message: str | None
    started_at: datetime | None
    heartbeat_at: datetime | None
    finished_at: datetime | None


class RunOut(BaseModel):
    id: int
    template_id: int
    template_name: str
    template_version: int
    site_id: int
    site_name: str
    snapshot_id: int
    status: str
    hit_fields: int
    missing_fields: int
    f1_score: float | None
    duration_ms: int
    triggered_by: str
    started_at: datetime | None
    finished_at: datetime | None
    error_message: str | None
    created_at: datetime
    field_results: list[dict[str, Any]]
    execution_owner: str | None = None
    lease_token: int = 0
    lease_active: bool = False
    lease_expires_at: datetime | None = None
    heartbeat_at: datetime | None = None
    progress_done: int = 0
    progress_total: int = 0
    result_complete: bool = False
    attempt_count: int = 0
    attempts: list[RunAttemptOut] = []


class RunActionOut(RunOut):
    created: bool = False
    reused: bool = False
    takeover: bool = False
    outcome: str | None = None


class RunListOut(BaseModel):
    items: list[RunOut]
    total: int
    trend: list[dict[str, Any]]
