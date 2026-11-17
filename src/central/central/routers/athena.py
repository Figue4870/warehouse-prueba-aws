"""
Reportes con ATHENA (sobre los archivos que el exportador sube a S3).

    GET  /reports/athena                         -> reportes disponibles y si Athena está configurado
    GET  /reports/athena/{nombre}/sql?days=30    -> el SQL que se ejecutaría (para estudiarlo)
    POST /reports/athena/{nombre}?days=30        -> 202: arranca la consulta en Athena
    GET  /reports/athena/executions/{id}         -> estado y, cuando termina, columnas y filas
    GET  /reports/athena/export-status           -> qué se ha exportado y qué falta
"""

from functools import lru_cache

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import PlainTextResponse
from sqlalchemy.orm import Session

from central.analytics.athena import REPORTS, AthenaNotConfigured, AthenaReports, render_sql
from central.analytics.exporter import export_status
from central.analytics.storage import storage_from_settings
from central.config import settings
from central.database import get_db

router = APIRouter(prefix="/reports/athena", tags=["Reports (Athena)"])


@lru_cache
def get_athena() -> AthenaReports:
    return AthenaReports()


def _not_configured(error: Exception):
    raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(error))


def _check(name: str):
    if name not in REPORTS:
        raise HTTPException(status_code=404, detail=f"Reporte desconocido. Disponibles: {', '.join(REPORTS)}")


@router.get("", summary="Reportes de Athena disponibles")
def list_reports(athena: AthenaReports = Depends(get_athena)):
    return {
        "configured": athena.configured,
        "workgroup": athena.workgroup or None,
        "database": athena.database,
        "export_destination": storage_from_settings().describe(),
        "note": None if athena.configured else "Athena solo existe en AWS (terraform apply). Localmente el exportador escribe en la carpeta exports/.",
        "reports": [{"name": name, "title": title, "sql_url": f"/reports/athena/{name}/sql"} for name, title in REPORTS.items()],
    }


@router.get("/export-status", summary="Exportación a S3: filas exportadas y pendientes")
def get_export_status(db: Session = Depends(get_db)):
    return {"destination": storage_from_settings().describe(), "every_seconds": settings.EXPORT_INTERVAL_SECONDS,
            **export_status(db)}


@router.get("/executions/{execution_id}", summary="Estado de una consulta y sus resultados cuando termina")
def get_execution(execution_id: str, athena: AthenaReports = Depends(get_athena)):
    try:
        info = athena.status(execution_id)
        if info["state"] == "SUCCEEDED":
            columns, rows = athena.results(execution_id)
            info.update({"columns": columns, "rows": rows, "row_count": len(rows)})
        return info
    except AthenaNotConfigured as error:
        _not_configured(error)
    except Exception as error:  # noqa: BLE001 - id inexistente, permisos...
        raise HTTPException(status_code=502, detail=f"Athena respondió con error: {error}")


@router.get("/{name}/sql", response_class=PlainTextResponse, summary="SQL de un reporte")
def get_sql(name: str, days: int = Query(30, ge=1, le=3650), athena: AthenaReports = Depends(get_athena)):
    _check(name)
    return render_sql(name, athena.database, days)


@router.post("/{name}", status_code=status.HTTP_202_ACCEPTED, summary="Ejecutar un reporte en Athena (asíncrono)")
def run_report(name: str, days: int = Query(30, ge=1, le=3650), athena: AthenaReports = Depends(get_athena)):
    _check(name)
    try:
        execution_id = athena.start(name, days)
    except AthenaNotConfigured as error:
        _not_configured(error)
    except Exception as error:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"Athena respondió con error: {error}")
    return {"report": name, "days": days, "query_execution_id": execution_id,
            "status_url": f"/reports/athena/executions/{execution_id}"}
