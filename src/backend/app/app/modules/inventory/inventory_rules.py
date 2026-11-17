"""
Reglas de negocio del inventario como FUNCIONES PURAS.

"Pura" significa: no toca la base de datos ni Redis; recibe números y
devuelve números. Por eso es muy fácil de probar (tests/unit/test_inventory_rules.py)
y es el lugar único donde vive la regla más importante de la Fase 1:

    El inventario NUNCA puede quedar negativo.
"""

from typing import Tuple

from app.modules.inventory.schemas import MovementType
from app.modules.shared.errors import BusinessRuleError

INCREASES_AVAILABLE = {MovementType.RECEIPT, MovementType.TRANSFER_IN, MovementType.RETURN}
DECREASES_AVAILABLE = {MovementType.SHIPMENT, MovementType.TRANSFER_OUT}


def calculate_new_balance(
    movement_type: MovementType, quantity: int, available: int, reserved: int, from_reservation: bool = False
) -> Tuple[int, int]:
    """
    Calcula el nuevo saldo (disponible, reservado) después de un movimiento.
    Lanza BusinessRuleError si el movimiento dejaría algo en negativo.

    from_reservation=True (Fase 6, solo TRANSFER_OUT de la Saga): las unidades
    salen de lo RESERVADO, no de lo disponible. Primero la Saga las reservó y,
    cuando el destino confirmó que las recibió, se despachan.
    """
    movement_type = MovementType(movement_type)

    if from_reservation:
        if movement_type != MovementType.TRANSFER_OUT:
            raise BusinessRuleError("Solo una salida por transferencia puede tomar unidades de la reserva")
        if quantity > reserved:
            raise BusinessRuleError(f"La reserva no alcanza. Reservado: {reserved}, requerido: {quantity}")
        return available, reserved - quantity

    if movement_type in INCREASES_AVAILABLE:
        return available + quantity, reserved

    if movement_type in DECREASES_AVAILABLE:
        if quantity > available:
            raise BusinessRuleError(f"Stock insuficiente. Disponible: {available}, requerido: {quantity}")
        return available - quantity, reserved

    if movement_type == MovementType.RESERVATION:
        if quantity > available:
            raise BusinessRuleError(f"Stock insuficiente para reservar. Disponible: {available}, requerido: {quantity}")
        return available - quantity, reserved + quantity

    if movement_type == MovementType.RELEASE:
        if quantity > reserved:
            raise BusinessRuleError(f"La liberación excede lo reservado. Reservado: {reserved}, solicitado: {quantity}")
        return available + quantity, reserved - quantity

    if movement_type == MovementType.ADJUSTMENT:
        # quantity con signo: +5 = aparecieron 5 unidades, -3 = faltan 3 unidades
        new_available = available + quantity
        if new_available < 0:
            raise BusinessRuleError(
                f"El ajuste dejaría el inventario en negativo. Disponible: {available}, ajuste: {quantity}"
            )
        return new_available, reserved

    raise BusinessRuleError(f"Tipo de movimiento no soportado: {movement_type}")
