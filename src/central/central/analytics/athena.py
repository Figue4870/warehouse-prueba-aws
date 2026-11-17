"""
Consultas de Athena (reporte sencillo sobre lo exportado a S3).

Las consultas viven en analytics/sql/*.sql con dos variables: ${database} y
${days}. Terraform crea con los MISMOS archivos las "named queries" que se ven
en la consola de Athena; la API las ejecuta con el número de días que se pida.

Flujo asíncrono (como los reportes de la Fase 3):
    start()    -> StartQueryExecution      -> id de la ejecución
    status()   -> GetQueryExecution        -> QUEUED | RUNNING | SUCCEEDED | FAILED | CANCELLED
    results()  -> GetQueryResults          -> columnas y filas
"""

from pathlib import Path
from string import Template

from central.config import settings

SQL_DIR = Path(__file__).parent / "sql"

REPORTS = {
    "ventas_por_dia": "Ventas (SHIPMENT) por día y bodega",
    "top_productos": "Los 10 productos más vendidos",
    "stock_actual": "Stock y valor del inventario por bodega (última foto)",
    "movimientos_por_tipo": "Movimientos por bodega y tipo",
    "transferencias": "Transferencias por resultado y duración (Saga)",
}


class AthenaNotConfigured(Exception):
    pass


class UnknownReport(KeyError):
    pass


def render_sql(name: str, database: str, days: int) -> str:
    if name not in REPORTS:
        raise UnknownReport(name)
    template = Template((SQL_DIR / f"{name}.sql").read_text(encoding="utf-8"))
    return template.substitute(database=database, days=int(days))


class AthenaReports:
    def __init__(self, client=None, workgroup: str | None = None, database: str | None = None):
        self.workgroup = workgroup if workgroup is not None else settings.ATHENA_WORKGROUP
        self.database = database or settings.ATHENA_DATABASE
        self._client = client

    @property
    def configured(self) -> bool:
        return bool(self.workgroup)

    @property
    def client(self):
        if not self.configured:
            raise AthenaNotConfigured("Athena no está configurado (ATHENA_WORKGROUP vacío). En AWS lo llena Terraform.")
        if self._client is None:
            import boto3  # solo hace falta en AWS

            self._client = boto3.client("athena", region_name=settings.AWS_DEFAULT_REGION)
        return self._client

    def start(self, name: str, days: int = 30) -> str:
        sql = render_sql(name, self.database, days)
        response = self.client.start_query_execution(
            QueryString=sql, QueryExecutionContext={"Database": self.database}, WorkGroup=self.workgroup)
        return response["QueryExecutionId"]

    def status(self, execution_id: str) -> dict:
        execution = self.client.get_query_execution(QueryExecutionId=execution_id)["QueryExecution"]
        state = execution["Status"]
        stats = execution.get("Statistics", {})
        return {
            "query_execution_id": execution_id,
            "state": state["State"],
            "reason": state.get("StateChangeReason"),
            "submitted_at": state.get("SubmissionDateTime"),
            "completed_at": state.get("CompletionDateTime"),
            "data_scanned_bytes": stats.get("DataScannedInBytes"),
            "engine_ms": stats.get("EngineExecutionTimeInMillis"),
            "output_location": execution.get("ResultConfiguration", {}).get("OutputLocation"),
        }

    def results(self, execution_id: str, max_rows: int | None = None) -> tuple[list[str], list[list]]:
        max_rows = max_rows or settings.ATHENA_MAX_ROWS
        columns: list[str] = []
        rows: list[list] = []
        token = None
        first_page = True
        while len(rows) < max_rows:
            kwargs = {"QueryExecutionId": execution_id, "MaxResults": min(1000, max_rows - len(rows) + (1 if first_page else 0))}
            if token:
                kwargs["NextToken"] = token
            page = self.client.get_query_results(**kwargs)
            result = page["ResultSet"]
            if first_page:
                columns = [c["Name"] for c in result["ResultSetMetadata"]["ColumnInfo"]]
            data = [[cell.get("VarCharValue") for cell in row["Data"]] for row in result["Rows"]]
            if first_page and data:
                data = data[1:]  # en una consulta SELECT, la primera fila son los nombres de las columnas
            rows.extend(data)
            first_page = False
            token = page.get("NextToken")
            if not token:
                break
        return columns, rows[:max_rows]
