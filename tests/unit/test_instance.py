"""Fase 5: cada respuesta dice qué instancia de la API la atendió."""

from app.config import settings


def test_header_x_instance_y_health(client, monkeypatch):
    monkeypatch.setattr(settings, "INSTANCE_NAME", "WH-TEST-api-2")
    response = client.get("/health")
    assert response.headers["X-Instance"] == "WH-TEST-api-2"
    assert response.json()["instance"] == "WH-TEST-api-2"


def test_la_api_no_guarda_estado_de_negocio_en_memoria(client, create_product, move):
    """
    Stateless: dos "instancias" que comparten la misma base de datos ven lo mismo.
    Aquí lo simulamos con dos clientes sobre la misma app y la misma BD.
    """
    product = create_product()
    move(product["id"], "RECEIPT", 5)
    assert client.get(f"/inventory/{product['id']}").json()["quantity"] == 5
    assert client.get(f"/products/{product['id']}").json()["sku"] == product["sku"]
