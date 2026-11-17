"""Fase 3: API asíncrona de reportes (202 Accepted + consulta del trabajo)."""

from app.events.models import OutboxEventEntity
from app.workers import report_worker
from platform_common.events import build_event, encode_event


def test_pedir_reporte_responde_202_y_deja_el_evento(client, session_factory):
    response = client.post("/reports", json={"type": "LOW_STOCK", "params": {"threshold": 3}})
    assert response.status_code == 202
    body = response.json()
    assert body["status"] == "PROCESSING" and body["job_id"].startswith("JOB-")
    assert response.headers["Location"] == f"/reports/jobs/{body['job_id']}"
    with session_factory() as db:
        [event] = db.query(OutboxEventEntity).filter(OutboxEventEntity.event_type == "REPORT_REQUESTED").all()
    assert event.payload["job_id"] == body["job_id"] and event.payload["params"]["threshold"] == 3
    assert client.get(body["status_url"]).json()["status"] == "PENDING"


def test_validaciones(client):
    assert client.post("/reports", json={"type": "INVENTADO"}).status_code == 422
    bad_range = {"type": "MOVEMENTS_SUMMARY", "params": {"date_from": "2026-12-01", "date_to": "2026-11-01"}}
    assert client.post("/reports", json=bad_range).status_code == 422
    assert client.get("/reports/jobs/JOB-NO-EXISTE").status_code == 404


def test_descargar_csv_solo_cuando_esta_listo(client, create_product, move, session_factory):
    product = create_product(sku="CSV-1")
    move(product["id"], "RECEIPT", 1)
    job_id = client.post("/reports", json={"type": "LOW_STOCK"}).json()["job_id"]
    assert client.get(f"/reports/jobs/{job_id}/download").status_code == 409  # aún PENDING

    body = encode_event(build_event("REPORT_REQUESTED", "warehouse-WH-TEST", {"job_id": job_id}))
    report_worker.make_handler(session_factory)(body, None, "")
    csv_response = client.get(f"/reports/jobs/{job_id}/download")
    assert csv_response.status_code == 200
    assert csv_response.text.splitlines()[0] == "sku,name,available,reserved,supplier"
    assert "CSV-1" in csv_response.text
    assert client.get(f"/reports/jobs/{job_id}/download", params={"format": "json"}).json()["rows"][0]["sku"] == "CSV-1"
