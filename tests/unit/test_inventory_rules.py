"""Pruebas de la función pura calculate_new_balance (sin base de datos)."""

import pytest

from app.modules.inventory.inventory_rules import calculate_new_balance
from app.modules.inventory.schemas import MovementType as T
from app.modules.shared.errors import BusinessRuleError


def test_entrada_suma_al_disponible():
    assert calculate_new_balance(T.RECEIPT, 5, available=10, reserved=0) == (15, 0)


def test_ejemplo_del_readme_salida_de_4_sobre_10_deja_6():
    assert calculate_new_balance(T.SHIPMENT, 4, available=10, reserved=0) == (6, 0)


def test_ejemplo_del_readme_salida_de_15_sobre_10_se_rechaza():
    with pytest.raises(BusinessRuleError, match="Stock insuficiente"):
        calculate_new_balance(T.SHIPMENT, 15, available=10, reserved=0)


def test_salida_exacta_deja_cero_y_no_negativo():
    assert calculate_new_balance(T.SHIPMENT, 10, available=10, reserved=0) == (0, 0)


def test_las_unidades_reservadas_no_se_pueden_vender():
    # 3 disponibles + 7 reservadas: vender 5 debe fallar
    with pytest.raises(BusinessRuleError):
        calculate_new_balance(T.SHIPMENT, 5, available=3, reserved=7)


def test_reserva_pasa_de_disponible_a_reservado():
    assert calculate_new_balance(T.RESERVATION, 4, available=10, reserved=1) == (6, 5)


def test_reserva_mayor_al_disponible_se_rechaza():
    with pytest.raises(BusinessRuleError):
        calculate_new_balance(T.RESERVATION, 11, available=10, reserved=0)


def test_liberar_reserva_devuelve_al_disponible():
    assert calculate_new_balance(T.RELEASE, 3, available=2, reserved=5) == (5, 2)


def test_liberar_mas_de_lo_reservado_se_rechaza():
    with pytest.raises(BusinessRuleError):
        calculate_new_balance(T.RELEASE, 6, available=2, reserved=5)


def test_ajuste_positivo_y_negativo():
    assert calculate_new_balance(T.ADJUSTMENT, 3, available=10, reserved=0) == (13, 0)
    assert calculate_new_balance(T.ADJUSTMENT, -4, available=10, reserved=0) == (6, 0)


def test_ajuste_que_dejaria_negativo_se_rechaza():
    with pytest.raises(BusinessRuleError):
        calculate_new_balance(T.ADJUSTMENT, -11, available=10, reserved=0)


@pytest.mark.parametrize("m_type", [T.TRANSFER_IN, T.RETURN])
def test_transfer_in_y_devolucion_suman(m_type):
    assert calculate_new_balance(m_type, 2, available=1, reserved=0) == (3, 0)


def test_transfer_out_no_puede_dejar_negativo():
    with pytest.raises(BusinessRuleError):
        calculate_new_balance(T.TRANSFER_OUT, 2, available=1, reserved=0)


def test_salida_por_transferencia_toma_de_la_reserva():
    # Fase 6: la Saga primero reservó 30; al confirmar el destino, salen de lo reservado
    assert calculate_new_balance(T.TRANSFER_OUT, 30, available=70, reserved=30, from_reservation=True) == (70, 0)


def test_salida_por_transferencia_sin_reserva_suficiente_se_rechaza():
    with pytest.raises(BusinessRuleError):
        calculate_new_balance(T.TRANSFER_OUT, 31, available=100, reserved=30, from_reservation=True)
    with pytest.raises(BusinessRuleError):
        calculate_new_balance(T.SHIPMENT, 1, available=100, reserved=30, from_reservation=True)
