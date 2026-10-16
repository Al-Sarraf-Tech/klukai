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
import threading
import uuid

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

# ── Durable Her POV work queue ──────────────────────────────────────────────
# One active HTTP job per bridge process. Core owns durable claims and any
# provider/GPU leases; the bridge is the only AMQP citizen.
JOBS_EXCHANGE = os.environ.get("JOBS_EXCHANGE", "klukai.jobs")
JOBS_QUEUE = os.environ.get("JOBS_QUEUE", "klukai.jobs.her_pov")
JOBS_ROUTING_KEY = os.environ.get("JOBS_ROUTING_KEY", "her_pov")
# Worst case for one job, end to end, before core answers the run hook:
#   LM gate wait (a chat turn in flight)          ~60s
#   gateway GPU lease acquire (read deadline)      150s
#   bounded ComfyUI render                         420s
#   interrupt / VRAM free / lease release          ~60s
# At 600s the bridge gave up on a job that was still legitimately finishing
# and requeued it; core's claim then re-ran the whole pipeline (second LLM
# call, second GPU render, second archive row). 900s covers the budget with
# margin. Core never takes over a started cloud claim after an ambiguous
# timeout; local work retains its separate stale-claim policy.
JOB_RUN_TIMEOUT = int(os.environ.get("JOB_RUN_TIMEOUT", "900"))


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
        mandatory=True,
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
        e.close()
        print(f"bridge: core rejected due task {task_id}: HTTP {e.code}", flush=True)
        return False
    except Exception as e:
        print(f"bridge: could not reach core for {task_id}: {e}", flush=True)
        return False


def declare_jobs_topology(ch) -> None:
    """Declare the durable Her POV work queue. Idempotent.

    Persistent quorum messages survive broker/process restarts. Core owns the
    durable claim; one HTTP slot serializes this bridge process only.
    """
    ch.exchange_declare(exchange=JOBS_EXCHANGE, exchange_type="direct", durable=True)
    ch.queue_declare(
        queue=JOBS_QUEUE,
        durable=True,
        arguments={"x-queue-type": "quorum"},
    )
    ch.queue_bind(
        exchange=JOBS_EXCHANGE, queue=JOBS_QUEUE, routing_key=JOBS_ROUTING_KEY
    )


def enqueue_job(ch, payload: dict) -> bool:
    """Publish a job id onto the durable work queue."""
    job_id = str(payload.get("job_id") or payload.get("data") or "")
    kind = str(payload.get("kind") or "her_pov")
    if not job_id or kind != "her_pov":
        return False
    ch.basic_publish(
        exchange=JOBS_EXCHANGE,
        mandatory=True,
        routing_key=JOBS_ROUTING_KEY,
        body=json.dumps({"job_id": job_id, "kind": kind}),
        properties=pika.BasicProperties(
            delivery_mode=2, content_type="application/json"
        ),
    )
    return True


def notify_core_run_her_pov(job_id: str) -> bool:
    """Block until companion-core finishes (or rejects) a Her POV job.

    Called only in the bounded job worker. Core owns claims/provider leases.
    Returning success means the bridge may ack; failure means requeue.
    """
    import urllib.error
    import urllib.request

    body = json.dumps({"job_id": job_id}).encode()
    req = urllib.request.Request(
        f"{CORE_URL}/internal/jobs/her-pov/run",
        data=body,
        headers={
            "Content-Type": "application/json",
            "X-Internal-Token": CORE_INTERNAL_TOKEN,
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=JOB_RUN_TIMEOUT) as resp:
            payload = json.loads(resp.read())
            return 200 <= resp.status < 300 and isinstance(payload, dict) and payload.get("ok") is True
    except urllib.error.HTTPError as e:
        e.close()
        # Only malformed requests are terminal. Auth/config/routing failures
        # do not establish that the durable job is invalid. Unknown job IDs
        # are acknowledged by core with 2xx.
        if e.code in (400, 422):
            print(
                f"bridge: core rejected her_pov job {job_id}: HTTP {e.code}",
                flush=True,
            )
            return True
        print(
            f"bridge: core error on her_pov job {job_id}: HTTP {e.code}",
            flush=True,
        )
        return False
    except Exception as e:
        print(f"bridge: could not reach core for her_pov {job_id}: {e}", flush=True)
        return False


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
    # Channel/declare failures must not leak a partially usable connection.
    try:
        ch = conn.channel()
        ch.exchange_declare(exchange=EXCHANGE, exchange_type="topic", durable=True)
        declare_defer_topology(ch)
        declare_jobs_topology(ch)
        ch.confirm_delivery()
    except BaseException:
        conn.close()
        raise
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


# Small bounded batches keep due-hop traffic from monopolizing the owner loop.
DUE_BATCH = 8
RETRY_SECONDS = 5.0


class HttpWork:
    """One bounded HTTP call. This object never receives a channel/connection."""

    def __init__(self, callback, identifier, generation, tag):
        self.generation = generation
        self.tag = tag
        self.success = False
        self.thread = threading.Thread(
            target=self._run, args=(callback, identifier), daemon=True,
            name="bridge-http",
        )
        self.thread.start()

    def _run(self, callback, identifier):
        try:
            self.success = bool(callback(identifier))
        except Exception as exc:
            print(f"bridge: HTTP worker failed ({exc})", flush=True)


class Bridge:
    """Own AMQP on one thread; preserve HTTP slots across all reconnects."""

    def __init__(self):
        self.conn = self.ch = None
        self.generation = 0
        self.job = self.due = None
        self.retry_at = {"job": 0.0, "due": 0.0}
        self.closed = False

    def attach(self, conn, ch):
        self.disconnect()
        self.generation += 1
        self.conn, self.ch = conn, ch

    def disconnect(self):
        if self.conn is not None:
            try:
                self.conn.close()
            except Exception:
                pass
        self.conn = self.ch = None

    def close(self):
        self.closed = True
        self.disconnect()
        # urllib cannot safely be cancelled mid-request. Daemon threads own no
        # AMQP resources, keep their bounded HTTP timeout, and never ack. Do not
        # wait 900 seconds at shutdown or pretend the provider outcome is known.

    def _complete(self, name):
        work = getattr(self, name)
        if work is None or work.thread.is_alive():
            return
        work.thread.join()
        setattr(self, name, None)
        if not work.success:
            self.retry_at[name] = time.monotonic() + RETRY_SECONDS
        if work.generation != self.generation:
            return  # Old delivery tags are meaningful only on the old channel.
        if work.success:
            self.ch.basic_ack(work.tag)
        else:
            self.ch.basic_nack(work.tag, requeue=True)

    def _pull(self, name, queue, callback, key, batch):
        if getattr(self, name) is not None or time.monotonic() < self.retry_at[name]:
            return
        for _ in range(batch):
            method, _props, body = self.ch.basic_get(queue=queue, auto_ack=False)
            if method is None:
                return
            try:
                payload = json.loads(body)
                identifier = payload[key]
                if not isinstance(identifier, str) or not identifier:
                    raise ValueError("missing identifier")
                if name == "job":
                    uuid.UUID(identifier)
                    if payload.get("kind", "her_pov") != "her_pov":
                        raise ValueError("unknown job kind")
                hops = payload.get("hops", [])
                if not isinstance(hops, list):
                    raise ValueError("invalid hops")
            except (ValueError, TypeError, KeyError):
                self.ch.basic_nack(method.delivery_tag, requeue=False)
                continue
            if name == "due" and hops:
                # Confirmed mandatory re-park before ack. Any failure escapes,
                # closes the channel, and leaves the original delivery unacked.
                if arm_defer(self.ch, payload):
                    self.ch.basic_ack(method.delivery_tag)
                else:
                    self.ch.basic_nack(method.delivery_tag, requeue=False)
                continue
            setattr(self, name, HttpWork(callback, identifier, self.generation, method.delivery_tag))
            return

    def forward(self, message):
        if not message or message.get("type") != "message":
            return
        payload = parse_payload(message.get("data") or "")
        if payload.get("type") == "job.enqueue":
            enqueue_job(self.ch, payload)
        elif payload.get("type") == "defer.arm":
            arm_defer(self.ch, payload)
        else:
            rk, body = routing_and_body(payload)
            self.ch.basic_publish(
                exchange=EXCHANGE, routing_key=rk,
                body=json.dumps(body, default=str),
                properties=pika.BasicProperties(delivery_mode=2, content_type="application/json"),
            )

    def tick(self, message=None):
        if self.closed:
            return
        self.conn.process_data_events(0)
        self._complete("job")
        self._complete("due")
        self._pull("job", JOBS_QUEUE, notify_core_run_her_pov, "job_id", 1)
        self._pull("due", DUE_QUEUE, notify_core_due, "task_id", DUE_BATCH)
        self.forward(message)


def run():
    runner = Bridge()
    r = redis.Redis.from_url(REDIS_URL, decode_responses=True)
    pubsub = None
    try:
        while True:
            message = None
            try:
                if pubsub is None:
                    pubsub = r.pubsub(ignore_subscribe_messages=True)
                    pubsub.subscribe(CHANNEL)
                if runner.conn is None:
                    runner.attach(*rabbit_connect())
                message = pubsub.get_message(timeout=min(POLL_TIMEOUT, 5.0))
                runner.tick(message)
            except Exception as exc:
                print(f"bridge: loop failed ({exc}); reconnecting", flush=True)
                runner.disconnect()
                if pubsub is not None:
                    try:
                        pubsub.close()
                    except Exception:
                        pass
                    pubsub = None
                # One bounded retry for this Redis event; queued jobs also have
                # the DB reconciler, and deferred tasks have core's sweeper.
                try:
                    runner.attach(*rabbit_connect())
                    runner.tick(message)
                except Exception as retry_exc:
                    print(f"bridge: event retry failed ({retry_exc}); ordinary event lost; "
                          "durable work remains recoverable", flush=True)
                    runner.disconnect()
                time.sleep(RETRY_SECONDS)
    finally:
        runner.close()
        if pubsub is not None:
            pubsub.close()
        r.close()


if __name__ == "__main__":
    run()
