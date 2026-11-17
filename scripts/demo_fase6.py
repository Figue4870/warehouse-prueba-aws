r"""
DEMO FASE 6 — Transferencias con Saga, con la plataforma REAL en Docker.

Repite los experimentos de docs/fases/FASE_6.md (caso de la guía: WH-001 = 100,
WH-002 = 20, transferir 30):

  1. Camino feliz (y el mismo POST repetido con la misma Idempotency-Key)
  2. Fallar ANTES de reservar (no hay stock)
  3. Fallar DESPUÉS de reservar (el destino rechaza) -> compensación
  4. Destino caído -> la Saga NO compensa a ciegas: espera, y compensa cuando vuelve
  5. Mensaje duplicado (comando y respuesta repetidos 3 veces)
  6. Timeout (destino encendido pero lento)
  7. Worker reiniciado (orquestador y relay de comandos apagados a mitad)
  8. Al final: las unidades físicas suman exactamente lo mismo que al principio

Requisitos:
    .\manage.ps1 start all          (Linux/EC2: ./manage_warehouse.sh start all)
    pip install httpx pika          (ya vienen en requirements-dev.txt)

Uso (desde la raíz del repositorio):
    python scripts/demo_fase6.py

Tarda 2-3 minutos. Crea su propio producto DEMO-T-xxxxxx (no toca sus datos) y
detiene/arranca contenedores de WH-002 y de la central.
"""

import argparse
import json
import subprocess
import sys
import time
import uuid

import httpx
import pika


def docker(*args: str) -> str:
    result = subprocess.run(["docker", *args], capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"docker {' '.join(args)} falló: {result.stderr.strip()}")
    return result.stdout


def wait(check, timeout: float, what: str, every: float = 1.0):
    start, last = time.time(), None
    while time.time() - start < timeout:
        try:
            last = check()
            if last:
                return last
        except (httpx.HTTPError, RuntimeError, KeyError, ValueError) as error:
            last = error
        time.sleep(every)
    raise AssertionError(f"Se acabó el tiempo ({timeout:.0f} s) esperando: {what}. Último valor: {last}")


def title(text):
    print(f"\n{text}")


def ok(text):
    print(f"   ✔ {text}")


def info(text):
    print(f"     {text}")


# Contenedores de WH-002 que "apagan la bodega" (su base y su RabbitMQ siguen: sus datos están a salvo)
WH2_ALL = ["lb_warehouse_WH-002", "warehouse_api_1_WH-002", "warehouse_api_2_WH-002", "warehouse_api_3_WH-002",
           "outbox_relay_WH-002", "central_forwarder_WH-002", "command_worker_WH-002"]


class Demo:
    def __init__(self, args):
        urls = dict(item.split("=", 1) for item in args.bodegas.split(","))
        self.wh = {name: httpx.Client(base_url=url, timeout=10) for name, url in urls.items()}
        self.central = httpx.Client(base_url=args.central, timeout=15)
        self.central_amqp = args.central_amqp
        self.sku = f"DEMO-T-{uuid.uuid4().hex[:6].upper()}"
        self.first_completed = None

    # ---------------------------------------------------------------- ayudas
    def stock(self, wh):
        data = self.wh[wh].get(f"/inventory/by-sku/{self.sku}").json()
        return data["quantity"], data["reserved_quantity"]

    def physical(self):
        return sum(sum(self.stock(wh)) for wh in ("WH-001", "WH-002"))

    def transfer(self, quantity, key=None, **extra):
        body = {"sku": self.sku, "source_warehouse_id": "WH-001", "destination_warehouse_id": "WH-002", "quantity": quantity, **extra}
        response = self.central.post("/transfers", json=body, headers={"Idempotency-Key": key} if key else {})
        assert response.status_code == 202, response.text
        return response.json()

    def get(self, transfer_id):
        return self.central.get(f"/transfers/{transfer_id}").json()

    def until(self, transfer_id, *states, timeout=90):
        return wait(lambda: (t := self.get(transfer_id))["state"] in states and t, timeout, f"{transfer_id} en {states}")

    @staticmethod
    def path(transfer):
        return " -> ".join(h["to"] for h in transfer["history"] if h["from"] != h["to"])

    def operations(self, wh, transfer_id):
        ops = self.wh[wh].get("/transfer-operations", params={"transfer_id": transfer_id}).json()
        return ", ".join(f"{op['step']}={op['outcome']}" for op in reversed(ops))

    def expect(self, wh1, wh2):
        assert self.stock("WH-001") == wh1 and self.stock("WH-002") == wh2, (self.stock("WH-001"), self.stock("WH-002"))

    # ---------------------------------------------------------------- escenarios
    def s0_preparar(self):
        title(f"0) PREPARACIÓN (caso de la guía) — producto {self.sku}: WH-001 = 100, WH-002 = 20")
        for wh, qty in (("WH-001", 100), ("WH-002", 20)):
            product = self.wh[wh].post("/products", json={"sku": self.sku, "name": "Casco de seguridad (demo)",
                                                         "unit": "UNIDAD", "retail_price": "32000"})
            assert product.status_code == 201, product.text
            pid = product.json()["id"]
            assert self.wh[wh].post("/inventory/movements", json={"product_id": pid, "type": "RECEIPT", "quantity": qty}).status_code == 201
        wait(lambda: self.central.get(f"/global/inventory/{self.sku}").json().get("total_quantity") == 120, 60, "la central vea 120")
        manual = self.wh["WH-001"].post("/inventory/movements", json={"product_id": pid, "type": "TRANSFER_OUT", "quantity": 1})
        assert manual.status_code == 409
        ok("la central ve 120 unidades; un TRANSFER_OUT manual se rechaza (409): solo la Saga mueve entre bodegas")

    def s1_camino_feliz(self):
        title("1) CAMINO FELIZ: transferir 30 de WH-001 a WH-002")
        key = str(uuid.uuid4())
        t = self.transfer(30, key=key)
        ok(f"POST /transfers -> 202 {{'transfer_id': '{t['transfer_id']}', 'status': '{t['status']}'}}")
        assert self.transfer(30, key=key)["transfer_id"] == t["transfer_id"]
        ok("el mismo POST repetido con la misma Idempotency-Key devuelve la MISMA transferencia (no crea otra)")
        done = self.until(t["transfer_id"], "COMPLETED")
        self.expect((70, 0), (50, 0))
        self.first_completed = t["transfer_id"]
        ok(f"COMPLETED: WH-001 = 70, WH-002 = 50\n       {self.path(done)}")

    def s2_falla_antes(self):
        title("2) FALLAR ANTES DE RESERVAR: pedir 1000 (no hay)")
        t = self.until(self.transfer(1000)["transfer_id"], "FAILED")
        self.expect((70, 0), (50, 0))
        ok(f"FAILED: \"{t['failure_reason']}\" — nadie cambió nada")

    def s3_falla_despues(self):
        title("3) FALLAR DESPUÉS DE RESERVAR: el destino rechaza (falla simulada)")
        t = self.until(self.transfer(15, simulate_failure="DESTINATION_REJECTS")["transfer_id"], "RESERVATION_RELEASED")
        self.expect((70, 0), (50, 0))
        ok(f"{self.path(t)}")
        ok(f"WH-001 hizo: {self.operations('WH-001', t['transfer_id'])} (la reserva se deshizo)")

    def s4_destino_caido(self):
        title("4) DESTINO CAÍDO: WH-002 apagada (APIs, relay, forwarder y command-worker)")
        docker("stop", *WH2_ALL)
        t = self.transfer(10, timeout_seconds=8)
        self.until(t["transfer_id"], "IN_TRANSIT")
        wait(lambda: self.stock("WH-001") == (60, 10), 30, "la reserva en WH-001")
        ok("IN_TRANSIT: WH-001 tiene 60 disponibles + 10 RESERVADAS (no se pueden vender, tampoco se pierden)")
        state = self.until(t["transfer_id"], "COMPENSATING", timeout=40)
        assert state["compensation_step"] == "CANCEL_RECEIVE"
        time.sleep(8)
        assert self.get(t["transfer_id"])["state"] == "COMPENSATING" and self.stock("WH-001") == (60, 10)
        ok("venció el plazo (8 s): COMPENSATING, pero NO libera a ciegas — espera que WH-002 confirme que no recibió")
        docker("start", *WH2_ALL)
        info("WH-002 encendida de nuevo; esperando su respuesta...")
        self.until(t["transfer_id"], "RESERVATION_RELEASED", timeout=120)
        self.expect((70, 0), (50, 0))
        ok(f"WH-002 respondió ({self.operations('WH-002', t['transfer_id'])}) -> RESERVATION_RELEASED. WH-001 = 70, WH-002 = 50")

    def s5_duplicados(self):
        title("5) MENSAJE DUPLICADO: el mismo comando y la misma respuesta llegan 3 veces")
        tid = self.first_completed
        sql = ("select row_to_json(c) from (select command_id, command_type, transfer_id, warehouse_id, payload from transfer_commands "
               f"where transfer_id = '{tid}' and command_type = 'RESERVE') c")
        command = json.loads(docker("exec", "central_postgres", "psql", "-U", "postgres", "-d", "central_db", "-tAc", sql).strip())
        command.update({"issued_at": "2026-01-01T00:00:00+00:00", "deadline": "2099-01-01T00:00:00+00:00"})
        last = self.wh["WH-001"].get("/sync/events", params={"after_sequence": 0, "limit": 1}).json()["last_sequence"]
        events = self.wh["WH-001"].get("/sync/events", params={"after_sequence": max(0, last - 2000), "limit": 2000}).json()["events"]
        reply = next(e for e in events if e["event_type"] == "TRANSFER_SOURCE_RESERVED" and e["payload"].get("transfer_id") == tid)
        ops_before = len(self.wh["WH-001"].get("/transfer-operations", params={"limit": 1000}).json())

        connection = pika.BlockingConnection(pika.URLParameters(self.central_amqp))
        channel = connection.channel()
        channel.confirm_delivery()
        for _ in range(3):
            channel.basic_publish("central.commands", "WH-001", json.dumps(command).encode(),
                                  pika.BasicProperties(delivery_mode=2), mandatory=True)
            channel.basic_publish("central.events", "WH-001.transfer.source.reserved", json.dumps(reply).encode(),
                                  pika.BasicProperties(delivery_mode=2), mandatory=True)
        connection.close()
        time.sleep(5)
        assert len(self.wh["WH-001"].get("/transfer-operations", params={"limit": 1000}).json()) == ops_before
        self.expect((70, 0), (50, 0))
        assert self.get(tid)["state"] == "COMPLETED"
        ok(f"3 copias del RESERVE de {tid}: WH-001 no reservó otra vez (llave transfer_id + paso)")
        ok(f"3 copias de su respuesta: {tid} sigue COMPLETED (respuesta ya procesada)")

    def s6_timeout(self):
        title("6) TIMEOUT: WH-002 encendida pero su command-worker detenido (lenta)")
        docker("stop", "command_worker_WH-002")
        t = self.transfer(5, timeout_seconds=6)
        self.until(t["transfer_id"], "IN_TRANSIT")
        self.until(t["transfer_id"], "COMPENSATING", timeout=40)
        ok("venció el plazo de 6 s: COMPENSATING (CANCEL_RECEIVE espera en la cola commands.WH-002)")
        docker("start", "command_worker_WH-002")
        self.until(t["transfer_id"], "RESERVATION_RELEASED", timeout=90)
        self.expect((70, 0), (50, 0))
        ok(f"al volver: {self.operations('WH-002', t['transfer_id'])} — el RECEIVE vencido se RECHAZÓ (no se sumó tarde)")

    def s7_reinicio(self):
        title("7) WORKER REINICIADO: se apaga el orquestador de la central a mitad de una transferencia")
        docker("stop", "central_saga")
        t = self.transfer(20)
        wait(lambda: self.stock("WH-001") == (50, 20), 60, "la reserva hecha con el orquestador apagado")
        ok("con el orquestador apagado WH-001 igual reservó 20; su respuesta espera en la cola central.saga")
        docker("stop", "central_command_relay")
        docker("start", "central_saga")
        self.until(t["transfer_id"], "IN_TRANSIT", timeout=60)
        ok("orquestador de vuelta: IN_TRANSIT. Ahora el relay de comandos está apagado: el RECEIVE espera en la base central")
        docker("start", "central_command_relay")
        self.until(t["transfer_id"], "COMPLETED")
        self.expect((50, 0), (70, 0))
        ok("relay de vuelta: COMPLETED. WH-001 = 50, WH-002 = 70")

    def s8_balance(self):
        title("8) NADA SE PIERDE NI SE DUPLICA")
        assert self.physical() == 120
        wait(lambda: self.central.get(f"/global/inventory/{self.sku}").json()["total_quantity"] == 120, 60, "la central vea 120")
        ok(f"WH-001 + WH-002 = {self.physical()} unidades (igual que al principio) y la central también ve 120")


def main() -> int:
    parser = argparse.ArgumentParser(description="Demo de la Fase 6 (Saga) contra la plataforma en Docker")
    parser.add_argument("--bodegas", default="WH-001=http://localhost:8001,WH-002=http://localhost:8002")
    parser.add_argument("--central", default="http://localhost:8090")
    parser.add_argument("--central-amqp", default="amqp://platform:platform@localhost:5680/%2F")
    demo = Demo(parser.parse_args())
    try:
        demo.central.get("/health").raise_for_status()
        for client in demo.wh.values():
            client.get("/health").raise_for_status()
    except httpx.HTTPError as error:
        print(f"La plataforma no responde ({error}). Levántala primero con: .\\manage.ps1 start all")
        return 1

    started = time.time()
    steps = (demo.s0_preparar, demo.s1_camino_feliz, demo.s2_falla_antes, demo.s3_falla_despues, demo.s4_destino_caido,
             demo.s5_duplicados, demo.s6_timeout, demo.s7_reinicio, demo.s8_balance)
    for step in steps:
        try:
            step()
        except AssertionError as error:
            print(f"\n   ✘ FALLÓ: {error}")
            print("     Revisa: http://localhost:8090/dashboard  y  .\\manage.ps1 logs central central_saga")
            return 1
    print(f"\nTODO OK en {time.time() - started:.0f} s — ninguna unidad se perdió ni se duplicó.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
