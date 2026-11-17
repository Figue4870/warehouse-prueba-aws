r"""
DEMO FASE 7 — "nunca se pierde información", con la plataforma REAL en Docker.

Repite, paso a paso, los experimentos de docs/fases/FASE_7.md:

  1. Inventario global (ejemplo del README: 100 + 50 + 80 = 230)
  2. La CENTRAL se cae: las bodegas siguen vendiendo y todo llega cuando vuelve
  3. Una BODEGA queda aislada: la central la marca OFFLINE y avisa
  4. Se PIERDEN mensajes a propósito (se vacía la cola central): el reconciliador los recupera
  5. Se BORRA la base central completa: se reconstruye sola desde las bodegas
  6. Llega un evento DUPLICADO 3 veces: se cuenta una sola
  7. Alternativa A (consulta en vivo) vs B (modelo de lectura), con y sin una bodega caída

Requisitos:
    .\manage.ps1 start all          (Linux/EC2: ./manage_warehouse.sh start all)
    pip install httpx pika          (ya vienen en requirements-dev.txt)

Uso (desde la raíz del repositorio):
    python scripts/demo_fase7.py

Tarda unos 6-8 minutos (hay que esperar heartbeats y reintentos de verdad).

OJO: detiene y vuelve a arrancar contenedores, vacía la cola central.projection y
BORRA el modelo de lectura de la central (se reconstruye solo). Los datos de las
bodegas NO se tocan, salvo el producto de prueba DEMO-xxxxxx que crea la demo.
"""

import argparse
import json
import statistics
import subprocess
import sys
import time
import uuid

import httpx
import pika

WAREHOUSES = ("WH-001", "WH-002", "WH-003")
CENTRAL_TABLES = ("warehouses, catalog_products, warehouse_products, warehouse_inventory, "
                  "movement_history, central_alerts, processed_events, sync_runs")


# ---------------------------------------------------------------- utilidades
def docker(*args: str, check: bool = True) -> str:
    result = subprocess.run(["docker", *args], capture_output=True, text=True)
    if check and result.returncode != 0:
        raise RuntimeError(f"docker {' '.join(args)} falló: {result.stderr.strip()}")
    return result.stdout


def stop(*containers: str) -> None:
    docker("stop", *containers)


def start(*containers: str) -> None:
    docker("start", *containers)


def central_containers() -> list[str]:
    """Todos los contenedores de la central (los forwarders de las bodegas también empiezan por central_)."""
    names = docker("ps", "-a", "--format", "{{.Names}}").split()
    return sorted(n for n in names if n.startswith("central_") and not n.startswith("central_forwarder_"))


def psql_count(container: str, database: str, sql: str) -> int:
    return int(docker("exec", container, "psql", "-U", "postgres", "-d", database, "-tAc", sql).strip())


def queue_messages(container: str, queue: str) -> int:
    for line in docker("exec", container, "rabbitmqctl", "-q", "list_queues", "name", "messages").splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[0] == queue:
            return int(parts[1])
    return 0


def wait(check, timeout: float, what: str, every: float = 1.0):
    start_time, last = time.time(), None
    while time.time() - start_time < timeout:
        try:
            last = check()
            if last:
                return last
        except (httpx.HTTPError, RuntimeError, KeyError, ValueError) as error:
            last = error
        time.sleep(every)
    raise AssertionError(f"Se acabó el tiempo ({timeout:.0f} s) esperando: {what}. Último valor: {last}")


def title(text: str) -> None:
    print(f"\n{text}")


def ok(text: str) -> None:
    print(f"   ✔ {text}")


def info(text: str) -> None:
    print(f"     {text}")


class Demo:
    def __init__(self, args):
        urls = dict(item.split("=", 1) for item in args.bodegas.split(","))
        self.wh = {wh: httpx.Client(base_url=urls[wh], timeout=10) for wh in WAREHOUSES}
        self.central = httpx.Client(base_url=args.central, timeout=15)
        self.central_amqp = args.central_amqp
        self.sku = f"DEMO-{uuid.uuid4().hex[:6].upper()}"
        self.pids: dict[str, int] = {}

    # ---------------------------------------------------------------- lecturas
    def central_view(self) -> dict:
        response = self.central.get(f"/global/inventory/{self.sku}")
        return response.json() if response.status_code == 200 else {}

    def central_total(self):
        return self.central_view().get("total_quantity")

    def real_total(self) -> int:
        """Suma preguntando a cada bodega (la verdad)."""
        return sum(self.wh[wh].get(f"/inventory/by-sku/{self.sku}").json()["quantity"] for wh in WAREHOUSES)

    def move(self, wh: str, kind: str, quantity: int) -> int:
        return self.wh[wh].post("/inventory/movements",
                                json={"product_id": self.pids[wh], "type": kind, "quantity": quantity}).status_code

    def central_matches_warehouses(self) -> bool:
        return self.central_total() == self.real_total() and not self.central_view().get("warnings")

    def histories_match(self) -> bool:
        for wh in WAREHOUSES:
            real = psql_count(f"db_warehouse_{wh}", "warehouse_db", "select count(*) from inventory_movements")
            central = psql_count("central_postgres", "central_db",
                                 f"select count(*) from movement_history where warehouse_id = '{wh}'")
            if real != central:
                return False
        return True

    # ---------------------------------------------------------------- escenarios
    def s1_inventario_global(self):
        title(f"1) INVENTARIO GLOBAL — producto de prueba {self.sku}: WH-001=100, WH-002=50, WH-003=80")
        for wh, qty in (("WH-001", 100), ("WH-002", 50), ("WH-003", 80)):
            response = self.wh[wh].post("/products", json={"sku": self.sku, "name": "Casco de seguridad (demo)",
                                                           "unit": "UNIDAD", "retail_price": "32000"})
            assert response.status_code == 201, response.text
            self.pids[wh] = response.json()["id"]
            assert self.move(wh, "RECEIPT", qty) == 201
        wait(lambda: self.central_total() == 230, 90, "total 230 en la central")
        ok(f"GET /global/inventory/{self.sku} -> 230 (modelo de lectura)")
        live = self.central.get(f"/global/inventory/{self.sku}/live").json()
        assert live["total_quantity"] == 230 and live["complete"], live
        ok("GET .../live (pregunta en vivo a las 3 bodegas) -> 230")

    def s2_central_caida(self):
        title("2) LA CENTRAL SE CAE: las bodegas siguen trabajando y la información llega después")
        central = central_containers()
        stop(*central)
        ok(f"central apagada: docker stop {' '.join(central)}")
        codes = [self.move(wh, "SHIPMENT", 2) for wh in WAREHOUSES for _ in range(5)]
        assert codes == [201] * 15, codes
        ok("15 salidas aceptadas por las bodegas con la central caída")
        waiting = wait(lambda: (n := sum(queue_messages(f"rabbitmq_warehouse_{wh}", "wh.central-forward") for wh in WAREHOUSES)) >= 15 and n,
                       30, "mensajes en las colas locales wh.central-forward")
        ok(f"{waiting} mensajes guardados en las colas locales wh.central-forward (store-and-forward)")
        start("central_postgres", "central_rabbitmq", *[c for c in central if c not in ("central_postgres", "central_rabbitmq")])
        info("central encendida de nuevo; esperando a que los forwarders reintenten (hasta ~30 s)...")
        wait(lambda: self.central_total() == 200, 240, "total 200 al volver la central", every=2)
        ok(f"total en la central = 230 - 30 = 200 = suma real de las bodegas ({self.real_total()}). Nada se perdió")

    def s3_bodega_aislada(self):
        title("3) UNA BODEGA QUEDA AISLADA: WH-002 pierde contacto con la central pero sigue vendiendo")
        stop("central_forwarder_WH-002")
        for _ in range(3):
            assert self.move("WH-002", "SHIPMENT", 1) == 201
        ok("WH-002 aceptó 3 ventas sin poder avisarle a la central")
        info("esperando a que la central note que WH-002 no envía heartbeats (ONLINE_AFTER_SECONDS = 45 s)...")
        view = wait(lambda: (v := self.central_view()) and any(w["warehouse_id"] == "WH-002" and w["warehouse_status"] == "OFFLINE"
                                                               for w in v["warehouses"]) and v, 150, "WH-002 OFFLINE", every=3)
        ok(f"la central sigue respondiendo y avisa: \"{view['warnings'][0]}\"")
        live = self.central.get(f"/global/inventory/{self.sku}/live").json()
        ok(f"la central muestra {view['total_quantity']} (último dato conocido) y el dato real es {live['total_quantity']}: consistencia eventual")
        start("central_forwarder_WH-002")
        wait(self.central_matches_warehouses, 120, "que WH-002 se ponga al día", every=2)
        ok(f"WH-002 reconectada: total {self.central_total()} y sin advertencias")

    def s4_mensajes_perdidos(self):
        title("4) SE PIERDEN MENSAJES: se vacía a propósito la cola central y la central igual se recupera")
        runs_before = max((r["id"] for r in self.central.get("/sync/runs").json()), default=0)
        stop("central_projector")
        for _ in range(4):
            assert self.move("WH-003", "SHIPMENT", 5) == 201
        lost = wait(lambda: (n := queue_messages("central_rabbitmq", "central.projection")) >= 4 and n, 60,
                    "los mensajes de WH-003 en central.projection")
        docker("exec", "central_rabbitmq", "rabbitmqctl", "-q", "purge_queue", "central.projection")
        ok(f"cola central.projection vaciada: {lost} mensajes perdidos a propósito (incluye las 4 salidas de WH-003)")
        start("central_projector")
        assert self.move("WH-003", "RECEIPT", 1) == 201  # llega un evento con secuencia mayor: el hueco se nota
        wait(lambda: self.central_total() == self.real_total(), 90, "que el saldo se corrija", every=2)
        ok(f"el SALDO se corrigió con el siguiente evento (trae el saldo absoluto y una versión mayor): {self.central_total()}")
        info("esperando al reconciliador (GAP_GRACE_SECONDS = 20 s + RECONCILE_SECONDS = 15 s)...")
        runs = wait(lambda: [r for r in self.central.get("/sync/runs").json() if r["id"] > runs_before and r["kind"] == "CATCH_UP"
                             and r["warehouse_id"] == "WH-003" and r["events_applied"] > 0], 150, "el catch-up de WH-003", every=3)
        wait(self.histories_match, 60, "historial central igual al de las bodegas", every=2)
        ok(f"el reconciliador pidió a WH-003 /sync/events y recuperó {runs[0]['events_applied']} eventos: "
           "el historial central es idéntico al de las 3 bodegas")

    def s5_base_central_borrada(self):
        title("5) SE PIERDE LA BASE CENTRAL COMPLETA: se reconstruye sola desde las bodegas")
        before = psql_count("central_postgres", "central_db", "select count(*) from movement_history")
        stop("central_api", "central_projector", "central_reconciler")
        docker("exec", "central_postgres", "psql", "-U", "postgres", "-d", "central_db", "-c",
               f"TRUNCATE {CENTRAL_TABLES} RESTART IDENTITY CASCADE")
        ok(f"modelo de lectura borrado (tenía {before} movimientos)")
        start("central_projector", "central_reconciler", "central_api")
        info("reconstruyendo: heartbeat -> snapshot -> catch-up desde la secuencia 0 (1-2 minutos)...")
        wait(lambda: self.central_total() == self.real_total(), 240, "la reconstrucción del saldo", every=3)
        wait(self.histories_match, 180, "la reconstrucción del historial", every=3)
        after = psql_count("central_postgres", "central_db", "select count(*) from movement_history")
        ok(f"reconstruido: total {self.central_total()} e historial completo ({after} movimientos)")

    def s6_duplicados(self):
        title("6) EVENTO DUPLICADO: llega 3 veces y se cuenta una sola")
        last = self.wh["WH-001"].get("/sync/events", params={"after_sequence": 0, "limit": 1}).json()["last_sequence"]
        events = self.wh["WH-001"].get("/sync/events", params={"after_sequence": max(0, last - 50), "limit": 50}).json()["events"]
        event = [e for e in events if e["event_type"] == "INVENTORY_MOVEMENT_RECORDED"][-1]
        total_before = self.central_total()
        history_before = psql_count("central_postgres", "central_db", "select count(*) from movement_history")
        connection = pika.BlockingConnection(pika.URLParameters(self.central_amqp))
        channel = connection.channel()
        channel.confirm_delivery()
        for _ in range(3):
            channel.basic_publish("central.events", "WH-001.inventory.movement.recorded", json.dumps(event).encode(),
                                  pika.BasicProperties(delivery_mode=2, message_id=event["event_id"]), mandatory=True)
        connection.close()
        ok(f"publicadas 3 copias del evento {event['event_id'][:8]}... (secuencia {event['sequence']}) en el RabbitMQ central")
        time.sleep(5)
        history_after = psql_count("central_postgres", "central_db", "select count(*) from movement_history")
        assert self.central_total() == total_before and history_after == history_before
        ok(f"total ({total_before}) e historial ({history_before} movimientos) sin cambios: processed_events lo detectó")

    def s7_a_vs_b(self):
        title("7) ALTERNATIVA A (en vivo) vs B (modelo de lectura)")

        def median_ms(path: str, n: int):
            times, body = [], None
            for _ in range(n):
                t0 = time.perf_counter()
                body = self.central.get(path).json()
                times.append((time.perf_counter() - t0) * 1000)
            return round(statistics.median(times), 1), body

        b_ms, _ = median_ms(f"/global/inventory/{self.sku}", 20)
        a_ms, _ = median_ms(f"/global/inventory/{self.sku}/live", 20)
        stop("lb_warehouse_WH-002")
        try:
            time.sleep(1)
            b2_ms, b2 = median_ms(f"/global/inventory/{self.sku}", 5)
            a2_ms, a2 = median_ms(f"/global/inventory/{self.sku}/live", 5)
        finally:
            start("lb_warehouse_WH-002")
        rows = [("Escenario", "B: modelo de lectura", "A: consulta en vivo"),
                ("3 bodegas arriba", f"{b_ms} ms", f"{a_ms} ms"),
                ("WH-002 sin responder", f"{b2_ms} ms · total {b2['total_quantity']}",
                 f"{a2_ms} ms · total {a2['total_quantity']} · completo={a2['complete']}")]
        widths = [max(len(row[i]) for row in rows) for i in range(3)]
        print()
        for index, row in enumerate(rows):
            print("     | " + " | ".join(cell.ljust(widths[i]) for i, cell in enumerate(row)) + " |")
            if index == 0:
                print("     |" + "|".join("-" * (w + 2) for w in widths) + "|")
        print()
        ok("B siempre responde (con advertencia si un dato puede estar viejo); A da un total INCOMPLETO si una bodega no contesta")


def main() -> int:
    parser = argparse.ArgumentParser(description="Demo de la Fase 7 (sin pérdida) contra la plataforma en Docker")
    parser.add_argument("--bodegas", default="WH-001=http://localhost:8001,WH-002=http://localhost:8002,WH-003=http://localhost:8003")
    parser.add_argument("--central", default="http://localhost:8090")
    parser.add_argument("--central-amqp", default="amqp://platform:platform@localhost:5680/%2F")
    demo = Demo(parser.parse_args())

    try:
        demo.central.get("/health").raise_for_status()
        for wh in WAREHOUSES:
            demo.wh[wh].get("/health").raise_for_status()
    except httpx.HTTPError as error:
        print(f"La plataforma no responde ({error}). Levántala primero con: .\\manage.ps1 start all")
        return 1

    steps = (demo.s1_inventario_global, demo.s2_central_caida, demo.s3_bodega_aislada, demo.s4_mensajes_perdidos,
             demo.s5_base_central_borrada, demo.s6_duplicados, demo.s7_a_vs_b)
    started = time.time()
    for step in steps:
        try:
            step()
        except AssertionError as error:
            print(f"\n   ✘ FALLÓ: {error}")
            print("     Revisa los logs:  .\\manage.ps1 logs central central_projector   (o central_reconciler, wh_00X central_forwarder)")
            return 1
    print(f"\nTODO OK en {time.time() - started:.0f} s — ninguna prueba perdió información.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
