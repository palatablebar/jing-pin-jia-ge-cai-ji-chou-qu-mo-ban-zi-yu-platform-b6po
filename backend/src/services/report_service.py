"""客户只读日报：每站点最新「完整」批次的字段快照 + CSV 导出。

口径：只采纳 ``result_complete=True`` 的批次（正常运行结束或字段全部落库）。
执行中 / 中断 / 被接管前的半成品一律不进入日报，避免早报采用半成品。
若某站点目前没有完整批次，行内 ``status`` 如实标注原因（executing / interrupted），
价格字段保持为空，而不是回退到旧的不完整结果。
"""
from __future__ import annotations

import csv
import io

from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from src.constants.enums import RunStatus
from src.models.run import ExtractionRun
from src.models.site import Site
from src.models.snapshot import Snapshot
from src.models.template import ExtractionTemplate
from src.utils.timeutil import iso_utc, utcnow

CSV_COLUMNS = [
    "站点", "域名", "快照", "采集时间", "商品标题", "标价", "到手价", "库存状态", "F1", "状态", "批次", "归属实例",
]

REPORTABLE_STATUSES = {RunStatus.SUCCESS.value, RunStatus.FAILED.value}


def _field_value(run: ExtractionRun, field: str) -> str | None:
    for result in run.field_results or []:
        if result.get("field") == field:
            return result.get("value")
    return None


def _latest_run(db: Session, template_ids: list[int]) -> ExtractionRun | None:
    return db.scalars(
        select(ExtractionRun)
        .where(ExtractionRun.template_id.in_(template_ids))
        .order_by(desc(ExtractionRun.id))
        .limit(1)
    ).first()


def _latest_complete_run(db: Session, template_ids: list[int]) -> ExtractionRun | None:
    return db.scalars(
        select(ExtractionRun)
        .where(
            ExtractionRun.template_id.in_(template_ids),
            ExtractionRun.result_complete.is_(True),
            ExtractionRun.status.in_(REPORTABLE_STATUSES),
        )
        .order_by(desc(ExtractionRun.finished_at), desc(ExtractionRun.id))
        .limit(1)
    ).first()


def build_rows(db: Session) -> list[dict]:
    rows: list[dict] = []
    sites = db.scalars(select(Site).order_by(Site.batch_priority, Site.id)).all()
    for site in sites:
        template_ids = db.scalars(
            select(ExtractionTemplate.id).where(ExtractionTemplate.site_id == site.id)
        ).all()
        if not template_ids:
            continue
        run = _latest_complete_run(db, list(template_ids))
        if run is not None:
            snapshot = db.get(Snapshot, run.snapshot_id)
            rows.append(
                {
                    "site_id": site.id,
                    "site_name": site.name,
                    "domain": site.domain,
                    "snapshot_label": snapshot.label if snapshot else "",
                    "captured_at": iso_utc(snapshot.captured_at) if snapshot else "",
                    "title": _field_value(run, "title"),
                    "list_price": _field_value(run, "list_price"),
                    "promo_price": _field_value(run, "promo_price"),
                    "stock": _field_value(run, "stock"),
                    "f1_score": round(run.f1_score, 4) if run.f1_score is not None else None,
                    "status": run.status,
                    "run_id": run.id,
                    "execution_owner": run.execution_owner,
                    "result_complete": True,
                    "pending_reason": None,
                }
            )
            continue

        # 没有任何完整批次：如实标注最新批次状态，绝不以半成品字段填充
        pending = _latest_run(db, list(template_ids))
        rows.append(
            {
                "site_id": site.id,
                "site_name": site.name,
                "domain": site.domain,
                "snapshot_label": "",
                "captured_at": "",
                "title": None,
                "list_price": None,
                "promo_price": None,
                "stock": None,
                "f1_score": None,
                "status": pending.status if pending else RunStatus.QUEUED.value,
                "run_id": pending.id if pending else None,
                "execution_owner": pending.execution_owner if pending else None,
                "result_complete": False,
                "pending_reason": (
                    "batch_executing" if pending and pending.status == RunStatus.RUNNING.value
                    else "batch_interrupted" if pending and pending.status == RunStatus.INTERRUPTED.value
                    else "no_complete_run"
                ),
            }
        )
    return rows


def build_report(db: Session) -> dict:
    rows = build_rows(db)
    scored = [row["f1_score"] for row in rows if row["f1_score"] is not None]
    complete = [row for row in rows if row.get("result_complete")]
    return {
        "generated_at": iso_utc(utcnow()),
        "rows": rows,
        "avg_f1": round(sum(scored) / len(scored), 4) if scored else None,
        "complete_rows": len(complete),
        "pending_rows": len(rows) - len(complete),
    }


def export_csv(db: Session) -> str:
    report = build_report(db)
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(CSV_COLUMNS)
    for row in report["rows"]:
        writer.writerow([
            row["site_name"], row["domain"], row["snapshot_label"], row["captured_at"],
            row["title"], row["list_price"], row["promo_price"], row["stock"],
            row["f1_score"], row["status"],
            row["run_id"] if row["run_id"] is not None else "",
            (row["execution_owner"] or "") if row.get("result_complete") else "",
        ])
    return buffer.getvalue()
