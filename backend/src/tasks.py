"""Celery 任务：worker 消费（心跳、全站巡检、批次执行、中断扫描与接管）。"""
from __future__ import annotations

import time

import redis

from src.config.celery_app import celery_app
from src.config.settings import settings
from src.utils.logger import get_logger

logger = get_logger("tasks")


def _redis():
    return redis.Redis.from_url(settings.redis_url, decode_responses=True)


@celery_app.task(name="pricelens.heartbeat")
def heartbeat() -> str:
    try:
        _redis().set("pricelens:worker:heartbeat", time.time())
        return "ok"
    except Exception as exc:  # noqa: BLE001
        logger.warning("heartbeat failed: %s", exc)
        return "error"


@celery_app.task(name="pricelens.scan_site")
def scan_site(site_id: int) -> dict:
    """后台巡检：对站点最新快照重算漂移并 upsert 告警（worker 侧任务）。"""
    from src.config.database import session_scope
    from src.constants.enums import TemplateStatus
    from src.models.snapshot import Snapshot
    from src.models.template import ExtractionTemplate
    from src.services import snapshot_service
    from sqlalchemy import desc, select

    with session_scope() as db:
        template = db.scalars(
            select(ExtractionTemplate)
            .where(ExtractionTemplate.site_id == site_id, ExtractionTemplate.status == TemplateStatus.ACTIVE.value)
        ).first()
        latest = db.scalars(
            select(Snapshot).where(Snapshot.site_id == site_id).order_by(desc(Snapshot.captured_at)).limit(1)
        ).first()
        if template is None or latest is None:
            return {"status": "skipped"}
        alert = snapshot_service.upsert_alert(db, latest, template)
        return {"status": "ok", "alert_id": alert.id, "drift_score": alert.drift_score}


@celery_app.task(name="pricelens.scan_all_sites")
def scan_all_sites() -> dict:
    from src.config.database import session_scope
    from src.models.site import Site
    from sqlalchemy import select

    with session_scope() as db:
        site_ids = db.scalars(select(Site.id)).all()
    results = [scan_site(site_id) for site_id in site_ids]
    return {"scanned": len(results)}


@celery_app.task(name="pricelens.run_extraction")
def run_extraction(run_id: int) -> dict:
    """执行指定批次；租约被占用则让出，过期/中断则由本 worker 接管续跑。"""
    from src.config.database import session_scope
    from src.services import run_service

    with session_scope() as db:
        run = run_service.get_run(db, run_id)
        if run is None:
            return {"run_id": run_id, "status": "not_found"}
        try:
            outcome = run_service.execute_batch(db, run, run_service.instance_id("worker"))
        except run_service.BatchBusy:
            return {"run_id": run_id, "status": "busy"}
        return {"run_id": run_id, **outcome}


@celery_app.task(name="pricelens.sweep_stale_runs")
def sweep_stale_runs() -> dict:
    """把心跳超时的执行中批次标为中断，并立即由当前 worker 实例接管续跑。"""
    from src.config.database import session_scope
    from src.services import run_service

    with session_scope() as db:
        stale = run_service.sweep_stale_batches(db)
    resumed: list[dict] = []
    for item in stale:
        resumed.append(run_extraction(item["run_id"]))
    return {"stale": len(stale), "resumed": resumed}
