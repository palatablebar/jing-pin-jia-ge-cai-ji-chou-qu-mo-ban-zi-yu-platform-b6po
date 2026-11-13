from __future__ import annotations

from datetime import datetime

from sqlalchemy import JSON, Boolean, DateTime, Float, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from src.models import Base
from src.utils.timeutil import utcnow


class ExtractionRun(Base):
    """一次抽取批次（batch）。

    同一 (template, snapshot) 只允许一条批次（batch_key 唯一），重复提交沿用原批次；
    批次同一时刻只有一个有效执行归属（execution_owner + lease_token 租约 + 围栏令牌）。
    """

    __tablename__ = "extraction_runs"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    template_id: Mapped[int] = mapped_column(ForeignKey("extraction_templates.id"), index=True)
    template_version: Mapped[int] = mapped_column(Integer, default=1)
    snapshot_id: Mapped[int] = mapped_column(ForeignKey("snapshots.id"), index=True)
    batch_key: Mapped[str] = mapped_column(String(160), unique=True, index=True, nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="queued", index=True)
    field_results: Mapped[list] = mapped_column(JSON)
    hit_fields: Mapped[int] = mapped_column(Integer, default=0)
    missing_fields: Mapped[int] = mapped_column(Integer, default=0)
    f1_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    duration_ms: Mapped[int] = mapped_column(Integer, default=0)
    triggered_by: Mapped[str] = mapped_column(String(160), default="seed")
    seed_key: Mapped[str | None] = mapped_column(String(120), unique=True, nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    error_message: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    # ---- 执行归属 / 租约 / 续跑进度（batch execution ownership）----
    execution_owner: Mapped[str | None] = mapped_column(String(120), nullable=True, index=True)
    lease_token: Mapped[int] = mapped_column(Integer, default=0)
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    progress_done: Mapped[int] = mapped_column(Integer, default=0)
    progress_total: Mapped[int] = mapped_column(Integer, default=0)
    result_complete: Mapped[bool] = mapped_column(Boolean, default=False)
    attempt_count: Mapped[int] = mapped_column(Integer, default=0)
