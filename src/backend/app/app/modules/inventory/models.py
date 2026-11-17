from sqlalchemy import CheckConstraint, Column, DateTime, ForeignKey, Index, Integer, String, UniqueConstraint

from app.database import Base
from app.modules.shared.utils import schemas_utils

MOVEMENT_TYPES = (
    "RECEIPT",       # entrada (compra / recepción)
    "SHIPMENT",      # salida (venta / despacho)
    "ADJUSTMENT",    # ajuste por desfase (conteo físico distinto al sistema)
    "RESERVATION",   # apartar unidades
    "RELEASE",       # liberar unidades apartadas
    "TRANSFER_OUT",  # salida hacia otra bodega (Fase 6: solo la registra la Saga)
    "TRANSFER_IN",   # entrada desde otra bodega (Fase 6: solo la registra la Saga)
    "RETURN",        # devolución
)

_types_sql = ", ".join(f"'{t}'" for t in MOVEMENT_TYPES)


class InventoryEntity(Base):
    """
    Saldo actual de un producto en ESTA bodega.
      quantity           -> unidades DISPONIBLES (se pueden vender / despachar)
      reserved_quantity  -> unidades APARTADAS (reservadas, no disponibles)
    Total físico en estantería = quantity + reserved_quantity
    """

    __tablename__ = "inventory"
    __table_args__ = (
        UniqueConstraint("product_id", "warehouse_id", name="uq_inventory_product_warehouse"),
        # Última línea de defensa: aunque el código fallara, la BD nunca acepta negativos.
        CheckConstraint("quantity >= 0", name="ck_inventory_quantity_non_negative"),
        CheckConstraint("reserved_quantity >= 0", name="ck_inventory_reserved_non_negative"),
    )

    id = Column(Integer, primary_key=True)
    product_id = Column(Integer, ForeignKey("products.id"), nullable=False)
    warehouse_id = Column(String(20), nullable=False)
    quantity = Column(Integer, nullable=False, default=0)
    reserved_quantity = Column(Integer, nullable=False, default=0)
    # Fase 3: sube en 1 con cada movimiento. Quien recibe los eventos (la central)
    # aplica un saldo solo si su versión es mayor: así un evento repetido o
    # atrasado nunca "pisa" un dato más nuevo.
    version = Column(Integer, nullable=False, default=0)
    updated_at = Column(
        DateTime(timezone=True),
        nullable=False,
        default=schemas_utils.current_utc_time,
        onupdate=schemas_utils.current_utc_time,
    )


class InventoryMovementEntity(Base):
    """
    Historial (kardex) de cada operación. Nunca se borra ni se edita:
    es la base de los reportes de compras/ventas que pide el PDF.
    quantity_after / reserved_after = saldo que quedó DESPUÉS del movimiento.
    """

    __tablename__ = "inventory_movements"
    __table_args__ = (
        CheckConstraint(f"type IN ({_types_sql})", name="ck_movements_type"),
        CheckConstraint("quantity <> 0", name="ck_movements_quantity_not_zero"),
        Index("ix_movements_product_created", "product_id", "created_at"),
    )

    id = Column(Integer, primary_key=True)
    product_id = Column(Integer, ForeignKey("products.id"), nullable=False)
    warehouse_id = Column(String(20), nullable=False)
    type = Column(String(20), nullable=False)
    quantity = Column(Integer, nullable=False)  # positivo; en ADJUSTMENT el signo indica +/-
    quantity_after = Column(Integer, nullable=False)
    reserved_after = Column(Integer, nullable=False)
    lot = Column(String(100))
    reference = Column(String(200))  # factura, guía, motivo del ajuste...
    created_at = Column(DateTime(timezone=True), nullable=False, default=schemas_utils.current_utc_time)
