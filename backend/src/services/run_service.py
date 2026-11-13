"""抽取批次：执行归属 / 租约 / 断点续跑 / 历史 / F1 趋势。

并发语义
--------
- 批次身份：``batch_key = "{template_id}:{snapshot_id}"``（唯一索引）。同一批重复提交
  只会沿用已有批次，不会新增行。
- 执行归属：批次同一时刻只有一个有效租约（``lease_token`` + ``execution_owner`` +
  ``lease_expires_at``）。领取租约是一次 ``lease_token`` 比较并交换（CAS）更新，
  即使没有行锁也只有一方能成功。
- 围栏：所有进度 / 终态写入都带 ``lease_token`` 条件。旧实例租约被接管后再写入会
  影响 0 行，直接放弃并把自己的 attempt 记为 ``rejected``（若已被标记 superseded /
  interrupted 则保留该状态，只补说明），绝不覆盖新实例结果。
- 续跑：进度按字段落库（``progress_done`` + 已抽字段结果），每次写入立即提交。
  接管时从 ``min(progress_done, len(field_results))`` 继续，已完成字段不重复抽取。
"""
from __future__ import annotations

import socket
import time
import uuid
from datetime import timedelta

from sqlalchemy import desc, func, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from src.config.settings import settings
from src.constants.enums import AttemptStatus, RunStatus
from src.models.run import ExtractionRun
from src.models.run_attempt import ExtractionRunAttempt
from src.models.site import Site
from src.models.snapshot import Snapshot
from src.models.template import ExtractionTemplate
from src.services import dom_html, extraction_service
from src.utils.timeutil import iso_utc, utcnow

TERMINAL_STATUSES = {RunStatus.SUCCESS.value, RunStatus.FAILED.value}


class BatchBusy(Exception):
    """批次已被另一个实例持有有效租约。"""

    def __init__(self, owner: str | None, lease_expires_at):
        self.owner = owner
        self.lease_expires_at = lease_expires_at
        super().__init__("batch_busy")


def batch_key_for(template_id: int, snapshot_id: int) -> str:
    return f"{template_id}:{snapshot_id}"


def instance_id(prefix: str = "api") -> str:
    return f"{prefix}@{socket.gethostname()}:{uuid.uuid4().hex[:6]}"


def _lease_window() -> timedelta:
    return timedelta(seconds=max(5, settings.run_lease_seconds))


# ---------------------------------------------------------------------------
# 批次身份：创建 / 复用
# ---------------------------------------------------------------------------
def ensure_batch(
    db: Session,
    template: ExtractionTemplate,
    snapshot: Snapshot,
    triggered_by: str,
    seed_key: str | None = None,
) -> tuple[ExtractionRun, bool]:
    """取或建批次；重复提交同一批返回 ``reused=True``，行数不增加。"""
    key = batch_key_for(template.id, snapshot.id)
    existing = db.scalar(select(ExtractionRun).where(ExtractionRun.batch_key == key))
    if existing is not None:
        return existing, True

    run = ExtractionRun(
        template_id=template.id,
        template_version=template.version,
        snapshot_id=snapshot.id,
        batch_key=key,
        status=RunStatus.QUEUED.value,
        field_results=[],
        triggered_by=triggered_by,
        seed_key=seed_key,
        progress_total=len(template.field_rules or []),
        result_complete=False,
    )
    try:
        with db.begin_nested():
            db.add(run)
            db.flush()
    except IntegrityError:
        # 并发提交同一批：另一请求先插入；回滚 savepoint 后沿用已有行
        existing = db.scalar(select(ExtractionRun).where(ExtractionRun.batch_key == key))
        if existing is None:
            raise
        return existing, True
    return run, False


# ---------------------------------------------------------------------------
# 租约：领取 / 续约 / 过期
# ---------------------------------------------------------------------------
def _latest_attempt(db: Session, run_id: int, token: int | None = None) -> ExtractionRunAttempt | None:
    stmt = select(ExtractionRunAttempt).where(ExtractionRunAttempt.run_id == run_id)
    if token is not None:
        stmt = stmt.where(ExtractionRunAttempt.lease_token == token)
    return db.scalars(stmt.order_by(desc(ExtractionRunAttempt.attempt_no)).limit(1)).first()


def claim_lease(
    db: Session,
    run: ExtractionRun,
    instance: str,
    force: bool = False,
    total_fields: int | None = None,
) -> dict | None:
    """领取执行租约。

    - 批次已完整结束 -> 返回 ``None``（幂等，无需再跑）
    - 有效租约被他人持有且未 force -> 抛 ``BatchBusy``
    - 过期 / 中断 / 失败 -> 递增 lease_token 接管，返回续跑进度
    """
    total = total_fields if total_fields is not None else len(run.field_results or [])
    for _ in range(6):
        # 每轮都重新读取最新已提交状态（MySQL REPEATABLE READ 下需 FOR UPDATE 才能看到
        # 其他实例已提交的新 token / owner；SQLite 无行锁，退化为普通 SELECT）
        db.refresh(run, with_for_update=True)
        now = utcnow()
        token = run.lease_token
        # 已完整落库的终态批次不再重复执行；失败且无完整结果（如超时）允许重提重跑
        if run.status in TERMINAL_STATUSES and run.result_complete:
            return None
        lease_active = (
            run.status == RunStatus.RUNNING.value
            and run.lease_expires_at is not None
            and run.lease_expires_at > now
        )
        if lease_active and not force:
            raise BatchBusy(run.execution_owner, run.lease_expires_at)

        resumable = (
            run.status in {RunStatus.RUNNING.value, RunStatus.INTERRUPTED.value}
            and not run.result_complete
            and run.progress_done > 0
        )
        # 只能从真正落库的字段结果续跑：progress_done 与 field_results 取较小值
        persisted = len(run.field_results or [])
        resume_from = min(run.progress_done, persisted, total) if resumable else 0
        takeover = bool(
            lease_active
            or force
            or run.status in {RunStatus.RUNNING.value, RunStatus.INTERRUPTED.value}
            or (run.attempt_count or 0) > 0
        )
        attempt_no = (run.attempt_count or 0) + 1
        new_token = token + 1
        values = {
            "lease_token": new_token,
            "execution_owner": instance,
            "lease_expires_at": now + _lease_window(),
            "heartbeat_at": now,
            "status": RunStatus.RUNNING.value,
            "attempt_count": attempt_no,
            "progress_done": resume_from,
            "progress_total": total,
            "result_complete": False,
            "finished_at": None,
            "error_message": None,
        }
        if resume_from == 0:
            # 全新尝试（或失败重跑）：清空旧结果，重新开始
            values["field_results"] = []
            values["started_at"] = now
        result = db.execute(
            update(ExtractionRun)
            .where(ExtractionRun.id == run.id, ExtractionRun.lease_token == token)
            .values(**values)
            .execution_options(synchronize_session=False)
        )
        if result.rowcount == 1:
            previous = _latest_attempt(db, run.id, token)
            if previous is not None and previous.status == AttemptStatus.RUNNING.value:
                previous.status = AttemptStatus.SUPERSEDED.value
                previous.finished_at = now
                previous.message = previous.message or f"租约被 {instance} 接管"
            attempt = ExtractionRunAttempt(
                run_id=run.id,
                attempt_no=attempt_no,
                lease_token=new_token,
                instance_id=instance,
                status=AttemptStatus.RUNNING.value,
                progress_done=resume_from,
                progress_total=total,
                resumed_from=resume_from if resume_from else None,
                heartbeat_at=now,
            )
            db.add(attempt)
            db.flush()
            db.refresh(run)
            return {
                "lease_token": new_token,
                "attempt_id": attempt.id,
                "attempt_no": attempt_no,
                "resume_from": resume_from,
                "takeover": takeover,
            }
        # CAS 失败：别的实例改过 lease_token，下一轮重新读取最新状态后再判定
    raise BatchBusy(run.execution_owner, run.lease_expires_at)


def renew_lease(db: Session, run: ExtractionRun, token: int) -> bool:
    """续约心跳；租约已被接管时返回 False（围栏拒绝）。"""
    res = db.execute(
        update(ExtractionRun)
        .where(ExtractionRun.id == run.id, ExtractionRun.lease_token == token)
        .values(
            lease_expires_at=utcnow() + _lease_window(),
            heartbeat_at=utcnow(),
        )
        .execution_options(synchronize_session=False)
    )
    return res.rowcount == 1


def _write_progress(
    db: Session,
    run: ExtractionRun,
    token: int,
    attempt: ExtractionRunAttempt,
    results: list[dict],
    done: int,
    total: int,
) -> bool:
    """围栏写入逐字段进度；返回 False 表示租约已丢失。"""
    now = utcnow()
    res = db.execute(
        update(ExtractionRun)
        .where(ExtractionRun.id == run.id, ExtractionRun.lease_token == token)
        .values(
            field_results=results,
            progress_done=done,
            progress_total=total,
            status=RunStatus.RUNNING.value,
            lease_expires_at=now + _lease_window(),
            heartbeat_at=now,
        )
        .execution_options(synchronize_session=False)
    )
    if res.rowcount != 1:
        return False
    attempt.progress_done = done
    attempt.progress_total = total
    attempt.heartbeat_at = now
    db.flush()
    return True


def _write_terminal(
    db: Session,
    run: ExtractionRun,
    token: int,
    attempt: ExtractionRunAttempt,
    *,
    status: str,
    results: list[dict],
    complete: bool,
    duration_ms: int,
    error_message: str | None,
    hit_fields: int,
    missing_fields: int,
    f1_score: float | None,
) -> bool:
    """围栏写入最终结果；返回 False 表示旧实例晚到、租约已被接管。"""
    now = utcnow()
    total = len(results)
    res = db.execute(
        update(ExtractionRun)
        .where(ExtractionRun.id == run.id, ExtractionRun.lease_token == token)
        .values(
            status=status,
            field_results=results,
            hit_fields=hit_fields,
            missing_fields=missing_fields,
            f1_score=f1_score,
            duration_ms=duration_ms,
            error_message=error_message,
            result_complete=complete,
            progress_done=total if complete else attempt.progress_done,
            progress_total=max(total, run.progress_total),
            finished_at=now,
            lease_expires_at=None,
            heartbeat_at=now,
        )
        .execution_options(synchronize_session=False)
    )
    if res.rowcount != 1:
        return False
    attempt.status = AttemptStatus.SUCCEEDED.value if status == RunStatus.SUCCESS.value else AttemptStatus.FAILED.value
    attempt.progress_done = len(results) if complete else attempt.progress_done
    attempt.finished_at = now
    attempt.heartbeat_at = now
    db.flush()
    db.refresh(run)
    return True


def _reject_attempt(attempt: ExtractionRunAttempt, reason: str) -> None:
    # 已因接管被标记 superseded / interrupted 的尝试保留原状态，只补充说明
    if attempt.status not in {AttemptStatus.SUPERSEDED.value, AttemptStatus.INTERRUPTED.value}:
        attempt.status = AttemptStatus.REJECTED.value
    attempt.message = reason[:255]
    if attempt.finished_at is None:
        attempt.finished_at = utcnow()


# ---------------------------------------------------------------------------
# 执行（断点续跑）
# ---------------------------------------------------------------------------
def execute_batch(
    db: Session,
    run: ExtractionRun,
    instance: str,
    *,
    force_takeover: bool = False,
    force_failed_reason: str | None = None,
    step_delay_ms: int | None = None,
    commit_progress: bool = True,
) -> dict:
    """执行 / 接管一个批次。返回 ``{"status": ..., "takeover": bool, ...}``。

    ``commit_progress=True``（生产语义）：领取租约、每写完一个字段、写终态都立刻提交，
    这样其他实例与页面能看到真实进度，实例崩溃时已完成的字段也不会被回滚。
    """
    template = db.get(ExtractionTemplate, run.template_id)
    snapshot = db.get(Snapshot, run.snapshot_id)
    rules = (template.field_rules if template else []) or []
    total = len(rules)

    claimed = claim_lease(db, run, instance, force=force_takeover, total_fields=total)
    if claimed is None:
        return {"status": "already_completed", "run_id": run.id, "takeover": False}
    if commit_progress:
        db.commit()
    token = claimed["lease_token"]
    attempt = db.get(ExtractionRunAttempt, claimed["attempt_id"])
    started = run.started_at or utcnow()
    clock = time.perf_counter()
    delay = settings.run_step_delay_ms if step_delay_ms is None else step_delay_ms

    def _lost() -> dict:
        _reject_attempt(attempt, "lease_lost: 租约已被新实例接管，晚到结果丢弃")
        db.flush()
        if commit_progress:
            db.commit()
        return {
            "status": "lease_lost",
            "run_id": run.id,
            "takeover": claimed["takeover"],
            "attempt_no": claimed["attempt_no"],
        }

    if force_failed_reason:
        applied = _write_terminal(
            db, run, token, attempt,
            status=RunStatus.FAILED.value,
            results=[],
            complete=False,
            duration_ms=30000,
            error_message=force_failed_reason,
            hit_fields=0,
            missing_fields=total,
            f1_score=None,
        )
        if not applied:
            return _lost()
        if commit_progress:
            db.commit()
        return {"status": run.status, "run_id": run.id, "takeover": claimed["takeover"], "attempt_no": claimed["attempt_no"]}

    if snapshot is None:
        applied = _write_terminal(
            db, run, token, attempt,
            status=RunStatus.FAILED.value,
            results=[],
            complete=False,
            duration_ms=0,
            error_message="snapshot_not_found",
            hit_fields=0,
            missing_fields=total,
            f1_score=None,
        )
        if not applied:
            return _lost()
        if commit_progress:
            db.commit()
        return {"status": run.status, "run_id": run.id, "takeover": claimed["takeover"], "attempt_no": claimed["attempt_no"]}

    if snapshot.byte_size > settings.max_snapshot_bytes:
        applied = _write_terminal(
            db, run, token, attempt,
            status=RunStatus.FAILED.value,
            results=[],
            complete=False,
            duration_ms=0,
            error_message=f"snapshot_too_large(>{settings.max_snapshot_bytes // 1024}KB)",
            hit_fields=0,
            missing_fields=total,
            f1_score=None,
        )
        if not applied:
            return _lost()
        if commit_progress:
            db.commit()
        return {"status": run.status, "run_id": run.id, "takeover": claimed["takeover"], "attempt_no": claimed["attempt_no"]}

    resume_from = claimed["resume_from"]
    results = [dict(item) for item in (run.field_results or [])][:resume_from] if resume_from else []
    root = dom_html.parse_html(snapshot.html)
    aborted = False
    for index in range(resume_from, total):
        results.append(extraction_service.extract_field(root, rules[index]))
        if not _write_progress(db, run, token, attempt, results, index + 1, total):
            aborted = True
            break
        if commit_progress:
            db.commit()
        if delay > 0:
            time.sleep(delay / 1000.0)

    if aborted:
        return _lost()

    f1, _detail = extraction_service.score_f1(results, total)
    extracted = sum(1 for item in results if item.get("value"))
    duration_ms = max(1, int((time.perf_counter() - clock) * 1000)) + 12
    status = RunStatus.SUCCESS.value if extracted else RunStatus.FAILED.value
    error_message = None if extracted else "all_fields_miss（页面无可解析商品节点）"
    applied = _write_terminal(
        db, run, token, attempt,
        status=status,
        results=results,
        complete=True,
        duration_ms=duration_ms,
        error_message=error_message,
        hit_fields=sum(1 for item in results if item["status"] == "hit"),
        missing_fields=sum(1 for item in results if item["status"] == "miss"),
        f1_score=f1,
    )
    if not applied:
        return _lost()
    if commit_progress:
        db.commit()
    return {
        "status": run.status,
        "run_id": run.id,
        "takeover": claimed["takeover"],
        "attempt_no": claimed["attempt_no"],
        "resume_from": resume_from,
    }


def execute_run(
    db: Session,
    template: ExtractionTemplate,
    snapshot: Snapshot,
    triggered_by: str,
    seed_key: str | None = None,
    force_failed_reason: str | None = None,
) -> ExtractionRun:
    """兼容旧调用（seed / 自愈复跑）：取或建批次并同步执行到底。

    由调用方统一提交事务（commit_progress=False），避免在 seed 的单事务里提前落盘。
    """
    run, _reused = ensure_batch(db, template, snapshot, triggered_by=triggered_by, seed_key=seed_key)
    execute_batch(
        db, run, instance_id("seed"),
        force_failed_reason=force_failed_reason,
        commit_progress=False,
    )
    return run


# ---------------------------------------------------------------------------
# 过期 / 中断检测（供 worker beat 扫）
# ---------------------------------------------------------------------------
def sweep_stale_batches(db: Session) -> list[dict]:
    """把租约过期的执行中批次标记为中断，返回需要新实例接管的批次。"""
    now = utcnow()
    stale = db.scalars(
        select(ExtractionRun).where(
            ExtractionRun.status == RunStatus.RUNNING.value,
            ExtractionRun.result_complete.is_(False),
            or_(
                ExtractionRun.lease_expires_at.is_(None),
                ExtractionRun.lease_expires_at < now,
            ),
        )
    ).all()
    outcomes: list[dict] = []
    for run in stale:
        attempt = _latest_attempt(db, run.id)
        if attempt is not None and attempt.status == AttemptStatus.RUNNING.value:
            attempt.status = AttemptStatus.INTERRUPTED.value
            attempt.message = f"实例 {attempt.instance_id} 心跳超时"
            attempt.finished_at = now
        old_token = run.lease_token
        res = db.execute(
            update(ExtractionRun)
            .where(ExtractionRun.id == run.id, ExtractionRun.lease_token == old_token)
            .values(status=RunStatus.INTERRUPTED.value)
            .execution_options(synchronize_session=False)
        )
        if res.rowcount == 1:
            outcomes.append(
                {
                    "run_id": run.id,
                    "owner": run.execution_owner,
                    "progress_done": run.progress_done,
                    "progress_total": run.progress_total,
                }
            )
    db.flush()
    return outcomes


# ---------------------------------------------------------------------------
# 序列化 / 查询
# ---------------------------------------------------------------------------
def _attempt_payload(attempt: ExtractionRunAttempt) -> dict:
    return {
        "id": attempt.id,
        "attempt_no": attempt.attempt_no,
        "lease_token": attempt.lease_token,
        "instance_id": attempt.instance_id,
        "status": attempt.status,
        "progress_done": attempt.progress_done,
        "progress_total": attempt.progress_total,
        "resumed_from": attempt.resumed_from,
        "message": attempt.message,
        "started_at": iso_utc(attempt.started_at),
        "heartbeat_at": iso_utc(attempt.heartbeat_at),
        "finished_at": iso_utc(attempt.finished_at),
    }


def serialize_run(db: Session, run: ExtractionRun, include_attempts: bool = False) -> dict:
    template = db.get(ExtractionTemplate, run.template_id)
    snapshot = db.get(Snapshot, run.snapshot_id)
    site = db.get(Site, template.site_id) if template else None
    now = utcnow()
    lease_active = (
        run.status == RunStatus.RUNNING.value
        and run.lease_expires_at is not None
        and run.lease_expires_at > now
    )
    payload = {
        "id": run.id,
        "template_id": run.template_id,
        "template_name": template.name if template else "",
        "template_version": run.template_version,
        "site_id": site.id if site else 0,
        "site_name": site.name if site else "",
        "snapshot_id": run.snapshot_id,
        "snapshot_label": snapshot.label if snapshot else "",
        "status": run.status,
        "hit_fields": run.hit_fields,
        "missing_fields": run.missing_fields,
        "f1_score": round(run.f1_score, 4) if run.f1_score is not None else None,
        "duration_ms": run.duration_ms,
        "triggered_by": run.triggered_by,
        "started_at": iso_utc(run.started_at),
        "finished_at": iso_utc(run.finished_at),
        "error_message": run.error_message,
        "created_at": iso_utc(run.created_at),
        "field_results": run.field_results or [],
        "execution_owner": run.execution_owner,
        "lease_token": run.lease_token,
        "lease_active": lease_active,
        "lease_expires_at": iso_utc(run.lease_expires_at),
        "heartbeat_at": iso_utc(run.heartbeat_at),
        "progress_done": run.progress_done,
        "progress_total": run.progress_total,
        "result_complete": run.result_complete,
        "attempt_count": run.attempt_count,
    }
    if include_attempts:
        attempts = db.scalars(
            select(ExtractionRunAttempt)
            .where(ExtractionRunAttempt.run_id == run.id)
            .order_by(ExtractionRunAttempt.attempt_no)
        ).all()
        payload["attempts"] = [_attempt_payload(a) for a in attempts]
    return payload


def get_run(db: Session, run_id: int) -> ExtractionRun | None:
    return db.get(ExtractionRun, run_id)


def list_runs(db: Session, site_id: int | None = None, limit: int = 40) -> dict:
    stmt = select(ExtractionRun).order_by(desc(ExtractionRun.id)).limit(limit)
    if site_id:
        template_ids = db.scalars(select(ExtractionTemplate.id).where(ExtractionTemplate.site_id == site_id)).all()
        if not template_ids:
            return {"items": [], "total": 0, "trend": []}
        stmt = (
            select(ExtractionRun)
            .where(ExtractionRun.template_id.in_(template_ids))
            .order_by(desc(ExtractionRun.id))
            .limit(limit)
        )
    runs = db.scalars(stmt).all()
    items = [serialize_run(db, run) for run in runs]
    total = db.scalar(select(func.count()).select_from(ExtractionRun)) or 0
    trend = [
        {
            "run_id": item["id"],
            "site_name": item["site_name"],
            "version": item["template_version"],
            "f1_score": item["f1_score"],
            "created_at": item["finished_at"] or item["created_at"],
            "status": item["status"],
        }
        for item in sorted(items, key=lambda x: (x["finished_at"] or ""))
        if item["f1_score"] is not None and item["result_complete"]
    ]
    return {"items": items, "total": total, "trend": trend}


def field_stability(db: Session, site_id: int, field: str) -> tuple[float, int]:
    """stability = 该站点该字段在历史完整批次中的正确率（跨模板版本）。"""
    runs = db.scalars(
        select(ExtractionRun)
        .join(ExtractionTemplate, ExtractionTemplate.id == ExtractionRun.template_id)
        .where(
            ExtractionTemplate.site_id == site_id,
            ExtractionRun.result_complete.is_(True),
        )
        .order_by(ExtractionRun.id)
    ).all()
    total = 0
    hit = 0
    for run in runs:
        for result in run.field_results or []:
            if result.get("field") == field:
                total += 1
                if result.get("status") == "hit":
                    hit += 1
                break
    if total == 0:
        return 0.5, 0
    return round(hit / total, 4), total


def stability_map(db: Session, site_id: int, field_rules: list[dict]) -> dict[str, float]:
    return {rule["field"]: field_stability(db, site_id, rule["field"])[0] for rule in field_rules}


def history_counts(db: Session, site_id: int, field_rules: list[dict]) -> dict[str, int]:
    return {rule["field"]: field_stability(db, site_id, rule["field"])[1] for rule in field_rules}
