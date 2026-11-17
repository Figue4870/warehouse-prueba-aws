r"""
REPORTE SENCILLO CON ATHENA desde tu PC (a través de la API central).

La central (en AWS) tiene permiso para usar Athena con el rol del Learner Lab;
este script solo le pide el reporte, espera el resultado y lo muestra.

    python scripts/athena_report.py --list
    python scripts/athena_report.py ventas_por_dia --central http://<ip-central>:8090
    python scripts/athena_report.py top_productos --days 7 --csv top.csv
    python scripts/athena_report.py stock_actual --sql      # solo muestra el SQL

La IP de la central sale de:  terraform output central
Requiere: pip install httpx
"""

import argparse
import csv
import sys
import time

import httpx


def print_table(columns, rows):
    widths = [max(len(str(c)), *(len(str(r[i] or "")) for r in rows)) if rows else len(str(c)) for i, c in enumerate(columns)]
    print("  ".join(str(c).ljust(w) for c, w in zip(columns, widths)))
    print("  ".join("-" * w for w in widths))
    for row in rows:
        print("  ".join(str(v if v is not None else "").ljust(w) for v, w in zip(row, widths)))


def main() -> int:
    parser = argparse.ArgumentParser(description="Reportes de Athena a través de la API central")
    parser.add_argument("report", nargs="?", help="ventas_por_dia | top_productos | stock_actual | movimientos_por_tipo | transferencias")
    parser.add_argument("--central", default="http://localhost:8090")
    parser.add_argument("--days", type=int, default=30)
    parser.add_argument("--csv", help="guardar el resultado en este archivo CSV")
    parser.add_argument("--sql", action="store_true", help="mostrar el SQL y salir")
    parser.add_argument("--list", action="store_true", help="listar los reportes")
    parser.add_argument("--timeout", type=int, default=120)
    args = parser.parse_args()

    client = httpx.Client(base_url=args.central, timeout=30)
    try:
        info = client.get("/reports/athena").json()
    except httpx.HTTPError as error:
        print(f"No se pudo hablar con la central en {args.central}: {error}")
        print("¿Está encendida? En AWS usa la IP de: terraform output central")
        return 1
    if args.list or not args.report:
        print(f"Athena configurado: {info['configured']}  ·  datos en: {info['export_destination']}")
        for report in info["reports"]:
            print(f"  {report['name']:<22} {report['title']}")
        return 0
    if args.sql:
        print(client.get(f"/reports/athena/{args.report}/sql", params={"days": args.days}).text)
        return 0

    started = client.post(f"/reports/athena/{args.report}", params={"days": args.days})
    if started.status_code != 202:
        print(f"Error {started.status_code}: {started.json().get('detail')}")
        return 1
    status_url = started.json()["status_url"]
    print(f"Consulta enviada a Athena: {started.json()['query_execution_id']}")
    deadline = time.time() + args.timeout
    while time.time() < deadline:
        result = client.get(status_url).json()
        state = result.get("state")
        if state == "SUCCEEDED":
            scanned = result.get("data_scanned_bytes")
            print(f"Listo · {result['row_count']} filas · {scanned} bytes leídos · {result.get('engine_ms')} ms\n")
            print_table(result["columns"], result["rows"])
            if args.csv:
                with open(args.csv, "w", newline="", encoding="utf-8") as handle:
                    writer = csv.writer(handle)
                    writer.writerow(result["columns"])
                    writer.writerows(result["rows"])
                print(f"\nGuardado en {args.csv}")
            return 0
        if state in ("FAILED", "CANCELLED") or state is None:
            print(f"La consulta terminó en {state}: {result.get('reason') or result.get('detail')}")
            return 1
        print(f"  {state}...")
        time.sleep(2)
    print("Se acabó el tiempo de espera")
    return 1


if __name__ == "__main__":
    sys.exit(main())
