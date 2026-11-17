from datetime import datetime
from decimal import Decimal
from typing import Annotated, Optional

from pydantic import BaseModel, ConfigDict, Field

# Dinero: Decimal (no float) para no perder centavos por redondeo.
# En el JSON de respuesta aparece como texto, por ejemplo "15000.00".
Money = Annotated[Decimal, Field(ge=0, max_digits=14, decimal_places=2)]


class ProductBase(BaseModel):
    name: str = Field(..., min_length=1, max_length=200, description="Nombre universal del producto")
    description: Optional[str] = None
    category: Optional[str] = Field(None, max_length=100)
    unit: str = Field(..., min_length=1, max_length=30, description="Unidad de medida: UNIDAD, CAJA, KG...")
    supplier: Optional[str] = Field(None, max_length=200, description="Proveedor asociado")
    lot: Optional[str] = Field(None, max_length=100, description="Lote")
    supplier_cost: Optional[Money] = Field(None, description="Costo / precio del proveedor")
    retail_price: Optional[Money] = Field(None, description="Precio de venta en tienda")
    wholesale_price: Optional[Money] = Field(None, description="Precio para mayoristas / terceros")


class ProductCreate(ProductBase):
    model_config = ConfigDict(str_strip_whitespace=True)

    sku: str = Field(
        ...,
        min_length=1,
        max_length=64,
        pattern=r"^[A-Za-z0-9._-]+$",
        description="Código único universal. Solo letras, números, punto, guion y guion bajo.",
    )


class ProductUpdate(BaseModel):
    """
    Datos que se pueden cambiar con PUT /products/{id}. Todos son opcionales:
    solo se modifican los campos que se envían.
    El SKU NO se puede cambiar (es el identificador universal del producto
    y en fases futuras lo usarán otras bodegas y la plataforma central).
    """

    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    name: Optional[str] = Field(None, min_length=1, max_length=200)
    description: Optional[str] = None
    category: Optional[str] = Field(None, max_length=100)
    unit: Optional[str] = Field(None, min_length=1, max_length=30)
    supplier: Optional[str] = Field(None, max_length=200)
    lot: Optional[str] = Field(None, max_length=100)
    supplier_cost: Optional[Money] = None
    retail_price: Optional[Money] = None
    wholesale_price: Optional[Money] = None


class ProductResponse(ProductBase):
    model_config = ConfigDict(from_attributes=True)

    id: int
    sku: str
    is_active: bool
    version: int = 1
    created_at: datetime
    updated_at: datetime
