import csv
import io
import logging
import uuid
from datetime import datetime, timezone

from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from platform_common.correlation import get_correlation_id

from app.config import settings
from app.events import types as events
from app.events.outbox import OutboxWriter
from app.modules.reports.models import ReportJobEntity
from app.modules.reports.schemas import ReportRequest

logger = logging.getLogger("warehouse.reports")


def new_job_id() -> str:
    return f"JOB-{datetime.now(timezone.utc):%Y%m%d}-{uuid.uuid4().hex[:8].upper()}"


class ReportService:
    def __init__(self, db: Session):
        self.db = db
        self.outbox = OutboxWriter(db)

    def request_report(self, request: ReportRequest) -> ReportJobEntity:
        """
        Crea el trabajo y su evento REPORT_REQUESTED en la MISMA transacción.
        La API responde enseguida (202 Accepted); el report-worker hace el trabajo.
        Aunque RabbitMQ esté caído, el pedido NO se pierde: queda en el outbox.
        """
        params = request.params.model_dump(mode="json")
        job = ReportJobEntity(
            id=new_job_id(),
            warehouse_id=settings.WAREHOUSE_ID,
            report_type=request.type.value,
            params=params,
            status="PENDING",
            attempts=0,
            correlation_id=get_correlation_id(),
        )
        self.db.add(job)
        self.outbox.add(events.REPORT_REQUESTED, "report_job", job.id,
                        {"job_id": job.id, "report_type": job.report_type, "params": params})
        self.db.commit()
        self.db.refresh(job)
        logger.info("Reporte solicitado", extra={"event": "report_requested", "context": {"job_id": job.id, "report_type": job.report_type}})
        return job

    def get_job(self, job_id: str) -> ReportJobEntity:
        job = self.db.get(ReportJobEntity, job_id)
        if job is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Trabajo de reporte no encontrado")
        return job

    def list_jobs(self, limit: int):
        return self.db.query(ReportJobEntity).order_by(ReportJobEntity.requested_at.desc()).limit(limit).all()

    @staticmethod
    def to_csv(job: ReportJobEntity) -> str:
        if job.status != "COMPLETED" or not job.result:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=f"El reporte aún no está listo (estado: {job.status})")
        columns = job.result.get("columns") or []
        buffer = io.StringIO()
        writer = csv.DictWriter(buffer, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        for row in job.result.get("rows", []):
            writer.writerow(row)
        return buffer.getvalue()
