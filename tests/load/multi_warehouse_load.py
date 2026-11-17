"""
CARGA DISTINTA POR BODEGA (Fase 4). Simula el escenario del README:

  WH-001 =  50 req/s   (baja actividad)
  WH-002 = 800 req/s   (alta lectura)
  WH-003 =  20 req/s   (baja actividad)

  python tests/load/multi_warehouse_load.py --seconds 30 \
      --target WH-001=http://localhost:8001@50 \
      --target WH-002=http://localhost:8002@800 \
      --target WH-003=http://localhost:8003@20

Por cada bodega muestra la tasa pedida vs la lograda, la latencia (P50/P95/P99)
y los errores. Si una bodega no alcanza su tasa, ESA bodega es la que necesita
escalar (más instancias de API, más CPU), sin tocar a las demás.

Lee GET /inventory/{id} de un producto que crea al inicio en cada bodega.
"""

import argparse
import statistics
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor

import httpx


def percentile(values, p):
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, max(0, round(p / 100 * len(ordered)) - 1))] if ordered else None


def prepare(url):
    with httpx.Client(base_url=url, timeout=10) as client:
        sku = f"LOAD-{uuid.uuid4().hex[:8].upper()}"
        pid = client.post("/products", json={"sku": sku, "name": "Producto carga", "unit": "U"}).json()["id"]
        client.post("/inventory/movements", json={"product_id": pid, "type": "RECEIPT", "quantity": 100})
        return pid


def run_target(name, url, rate, seconds, threads, results):
    pid = prepare(url)
    latencies, errors = [], 0
    lock = threading.Lock()
    deadline = time.time() + seconds
    interval = threads / rate  # cada hilo dispara cada "interval" segundos

    def loop(offset):
        nonlocal errors
        with httpx.Client(base_url=url, timeout=5) as client:
            next_at = time.time() + offset
            while time.time() < deadline:
                time.sleep(max(0.0, next_at - time.time()))
                t0 = time.perf_counter()
                try:
                    ok = client.get(f"/inventory/{pid}").status_code == 200
                except httpx.HTTPError:
                    ok = False
                elapsed = (time.perf_counter() - t0) * 1000
                with lock:
                    latencies.append(elapsed)
                    errors += 0 if ok else 1
                next_at += interval

    with ThreadPoolExecutor(max_workers=threads) as pool:
        list(pool.map(loop, [i * interval / threads for i in range(threads)]))
    results[name] = {
        "pedido_rps": rate,
        "logrado_rps": round(len(latencies) / seconds, 1),
        "p50_ms": round(statistics.median(latencies), 1) if latencies else None,
        "p95_ms": round(percentile(latencies, 95), 1) if latencies else None,
        "p99_ms": round(percentile(latencies, 99), 1) if latencies else None,
        "errores": errors,
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--target", action="append", required=True, help="NOMBRE=URL@REQ_POR_SEG (repetible)")
    parser.add_argument("--seconds", type=int, default=30)
    args = parser.parse_args()
    results, jobs = {}, []
    for target in args.target:
        name, rest = target.split("=", 1)
        url, rate = rest.rsplit("@", 1)
        rate = float(rate)
        threads = max(2, min(64, int(rate / 25) + 2))  # más hilos para tasas altas
        jobs.append(threading.Thread(target=run_target, args=(name, url, rate, args.seconds, threads, results)))
    for job in jobs:
        job.start()
    for job in jobs:
        job.join()
    print(f"\n{'Bodega':<8} {'pedido':>8} {'logrado':>8} {'P50 ms':>8} {'P95 ms':>8} {'P99 ms':>8} {'errores':>8}")
    for name, r in results.items():
        print(f"{name:<8} {r['pedido_rps']:>8} {r['logrado_rps']:>8} {r['p50_ms']:>8} {r['p95_ms']:>8} {r['p99_ms']:>8} {r['errores']:>8}")


if __name__ == "__main__":
    main()
