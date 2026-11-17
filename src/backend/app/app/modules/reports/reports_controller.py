from typing import List, Literal

from fastapi import APIRouter, Depends, Query, Response, status
from sqlalchemy.orm import Session

from app.database import get_db
from app.modules.reports.reports_service import ReportService
from app.modules.reports.schemas import ReportJobAccepted, ReportJobResponse, ReportRequest

router = APIRouter(prefix="/reports", tags=["Reports (asíncronos)"])


@router.post("", response_model=ReportJobAccepted, status_code=status.HTTP_202_ACCEPTED,
             summary="Pedir un reporte: responde 202 de inmediato; lo genera un worker")
def request_report(payload: ReportRequest, response: Response, db: Session = Depends(get_db)):
    job = ReportService(db).request_report(payload)
    status_url = f"/reports/jobs/{job.id}"
    response.headers["Location"] = status_url
    return ReportJobAccepted(job_id=job.id, status="PROCESSING", status_url=status_url)


@router.get("/jobs", response_model=List[ReportJobResponse], summary="Últimos trabajos de reporte")
def list_jobs(limit: int = Query(20, ge=1, le=200), db: Session = Depends(get_db)):
    return ReportService(db).list_jobs(limit)


@router.get("/jobs/{job_id}", response_model=ReportJobResponse, summary="Estado (y resultado) de un reporte")
def get_job(job_id: str, db: Session = Depends(get_db)):
    return ReportService(db).get_job(job_id)


@router.get("/jobs/{job_id}/download", summary="Descargar el reporte terminado (CSV o JSON)")
def download(job_id: str, format: Literal["csv", "json"] = "csv", db: Session = Depends(get_db)):
    service = ReportService(db)
    job = service.get_job(job_id)
    if format == "json":
        return job.result
    content = service.to_csv(job)
    return Response(
        content=content,
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{job.id}.csv"'},
    )
