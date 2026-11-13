"""测试夹具：SQLite 内存库 + 确定性快照。不依赖 MySQL / Redis。"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from scripts.build_snapshots import SITES, build_snapshot_html  # noqa: E402
from scripts.demo_config import field_rules_for  # noqa: E402
from src.models import Base  # noqa: E402
from src.models.site import Site  # noqa: E402
from src.models.snapshot import Snapshot  # noqa: E402
from src.models.template import ExtractionTemplate  # noqa: E402
from src.constants.enums import SiteStatus, TemplateStatus  # noqa: E402


@pytest.fixture()
def db():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
        future=True,
    )
    Base.metadata.create_all(bind=engine)
    maker = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False, class_=Session)
    session = maker()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


@pytest.fixture()
def context(db):
    """一个站点 + 生效模板 + baseline/new 快照（与 seed 同源的确定性 HTML）。"""
    spec = SITES[0]
    site = Site(
        name=spec.name,
        domain=spec.domain,
        market=spec.market,
        robots_policy=spec.robots_policy,
        qps_limit=spec.qps_limit,
        status=SiteStatus.ACTIVE.value,
        batch_priority=spec.priority,
    )
    db.add(site)
    db.flush()
    baseline_html = build_snapshot_html(spec, "baseline")
    new_html = build_snapshot_html(spec, "new")
    baseline = Snapshot(
        site_id=site.id, url="https://x/item", label="baseline", captured_at=None,
        html=baseline_html, dom_hash="h-base", byte_size=len(baseline_html.encode()), is_baseline=True,
    )
    from src.utils.timeutil import utcnow
    baseline.captured_at = utcnow()
    new = Snapshot(
        site_id=site.id, url="https://x/item2", label="daily", captured_at=utcnow(),
        html=new_html, dom_hash="h-new", byte_size=len(new_html.encode()), is_baseline=False,
    )
    template = ExtractionTemplate(
        site_id=site.id,
        name=f"{spec.name} · 商品详情",
        version=1,
        status=TemplateStatus.ACTIVE.value,
        field_rules=field_rules_for(spec, with_coupon=False),
        coverage_score=0.82,
    )
    db.add_all([baseline, new, template])
    db.flush()
    return {"site": site, "template": template, "baseline": baseline, "new": new}
