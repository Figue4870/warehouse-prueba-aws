r"""
DEMO FASES 4 y 5 — bodegas independientes y balanceo de carga, con Docker REAL.

Repite, verificando cada resultado, los experimentos de docs/fases/FASE_4_5.md:

  Fase 4
    1. WH-002 caída NO impide que WH-001 y WH-003 sigan funcionando
    2. Cada bodega tiene SUS datos (un producto de WH-001 no existe en WH-002)
    3. Carga distinta por bodega (50 / 200 / 20 req/s): cada una responde a su ritmo
  Fase 5
    4. El balanceador reparte entre api-1, api-2 y api-3 (cabecera X-Instance)
    5. 1 instancia vs 3 instancias: throughput y latencia
    6. Se apaga una instancia CON tráfico: cero errores
    7. Escrituras con una instancia caída: todas se aceptan y el saldo final es exacto

Requisitos:
    .\manage.ps1 start all          (Linux/EC2: ./manage_warehouse.sh start all)
    pip install httpx               (ya viene en requirements-dev.txt)

Uso (desde la raíz del repositorio):
    python scripts/demo_fase4_5.py                 # unos 2 minutos
    python scripts/demo_fase4_5.py --alta 800      # la carga del enunciado para WH-002 (exige un PC potente)

Las cifras de rendimiento dependen del PC (Docker Desktop reparte la CPU entre todas
las bodegas); lo que se VERIFICA es que no haya errores ni datos perdidos.
"""

import argparse
import statistics
import sys
import threading
import time
import uuid
from collections import Counter
from concurrent.futures import ThreadPoolExecutor

import httpx

WAREHOUSES = ("WH-001", "WH-002", "WH-003")


def docker(*args: str) -> str:
    import subprocess

    result = subprocess.run(["docker", *args], capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"docker {' '.join(args)} falló: {result.stderr.strip()}")
    return result.stdout


def containers_of(warehouse_id: str) -> list[str]:
    """Todos los contenedores de una bodega (sus nombres terminan en _WH-00X)."""
    return [n for n in docker("ps", "-a", "--format", "{{.Names}}").split() if n.endswith(f"_{warehouse_id}")]


def wait(check, timeout: float, what: str, every: float = 0.5):
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


def pct(values, p):
    if not values:
        return 0.0
    values = sorted(values)
    return values[min(len(values) - 1, int(round(p / 100 * (len(values) - 1))))]


class Demo:
    def __init__(self, args):
        self.urls = dict(item.split("=", 1) for item in args.bodegas.split(","))
        self.wh = {wh: httpx.Client(base_url=url, timeout=5) for wh, url in self.urls.items()}
        self.high_rate = args.alta
        self.products: dict[str, int] = {}

    # ---------------------------------------------------------------- ayudas
    def healthy(self, wh) -> bool:
        try:
            return self.wh[wh].get("/health").status_code == 200
        except httpx.HTTPError:
            return False

    def has_sku(self, wh, sku) -> bool:
        skip = 0
        while True:
            page = self.wh[wh].get("/products", params={"skip": skip, "limit": 500}).json()
            if any(p["sku"] == sku for p in page):
                return True
            if len(page) < 500:
                return False
            skip += 500

    def create_product(self, wh, sku):
        response = self.wh[wh].post("/products", json={"sku": sku, "name": "Producto demo Fase 4-5", "unit": "UNIDAD"})
        assert response.status_code == 201, response.text
        return response.json()["id"]

    def burst(self, url, seconds, concurrency, path):
        """Tantas peticiones como se pueda durante `seconds` con `concurrency` hilos."""
        latencies, errors, lock = [], Counter(), threading.Lock()
        stop_at = time.time() + seconds

        def worker():
            with httpx.Client(base_url=url, timeout=5) as client:
                while time.time() < stop_at:
                    t0 = time.perf_counter()
                    try:
                        status = client.get(path).status_code
                    except httpx.HTTPError as error:
                        status = type(error).__name__
                    elapsed = (time.perf_counter() - t0) * 1000
                    with lock:
                        if status == 200:
                            latencies.append(elapsed)
                        else:
                            errors[status] += 1

        threads = [threading.Thread(target=worker) for _ in range(concurrency)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        return {"rps": round(len(latencies) / seconds, 1), "p50": round(pct(latencies, 50), 1),
                "p95": round(pct(latencies, 95), 1), "errors": dict(errors)}

    def paced(self, url, rate, seconds, path, results):
        """`rate` peticiones por segundo durante `seconds` (como un cliente real)."""
        latencies, errors = [], 0
        interval = 1.0 / rate
        start = time.perf_counter()
        sent = 0
        with httpx.Client(base_url=url, timeout=5) as client, ThreadPoolExecutor(max_workers=min(64, int(rate) + 4)) as pool:
            futures = []

            def call():
                t0 = time.perf_counter()
                try:
                    return client.get(path).status_code, (time.perf_counter() - t0) * 1000
                except httpx.HTTPError:
                    return 0, 0.0

            while time.perf_counter() - start < seconds:
                futures.append(pool.submit(call))
                sent += 1
                next_at = start + sent * interval
                time.sleep(max(0.0, next_at - time.perf_counter()))
            for future in futures:
                status, elapsed = future.result()
                if status == 200:
                    latencies.append(elapsed)
                else:
                    errors += 1
        results.update({"asked": rate, "achieved": round(len(latencies) / seconds, 1), "p95": round(pct(latencies, 95), 1),
                        "errors": errors})

    def stop_api(self, *numbers):
        docker("stop", *[f"warehouse_api_{n}_WH-001" for n in numbers])

    def start_api(self, *numbers):
        docker("start", *[f"warehouse_api_{n}_WH-001" for n in numbers])
        expected = {f"WH-001-api-{n}" for n in numbers}
        # el balanceador vuelve a usar una instancia cuando ella responde
        wait(lambda: expected <= {self.wh["WH-001"].get("/health").headers.get("X-Instance") for _ in range(30)},
             60, f"que vuelvan {sorted(expected)}")

    # ---------------------------------------------------------------- Fase 4
    def s1_bodega_caida(self):
        title("1) FASE 4 — WH-002 DOWN no impide que WH-001 y WH-003 sigan funcionando")
        names = containers_of("WH-002")
        docker("stop", *names)
        wait(lambda: not self.healthy("WH-002"), 30, "que WH-002 deje de responder")
        ok(f"WH-002 apagada ({len(names)} contenedores)")
        for wh in ("WH-001", "WH-003"):
            pid = self.create_product(wh, f"F4-{wh}-{uuid.uuid4().hex[:6]}")
            moved = self.wh[wh].post("/inventory/movements", json={"product_id": pid, "type": "RECEIPT", "quantity": 5})
            assert self.healthy(wh) and moved.status_code == 201
            ok(f"{wh}: /health 200 y acepta movimientos (201) mientras WH-002 está caída")
        docker("start", *names)
        info("encendiendo WH-002 de nuevo...")
        wait(lambda: self.healthy("WH-002"), 120, "que WH-002 vuelva")
        ok("WH-002 volvió sola; nunca afectó a las demás")

    def s2_datos_propios(self):
        title("2) FASE 4 — cada bodega tiene SUS datos (particionamiento por dominio)")
        sku = f"F4-SOLO-WH1-{uuid.uuid4().hex[:6]}"
        self.create_product("WH-001", sku)
        assert self.has_sku("WH-001", sku) and not self.has_sku("WH-002", sku)
        ok(f"{sku} existe en WH-001 y NO en WH-002: no comparten base de datos")

    def s3_cargas_distintas(self):
        rates = {"WH-001": 50, "WH-002": self.high_rate, "WH-003": 20}
        title(f"3) FASE 4 — carga distinta por bodega ({rates['WH-001']} / {rates['WH-002']} / {rates['WH-003']} req/s, 8 s)")
        results = {wh: {} for wh in rates}
        for wh in rates:
            self.products.setdefault(wh, self.create_product(wh, f"F4-CARGA-{wh}-{uuid.uuid4().hex[:6]}"))
        threads = [threading.Thread(target=self.paced, args=(self.urls[wh], rate, 8, f"/inventory/{self.products[wh]}", results[wh]))
                   for wh, rate in rates.items()]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        print("     bodega   pedida  lograda  p95 (ms)  errores")
        for wh, r in results.items():
            print(f"     {wh}  {r['asked']:>6}  {r['achieved']:>7}  {r['p95']:>8}  {r['errors']:>7}")
        ok("cada bodega atiende su propia carga; si una no llega a su tasa, ESA es la que se escala (no todas)")

    # ---------------------------------------------------------------- Fase 5
    def s4_balanceo(self):
        title("4) FASE 5 — el balanceador reparte las peticiones (cabecera X-Instance)")
        seen = Counter(self.wh["WH-001"].get("/health").headers.get("X-Instance") for _ in range(60))
        assert len(seen) == 3, seen
        ok("60 peticiones a WH-001: " + ", ".join(f"{k} = {v}" for k, v in sorted(seen.items())))

    def s5_una_vs_tres(self):
        title("5) FASE 5 — 1 instancia vs 3 instancias (16 clientes simultáneos, 8 s cada medición)")
        path = f"/inventory/{self.products.get('WH-001') or self.create_product('WH-001', f'F5-{uuid.uuid4().hex[:6]}')}"
        self.stop_api(2, 3)
        one = self.burst(self.urls["WH-001"], 8, 16, path)
        self.start_api(2, 3)
        three = self.burst(self.urls["WH-001"], 8, 16, path)
        print("     instancias   req/s    p50 ms   p95 ms   errores")
        for label, r in (("1", one), ("3", three)):
            print(f"     {label:>10}  {r['rps']:>6}  {r['p50']:>7}  {r['p95']:>7}  {sum(r['errors'].values()):>7}")
        assert not one["errors"] and not three["errors"]
        ok("sin errores en ninguna de las dos mediciones (la mejora depende de cuántos núcleos tenga el PC)")

    def s6_instancia_caida_con_trafico(self):
        title("6) FASE 5 — se apaga una instancia mientras hay tráfico (40 req/s durante 15 s)")
        results = {}
        probe = threading.Thread(target=self.paced, args=(self.urls["WH-001"], 40, 15, "/health", results))
        probe.start()
        time.sleep(4)
        self.stop_api(2)
        info("docker stop warehouse_api_2_WH-001 (a los 4 s)")
        time.sleep(5)
        docker("start", "warehouse_api_2_WH-001")
        info("docker start warehouse_api_2_WH-001 (a los 9 s)")
        probe.join()
        assert results["errors"] == 0, results
        ok(f"{int(results['achieved'] * 15)} peticiones y {results['errors']} errores: el balanceador reintentó en otra instancia")
        self.start_api(2)

    def s7_escrituras(self):
        title("7) FASE 5 — escrituras con una instancia caída (API stateless)")
        pid = self.create_product("WH-001", f"F5-ESCR-{uuid.uuid4().hex[:6]}")
        self.stop_api(3)
        codes = Counter()
        for _ in range(30):
            codes[self.wh["WH-001"].post("/inventory/movements", json={"product_id": pid, "type": "RECEIPT", "quantity": 1}).status_code] += 1
        quantity = self.wh["WH-001"].get(f"/inventory/{pid}").json()["quantity"]
        self.start_api(3)
        assert codes == Counter({201: 30}) and quantity == 30, (codes, quantity)
        ok("30 de 30 entradas aceptadas (201) y el saldo final es exactamente 30: ningún dato vivía en una instancia")


def main() -> int:
    parser = argparse.ArgumentParser(description="Demo de las Fases 4 y 5 contra Docker")
    parser.add_argument("--bodegas", default="WH-001=http://localhost:8001,WH-002=http://localhost:8002,WH-003=http://localhost:8003")
    parser.add_argument("--alta", type=int, default=200, help="req/s para WH-002 en el experimento 3 (el enunciado dice 800)")
    demo = Demo(parser.parse_args())
    deadline = time.time() + 30  # recién encendidas, las bodegas tardan unos segundos en responder
    while (missing := [wh for wh in WAREHOUSES if not demo.healthy(wh)]) and time.time() < deadline:
        time.sleep(2)
    if missing:
        print(f"No responden: {', '.join(missing)}. Levanta las bodegas con: .\\manage.ps1 start all")
        return 1
    started = time.time()
    for step in (demo.s1_bodega_caida, demo.s2_datos_propios, demo.s3_cargas_distintas, demo.s4_balanceo,
                 demo.s5_una_vs_tres, demo.s6_instancia_caida_con_trafico, demo.s7_escrituras):
        try:
            step()
        except AssertionError as error:
            print(f"\n   ✘ FALLÓ: {error}")
            return 1
    print(f"\nTODO OK en {time.time() - started:.0f} s.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
