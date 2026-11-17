"""
Herramienta para la Dead Letter Queue (DLQ): ningún mensaje se bota, se
revisa y se puede reprocesar cuando se arregla la causa del error.

  # ver qué hay en la DLQ de reportes de WH-001 (no los saca de la cola)
  python scripts/dlq.py list --queue wh.reports

  # devolverlos a su cola original para procesarlos otra vez
  python scripts/dlq.py replay --queue wh.reports

Conexión: --amqp (por defecto la de WH-001 publicada en tu PC: localhost:5673).
Requiere: pip install pika==1.3.2
"""

import argparse
import json

import pika

DEFAULT_AMQP = "amqp://warehouse:warehouse@localhost:5673/%2F"


def connect(url):
    return pika.BlockingConnection(pika.URLParameters(url))


def list_messages(url: str, queue: str, limit: int) -> None:
    connection = connect(url)
    channel = connection.channel()
    seen = 0
    while seen < limit:
        method, properties, body = channel.basic_get(queue=f"{queue}.dlq", auto_ack=False)
        if method is None:
            break
        seen += 1
        headers = properties.headers or {}
        print(json.dumps({
            "message_id": properties.message_id,
            "type": properties.type,
            "original_routing_key": headers.get("x-original-routing-key"),
            "error": headers.get("x-error"),
            "body": body.decode("utf-8", errors="replace")[:300],
        }, ensure_ascii=False, indent=2))
    # Sin ack: al cerrar la conexión los mensajes vuelven a la DLQ (solo mirar)
    connection.close()
    print(f"\n{seen} mensaje(s) mostrados de {queue}.dlq (siguen en la DLQ)")


def replay(url: str, queue: str, exchange: str, limit: int) -> None:
    connection = connect(url)
    channel = connection.channel()
    channel.confirm_delivery()
    moved = 0
    while moved < limit:
        method, properties, body = channel.basic_get(queue=f"{queue}.dlq", auto_ack=False)
        if method is None:
            break
        headers = dict(properties.headers or {})
        routing_key = headers.pop("x-original-routing-key", queue)
        headers.pop("x-error", None)
        headers["x-replayed"] = True
        new_props = pika.BasicProperties(
            content_type=properties.content_type, delivery_mode=2, message_id=properties.message_id,
            correlation_id=properties.correlation_id, type=properties.type, headers=headers,
        )
        # Publicar con confirmación y SOLO después sacarlo de la DLQ: nunca se pierde
        channel.basic_publish(exchange=exchange, routing_key=routing_key, body=body, properties=new_props, mandatory=True)
        channel.basic_ack(method.delivery_tag)
        moved += 1
    connection.close()
    print(f"{moved} mensaje(s) devueltos de {queue}.dlq a '{exchange}'")


def main():
    parser = argparse.ArgumentParser(description="Revisar o reprocesar la DLQ de una cola")
    parser.add_argument("action", choices=["list", "replay"])
    parser.add_argument("--queue", required=True, help="cola principal, ej. wh.reports")
    parser.add_argument("--amqp", default=DEFAULT_AMQP)
    parser.add_argument("--exchange", default="warehouse.events", help="exchange al que se republica")
    parser.add_argument("--limit", type=int, default=100)
    args = parser.parse_args()
    if args.action == "list":
        list_messages(args.amqp, args.queue, args.limit)
    else:
        replay(args.amqp, args.queue, args.exchange, args.limit)


if __name__ == "__main__":
    main()
