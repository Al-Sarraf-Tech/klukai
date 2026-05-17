"""End-to-end canon recall probe.

Connects to Klukai's WebSocket with the Commander's auth token, sends three
canon probe messages, and prints her replies. Pass/fail is qualitative —
this is meant for a human to read and judge whether her responses are
canon-faithful.
"""

from __future__ import annotations

import asyncio
import json
import sys
import time
from pathlib import Path


CANON_PROBES = [
    "Tell me about Aphelion.",
    "What's your relationship with Mechty?",
    "What's a Klukadile?",
]

CANON_KEYWORDS = {
    "Tell me about Aphelion.": ["Aphelion", "Slovakia", "Yellow Zone", "Dmitry", "Bluesphere", "mech", "I'm here"],
    "What's your relationship with Mechty?": ["Mechty", "G11", "404", "comrade", "tactical hoodie", "drowsy"],
    "What's a Klukadile?": ["plush", "crocodile", "klukadile", "deny"],
}


async def probe():
    try:
        import httpx
    except ImportError:
        print("httpx not available in this container; skipping probe", file=sys.stderr)
        return

    token = Path("/tmp/klukai_token.txt").read_text().strip()
    if not token:
        print("No token at /tmp/klukai_token.txt", file=sys.stderr)
        return

    try:
        import websockets
    except ImportError:
        print("websockets package not available; skipping ws probe", file=sys.stderr)
        return

    uri = f"ws://localhost:8300/ws?token={token}"
    async with websockets.connect(uri, ping_interval=None) as ws:
        # Drain the connect handshake frame
        try:
            await asyncio.wait_for(ws.recv(), timeout=2.0)
        except asyncio.TimeoutError:
            pass

        for probe_text in CANON_PROBES:
            print(f"\n{'=' * 70}")
            print(f"COMMANDER → KLUKAI: {probe_text}")
            print(f"{'=' * 70}")
            await ws.send(json.dumps({"type": "message", "content": probe_text}))

            # Read frames for up to 60 seconds, assembling streamed reply
            reply_chunks: list[str] = []
            start = time.time()
            while time.time() - start < 60:
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=5.0)
                except asyncio.TimeoutError:
                    if reply_chunks:
                        break
                    continue
                try:
                    frame = json.loads(raw)
                except Exception:
                    continue
                t = frame.get("type", "")
                if t == "stream_token":
                    reply_chunks.append(frame.get("content", ""))
                elif t == "stream_end":
                    break
                elif t == "message":
                    reply_chunks.append(frame.get("content", ""))
                    break
                elif t == "thinking":
                    continue
                elif t == "mood":
                    continue
                elif t == "error":
                    print("[ERROR]", frame.get("content", ""))
                    break

            reply = "".join(reply_chunks).strip()
            print(f"\nKLUKAI: {reply or '(silent)'}")

            # Canon keyword check
            expected = CANON_KEYWORDS.get(probe_text, [])
            hits = [k for k in expected if k.lower() in reply.lower()]
            print(f"\nCANON HITS: {hits} / expected any of {expected}")
            if not hits:
                print("⚠️  WARNING: no canon keyword hit — reply may be ungrounded")

            # Brief pause between probes so she finishes background tasks
            await asyncio.sleep(2)


if __name__ == "__main__":
    asyncio.run(probe())
