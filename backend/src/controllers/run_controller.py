"""批次控制器：提交（幂等） / 接管续跑 / 详情时间线。"""
from __future__ import annotations

from fastapi import Request
from sqlalchemy.orm import Session

from src.services import audit_service, run_service, snapshot_service, template_service
from src.utils.errors import AppError
from src.utils.timeutil import iso_utc


def list_runs(db: Session, site_id: int | None) -> dict:
    return run_service.list_runs(db, site_id)


def get_run(db: Session, run_id: int) -> dict:
    run = run_service.get_run(db, run_id)
    if run is None:
        raise AppError("NOT_FOUND", 404)
    return run_service.serialize_run(db, run, include_attempts=True)


def _load_context(db: Session, template_id: int, snapshot_id: int):
    template = template_service.get_template(db, template_id)
    snapshot = snapshot_service.get_snapshot(db, snapshot_id)
    if template is None or snapshot is None:
        raise AppError("NOT_FOUND", 404)
    if template.site_id != snapshot.site_id:
        raise AppError("CONFLICT", 409, detail="template_site_mismatch")
    return template, snapshot


def execute(db: Session, principal, payload: dict, request: Request) -> dict:
    """提交批次：同一 (模板, 快照) 幂等复用，中断批次由本次请求接管续跑。"""
    template, snapshot = _load_context(db, payload["template_id"], payload["snapshot_id"])
    run, reused = run_service.ensure_batch(
        db, template, snapshot, triggered_by=principal.email
    )
    instance = run_service.instance_id("api")
    try:
        outcome = run_service.execute_batch(db, run, instance)
    except run_service.BatchBusy as busy:
        db.rollback()
        current = run_service.get_run(db, run.id)
        audit_service.record(
            db,
            actor_email=principal.email,
            actor_role=principal.role,
            action="run.contended",
            entity="run",
            entity_id=run.id,
            detail={"owner": busy.owner, "requested_by": principal.email},
            ip=request.client.host if request.client else None,
        )
        db.commit()
        raise AppError(
            "CONFLICT",
            409,
            detail={
                "reason": "batch_busy",
                "run_id": run.id,
                "execution_owner": busy.owner,
                "lease_expires_at": iso_utc(busy.lease_expires_at),
                "progress_done": current.progress_done if current else 0,
                "progress_total": current.progress_total if current else 0,
            },
        ) from None

    audit_service.record(
        db,
        actor_email=principal.email,
        actor_role=principal.role,
        action="run.execute",
        entity="run",
        entity_id=run.id,
        detail={
            "template_id": template.id,
            "version": template.version,
            "snapshot_id": snapshot.id,
            "f1_score": run.f1_score,
            "status": run.status,
            "reused": reused,
            "takeover": outcome.get("takeover", False),
            "resume_from": outcome.get("resume_from", 0),
            "attempt_no": outcome.get("attempt_no", 0),
            "instance": instance,
        },
        ip=request.client.host if request.client else None,
    )
    db.commit()
    data = run_service.serialize_run(db, run, include_attempts=True)
    data["created"] = not reused
    data["reused"] = reused
    data["takeover"] = outcome.get("takeover", False)
    data["outcome"] = outcome.get("status")
    return data


def takeover(db: Session, run_id: int, principal, payload: dict, request: Request) -> dict:
    """显式接管：实例失联后由新实例接手并续跑。未过期批次需 force。"""
    run = run_service.get_run(db, run_id)
    if run is None:
        raise AppError("NOT_FOUND", 404)
    if run.status in run_service.TERMINAL_STATUSES and run.result_complete:
        return run_service.serialize_run(db, run, include_attempts=True)
    instance = run_service.instance_id("api")
    force = bool(payload.get("force"))
    try:
        outcome = run_service.execute_batch(db, run, instance, force_takeover=force)
    except run_service.BatchBusy as busy:
        db.rollback()
        audit_service.record(
            db,
            actor_email=principal.email,
            actor_role=principal.role,
            action="run.takeover_denied",
            entity="run",
            entity_id=run.id,
            detail={"owner": busy.owner},
            ip=request.client.host if request.client else None,
        )
        db.commit()
        raise AppError(
            "CONFLICT",
            409,
            detail={
                "reason": "batch_busy",
                "run_id": run.id,
                "execution_owner": busy.owner,
                "lease_expires_at": iso_utc(busy.lease_expires_at),
            },
        ) from None
    audit_service.record(
        db,
        actor_email=principal.email,
        actor_role=principal.role,
        action="run.takeover",
        entity="run",
        entity_id=run.id,
        detail={
            "instance": instance,
            "outcome": outcome.get("status"),
            "resume_from": outcome.get("resume_from", 0),
            "takeover": outcome.get("takeover", False),
            "attempt_no": outcome.get("attempt_no", 0),
        },
        ip=request.client.host if request.client else None,
    )
    db.commit()
    data = run_service.serialize_run(db, run, include_attempts=True)
    data["created"] = False
    data["reused"] = True
    data["takeover"] = outcome.get("takeover", False)
    data["outcome"] = outcome.get("status")
    return data


def sweep(db: Session) -> dict:
    """扫描心跳超时的执行中批次并标为中断（worker beat 调用；也可手工触发）。"""
    stale = run_service.sweep_stale_batches(db)
    db.commit()
    return {"stale": len(stale), "items": stale}
