"""Fase 5 contra la bodega REAL: el balanceador reparte entre las 3 instancias."""


def test_el_balanceador_reparte_entre_instancias(api):
    instances = {api.get("/health").headers.get("X-Instance") for _ in range(12)}
    assert len(instances) >= 2, f"Solo respondió {instances}: ¿están arriba api_1, api_2 y api_3?"


def test_todas_las_instancias_ven_los_mismos_datos(api, new_product):
    """Stateless: el saldo es el mismo sin importar qué instancia responda."""
    product = new_product(initial_stock=7)
    seen = {}
    for _ in range(9):
        response = api.get(f"/products/{product['id']}")
        seen[response.headers.get("X-Instance")] = response.json()["sku"]
    assert set(seen.values()) == {product["sku"]}
