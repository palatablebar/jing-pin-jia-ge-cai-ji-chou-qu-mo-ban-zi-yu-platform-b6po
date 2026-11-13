"""批次执行归属 / 租约围栏 / 断点续跑 / 日报完整性的核心回归测试。"""
from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import select

from src.models.run import ExtractionRun
from src.models.run_attempt import ExtractionRunAttempt
from src.services import report_service, run_service
from src.utils.timeutil import utcnow


def _make_run(db, context, triggered_by="analyst@pricelens.io"):
    run, reused = run_service.ensure_batch(
        db, context["template"], context["new"], triggered_by=triggered_by
    )
    assert reused is False
    db.commit()
    return run


def test_same_batch_is_idempotent(db, context):
    run1, reused1 = run_service.ensure_batch(db, context["template"], context["new"], "a@x")
    run2, reused2 = run_service.ensure_batch(db, context["template"], context["new"], "b@x")
    db.commit()
    assert reused1 is False
    assert reused2 is True
    assert run1.id == run2.id
    rows = db.scalars(select(ExtractionRun)).all()
    assert len(rows) == 1, "重复提交同一批不得新增批次"
    assert rows[0].batch_key == f"{context['template'].id}:{context['new'].id}"


def test_execute_completes_and_is_idempotent(db, context):
    run = _make_run(db, context)
    outcome = run_service.execute_batch(db, run, "inst-A", commit_progress=False)
    db.commit()
    assert outcome["status"] == "success"
    assert run.result_complete is True
    assert run.progress_done == run.progress_total == 6

    # 再次执行同一批：不产生新 attempt、不改结果
    again = run_service.execute_batch(db, run, "inst-A", commit_progress=False)
    db.commit()
    assert again["status"] == "already_completed"
    assert run.attempt_count == 1
    attempts = db.scalars(select(ExtractionRunAttempt).where(ExtractionRunAttempt.run_id == run.id)).all()
    assert len(attempts) == 1


def test_second_instance_is_rejected_while_lease_active(db, context):
    run = _make_run(db, context)
    claimed = run_service.claim_lease(db, run, "inst-A", total_fields=6)
    db.commit()
    assert claimed is not None

    with pytest.raises(run_service.BatchBusy):
        run_service.claim_lease(db, run, "inst-B", total_fields=6)

    # 有效租约仍在 inst-A 名下，token 未变
    db.refresh(run)
    assert run.execution_owner == "inst-A"
    assert run.lease_token == claimed["lease_token"]


def test_takeover_after_lease_expiry_resumes_from_checkpoint(db, context, monkeypatch):
    run = _make_run(db, context)
    claimed = run_service.claim_lease(db, run, "inst-A", total_fields=6)
    db.commit()

    # 模拟 inst-A 跑了 3 个字段后心跳停滞
    root_batch = run_service.execute_batch if False else None  # noqa: F841 - 保持 impl 只走服务层
    old_token = claimed["lease_token"]
    import src.services.run_service as svc

    attempt = db.get(ExtractionRunAttempt, claimed["attempt_id"])
    from src.services import dom_html, extraction_service

    template = db.get(type(context["template"]), run.template_id)
    root = dom_html.parse_html(context["new"].html)
    partial = [extraction_service.extract_field(root, rule) for rule in template.field_rules[:3]]
    assert svc._write_progress(db, run, old_token, attempt, partial, 3, 6)
    db.commit()
    db.refresh(run)
    assert run.progress_done == 3

    # 心跳超时 -> inst-B 接管
    run.lease_expires_at = utcnow() - timedelta(seconds=5)
    db.commit()
    stale = run_service.sweep_stale_batches(db)
    db.commit()
    assert [item["run_id"] for item in stale] == [run.id]
    db.refresh(run)
    assert run.status == "interrupted"

    outcome = run_service.execute_batch(db, run, "inst-B", force_takeover=True, commit_progress=False)
    db.commit()
    assert outcome["status"] == "success"
    assert outcome["takeover"] is True
    assert outcome["resume_from"] == 3
    db.refresh(run)
    assert run.execution_owner == "inst-B"
    assert run.lease_token == old_token + 1
    assert run.result_complete is True
    assert run.progress_done == 6

    attempts = db.scalars(
        select(ExtractionRunAttempt).where(ExtractionRunAttempt.run_id == run.id).order_by(ExtractionRunAttempt.attempt_no)
    ).all()
    assert [a.status for a in attempts] == ["interrupted", "succeeded"]
    assert [a.resumed_from for a in attempts] == [None, 3]


def test_late_write_from_old_instance_is_fenced(db, context):
    """旧实例晚到结果不能覆盖新实例已写好的终态。"""
    run = _make_run(db, context)
    old = run_service.claim_lease(db, run, "inst-A", total_fields=6)
    db.commit()
    # inst-B 强制接管并完成
    run_service.execute_batch(db, run, "inst-B", force_takeover=True, commit_progress=False)
    db.commit()
    db.refresh(run)
    assert run.result_complete is True
    final_f1 = run.f1_score
    final_owner = run.execution_owner

    # 旧实例 inst-A 拿着旧 token 回来写终态
    import src.services.run_service as svc
    old_attempt = db.get(ExtractionRunAttempt, old["attempt_id"])
    applied = svc._write_terminal(
        db, run, old["lease_token"], old_attempt,
        status="success", results=[], complete=True, duration_ms=1,
        error_message=None, hit_fields=0, missing_fields=0, f1_score=0.0,
    )
    db.commit()
    assert applied is False, "旧 token 写入必须被围栏拒绝"
    db.refresh(run)
    assert run.f1_score == final_f1
    assert run.execution_owner == final_owner
    assert run.result_complete is True
    # 接管时旧 attempt 已标记 superseded；晚到写入不改变它，也不产生新结果
    assert old_attempt.status == "superseded"

    # 旧实例尝试续约会话也失败
    assert svc.renew_lease(db, run, old["lease_token"]) is False


def test_report_uses_only_complete_results(db, context):
    run = _make_run(db, context)
    # 先有一次完整结果
    run_service.execute_batch(db, run, "inst-A", commit_progress=False)
    db.commit()
    rows = report_service.build_rows(db)
    row = next(r for r in rows if r["site_id"] == context["site"].id)
    assert row["result_complete"] is True
    assert row["title"]

    # 新快照批次跑到一半（执行中）：日报不得采用半成品
    from src.models.snapshot import Snapshot
    from src.constants.enums import TemplateStatus
    from src.models.template import ExtractionTemplate

    partial_html = context["new"].html
    snap2 = Snapshot(
        site_id=context["site"].id, url="https://x/item3", label="daily-2", captured_at=utcnow(),
        html=partial_html, dom_hash="h-new2", byte_size=len(partial_html.encode()), is_baseline=False,
    )
    db.add(snap2)
    db.flush()
    run2, _reused = run_service.ensure_batch(db, context["template"], snap2, "sched")
    claimed = run_service.claim_lease(db, run2, "inst-A", total_fields=6)
    db.commit()
    assert claimed["resume_from"] == 0
    # 只写 3 个字段进度，保持在执行中
    import src.services.run_service as svc
    from src.services import dom_html, extraction_service
    template = db.get(ExtractionTemplate, run2.template_id)
    root = dom_html.parse_html(partial_html)
    partial = [extraction_service.extract_field(root, rule) for rule in template.field_rules[:3]]
    attempt = db.get(ExtractionRunAttempt, claimed["attempt_id"])
    assert svc._write_progress(db, run2, claimed["lease_token"], attempt, partial, 3, 6)
    db.commit()

    rows = report_service.build_rows(db)
    row = next(r for r in rows if r["site_id"] == context["site"].id)
    assert row["result_complete"] is True
    assert row["run_id"] == run.id, "日报应继续用上一份完整批次，而不是最新半成品"
    assert row["pending_reason"] is None

    report = report_service.build_report(db)
    assert report["complete_rows"] == 1
    assert report["pending_rows"] == 0


def test_report_flags_site_without_complete_run(db, context):
    # 只有执行中的批次，没有任何完整结果
    run, _reused = run_service.ensure_batch(db, context["template"], context["new"], "sched")
    claimed = run_service.claim_lease(db, run, "inst-A", total_fields=6)
    db.commit()
    attempt = db.get(ExtractionRunAttempt, claimed["attempt_id"])
    import src.services.run_service as svc
    from src.services import dom_html, extraction_service
    root = dom_html.parse_html(context["new"].html)
    partial = [extraction_service.extract_field(root, rule) for rule in context["template"].field_rules[:3]]
    svc._write_progress(db, run, claimed["lease_token"], attempt, partial, 3, 6)
    db.commit()
    db.refresh(run)

    rows = report_service.build_rows(db)
    row = next(r for r in rows if r["site_id"] == context["site"].id)
    assert row["result_complete"] is False
    assert row["title"] is None and row["list_price"] is None
    assert row["status"] == "running"
    assert row["pending_reason"] == "batch_executing"


def test_serialize_exposes_progress_and_attempts(db, context):
    run = _make_run(db, context)
    run_service.execute_batch(db, run, "inst-A", commit_progress=False)
    db.commit()
    data = run_service.serialize_run(db, run, include_attempts=True)
    assert data["result_complete"] is True
    assert data["progress_done"] == data["progress_total"] == 6
    assert data["attempts"][0]["instance_id"] == "inst-A"
    assert data["attempts"][0]["status"] == "succeeded"
    assert data["execution_owner"] == "inst-A"
    assert data["lease_active"] is False
