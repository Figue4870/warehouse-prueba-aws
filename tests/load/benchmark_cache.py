"""
EXPERIMENTO FASE 2: PostgreSQL solamente  vs  PostgreSQL + Redis

Lanza muchas lecturas concurrentes a GET /inventory/{id} y mide:
  * requests por segundo (throughput)
  * latencia P50 / P95 / P99 (milisegundos)
  * errores
  * consultas SQL que hizo la API (GET /metrics)
  * hit ratio del caché

Uso (desde la carpeta warehouse_cloud_project, con la bodega corriendo):

  1) Con Redis (CACHE_ENABLED=true, el valor por defecto):
       python tests/load/benchmark_cache.py --label "con-redis"

  2) Sin Redis: reinicia la API con el caché apagado y repite
       PowerShell:  $env:CACHE_ENABLED="false"; docker compose -f basic-docker-compose.yml up -d
       Bash:        CACHE_ENABLED=false docker compose -f basic-docker-compose.yml up -d
       python tests/load/benchmark_cache.py --label "solo-postgres"

  Mientras corre, en otra terminal:  docker stats   (para ver CPU de API y PostgreSQL)

Opciones: --url, --requests, --concurrency, --product-id, --json resultados.json
"""

import argparse
import json
import statistics
import threading
import time
import uuid
from collections import Counter
from concurrent.futures import ThreadPoolExecutor

import httpx


def percentile(values, p):
    if not values:
        return None
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, round(p / 100 * len(ordered)) - 1))
    return ordered[index]


def prepare_product(client: httpx.Client) -> int:
    sku = f"BENCH-{uuid.uuid4().hex[:8].upper()}"
    product = client.post("/products", json={"sku": sku, "name": "Producto benchmark", "unit": "UNIDAD"})
    product.raise_for_status()
    pid = product.json()["id"]
    client.post("/inventory/movements", json={"product_id": pid, "type": "RECEIPT", "quantity": 1000}).raise_for_status()
    return pid


def main():
    parser = argparse.ArgumentParser(description="Benchmark de GET /inventory/{id} con y sin caché")
    parser.add_argument("--url", default="http://localhost:8001")
    parser.add_argument("--requests", type=int, default=3000)
    parser.add_argument("--concurrency", type=int, default=20)
    parser.add_argument("--product-id", type=int, default=None)
    parser.add_argument("--label", default=None)
    parser.add_argument("--json", dest="json_path", default=None)
    args = parser.parse_args()

    with httpx.Client(base_url=args.url, timeout=10) as client:
        health = client.get("/health").json()
        pid = args.product_id or prepare_product(client)
        for _ in range(50):  # calentamiento (no se mide)
            client.get(f"/inventory/{pid}")
        client.post("/metrics/reset")

    latencies, statuses, cache_headers, instances = [], Counter(), Counter(), Counter()
    lock = threading.Lock()
    local = threading.local()

    def one_request(_):
        if not hasattr(local, "client"):
            local.client = httpx.Client(base_url=args.url, timeout=10)
        start = time.perf_counter()
        try:
            response = local.client.get(f"/inventory/{pid}")
            code, header = response.status_code, response.headers.get("X-Cache", "-")
            instance = response.headers.get("X-Instance", "-")
        except httpx.HTTPError:
            code, header, instance = "error", "-", "-"
        elapsed_ms = (time.perf_counter() - start) * 1000
        with lock:
            latencies.append(elapsed_ms)
            statuses[code] += 1
            cache_headers[header] += 1
            instances[instance] += 1

    started = time.perf_counter()
    with ThreadPoolExecutor(max_workers=args.concurrency) as pool:
        list(pool.map(one_request, range(args.requests)))
    duration = time.perf_counter() - started

    with httpx.Client(base_url=args.url, timeout=10) as client:
        metrics = client.get("/metrics").json()

    errors = sum(count for code, count in statuses.items() if code != 200)
    cache = metrics["cache"]
    result = {
        "label": args.label or ("con-redis" if cache["enabled"] else "solo-postgres"),
        "warehouse_id": health.get("warehouse_id"),
        "cache_enabled": cache["enabled"],
        "requests": args.requests,
        "concurrency": args.concurrency,
        "duration_s": round(duration, 2),
        "requests_per_second": round(args.requests / duration, 1),
        "p50_ms": round(statistics.median(latencies), 2),
        "p95_ms": round(percentile(latencies, 95), 2),
        "p99_ms": round(percentile(latencies, 99), 2),
        "errors": errors,
        "error_rate_pct": round(100 * errors / args.requests, 2),
        # Ojo con varias instancias: este contador es SOLO de la instancia que respondió /metrics
        "sql_queries_in_api": metrics["sql_queries_total"],
        "cache_hit_ratio": cache["hit_ratio"],
        "x_cache_headers": dict(cache_headers),
        # Fase 5: cuántas peticiones atendió cada instancia de la API (balanceo)
        "por_instancia": dict(instances),
    }

    print("\n=== Resultado del benchmark ===")
    for key, value in result.items():
        print(f"{key:>22}: {value}")
    if args.json_path:
        with open(args.json_path, "w", encoding="utf-8") as fh:
            json.dump(result, fh, indent=2, ensure_ascii=False)
        print(f"\nGuardado en {args.json_path}")


if __name__ == "__main__":
    main()
