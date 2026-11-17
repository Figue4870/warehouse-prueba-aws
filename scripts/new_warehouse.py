r"""
Agrega una bodega nueva copiando la configuración de la última y moviendo sus puertos.
Con esto, pasar de 3 a 100 bodegas es repetir un comando (pregunta final de la guía).

  python scripts/new_warehouse.py WH-004
  .\manage.ps1 start wh_004

Probado en tests/unit/test_new_warehouse.py.
"""

import re
import sys
from pathlib import Path

PORT_KEYS = ["DB_PORT", "BE_PORT", "FE_PORT", "REDIS_PORT", "RABBIT_PORT", "RABBIT_MGMT_PORT"]
COMPONENTS = Path(__file__).resolve().parent.parent / "docker" / "components"


class WarehouseError(ValueError):
    pass


def used_ports(components: Path) -> set[int]:
    """Puertos que ya usa cualquier componente (bodegas y lo que se agregue después) en este PC."""
    ports = set()
    for env in components.glob("*/.env"):
        ports |= {int(port) for port in re.findall(r"^\w+_PORT=(\d+)$", env.read_text(encoding="utf-8"), re.M)}
    return ports


def create_warehouse(wh_id: str, components: Path = COMPONENTS) -> Path:
    """Crea components/wh_NNN/.env a partir de la bodega con número más alto. Devuelve la ruta del .env."""
    if not re.fullmatch(r"WH-\d{3}", wh_id):
        raise WarehouseError("El código debe ser WH-NNN, por ejemplo WH-004")
    target = components / f"wh_{wh_id[3:]}"
    if target.exists():
        raise WarehouseError(f"Ya existe {target}")
    existing = sorted(p for p in components.iterdir() if p.is_dir() and re.fullmatch(r"wh_\d{3}", p.name))
    if not existing:
        raise WarehouseError(f"No hay ninguna bodega en {components} para copiar")
    template = existing[-1]
    text = (template / ".env").read_text(encoding="utf-8")
    old_id = re.search(r"^WH_ID=(.+)$", text, re.M).group(1).strip()
    step = int(wh_id[3:]) - int(old_id[3:])
    text = text.replace(old_id, wh_id)
    used = used_ports(components)
    for key in PORT_KEYS:
        match = re.search(rf"^{key}=(\d+)$", text, re.M)
        if match is None:
            continue
        port = int(match.group(1)) + step
        # Con muchas bodegas los rangos se cruzan (ej. la API de WH-081 sería 8081 = la web de WH-001):
        # si el puerto ya está ocupado se toma el siguiente libre.
        while port in used:
            port += 1
        used.add(port)
        text = re.sub(rf"^{key}=\d+$", f"{key}={port}", text, flags=re.M)
    target.mkdir()
    env_file = target / ".env"
    env_file.write_text(text, encoding="utf-8")
    return env_file


def main():
    if len(sys.argv) != 2:
        sys.exit("Uso: python scripts/new_warehouse.py WH-004")
    try:
        env_file = create_warehouse(sys.argv[1])
    except WarehouseError as error:
        sys.exit(str(error))
    text = env_file.read_text(encoding="utf-8")
    print(f"Creada {env_file}:")
    print("\n".join(line for line in text.splitlines() if line.split("=")[0] in PORT_KEYS + ["WH_ID"]))


if __name__ == "__main__":
    main()
