"""并发语义：跨 session 的重复提交与围栏（模拟两个独立实例/请求）。"""
from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from scripts.build_snapshots import SITES, build_snapshot_html
from scripts.demo_config import field_rules_for
from src.constants.enums import SiteStatus, TemplateStatus
from src.models import Base
from src.models.run import ExtractionRun
from src.models.run_attempt import ExtractionRunAttempt
from src.models.site import Site
from src.models.snapshot import Snapshot
from src.models.template import ExtractionTemplate
from src.services import run_service
from src.utils.timeutil import utcnow


@pytest.fixture()
def engine():
    eng = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool, future=True
    )
    Base.metadata.create_all(bind=eng)
    yield eng
    eng.dispose()


@pytest.fixture()
def ids(engine):
    maker = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False, class_=Session)
    with maker() as db:
        spec = SITES[1]
        site = Site(name=spec.name, domain=spec.domain, market=spec.market,
                    robots_policy=spec.robots_policy, qps_limit=1.0,
                    status=SiteStatus.ACTIVE.value, batch_priority=2)
        db.add(site)
        db.flush()
        html = build_snapshot_html(spec, "new")
        snap = Snapshot(site_id=site.id, url="u", label="d", captured_at=utcnow(),
                        html=html, dom_hash="h", byte_size=len(html), is_baseline=False)
        db.add(snap)
        db.flush()
        tpl = ExtractionTemplate(site_id=site.id, name="t", version=1,
                                 status=TemplateStatus.ACTIVE.value,
                                 field_rules=field_rules_for(spec, with_coupon=False),
                                 coverage_score=1.0)
        db.add(tpl)
        db.commit()
        return {"template_id": tpl.id, "snapshot_id": snap.id}


def test_two_sessions_submitting_same_batch_create_one_row(engine, ids):
    maker = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False, class_=Session)
    with maker() as sess_a:
        tpl = sess_a.get(ExtractionTemplate, ids["template_id"])
        snap = sess_a.get(Snapshot, ids["snapshot_id"])
        run_a, reused_a = run_service.ensure_batch(sess_a, tpl, snap, "a@x")
        sess_a.commit()

    with maker() as sess_b:
        tpl = sess_b.get(ExtractionTemplate, ids["template_id"])
        snap = sess_b.get(Snapshot, ids["snapshot_id"])
        run_b, reused_b = run_service.ensure_batch(sess_b, tpl, snap, "b@x")
        sess_b.commit()

    assert reused_a is False
    assert reused_b is True
    assert run_a.id == run_b.id
    with maker() as check:
        assert len(check.scalars(select(ExtractionRun)).all()) == 1


def test_cas_claim_only_one_instance_wins(engine, ids):
    maker = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False, class_=Session)
    with maker() as sess:
        tpl = sess.get(ExtractionTemplate, ids["template_id"])
        snap = sess.get(Snapshot, ids["snapshot_id"])
        run, _ = run_service.ensure_batch(sess, tpl, snap, "a@x")
        sess.commit()
        run_id = run.id

    # 两个会话都读到 token=0 的同一行，先后领取
    with maker() as sess_a, maker() as sess_b:
        run_a = sess_a.get(ExtractionRun, run_id)
        run_b = sess_b.get(ExtractionRun, run_id)
        first = run_service.claim_lease(sess_a, run_a, "inst-A", total_fields=6)
        sess_a.commit()
        assert first is not None
        with pytest.raises(run_service.BatchBusy):
            run_service.claim_lease(sess_b, run_b, "inst-B", total_fields=6)

    # 租约过期后同一行可被接管，token 单调递增
    with maker() as sess:
        run = sess.get(ExtractionRun, run_id)
        run.lease_expires_at = utcnow() - timedelta(seconds=1)
        sess.commit()
    with maker() as sess:
        run = sess.get(ExtractionRun, run_id)
        second = run_service.claim_lease(sess, run, "inst-B", force=True, total_fields=6)
        sess.commit()
        assert second["lease_token"] == first["lease_token"] + 1

    with maker() as check:
        attempts = check.scalars(
            select(ExtractionRunAttempt).where(ExtractionRunAttempt.run_id == run_id)
        ).all()
        assert [a.instance_id for a in attempts] == ["inst-A", "inst-B"]


def test_old_instance_cannot_advance_after_takeover(engine, ids):
    """inst-A 在 inst-B 接管后继续跑：每一步写进度都被围栏拒绝。"""
    maker = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False, class_=Session)
    with maker() as sess:
        tpl = sess.get(ExtractionTemplate, ids["template_id"])
        snap = sess.get(Snapshot, ids["snapshot_id"])
        run, _ = run_service.ensure_batch(sess, tpl, snap, "a@x")
        sess.commit()
        run_id = run.id

    with maker() as sess_a:
        run_a = sess_a.get(ExtractionRun, run_id)
        claim_a = run_service.claim_lease(sess_a, run_a, "inst-A", total_fields=6)
        sess_a.commit()
        token_a = claim_a["lease_token"]
        attempt_a_id = claim_a["attempt_id"]

    # inst-B 接管（A 的租约被 force 收回）
    with maker() as sess_b:
        run_b = sess_b.get(ExtractionRun, run_id)
        claim_b = run_service.claim_lease(sess_b, run_b, "inst-B", force=True, total_fields=6)
        sess_b.commit()
        assert claim_b["lease_token"] == token_a + 1

    # A 拿着旧 token 继续写
    with maker() as sess_a:
        run_a = sess_a.get(ExtractionRun, run_id)
        attempt_a = sess_a.get(ExtractionRunAttempt, attempt_a_id)
        assert run_service._write_progress(sess_a, run_a, token_a, attempt_a, [{"field": "x"}], 1, 6) is False
        sess_a.rollback()

    with maker() as check:
        run = check.get(ExtractionRun, run_id)
        assert run.progress_done == 0
        assert run.execution_owner == "inst-B"
        # A 的 attempt 已在接管时标记 superseded
        attempt_a = check.get(ExtractionRunAttempt, attempt_a_id)
        assert attempt_a.status == "superseded"
