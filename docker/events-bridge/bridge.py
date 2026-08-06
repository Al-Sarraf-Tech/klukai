"""Bridge companion Redis pub/sub events onto the homelab RabbitMQ bus.

Topology (amarillo):
  companion-core --(Redis PUBLISH companion:events)--> aichat-redis
       ^
       |
  this bridge --(SUBSCRIBE)--+
                             |
                             +--(AMQP)--> rabbitmq@100.111.198.19:5672
                                           exchange: homelab.events (topic)
                                           routing: host.<host>.companion.<type>
                                                    host.<host>.gpu_lease.<workload>.<action>

Why a bridge (not an in-process AMQP client in companion-core):
  - companion-core already reaches the Tailscale-bound broker, but adding pika
    would force a hashed requirements recompile of the heavy core image.
  - The homelab bus already has docker + gpu_lease consumers (rabbitmq-ops /
    rabbitmq-metrics). Companion lifecycle events belong on the same bus so
    Grafana/notifiarr/future workers can see them without knowing Redis.

Fail-soft: reconnect loops on either side; never crash-loop the host.
"""
from __future__ import annotations

import json
import os
import socket
import time

import pika
import redis

REDIS_URL = os.environ.get("REDIS_URL", "redis://aichat-redis:6379/1")
CHANNEL = os.environ.get("REDIS_CHANNEL", "companion:events")
RABBIT_HOST = os.environ.get("RABBIT_HOST", "100.111.198.19")
RABBIT_PORT = int(os.environ.get("RABBIT_PORT", "5672"))
RABBIT_USER = os.environ.get("RABBIT_USER", "homelab")
RABBIT_PASS = os.environ["RABBIT_PASS"]
EXCHANGE = os.environ.get("RABBIT_EXCHANGE", "homelab.events")
LOCAL_HOSTNAME = os.environ.get("LOCAL_HOSTNAME", socket.gethostname().split(".")[0])
# Heartbeat is 30s; poll well inside that so idle ticks keep the AMQP socket fed.
POLL_TIMEOUT = float(os.environ.get("BRIDGE_POLL_TIMEOUT", "5.0"))


def rabbit_connect():
    creds = pika.PlainCredentials(RABBIT_USER, RABBIT_PASS)
    params = pika.ConnectionParameters(
        host=RABBIT_HOST,
        port=RABBIT_PORT,
        credentials=creds,
        heartbeat=30,
        blocked_connection_timeout=30,
        connection_attempts=3,
        retry_delay=2,
    )
    conn = pika.BlockingConnection(params)
    ch = conn.channel()
    ch.exchange_declare(exchange=EXCHANGE, exchange_type="topic", durable=True)
    return conn, ch


def routing_and_body(payload: dict) -> tuple[str, dict]:
    """Map a companion Redis event onto a homelab.events routing key + body."""
    etype = str(payload.get("type") or "unknown").replace(" ", "_")
    # GPU lease markers: type like "gpu_lease.acquired" or explicit fields
    if etype.startswith("gpu_lease.") or payload.get("domain") == "gpu_lease":
        parts = etype.split(".")
        action = parts[-1] if len(parts) > 1 else str(payload.get("action", "unknown"))
        workload = str(payload.get("workload") or payload.get("data") or "unknown")
        workload = workload.replace(" ", "_")[:64]
        rk = f"host.{LOCAL_HOSTNAME}.gpu_lease.{workload}.{action}"
        body = {
            "host": LOCAL_HOSTNAME,
            "domain": "gpu_lease",
            "workload": workload,
            "action": action,
            "source": "klukai-companion",
            "time": int(time.time()),
            "payload": payload,
        }
        return rk, body

    # Default companion lifecycle
    rk = f"host.{LOCAL_HOSTNAME}.companion.{etype}"
    body = {
        "host": LOCAL_HOSTNAME,
        "domain": "companion",
        "type": etype,
        "action": etype,
        "source": "klukai-companion",
        "time": int(time.time()),
        "data": payload.get("data", ""),
        "payload": payload,
    }
    return rk, body


def parse_payload(raw) -> dict:
    """Normalize a raw Redis pub/sub payload into an event dict."""
    try:
        payload = json.loads(raw) if isinstance(raw, str) else {}
        if not isinstance(payload, dict):
            payload = {"type": "unknown", "data": raw}
    except json.JSONDecodeError:
        payload = {"type": "raw", "data": raw}
    return payload


def run():
    r = redis.Redis.from_url(REDIS_URL, decode_responses=True)
    pubsub = r.pubsub(ignore_subscribe_messages=True)
    pubsub.subscribe(CHANNEL)
    conn, ch = rabbit_connect()
    published = 0
    print(
        f"bridge: subscribed {CHANNEL} @ {REDIS_URL} -> "
        f"amqp://{RABBIT_HOST}:{RABBIT_PORT}/{EXCHANGE} as host.{LOCAL_HOSTNAME}.*",
        flush=True,
    )
    # Poll rather than block in pubsub.listen(): companion events are sparse
    # (check-ins, GPU lease pairs), so a blocking read would starve the AMQP
    # connection of heartbeats and the broker would drop us between events.
    while True:
        message = pubsub.get_message(timeout=POLL_TIMEOUT)
        if message is None:
            # Idle tick — service the AMQP socket so the connection stays alive.
            try:
                conn.process_data_events(0)
            except Exception as exc:
                print(f"bridge: idle heartbeat failed ({exc}), reconnecting...",
                      flush=True)
                conn, ch = _reconnect(conn)
            continue
        if message.get("type") != "message":
            continue

        payload = parse_payload(message.get("data") or "")
        rk, body = routing_and_body(payload)
        encoded = json.dumps(body, default=str)

        # One retry across a reconnect: the publish that discovers a dead
        # connection must not be the event we throw away.
        for attempt in (1, 2):
            try:
                ch.basic_publish(
                    exchange=EXCHANGE,
                    routing_key=rk,
                    body=encoded,
                    properties=pika.BasicProperties(
                        delivery_mode=2,
                        content_type="application/json",
                    ),
                )
                published += 1
                if published % 20 == 0:
                    print(f"bridge: published {published} events (last rk={rk})",
                          flush=True)
                break
            except Exception as exc:
                if attempt == 2:
                    print(f"bridge: dropping event after retry ({exc}) rk={rk}",
                          flush=True)
                    break
                print(f"bridge: publish error ({exc}), reconnecting rabbit...",
                      flush=True)
                conn, ch = _reconnect(conn)


def _reconnect(conn):
    try:
        conn.close()
    except Exception:
        pass
    return rabbit_connect()


if __name__ == "__main__":
    while True:
        try:
            run()
        except Exception as exc:
            print(f"bridge: fatal ({exc}), retry in 5s", flush=True)
            time.sleep(5)
