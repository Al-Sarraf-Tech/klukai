"""End-to-end canon recall probe.

Connects to Klukai's WebSocket as a TEST user, sends three canon probe
messages, and prints her replies. Pass/fail is qualitative — this is meant for
a human to read and judge whether her responses are canon-faithful.

SAFETY: the probes land in the target user's live chat history, so this runs as
the ``claude`` test user by default (env KLUKAI_PROBE_USER or --user). The
Commander's (jalsarraf's) history is SACRED; the script refuses that account
unless --i-know-what-im-doing is given. The session token is minted for the
run (scripts/e2e_live.py) and always revoked afterwards.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from e2e_live import BASE_URL, mint_token, revoke_token  # noqa: E402

DEFAULT_USER = os.environ.get("KLUKAI_PROBE_USER", "claude")
SACRED_USER = "jalsarraf"


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


def ws_url(token: str) -> str:
    """The socket authenticates by query param, not by header (see chat.py)."""
    return (
        BASE_URL.replace("http://", "ws://").replace("https://", "wss://")
        + f"/ws?token={token}"
    )


async def probe(token: str) -> None:
    try:
        import websockets
    except ImportError:
        print("websockets package not available; skipping ws probe", file=sys.stderr)
        return

    async with websockets.connect(ws_url(token), ping_interval=None) as ws:
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

            # Read frames for up to 120 seconds, assembling streamed reply
            reply_chunks: list[str] = []
            start = time.time()
            while time.time() - start < 120:
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=10.0)
                except asyncio.TimeoutError:
                    if reply_chunks:
                        break
                    continue
                try:
                    frame = json.loads(raw)
                except Exception:
                    continue
                t = frame.get("type", "")
                if t == "token":
                    # Real frame type: {"type":"token","text":"..."}
                    reply_chunks.append(frame.get("text", ""))
                elif t == "done":
                    break
                elif t in ("thinking", "mood", "read_receipt", "tool_use",
                           "affection", "affection_level_change", "heartbeat_spike",
                           "proactive", "voice_audio", "connected"):
                    continue
                elif t == "error":
                    print("[ERROR]", frame.get("content", frame.get("message", "")))
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


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--user", default=DEFAULT_USER,
        help=f"test user to probe as (default: {DEFAULT_USER}, env KLUKAI_PROBE_USER)",
    )
    ap.add_argument(
        "--i-know-what-im-doing",
        action="store_true",
        help=f"permit probing as {SACRED_USER} (SACRED history) — don't",
    )
    args = ap.parse_args(argv)

    if args.user == SACRED_USER and not args.i_know_what_im_doing:
        print(
            f"refusing to probe as '{SACRED_USER}' — the probes would land in the "
            f"Commander's SACRED chat history. Use --user {DEFAULT_USER}.",
            file=sys.stderr,
        )
        return 2

    token = mint_token(args.user)
    try:
        asyncio.run(probe(token))
    finally:
        revoke_token(token)
    return 0


if __name__ == "__main__":
    sys.exit(main())
