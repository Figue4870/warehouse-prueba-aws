"""Reportes con Athena: exportación a S3 (sin duplicados) y consultas (Athena simulado con botocore Stubber)."""

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import boto3
import pytest
from botocore.stub import ANY, Stubber

from central.analytics.athena import REPORTS, AthenaReports, render_sql
from central.analytics.exporter import TABLES, Exporter, export_dataset, movement_row, transfer_row
from central.analytics.storage import LocalStorage, S3Storage
from central.api import app
from central.models import ExportBatchEntity, MovementHistoryEntity, TransferEntity
from central.routers.athena import get_athena


def read_jsonl(base: Path, dataset: str) -> dict[str, list[dict]]:
    files = {}
    for path in sorted((base / dataset).glob("dt=*/*.json")):
        files[f"{path.parent.name}/{path.name}"] = [json.loads(line) for line in path.read_text().splitlines()]
    return files


@pytest.fixture
def movements(apply, central_sf, wh1, wh2):
    apply(wh1.product(), wh1.movement(quantity=10, after=10), wh1.movement(m_type="SHIPMENT", quantity=3, after=7),
          wh2.movement(quantity=5, after=5))
    with central_sf() as db:  # uno de los movimientos ocurrió AYER (otra partición)
        row = db.query(MovementHistoryEntity).filter(MovementHistoryEntity.warehouse_id == "WH-002").one()
        row.occurred_at = datetime.now(timezone.utc) - timedelta(days=1)
        db.commit()


def test_exporta_jsonl_particionado_por_dia_y_sin_repetir(movements, central_sf, apply, wh1, tmp_path):
    exporter = Exporter(central_sf, LocalStorage(tmp_path), snapshot_seconds=3600)
    result = exporter.run_once()
    assert result["movements"] == 3 and result["inventory"] == 2
    files = read_jsonl(tmp_path, "movements")
    days = {name.split("/")[0] for name in files}
    assert len(days) == 2                                     # hoy y ayer: dos particiones dt=
    assert sum(len(rows) for rows in files.values()) == 3
    shipment = next(r for rows in files.values() for r in rows if r["type"] == "SHIPMENT")
    assert shipment["unit_retail_price"] == "100.00" and shipment["occurred_at"].endswith("Z")

    assert exporter.run_once() == {"movements": 0, "transfers": 0}   # nada nuevo (y la foto aún no toca)
    apply(wh1.movement(m_type="SHIPMENT", quantity=1, after=6))
    assert exporter.run_once()["movements"] == 1


def test_si_s3_falla_el_lote_se_vuelve_a_subir_con_el_mismo_nombre(movements, central_sf, tmp_path):
    class FlakyStorage(LocalStorage):
        fail = True

        def put(self, key, body):
            if self.fail:
                raise ConnectionError("S3 no responde (simulado)")
            super().put(key, body)

    storage = FlakyStorage(tmp_path)
    with pytest.raises(ConnectionError):
        export_dataset(central_sf, storage, "movements")
    with central_sf() as db:
        batch = db.query(ExportBatchEntity).one()
        assert batch.uploaded_at is None and "S3 no responde" in batch.last_error
    storage.fail = False
    assert export_dataset(central_sf, storage, "movements") == 3   # el MISMO lote
    assert all(batch.batch_id in name for name in read_jsonl(tmp_path, "movements"))
    assert export_dataset(central_sf, storage, "movements") == 0
    assert sum(len(r) for r in read_jsonl(tmp_path, "movements").values()) == 3   # sin duplicados


def test_solo_se_exportan_transferencias_terminadas(central_sf, tmp_path):
    now = datetime.now(timezone.utc)
    with central_sf() as db:
        for i, state in enumerate(("COMPLETED", "IN_TRANSIT", "RESERVATION_RELEASED")):
            db.add(TransferEntity(transfer_id=f"TR-{i:06d}", sku="P-001", source_warehouse_id="WH-001",
                                  destination_warehouse_id="WH-002", quantity=5, state=state, timeout_seconds=60,
                                  created_at=now - timedelta(seconds=30), updated_at=now,
                                  finished_at=now if state != "IN_TRANSIT" else None))
        db.commit()
    assert export_dataset(central_sf, LocalStorage(tmp_path), "transfers") == 2
    rows = [r for rows in read_jsonl(tmp_path, "transfers").values() for r in rows]
    assert {r["final_state"] for r in rows} == {"COMPLETED", "RESERVATION_RELEASED"}
    assert all(29 <= r["duration_seconds"] <= 31 for r in rows)


def test_las_filas_tienen_exactamente_las_columnas_de_athena(movements, central_sf):
    with central_sf() as db:
        movement = db.query(MovementHistoryEntity).first()
        assert list(movement_row(movement)) == [c["name"] for c in TABLES["movements"]["columns"]]
    transfer = TransferEntity(transfer_id="TR-1", sku="X", source_warehouse_id="A", destination_warehouse_id="B", quantity=1,
                              state="COMPLETED", timeout_seconds=5, created_at=datetime.now(timezone.utc))
    assert list(transfer_row(transfer)) == [c["name"] for c in TABLES["transfers"]["columns"]]


def test_s3_put_object(monkeypatch):
    client = boto3.client("s3", region_name="us-east-1", aws_access_key_id="x", aws_secret_access_key="y")
    with Stubber(client) as stub:
        stub.add_response("put_object", {"ETag": '"abc"'}, {"Bucket": "datos", "Key": "movements/dt=2026-11-16/l.json",
                                                            "Body": ANY, "ContentType": "application/x-ndjson"})
        S3Storage("datos", client).put("movements/dt=2026-11-16/l.json", b'{"a": 1}\n')
        stub.assert_no_pending_responses()


def test_sql_de_cada_reporte(central_client):
    for name in REPORTS:
        sql = render_sql(name, "warehouse_analytics", 7)
        assert '"warehouse_analytics".' in sql and "-7" in sql and "${" not in sql
    response = central_client.get("/reports/athena/ventas_por_dia/sql", params={"days": 15})
    assert response.status_code == 200 and "date_add('day', -15, current_date)" in response.text
    assert central_client.get("/reports/athena/no_existe/sql").status_code == 404


def test_sin_athena_configurado_responde_503(central_client):
    app.dependency_overrides[get_athena] = lambda: AthenaReports(workgroup="", database="db")
    try:
        assert central_client.get("/reports/athena").json()["configured"] is False
        assert central_client.post("/reports/athena/top_productos").status_code == 503
    finally:
        app.dependency_overrides.pop(get_athena)


def test_reporte_de_athena_de_punta_a_punta(central_client):
    client = boto3.client("athena", region_name="us-east-1", aws_access_key_id="x", aws_secret_access_key="y")
    stub = Stubber(client)
    stub.add_response("start_query_execution", {"QueryExecutionId": "q-123"},
                      {"QueryString": render_sql("top_productos", "analytics", 30),
                       "QueryExecutionContext": {"Database": "analytics"}, "WorkGroup": "reportes"})
    stub.add_response("get_query_execution", {"QueryExecution": {
        "QueryExecutionId": "q-123", "Status": {"State": "SUCCEEDED"},
        "Statistics": {"DataScannedInBytes": 2048, "EngineExecutionTimeInMillis": 900},
        "ResultConfiguration": {"OutputLocation": "s3://datos/athena-results/q-123.csv"}}}, {"QueryExecutionId": "q-123"})
    columns = [{"Name": n, "Type": "varchar"} for n in ("sku", "producto", "unidades_vendidas")]
    stub.add_response("get_query_results", {"ResultSet": {
        "ResultSetMetadata": {"ColumnInfo": columns},
        "Rows": [{"Data": [{"VarCharValue": "sku"}, {"VarCharValue": "producto"}, {"VarCharValue": "unidades_vendidas"}]},
                 {"Data": [{"VarCharValue": "P-001"}, {"VarCharValue": "Casco"}, {"VarCharValue": "42"}]}]}},
        {"QueryExecutionId": "q-123", "MaxResults": ANY})
    stub.activate()
    app.dependency_overrides[get_athena] = lambda: AthenaReports(client=client, workgroup="reportes", database="analytics")
    try:
        started = central_client.post("/reports/athena/top_productos", params={"days": 30})
        assert started.status_code == 202 and started.json()["query_execution_id"] == "q-123"
        result = central_client.get(started.json()["status_url"]).json()
        assert result["state"] == "SUCCEEDED" and result["data_scanned_bytes"] == 2048
        assert result["columns"] == ["sku", "producto", "unidades_vendidas"]
        assert result["rows"] == [["P-001", "Casco", "42"]]          # sin la fila de encabezados
        stub.assert_no_pending_responses()
    finally:
        app.dependency_overrides.pop(get_athena)


def test_estado_de_la_exportacion(movements, central_sf, central_client, tmp_path):
    assert central_client.get("/reports/athena/export-status").json()["movements"]["pending_rows"] == 3
    Exporter(central_sf, LocalStorage(tmp_path)).run_once()
    status = central_client.get("/reports/athena/export-status").json()
    assert status["movements"] == {"pending_rows": 0, "exported_rows": 3}


def test_tablero_web(central_client):
    page = central_client.get("/dashboard")
    assert page.status_code == 200 and "Plataforma Central" in page.text and "/transfers" in page.text
