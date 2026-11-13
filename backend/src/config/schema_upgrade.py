"""轻量 schema 升级：create_all 不会给已有表补列，这里做幂等补列 + 数据回填。

生产环境应改用 Alembic；本项目为演示，保留 `migrations/0002_batch_ownership.sql`
等价的运行时升级，保证已有 MySQL 数据卷升级后仍可用。

回填逻辑走 Python/ORM 而非 MySQL 专有函数（CONCAT / JSON_LENGTH / 多表 UPDATE），
因此在 MySQL 与 SQLite（测试）上行为一致。
"""
from __future__ import annotations

from sqlalchemy import inspect, select, text
from sqlalchemy.engine import Engine

from src.utils.logger import get_logger

logger = get_logger("schema")

# extraction_runs 新增列（与 models/run.py 保持一致）
RUN_COLUMNS: list[tuple[str, str]] = [
    ("batch_key", "VARCHAR(160) NULL"),
    ("execution_owner", "VARCHAR(120) NULL"),
    ("lease_token", "INT NOT NULL DEFAULT 0"),
    ("lease_expires_at", "DATETIME NULL"),
    ("heartbeat_at", "DATETIME NULL"),
    ("progress_done", "INT NOT NULL DEFAULT 0"),
    ("progress_total", "INT NOT NULL DEFAULT 0"),
    ("result_complete", "TINYINT(1) NOT NULL DEFAULT 0"),
    ("attempt_count", "INT NOT NULL DEFAULT 0"),
]


def _columns(inspector, table: str) -> set[str]:
    return {col["name"] for col in inspector.get_columns(table)}


def _backfill_runs(conn) -> None:
    from src.models.run import ExtractionRun

    rows = conn.execute(
        select(
            ExtractionRun.id,
            ExtractionRun.template_id,
            ExtractionRun.snapshot_id,
            ExtractionRun.batch_key,
            ExtractionRun.status,
            ExtractionRun.field_results,
            ExtractionRun.result_complete,
            ExtractionRun.progress_total,
        ).order_by(ExtractionRun.id)
    ).all()
    if not rows:
        return

    seen: dict[str, int] = {}
    # 倒序优先保留最新一条作为有效批次
    for row in sorted(rows, key=lambda r: r.id, reverse=True):
        key = row.batch_key or f"{row.template_id}:{row.snapshot_id}"
        if key in seen:
            # 历史重复批次：置 NULL，不参与去重（唯一索引允许多个 NULL）
            conn.execute(
                text("UPDATE extraction_runs SET batch_key = NULL WHERE id = :id"), {"id": row.id}
            )
            continue
        seen[key] = row.id
        if row.batch_key != key:
            conn.execute(
                text("UPDATE extraction_runs SET batch_key = :key WHERE id = :id"),
                {"key": key, "id": row.id},
            )

    for row in rows:
        persisted = len(row.field_results or [])
        if row.status in {"success", "failed"} and not row.result_complete:
            conn.execute(
                text(
                    "UPDATE extraction_runs SET result_complete = 1, "
                    "progress_total = CASE WHEN progress_total > :persisted THEN progress_total ELSE :persisted END, "
                    "progress_done = CASE WHEN progress_done > :persisted THEN progress_done ELSE :persisted END "
                    "WHERE id = :id"
                ),
                {"persisted": persisted, "id": row.id},
            )
        elif row.progress_total == 0 and persisted:
            conn.execute(
                text("UPDATE extraction_runs SET progress_total = :persisted WHERE id = :id"),
                {"persisted": persisted, "id": row.id},
            )


def _close_stale_attempts(conn) -> None:
    """升级前遗留的 running attempt：租约 token 落后于批次的标记为 interrupted。"""
    from sqlalchemy import update

    from src.models.run import ExtractionRun
    from src.models.run_attempt import ExtractionRunAttempt

    sub = select(ExtractionRun.lease_token).where(ExtractionRun.id == ExtractionRunAttempt.run_id)
    conn.execute(
        update(ExtractionRunAttempt)
        .where(
            ExtractionRunAttempt.status == "running",
            ExtractionRunAttempt.lease_token < sub.scalar_subquery(),
        )
        .values(status="interrupted")
        .execution_options(synchronize_session=False)
    )


def apply_schema_upgrades(engine: Engine) -> None:
    inspector = inspect(engine)
    if "extraction_runs" not in inspector.get_table_names():
        return
    existing = _columns(inspector, "extraction_runs")
    with engine.begin() as conn:
        for name, ddl in RUN_COLUMNS:
            if name not in existing:
                conn.execute(text(f"ALTER TABLE extraction_runs ADD COLUMN {name} {ddl}"))
                logger.warning("[SCHEMA] added extraction_runs.%s", name)

        _backfill_runs(conn)

        inspector = inspect(conn)
        has_unique_batch_key = any(
            idx.get("unique") and idx.get("column_names") == ["batch_key"]
            for idx in inspector.get_indexes("extraction_runs")
        )
        if not has_unique_batch_key:
            try:
                conn.execute(text("CREATE UNIQUE INDEX uq_run_batch_key ON extraction_runs (batch_key)"))
                logger.warning("[SCHEMA] created unique index uq_run_batch_key")
            except Exception as exc:  # noqa: BLE001 - 已有唯一约束/重复键时告警不阻塞启动
                logger.warning("[SCHEMA] unique index skipped: %s", exc)

        if "extraction_run_attempts" in inspector.get_table_names():
            _close_stale_attempts(conn)
