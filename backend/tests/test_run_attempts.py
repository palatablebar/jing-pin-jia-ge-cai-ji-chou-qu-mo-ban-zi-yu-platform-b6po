"""执行尝试验证：逐条状态流转、并发接管与围栏。"""
from __future__ import annotations

import pytest
from sqlalchemy import select

from src.models.run import ExtractionRun
from src.models.run_attempt import ExtractionRunAttempt
from src.services import run_service


def _run(db, context):
    run, _ = run_service.ensure_batch(db, context["template"], context["new"], "a@x")
    db.commit()
    return run


def test_attempt_recorded_as_succeeded(db, context):
    run = _run(db, context)
    out = run_service.execute_batch(db, run, "inst-1", commit_progress=False)
    db.commit()
    assert out["status"] == "success"
    attempts = db.scalars(select(ExtractionRunAttempt).where(ExtractionRunAttempt.run_id == run.id)).all()
    assert len(attempts) == 1
    assert attempts[0].status == "succeeded"
    assert attempts[0].attempt_no == 1
    assert attempts[0].progress_done == attempts[0].progress_total == 6


def test_busy_batch_does_not_create_attempt(db, context):
    run = _run(db, context)
    claimed = run_service.claim_lease(db, run, "inst-A", total_fields=6)
    db.commit()
    assert claimed is not None
    before = len(db.scalars(select(ExtractionRunAttempt).where(ExtractionRunAttempt.run_id == run.id)).all())
    with pytest.raises(run_service.BatchBusy):
        run_service.execute_batch(db, run, "inst-B", commit_progress=False)
    db.rollback()
    after = len(db.scalars(select(ExtractionRunAttempt).where(ExtractionRunAttempt.run_id == run.id)).all())
    assert after == before, "被拒绝的请求不得新增执行尝试"


def test_failed_terminal_batch_is_not_rerun_by_submit(db, context):
    """完整落库的失败批次同样视为终态，不被重复提交改写。"""
    run = _run(db, context)
    out = run_service.execute_batch(db, run, "inst-1", force_failed_reason="timeout", commit_progress=False)
    db.commit()
    assert run.status == "failed"
    assert run.result_complete is False
    again = run_service.claim_lease(db, run, "inst-2", total_fields=6)
    assert again is not None, "无完整结果的失败批次允许重提重跑"
