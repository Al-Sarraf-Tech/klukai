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

# ── Deferred-task rail ──────────────────────────────────────────────────────
# The bridge is the only process that speaks AMQP, so it also runs the timer
# for companion-core's one-shot deferred tasks ("remind him in three hours").
# Core publishes a `defer.arm` event over the same Redis channel; the bridge
# parks the task in a fixed-TTL bucket queue that dead-letters onto the due
# queue when it expires. That is the timer. On delivery the bridge calls core
# back over HTTP, so core never needs an AMQP client.
DEFER_EXCHANGE = os.environ.get("DEFER_EXCHANGE", "klukai.defer")
DUE_EXCHANGE = os.environ.get("DUE_EXCHANGE", "klukai.due")
DUE_QUEUE = os.environ.get("DUE_QUEUE", "klukai.due.tasks")
DELAY_BUCKETS = (10, 60, 300, 900, 3600, 21600, 86400)
CORE_URL = os.environ.get("CORE_URL", "http://companion-core:8300")
CORE_INTERNAL_TOKEN = os.environ.get("CORE_INTERNAL_TOKEN", "")


def defer_queue(bucket: int) -> str:
    return f"klukai.defer.{bucket}s"


def declare_defer_topology(ch) -> None:
    """Declare the delay buckets and the due queue. Idempotent.

    Fixed per-queue TTL, deliberately not per-message TTL: RabbitMQ only expires
    the message at the *head* of a queue, so a single long-delay message would
    hold back every shorter one queued behind it. With one queue per delay,
    everything inside expires in arrival order.
    """
    ch.exchange_declare(exchange=DEFER_EXCHANGE, exchange_type="direct", durable=True)
    ch.exchange_declare(exchange=DUE_EXCHANGE, exchange_type="direct", durable=True)
    ch.queue_declare(queue=DUE_QUEUE, durable=True)
    ch.queue_bind(exchange=DUE_EXCHANGE, queue=DUE_QUEUE, routing_key=DUE_QUEUE)
    for bucket in DELAY_BUCKETS:
        name = defer_queue(bucket)
        ch.queue_declare(
            queue=name,
            durable=True,
            arguments={
                "x-message-ttl": bucket * 1000,
                "x-dead-letter-exchange": DUE_EXCHANGE,
                "x-dead-letter-routing-key": DUE_QUEUE,
            },
        )
        ch.queue_bind(exchange=DEFER_EXCHANGE, queue=name, routing_key=name)


def arm_defer(ch, payload: dict) -> bool:
    """Park a task in its next delay bucket. Returns False if nothing to do."""
    task_id = str(payload.get("task_id") or "")
    hops = payload.get("hops") or []
    if not task_id or not isinstance(hops, list) or not hops:
        return False
    try:
        head = int(hops[0])
    except (TypeError, ValueError):
        return False
    if head not in DELAY_BUCKETS:
        print(f"bridge: unknown delay bucket {head}, dropping", flush=True)
        return False
    rest = hops[1:]
    ch.basic_publish(
        exchange=DEFER_EXCHANGE,
        routing_key=defer_queue(head),
        body=json.dumps({"task_id": task_id, "hops": rest}),
        properties=pika.BasicProperties(
            delivery_mode=2, content_type="application/json"
        ),
    )
    return True


def notify_core_due(task_id: str) -> bool:
    """Tell companion-core a deferred task is due.

    Core owns the durable record and the idempotency check, so a duplicate call
    here is harmless -- it simply matches no pending row.
    """
    import urllib.error
    import urllib.request

    body = json.dumps({"task_id": task_id}).encode()
    req = urllib.request.Request(
        f"{CORE_URL}/internal/deferred/fire",
        data=body,
        headers={
            "Content-Type": "application/json",
            "X-Internal-Token": CORE_INTERNAL_TOKEN,
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return 200 <= resp.status < 300
    except urllib.error.HTTPError as e:
        print(f"bridge: core rejected due task {task_id}: HTTP {e.code}", flush=True)
        return False
    except Exception as e:
        print(f"bridge: could not reach core for {task_id}: {e}", flush=True)
        return False


def drain_due(ch) -> int:
    """Deliver every task whose timer has expired.

    Each message is acked only after core has accepted it, so a core restart
    mid-delivery redelivers rather than drops. A task whose remaining hops are
    non-empty is re-parked in the next bucket instead of being delivered.
    """
    delivered = 0
    while True:
        method, _props, body = ch.basic_get(queue=DUE_QUEUE, auto_ack=False)
        if method is None:
            return delivered
        try:
            payload = json.loads(body)
            task_id = str(payload.get("task_id") or "")
            hops = payload.get("hops") or []
        except Exception:
            ch.basic_ack(method.delivery_tag)  # unparseable: drop, don't loop
            continue

        if not task_id:
            ch.basic_ack(method.delivery_tag)
            continue

        if hops:
            # More waiting to do -- hop to the next bucket.
            if arm_defer(ch, {"task_id": task_id, "hops": hops}):
                ch.basic_ack(method.delivery_tag)
            else:
                ch.basic_nack(method.delivery_tag, requeue=False)
            continue

        if notify_core_due(task_id):
            ch.basic_ack(method.delivery_tag)
            delivered += 1
        else:
            # Leave it queued; core's own sweeper is the backstop either way.
            ch.basic_nack(method.delivery_tag, requeue=True)
            return delivered


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
    # The deferred rail lives on the same connection; declaring it here means a
    # reconnect re-establishes it without a separate code path.
    try:
        declare_defer_topology(ch)
    except Exception as exc:
        print(f"bridge: defer topology declare failed ({exc})", flush=True)
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
            # Idle tick — service the AMQP socket so the connection stays alive,
            # and deliver any deferred task whose timer expired.
            try:
                conn.process_data_events(0)
                drain_due(ch)
            except Exception as exc:
                print(f"bridge: idle tick failed ({exc}), reconnecting...",
                      flush=True)
                conn, ch = _reconnect(conn)
            continue
        if message.get("type") != "message":
            continue

        payload = parse_payload(message.get("data") or "")

        # Deferred-task arm requests are handled here, not forwarded to the bus.
        if str(payload.get("type") or "") == "defer.arm":
            try:
                if arm_defer(ch, payload):
                    print(f"bridge: armed defer task {payload.get('task_id')} "
                          f"hops={payload.get('hops')}", flush=True)
            except Exception as exc:
                print(f"bridge: arm failed ({exc}), reconnecting...", flush=True)
                conn, ch = _reconnect(conn)
                try:
                    arm_defer(ch, payload)
                except Exception as exc2:
                    print(f"bridge: arm retry failed ({exc2}); core sweeper "
                          f"will cover it", flush=True)
            continue

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
