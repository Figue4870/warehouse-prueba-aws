from datetime import datetime
from enum import Enum
from typing import List, Optional

from pydantic import BaseModel, ConfigDict, Field, model_validator


class MovementType(str, Enum):
    RECEIPT = "RECEIPT"
    SHIPMENT = "SHIPMENT"
    ADJUSTMENT = "ADJUSTMENT"
    RESERVATION = "RESERVATION"
    RELEASE = "RELEASE"
    TRANSFER_OUT = "TRANSFER_OUT"
    TRANSFER_IN = "TRANSFER_IN"
    RETURN = "RETURN"


class MovementCreate(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    product_id: int = Field(..., gt=0)
    type: MovementType = Field(..., description="RECEIPT, SHIPMENT, ADJUSTMENT, RESERVATION, RELEASE, TRANSFER_OUT, TRANSFER_IN, RETURN")
    quantity: int = Field(
        ...,
        description="Unidades. Siempre > 0, excepto en ADJUSTMENT: positivo suma, negativo resta.",
    )
    lot: Optional[str] = Field(None, max_length=100)
    reference: Optional[str] = Field(None, max_length=200, description="Factura, guía, motivo del ajuste...")

    @model_validator(mode="after")
    def validate_quantity(self):
        if self.type == MovementType.ADJUSTMENT:
            if self.quantity == 0:
                raise ValueError("En un ADJUSTMENT la cantidad no puede ser 0 (positivo suma, negativo resta)")
        elif self.quantity <= 0:
            raise ValueError("La cantidad debe ser mayor que 0")
        return self


class MovementResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    product_id: int
    warehouse_id: str
    type: str
    quantity: int
    quantity_after: int
    reserved_after: int
    lot: Optional[str]
    reference: Optional[str]
    created_at: datetime


class MovementResult(MovementResponse):
    """Respuesta de POST /inventory/movements: el movimiento + alertas que generó."""

    alerts_generated: List[str] = []


class InventoryResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    product_id: int
    warehouse_id: str
    quantity: int
    reserved_quantity: int
    version: int = 0
    updated_at: datetime
