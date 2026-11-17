"""Fase 4: agregar bodegas con scripts/new_warehouse.py (de 3 a 100 bodegas sin copiar a mano)."""

import importlib.util
import shutil
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("new_warehouse", ROOT / "scripts" / "new_warehouse.py")
new_warehouse = importlib.util.module_from_spec(spec)
spec.loader.exec_module(new_warehouse)


@pytest.fixture
def components(tmp_path):
    for name in ("wh_001", "wh_002", "wh_003"):
        shutil.copytree(ROOT / "docker" / "components" / name, tmp_path / name)
    return tmp_path


def env(path):
    return dict(line.split("=", 1) for line in path.read_text().splitlines() if "=" in line and not line.startswith("#"))


def test_la_bodega_nueva_copia_la_ultima_y_mueve_los_puertos(components):
    created = env(new_warehouse.create_warehouse("WH-004", components))
    last = env(components / "wh_003" / ".env")
    assert created["WH_ID"] == "WH-004"
    for key in new_warehouse.PORT_KEYS:
        assert int(created[key]) == int(last[key]) + 1
    assert created["DB_USER"] == last["DB_USER"]          # lo demás se conserva


def test_saltar_numeros_mueve_los_puertos_lo_mismo(components):
    created = env(new_warehouse.create_warehouse("WH-010", components))
    assert int(created["BE_PORT"]) == int(env(components / "wh_003" / ".env")["BE_PORT"]) + 7


def test_errores_claros(components):
    with pytest.raises(new_warehouse.WarehouseError, match="WH-NNN"):
        new_warehouse.create_warehouse("bodega4", components)
    with pytest.raises(new_warehouse.WarehouseError, match="Ya existe"):
        new_warehouse.create_warehouse("WH-002", components)


def test_salta_los_puertos_ocupados(components):
    # Otro componente del PC ya usa 8004 (lo que le tocaría a la API de WH-004): se toma el siguiente libre
    (components / "otro").mkdir()
    (components / "otro" / ".env").write_text("ALGO_PORT=8004\n")
    created = env(new_warehouse.create_warehouse("WH-004", components))
    assert created["BE_PORT"] == "8005" and created["FE_PORT"] == "8084"


def test_cien_bodegas(components):
    for n in range(4, 101):
        new_warehouse.create_warehouse(f"WH-{n:03d}", components)
    ports = [int(v) for d in components.iterdir() for k, v in env(d / ".env").items() if k.endswith("_PORT")]
    assert len(ports) == len(set(ports)) == 100 * len(new_warehouse.PORT_KEYS)   # ningún puerto repetido
