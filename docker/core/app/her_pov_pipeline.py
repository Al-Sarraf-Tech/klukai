"""Her POV search/write/render/archive pipeline, with one coordinator claim."""

from __future__ import annotations

import asyncio
import base64
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
import logging
from typing import Any

from .her_pov_story import POVScene
from .image_prompts import lighting_period

logger = logging.getLogger(__name__)


@dataclass
class HerPOVPipeline:
    set_job: Callable[..., Awaitable[None]]
    pick_exchange: Callable[[str], Awaitable[dict[str, Any] | None]]
    compose_pov: Callable[[dict[str, Any], int], Awaitable[POVScene]]
    release_claim: Callable[[str, str], Awaitable[None]]

    async def run(self, user_id: str, job_id: str) -> None:
        """Full pipeline with WS status updates. Never raises out of task."""
        from .context import affection
        from . import memory_archive
        from .context import ws
        from .image_gen import INTIMATE_MIN_LEVEL, build_prompt, generate_image
        from .her_day import render_costume
        from . import wardrobe as _wardrobe

        completed = False
        try:
            await self.set_job(
                job_id,
                status="searching",
                phase="searching",
                message="Searching our records…",
            )
            try:
                await ws.send_thinking(user_id, "Searching our records…")
                await ws.send(
                    user_id,
                    {
                        "type": "her_pov",
                        "job_id": job_id,
                        "status": "searching",
                        "phase": "searching",
                        "message": "Searching our records…",
                    },
                )
            except Exception:
                pass

            exchange = await self.pick_exchange(user_id)
            if not exchange:
                await self.set_job(
                    job_id,
                    status="failed",
                    phase="failed",
                    error="no_history",
                    message="…Our records are still thin. Talk to me more first.",
                )
                try:
                    await ws.send_proactive(
                        user_id,
                        "…Our records are still thin. Talk to me more first, Commander.",
                        persist=False,
                    )
                    await ws.send(
                        user_id,
                        {
                            "type": "her_pov",
                            "job_id": job_id,
                            "status": "failed",
                            "phase": "failed",
                            "error": "no_history",
                        },
                    )
                except Exception:
                    pass
                return

            aff = await affection.get_state(user_id)
            level = aff.level

            await self.set_job(
                job_id,
                status="thinking",
                phase="thinking",
                message="Replaying the moment…",
                exchange_preview={
                    "user": exchange["user_content"][:160],
                    "assistant": exchange["assistant_content"][:160],
                },
            )
            try:
                await ws.send_thinking(user_id, "Replaying the moment from my side…")
                await ws.send(
                    user_id,
                    {
                        "type": "her_pov",
                        "job_id": job_id,
                        "status": "thinking",
                        "phase": "thinking",
                    },
                )
            except Exception:
                pass

            pov = await self.compose_pov(exchange, level)

            await self.set_job(
                job_id,
                status="drawing",
                phase="drawing",
                message="Sketching from my side…",
                title=pov["title"],
                annotation=pov["annotation"],
            )
            try:
                await ws.send_thinking(user_id, "Sketching from my side…")
                await ws.send(
                    user_id,
                    {
                        "type": "her_pov",
                        "job_id": job_id,
                        "status": "drawing",
                        "phase": "drawing",
                        "title": pov["title"],
                    },
                )
            except Exception:
                pass

            # She draws herself in what she has on today.
            costume = await render_costume(user_id, level)

            # The Commander's wall clock, not the container's UTC — otherwise an
            # evening portrait gets rendered with 1am lighting.
            from .proactive.state import now_local

            hour = now_local().hour
            tod = lighting_period(hour)

            from .image_provider import selected_provider

            image_context = (
                ""
                if selected_provider() == "agy"
                else f"{exchange['user_content']} {exchange['assistant_content']}"[:400]
            )
            prompt = build_prompt(
                pov["scene_tags"],
                couple=pov["couple"],
                affection_level=level,
                context=image_context,
                mood=pov["mood"],
                time_of_day=tod,
                costume=costume,
                costume_pinned=_wardrobe.granted_request_for(user_id, costume),
            )

            img = await generate_image(
                prompt,
                sfw=level < INTIMATE_MIN_LEVEL,
                user_id=user_id,
                include_commander=bool(pov["couple"]) and user_id == "jalsarraf",
            )
            if not img:
                await self.set_job(
                    job_id,
                    status="failed",
                    phase="failed",
                    error="image_failed",
                    message="…Visualization failed. Interference in the rendering pipeline.",
                    annotation=pov["annotation"],
                    title=pov["title"],
                )
                try:
                    await ws.send_proactive(
                        user_id,
                        "…Visualization failed. Interference in the rendering pipeline. "
                        "Ask me again.",
                        persist=False,
                    )
                    await ws.send(
                        user_id,
                        {
                            "type": "her_pov",
                            "job_id": job_id,
                            "status": "failed",
                            "phase": "failed",
                            "error": "image_failed",
                        },
                    )
                except Exception:
                    pass
                return

            memory_id = await memory_archive.save_image(
                image_bytes=img,
                prompt=prompt,
                conversation_id=f"her_pov:{job_id}",
                mood=pov["mood"],
                affection_level=level,
                curation={
                    "keep": True,
                    "annotation": pov["annotation"],
                    "category": "Precious Memories"
                    if level >= 6
                    else "The Commander"
                    if level >= 3
                    else "Quiet Hours",
                    "image_tags": ["her_pov", "from_her_side", "commander_request"],
                },
                user_id=user_id,
            )
            if not memory_id:
                # save_image returns None when the archive rejects the row (e.g. the
                # annotation deduped against an earlier one) and deletes the files.
                # Reporting "done" here left the client showing "Kept." over an
                # empty stage with nothing in the archive.
                await self.set_job(
                    job_id,
                    status="failed",
                    phase="failed",
                    error="not_saved",
                    message="…I drew it, but it wouldn't keep. Ask me again.",
                    annotation=pov["annotation"],
                    title=pov["title"],
                )
                try:
                    await ws.send(
                        user_id,
                        {
                            "type": "her_pov",
                            "job_id": job_id,
                            "status": "failed",
                            "phase": "failed",
                            "error": "not_saved",
                        },
                    )
                except Exception:
                    pass
                return

            img_b64 = base64.b64encode(img).decode()
            await self.set_job(
                job_id,
                status="done",
                phase="done",
                message="Kept.",
                memory_id=memory_id,
                title=pov["title"],
                annotation=pov["annotation"],
                mood=pov["mood"],
                # do not store full image b64 in job forever — client gets WS once
                has_image=True,
            )
            completed = True

            try:
                line = pov["annotation"]
                await ws.send_proactive(user_id, line)
                await asyncio.sleep(0.8)
                await ws.send(
                    user_id,
                    {
                        "type": "image",
                        "data": img_b64,
                        "memory_id": memory_id,
                    },
                )
                await ws.send(
                    user_id,
                    {
                        "type": "her_pov",
                        "job_id": job_id,
                        "status": "done",
                        "phase": "done",
                        "memory_id": memory_id,
                        "title": pov["title"],
                        "annotation": pov["annotation"],
                        "mood": pov["mood"],
                    },
                )
            except Exception as e:
                logger.warning("her_pov WS deliver failed: %s", e)

            logger.info(
                "her_pov done user=%s job=%s mem=%s", user_id, job_id, memory_id
            )

        except asyncio.CancelledError:
            # Delivery can be cancelled after the archive and terminal job are
            # saved. Keep that successful result available to reconnecting clients.
            if completed:
                raise
            # WSManager.disconnect cancels tracked tasks when the last device drops,
            # and this pipeline runs for minutes. CancelledError is not an Exception
            # subclass, so without this the job would sit at "drawing" forever and
            # the client would poll it for the life of the process.
            logger.info("her_pov cancelled user=%s job=%s", user_id, job_id)
            await self.set_job(
                job_id,
                status="failed",
                phase="failed",
                error="cancelled",
                message="…Interrupted. Ask me again when you're back.",
            )
            raise
        except Exception as e:
            logger.exception("her_pov failed: %s", e)
            await self.set_job(
                job_id,
                status="failed",
                phase="failed",
                error="internal",
                message=str(e)[:200],
            )
            try:
                await ws.send(
                    user_id,
                    {
                        "type": "her_pov",
                        "job_id": job_id,
                        "status": "failed",
                        "phase": "failed",
                        "error": "internal",
                    },
                )
            except Exception:
                pass
        finally:
            await self.release_claim(user_id, job_id)
