r"""
SONDA DE DISPONIBILIDAD (Fases 4 y 5): consulta una o varias bodegas sin parar
y muestra, segundo a segundo, cuántas peticiones salieron bien o mal y qué
instancia de la API atendió cada una.

Úsala mientras provocas una falla en otra terminal:

  # Fase 5 – apagar una instancia de la API mientras hay tráfico
  python tests/load/availability_probe.py --target WH-001=http://localhost:8001 --seconds 40
  docker stop warehouse_api_2_WH-001        (en otra terminal, a los 10 s)
  docker start warehouse_api_2_WH-001       (a los 25 s)

  # Fase 4 – apagar una bodega completa: las otras siguen
  python tests/load/availability_probe.py --target WH-001=http://localhost:8001 \
      --target WH-002=http://localhost:8002 --target WH-003=http://localhost:8003 --seconds 40
  .\manage.ps1 stop wh_002                   (en otra terminal)
"""

import argparse
import threading
import time
from collections import Counter, defaultdict

import httpx


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--target", action="append", required=True, help="NOMBRE=URL (repetible)")
    parser.add_argument("--path", default="/health")
    parser.add_argument("--seconds", type=int, default=30)
    parser.add_argument("--rate", type=float, default=20.0, help="peticiones por segundo por bodega")
    args = parser.parse_args()

    targets = dict(t.split("=", 1) for t in args.target)
    per_second = defaultdict(lambda: defaultdict(Counter))  # segundo -> bodega -> {ok, error, instancia}
    totals = defaultdict(Counter)
    lock = threading.Lock()
    start = time.time()

    def worker(name, url):
        with httpx.Client(base_url=url, timeout=2.0) as client:
            interval = 1.0 / args.rate
            next_at = time.time()
            while time.time() - start < args.seconds:
                second = int(time.time() - start)
                try:
                    response = client.get(args.path)
                    status = "ok" if response.status_code < 500 else f"http_{response.status_code}"
                    instance = response.headers.get("X-Instance", "?")
                except httpx.HTTPError:
                    status, instance = "error", "-"
                with lock:
                    per_second[second][name][status] += 1
                    per_second[second][name][f"@{instance}"] += 1
                    totals[name][status] += 1
                    totals[name][f"@{instance}"] += 1
                next_at += interval
                time.sleep(max(0.0, next_at - time.time()))

    threads = [threading.Thread(target=worker, args=item, daemon=True) for item in targets.items()]
    for thread in threads:
        thread.start()

    shown = 0
    while any(t.is_alive() for t in threads) or shown < args.seconds:
        time.sleep(1)
        with lock:
            row = per_second.get(shown, {})
            cells = []
            for name in targets:
                c = row.get(name, Counter())
                instances = ",".join(f"{k[1:].split('-')[-1]}:{v}" for k, v in sorted(c.items()) if k.startswith("@") and k != "@-")
                errors = sum(v for k, v in c.items() if not k.startswith("@") and k != "ok")
                cells.append(f"{name}: ok={c['ok']:<3} fallos={errors:<3} [{instances}]")
            print(f"t={shown:>3}s  " + " | ".join(cells))
        shown += 1
        if shown >= args.seconds:
            break

    print("\n=== Totales ===")
    for name in targets:
        c = totals[name]
        total = sum(v for k, v in c.items() if not k.startswith("@"))
        ok = c["ok"]
        print(f"{name}: {ok}/{total} correctas ({100 * ok / max(total, 1):.1f} %) · por instancia: "
              + ", ".join(f"{k[1:]}={v}" for k, v in sorted(c.items()) if k.startswith("@")))


if __name__ == "__main__":
    main()
