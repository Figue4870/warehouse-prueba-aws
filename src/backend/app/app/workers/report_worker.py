"""
REPORT WORKER — genera los reportes pedidos con POST /reports.

PENDING -> PROCESSING -> COMPLETED   (o FAILED si los parámetros no sirven)

Si el worker se cae a mitad de un reporte, el mensaje no tuvo ack y RabbitMQ
lo vuelve a entregar: el reporte se genera de nuevo (no se pierde el pedido).
Si el reporte ya estaba COMPLETED, un mensaje repetido se ignora.
"""

import logging
import time
from datetime import datetime, timezone

from platform_common.messaging import PermanentError

from app.config import settings
from app.database import SessionLocal
from app.modules.reports.models import ReportJobEntity
from app.modules.reports.report_builders import build_report
from app.modules.shared.errors import BusinessRuleError
from app.workers.base import parse_or_permanent, run_consumer

logger = logging.getLogger("warehouse.report_worker")


def make_handler(session_factory, simulated_delay: float = 0.0):
    def handle(body, properties, routing_key):
        event = parse_or_permanent(body)
        job_id = event.payload.get("job_id")
        with session_factory() as db:
            job = db.get(ReportJobEntity, job_id)
            if job is None:
                raise PermanentError(f"El trabajo {job_id} no existe")
            if job.status in ("COMPLETED", "FAILED"):
                return  # repetido: ya terminó
            job.status = "PROCESSING"
            job.started_at = datetime.now(timezone.utc)
            job.attempts = (job.attempts or 0) + 1
            db.commit()

            if simulated_delay:
                time.sleep(simulated_delay)  # solo para "ver" el estado PROCESSING en clase
            try:
                result = build_report(db, job.report_type, job.params or {})
            except BusinessRuleError as error:
                job.status = "FAILED"
                job.error = error.message
                job.finished_at = datetime.now(timezone.utc)
                db.commit()
                logger.warning("Reporte fallido", extra={"event": "report_failed", "context": {"job_id": job_id, "error": error.message}})
                return
            job.status = "COMPLETED"
            job.result = result
            job.error = None
            job.finished_at = datetime.now(timezone.utc)
            db.commit()
            logger.info("Reporte generado", extra={"event": "report_completed", "context": {"job_id": job_id, "rows": len(result.get("rows", []))}})

    return handle


def main() -> None:
    run_consumer("reports", make_handler(SessionLocal, settings.REPORT_SIMULATED_DELAY_SECONDS), "report-worker")


if __name__ == "__main__":
    main()
