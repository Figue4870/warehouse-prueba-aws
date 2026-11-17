"""Pruebas de los endpoints de productos (Fase 1)."""


def test_crear_producto_con_campos_del_pdf_e_inventario_en_cero(client):
    payload = {
        "sku": "EPP-CASCO-001",
        "name": "Casco de seguridad",
        "unit": "UNIDAD",
        "category": "EPP",
        "supplier": "Seguridad Industrial del Valle",
        "lot": "L-2026-10-A",
        "supplier_cost": "18000.00",
        "retail_price": 32000,
        "wholesale_price": "26000.5",
    }
    response = client.post("/products", json=payload)
    assert response.status_code == 201
    product = response.json()
    assert product["sku"] == "EPP-CASCO-001"
    assert product["supplier"] == "Seguridad Industrial del Valle"
    assert product["lot"] == "L-2026-10-A"
    assert product["retail_price"] == "32000.00"  # Decimal: llega como texto
    assert product["wholesale_price"] == "26000.50"
    assert product["is_active"] is True

    stock = client.get(f"/inventory/{product['id']}").json()
    assert stock["quantity"] == 0 and stock["reserved_quantity"] == 0
    assert stock["warehouse_id"] == "WH-TEST"


def test_sku_duplicado_devuelve_409(client, create_product):
    create_product(sku="DUP-1")
    response = client.post("/products", json={"sku": "DUP-1", "name": "Otro", "unit": "UNIDAD"})
    assert response.status_code == 409


def test_validaciones_de_entrada_devuelven_422(client):
    assert client.post("/products", json={"sku": "SIN NOMBRE", "unit": "U"}).status_code == 422
    assert client.post("/products", json={"sku": "tiene espacios", "name": "X", "unit": "U"}).status_code == 422
    assert client.post("/products", json={"sku": "NEG-1", "name": "X", "unit": "U", "retail_price": -1}).status_code == 422


def test_consultar_y_listar(client, create_product):
    a = create_product()
    b = create_product()
    assert client.get(f"/products/{a['id']}").json()["sku"] == a["sku"]
    assert [p["id"] for p in client.get("/products").json()] == [a["id"], b["id"]]
    assert client.get("/products/9999").status_code == 404


def test_actualizar_producto_con_put(client, create_product):
    product = create_product()
    response = client.put(f"/products/{product['id']}", json={"name": "Nuevo nombre", "retail_price": "1500"})
    assert response.status_code == 200
    assert response.json()["name"] == "Nuevo nombre"
    assert response.json()["retail_price"] == "1500.00"
    assert response.json()["unit"] == "UNIDAD"  # lo que no se envía no cambia


def test_el_sku_no_se_puede_cambiar(client, create_product):
    product = create_product()
    assert client.put(f"/products/{product['id']}", json={"sku": "OTRO"}).status_code == 422


def test_dar_de_baja_producto_sin_existencias(client, create_product, move):
    product = create_product()
    assert client.delete(f"/products/{product['id']}").status_code == 204
    # Ya no aparece en el listado normal, pero sí con include_inactive
    assert client.get("/products").json() == []
    inactive = client.get("/products", params={"include_inactive": True}).json()
    assert inactive[0]["is_active"] is False
    # Un producto dado de baja no admite movimientos
    assert move(product["id"], "RECEIPT", 5).status_code == 409


def test_no_se_puede_dar_de_baja_con_existencias(client, create_product, move):
    product = create_product()
    move(product["id"], "RECEIPT", 3)
    response = client.delete(f"/products/{product['id']}")
    assert response.status_code == 409
    assert "existencias" in response.json()["detail"]


def test_el_historial_se_conserva_despues_de_dar_de_baja(client, create_product, move):
    product = create_product()
    move(product["id"], "RECEIPT", 3)
    move(product["id"], "SHIPMENT", 3)
    client.delete(f"/products/{product['id']}")
    assert len(client.get(f"/inventory/{product['id']}/movements").json()) == 2
