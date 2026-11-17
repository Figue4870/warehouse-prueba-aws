"""Tipos de evento que publica una bodega (routing key = tipo en minúsculas con puntos)."""

PRODUCT_CREATED = "PRODUCT_CREATED"                          # product.created
PRODUCT_UPDATED = "PRODUCT_UPDATED"                          # product.updated
PRODUCT_DEACTIVATED = "PRODUCT_DEACTIVATED"                  # product.deactivated
INVENTORY_MOVEMENT_RECORDED = "INVENTORY_MOVEMENT_RECORDED"  # inventory.movement.recorded
STOCK_DEPLETED = "STOCK_DEPLETED"                            # stock.depleted   (alerta: agotado)
STOCK_DISCREPANCY = "STOCK_DISCREPANCY"                      # stock.discrepancy (alerta: desfase)
REPORT_REQUESTED = "REPORT_REQUESTED"                        # report.requested
WAREHOUSE_HEARTBEAT = "WAREHOUSE_HEARTBEAT"                  # warehouse.heartbeat (Fase 7, no pasa por el outbox)

# Fase 6 — respuestas de la bodega a los comandos de la Saga (contrato en platform_common/commands.py)
from platform_common.commands import (  # noqa: E402,F401
    TRANSFER_DESTINATION_CANCELLED,
    TRANSFER_DESTINATION_RECEIVED,
    TRANSFER_DESTINATION_REJECTED,
    TRANSFER_SOURCE_COMMIT_FAILED,
    TRANSFER_SOURCE_COMMITTED,
    TRANSFER_SOURCE_REJECTED,
    TRANSFER_SOURCE_RELEASED,
    TRANSFER_SOURCE_RESERVED,
)
