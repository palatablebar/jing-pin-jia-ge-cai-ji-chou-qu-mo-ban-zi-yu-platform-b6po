"""HTTP 层回归：真实路由 + RBAC + 幂等提交 / 409 / 接管 / 日报完整结果。

用 FastAPI TestClient + SQLite 内存库（替换 get_db 依赖），不依赖 MySQL。
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from scripts.build_snapshots import SITES, build_snapshot_html
from scripts.demo_config import field_rules_for
from src.config.app import create_app
from src.config.database import get_db
from src.constants.enums import AttemptStatus, SiteStatus, TemplateStatus
from src.models import Base
from src.models.run import ExtractionRun
from src.models.run_attempt import ExtractionRunAttempt
from src.models.site import Site
from src.models.snapshot import Snapshot
from src.models.template import ExtractionTemplate
from src.models.user import User
from src.services import run_service, snapshot_service
from src.utils.security import hash_password
from src.utils.timeutil import utcnow


@pytest.fixture()
def client():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool, future=True
    )
    Base.metadata.create_all(bind=engine)
    maker = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False, class_=Session)

    def override_get_db():
        db = maker()
        try:
            yield db
        finally:
            db.close()

    app = create_app()
    app.dependency_overrides[get_db] = override_get_db
    # 跳过启动钩子（lifespan）里的 MySQL 等待 / seed
    app.router.lifespan_context = None

    with maker() as db:
        spec = SITES[2]
        site = Site(name=spec.name, domain=spec.domain, market=spec.market,
                    robots_policy=spec.robots_policy, qps_limit=1.0,
                    status=SiteStatus.ACTIVE.value, batch_priority=3)
        db.add(site)
        db.flush()
        html = build_snapshot_html(spec, "new")
        snap = Snapshot(site_id=site.id, url="u", label="daily", captured_at=utcnow(),
                        html=html, dom_hash="h", byte_size=len(html), is_baseline=False)
        db.add(snap)
        db.flush()
        tpl = ExtractionTemplate(site_id=site.id, name="t", version=1,
                                 status=TemplateStatus.ACTIVE.value,
                                 field_rules=field_rules_for(spec, with_coupon=False),
                                 coverage_score=1.0)
        db.add(tpl)
        for email, role in (("analyst@pricelens.io", "analyst"), ("client@pricelens.io", "client")):
            db.add(User(email=email, password_hash=hash_password("Demo@2026"),
                        display_name=email, role=role))
        db.commit()
        ids = {"template_id": tpl.id, "snapshot_id": snap.id, "run_id": None, "maker": maker}

    tc = TestClient(app, raise_server_exceptions=False)
    tc.ids = ids
    tc.maker = maker
    yield tc
    tc.close()
    engine.dispose()


def _login(tc, email):
    resp = tc.post("/api/auth/login", json={"email": email, "password": "Demo@2026"})
    assert resp.status_code == 200, resp.text
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


def _prepare_run(tc, status="running", lease_lag_seconds=0, done=0):
    """造一条指定归属状态的批次（模拟实例失联现场的落库状态）。"""
    from datetime import timedelta

    from src.services import dom_html, extraction_service

    with tc.maker() as db:
        tpl = db.get(ExtractionTemplate, tc.ids["template_id"])
        snap = db.get(Snapshot, tc.ids["snapshot_id"])
        run, _reused = run_service.ensure_batch(db, tpl, snap, "schedule:06:00")
        rules = tpl.field_rules
        root = dom_html.parse_html(snap.html)
        partial = [extraction_service.extract_field(root, rule) for rule in rules[:done]]
        run.status = status
        run.execution_owner = "worker@crawler-2:lost"
        run.lease_token = 1
        run.lease_expires_at = (
            utcnow() - timedelta(seconds=lease_lag_seconds)
            if lease_lag_seconds
            else utcnow() + timedelta(seconds=60)
        )
        run.heartbeat_at = utcnow() - timedelta(minutes=4)
        run.field_results = partial
        run.progress_done = len(partial)
        run.progress_total = len(rules)
        run.attempt_count = 1
        db.add(ExtractionRunAttempt(run_id=run.id, attempt_no=1, lease_token=1,
                                    instance_id="worker@crawler-2:lost",
                                    status=AttemptStatus.RUNNING.value,
                                    progress_done=len(partial), progress_total=len(rules),
                                    heartbeat_at=utcnow() - timedelta(minutes=4)))
        db.commit()
        return run.id


def test_duplicate_submit_reuses_batch(client):
    headers = _login(client, "analyst@pricelens.io")
    payload = {"template_id": client.ids["template_id"], "snapshot_id": client.ids["snapshot_id"]}

    first = client.post("/api/runs", json=payload, headers=headers)
    assert first.status_code == 200, first.text
    body = first.json()
    assert body["created"] is True and body["reused"] is False
    assert body["status"] == "success"
    assert body["result_complete"] is True
    assert body["progress_done"] == body["progress_total"]
    run_id = body["id"]

    second = client.post("/api/runs", json=payload, headers=headers)
    assert second.status_code == 200, second.text
    assert second.json()["id"] == run_id
    assert second.json()["created"] is False and second.json()["reused"] is True

    listing = client.get("/api/runs", headers=headers).json()
    assert len(listing["items"]) == 1, "重复提交同一批不得新增批次"


def test_active_lease_returns_409(client):
    headers = _login(client, "analyst@pricelens.io")
    run_id = _prepare_run(client, status="running", lease_lag_seconds=0, done=2)
    payload = {"template_id": client.ids["template_id"], "snapshot_id": client.ids["snapshot_id"]}

    resp = client.post("/api/runs", json=payload, headers=headers)
    assert resp.status_code == 409, resp.text
    detail = resp.json()["detail"]
    assert detail["reason"] == "batch_busy"
    assert detail["execution_owner"] == "worker@crawler-2:lost"
    assert detail["run_id"] == run_id

    detail_view = client.get(f"/api/runs/{run_id}", headers=headers).json()
    assert detail_view["execution_owner"] == "worker@crawler-2:lost"
    assert detail_view["progress_done"] == 2
    assert len(detail_view["attempts"]) == 1


def test_expired_lease_is_taken_over_and_resumed(client):
    headers = _login(client, "analyst@pricelens.io")
    run_id = _prepare_run(client, status="running", lease_lag_seconds=5, done=2)

    # 巡检：把心跳超时的批次标记为 interrupted
    swept = client.post("/api/runs/sweep-stale", headers=headers).json()
    assert swept["stale"] == 1

    resp = client.post(f"/api/runs/{run_id}/takeover", json={"force": False}, headers=headers)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["takeover"] is True
    assert body["outcome"] == "success"
    assert body["status"] == "success"
    assert body["result_complete"] is True
    assert body["execution_owner"].startswith("api@")
    assert body["lease_token"] == 2

    attempts = body["attempts"]
    assert [a["status"] for a in attempts] == ["interrupted", "succeeded"]
    assert attempts[1]["resumed_from"] == 2

    report = client.get("/api/reports/daily", headers=_login(client, "client@pricelens.io")).json()
    row = report["rows"][0]
    assert row["result_complete"] is True
    assert row["run_id"] == run_id
    assert row["list_price"], "日报必须来自完整批次的真实字段值"
    assert report["pending_rows"] == 0


def test_client_cannot_read_runs(client):
    resp = client.get("/api/runs", headers=_login(client, "client@pricelens.io"))
    assert resp.status_code == 403
