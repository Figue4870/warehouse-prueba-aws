"""
Librería compartida por la bodega (warehouse API + workers) y, desde la Fase 7,
por la plataforma central. Contiene:

  events.py       -> contrato de eventos (sobre / envelope)
  correlation.py  -> correlation_id para seguir una operación de punta a punta
  messaging.py    -> RabbitMQ confiable: publisher confirms, reintentos y DLQ
"""
