from sqlalchemy import Column, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint

from app.database import Base
from app.modules.shared.utils import schemas_utils


class TransferOperationEntity(Base):
    """
    Fase 6 — lo que ESTA bodega hizo por cada paso de una transferencia (Saga).

    (transfer_id, step) es ÚNICO: es la llave de idempotencia. Si el comando
    "RESERVE de TR-000001" llega dos veces, la segunda vez ya existe la fila y
    no se reserva de nuevo (el mensaje repetido se ignora).

    También sirve de "marca": si llega RELEASE antes que RESERVE (o un RESERVE
    vencido), queda registrado y un RESERVE posterior se rechaza.

      step     RESERVE | RECEIVE | COMMIT | RELEASE | CANCEL_RECEIVE
      outcome  DONE (se hizo) | REJECTED (no se pudo, con reason) | NOOP (no había nada que hacer)
    """

    __tablename__ = "transfer_operations"
    __table_args__ = (UniqueConstraint("transfer_id", "step", name="uq_transfer_operations_step"),)

    id = Column(Integer, primary_key=True)
    transfer_id = Column(String(30), nullable=False, index=True)
    step = Column(String(20), nullable=False)
    outcome = Column(String(20), nullable=False)
    sku = Column(String(64))
    product_id = Column(Integer, ForeignKey("products.id"))
    quantity = Column(Integer)
    reason = Column(Text)
    movement_id = Column(Integer, ForeignKey("inventory_movements.id"))
    command_id = Column(String(64))
    created_at = Column(DateTime(timezone=True), nullable=False, default=schemas_utils.current_utc_time)
