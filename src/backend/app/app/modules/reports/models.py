from sqlalchemy import CheckConstraint, Column, DateTime, Integer, String, Text

from app.database import Base
from app.events.models import JsonType
from app.modules.shared.utils import schemas_utils

JOB_STATUSES = ("PENDING", "PROCESSING", "COMPLETED", "FAILED")


class ReportJobEntity(Base):
    """
    Un reporte pedido con POST /reports. El resultado se guarda AQUÍ (en la base
    de datos) y no en el disco del contenedor: así cualquier instancia de la API
    puede responder GET /reports/jobs/{id} (la API sigue siendo stateless).
    """

    __tablename__ = "report_jobs"
    __table_args__ = (
        CheckConstraint("status IN ('PENDING', 'PROCESSING', 'COMPLETED', 'FAILED')", name="ck_report_jobs_status"),
    )

    id = Column(String(40), primary_key=True)  # JOB-20261117-1A2B3C4D
    warehouse_id = Column(String(20), nullable=False)
    report_type = Column(String(40), nullable=False)
    params = Column(JsonType, nullable=False)
    status = Column(String(20), nullable=False, default="PENDING")
    result = Column(JsonType)
    error = Column(Text)
    attempts = Column(Integer, nullable=False, default=0)
    correlation_id = Column(String(80))
    requested_at = Column(DateTime(timezone=True), nullable=False, default=schemas_utils.current_utc_time)
    started_at = Column(DateTime(timezone=True))
    finished_at = Column(DateTime(timezone=True))
