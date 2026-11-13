from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from src.models import Base
from src.utils.timeutil import utcnow


class ExtractionRunAttempt(Base):
    """批次执行尝试：每次领取租约生成一条，用于展示接管时间线与围栏审计。

    lease_token 与批次上的 lease_token 同值；提交结果时必须同时匹配，
    旧实例晚到的写入会因为 token 不匹配而被拒绝。
    """

    __tablename__ = "extraction_run_attempts"
    __table_args__ = (UniqueConstraint("run_id", "attempt_no", name="uq_run_attempt"),)

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("extraction_runs.id"), index=True)
    attempt_no: Mapped[int] = mapped_column(Integer, default=1)
    lease_token: Mapped[int] = mapped_column(Integer, default=1)
    instance_id: Mapped[str] = mapped_column(String(120))
    status: Mapped[str] = mapped_column(String(20), default="running", index=True)
    progress_done: Mapped[int] = mapped_column(Integer, default=0)
    progress_total: Mapped[int] = mapped_column(Integer, default=0)
    resumed_from: Mapped[int | None] = mapped_column(Integer, nullable=True)
    message: Mapped[str | None] = mapped_column(String(255), nullable=True)
    started_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
