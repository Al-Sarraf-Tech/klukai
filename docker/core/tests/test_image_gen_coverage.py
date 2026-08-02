"""Behavioral coverage tests for app.image_gen ComfyUI workflow + job queue.

Every test asserts a concrete behavior: the workflow JSON ComfyUI receives
(prompt text routed to node 6, width/height on node 5, a per-call seed on
node 3, the NoobAI checkpoint + Klukai LoRA weights baked into the template),
the readiness/interrupt/free-VRAM HTTP calls, the poll loop's status handling
(queue failure, missing prompt_id, history polling, image fetch + view bytes,
timeout, exception swallow), and the retry-on-stale-job path.

All network is mocked at the httpx boundary by swapping the module-level
``_http`` singleton for a fake AsyncClient. ``asyncio.sleep`` is patched to a
no-op so the 300-iteration poll loop runs instantly and deterministically;
``uuid.uuid4`` is frozen so the seed is reproducible.
"""

from __future__ import annotations

import sys
from contextlib import asynccontextmanager
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import app.image_gen as ig
from app.gpu_lease import (
    GPU_LEASE_HEADER,
    GPU_LEASE_TTL_SECONDS,
    GPULease,
    GPULeaseError,
)
from app.image_gen import (
    KLUKAI_LORA,
    NEGATIVE_TAGS,
    build_mission_prompt,
)


# ── Helpers ─────────────────────────────────────────────────────────────────


class _Resp:
    """Minimal httpx.Response stand-in: status_code + .json()/.text/.content."""

    def __init__(self, status_code=200, json_data=None, content=b"", text=""):
        self.status_code = status_code
        self._json = json_data if json_data is not None else {}
        self.content = content
        self.text = text

    def json(self):
        return self._json


def _fake_client(get=None, post=None):
    """Build a fake AsyncClient exposing AsyncMock .get/.post and is_closed."""
    client = MagicMock()
    client.is_closed = False
    client.get = get or AsyncMock()
    client.post = post or AsyncMock()
    return client


class _Gate:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False


_TEST_LEASE = GPULease(ttl_seconds=600, token="test-lease")


@asynccontextmanager
async def _granted_lease(_workload):
    yield _TEST_LEASE


@pytest.fixture(autouse=True)
def _mock_gpu_handoff():
    """Unit tests opt into a granted lease; contract failures are tested below."""
    with (
        patch("app.image_gen.gpu_lease", new=_granted_lease),
        patch("app.llm_router.get_lm_gate", return_value=_Gate()),
    ):
        yield


# ═══════════════════════════════════════════════════════════════════════════
# _get_http — lazy singleton (lines 80-82)
# ═══════════════════════════════════════════════════════════════════════════


class TestGetHttp:
    def test_creates_client_when_none(self):
        with patch.object(ig, "_http", None):
            client = ig._get_http()
            assert client is not None
            assert isinstance(client, ig.httpx.AsyncClient)

    def test_reuses_open_client(self):
        existing = _fake_client()
        with patch.object(ig, "_http", existing):
            assert ig._get_http() is existing

    def test_recreates_when_closed(self):
        closed = _fake_client()
        closed.is_closed = True
        with patch.object(ig, "_http", closed):
            client = ig._get_http()
            assert client is not closed
            assert client.is_closed is False


# ═══════════════════════════════════════════════════════════════════════════
# build_mission_prompt — injury branches (lines 144, 146)
# ═══════════════════════════════════════════════════════════════════════════


class TestMissionPromptInjuries:
    def test_squad_injured_tag_when_squad_present(self):
        prompt = build_mission_prompt(
            scene_type="combat",
            squad_members=["mechty"],
            injuries=["squad_injured"],
        )
        assert "injured teammate" in prompt
        assert "supporting each other" in prompt

    def test_medical_emergency_tag_when_squad_present(self):
        prompt = build_mission_prompt(
            scene_type="combat",
            squad_members=["mechty"],
            injuries=["medical_emergency"],
        )
        assert "field medic" in prompt
        assert "treating wounds" in prompt

    def test_squad_injury_tags_skipped_without_squad(self):
        # The squad-injury branches live under `if squad_members:` — with no
        # squad they must not fire even when the injury flags are set.
        prompt = build_mission_prompt(
            scene_type="combat",
            squad_members=[],
            injuries=["squad_injured", "medical_emergency"],
        )
        assert "injured teammate" not in prompt
        assert "field medic" not in prompt


# ═══════════════════════════════════════════════════════════════════════════
# check_comfyui_ready — lines 251-264
# ═══════════════════════════════════════════════════════════════════════════


class TestCheckComfyuiReady:
    @pytest.mark.asyncio
    async def test_ready_when_queue_empty(self):
        get = AsyncMock(
            return_value=_Resp(200, {"queue_running": [], "queue_pending": []})
        )
        with patch.object(ig, "_http", _fake_client(get=get)):
            assert await ig.check_comfyui_ready() is True

    @pytest.mark.asyncio
    async def test_not_ready_when_running(self):
        get = AsyncMock(
            return_value=_Resp(200, {"queue_running": [["job"]], "queue_pending": []})
        )
        with patch.object(ig, "_http", _fake_client(get=get)):
            assert await ig.check_comfyui_ready() is False

    @pytest.mark.asyncio
    async def test_not_ready_when_pending(self):
        get = AsyncMock(
            return_value=_Resp(200, {"queue_running": [], "queue_pending": [["q"]]})
        )
        with patch.object(ig, "_http", _fake_client(get=get)):
            assert await ig.check_comfyui_ready() is False

    @pytest.mark.asyncio
    async def test_not_ready_on_non_200(self):
        get = AsyncMock(return_value=_Resp(503, {}))
        with patch.object(ig, "_http", _fake_client(get=get)):
            assert await ig.check_comfyui_ready() is False

    @pytest.mark.asyncio
    async def test_returns_false_on_exception(self):
        get = AsyncMock(side_effect=ig.httpx.ConnectError("no route"))
        with patch.object(ig, "_http", _fake_client(get=get)):
            assert await ig.check_comfyui_ready() is False

    @pytest.mark.asyncio
    async def test_hits_queue_endpoint(self):
        get = AsyncMock(
            return_value=_Resp(200, {"queue_running": [], "queue_pending": []})
        )
        with patch.object(ig, "_http", _fake_client(get=get)):
            await ig.check_comfyui_ready()
        url = get.call_args.args[0]
        assert url.endswith("/queue")
        assert get.call_args.kwargs["headers"][GPU_LEASE_HEADER] == "test-lease"
        assert get.call_args.kwargs["headers"]["Authorization"].startswith("Bearer ")


# ═══════════════════════════════════════════════════════════════════════════
# _interrupt_comfyui — lines 269-275
# ═══════════════════════════════════════════════════════════════════════════


class TestInterruptComfyui:
    @pytest.mark.asyncio
    async def test_posts_to_interrupt(self):
        post = AsyncMock(return_value=_Resp(200))
        with (
            patch.object(ig, "_http", _fake_client(post=post)),
            patch("app.image_gen.asyncio.sleep", new=AsyncMock()),
        ):
            assert await ig._interrupt_comfyui(_TEST_LEASE) is True
        url = post.call_args.args[0]
        assert url.endswith("/interrupt")
        assert post.call_args.kwargs["headers"][GPU_LEASE_HEADER] == "test-lease"

    @pytest.mark.asyncio
    async def test_swallows_exception(self):
        post = AsyncMock(side_effect=ig.httpx.ConnectError("down"))
        with (
            patch.object(ig, "_http", _fake_client(post=post)),
            patch("app.image_gen.asyncio.sleep", new=AsyncMock()),
        ):
            # Must not raise — the except logs a warning and returns.
            assert await ig._interrupt_comfyui(_TEST_LEASE) is False


# ═══════════════════════════════════════════════════════════════════════════
# _free_comfyui_vram — lines 280-290
# ═══════════════════════════════════════════════════════════════════════════


class TestFreeComfyuiVram:
    @pytest.mark.asyncio
    async def test_posts_free_with_unload_flags(self):
        post = AsyncMock(return_value=_Resp(200))
        with (
            patch.object(ig, "_http", _fake_client(post=post)),
            patch("app.image_gen.asyncio.sleep", new=AsyncMock()),
        ):
            assert await ig._free_comfyui_vram(_TEST_LEASE) is True
        url = post.call_args.args[0]
        assert url.endswith("/free")
        body = post.call_args.kwargs["json"]
        assert body == {"unload_models": True, "free_memory": True}
        assert post.call_args.kwargs["headers"][GPU_LEASE_HEADER] == "test-lease"

    @pytest.mark.asyncio
    async def test_swallows_exception(self):
        post = AsyncMock(side_effect=RuntimeError("boom"))
        with (
            patch.object(ig, "_http", _fake_client(post=post)),
            patch("app.image_gen.asyncio.sleep", new=AsyncMock()),
        ):
            assert await ig._free_comfyui_vram(_TEST_LEASE) is False  # no raise
        assert post.await_count == 3

    @pytest.mark.asyncio
    async def test_retries_non_200_then_confirms_release(self):
        post = AsyncMock(side_effect=[_Resp(500), _Resp(503), _Resp(200)])
        with (
            patch.object(ig, "_http", _fake_client(post=post)),
            patch("app.image_gen.asyncio.sleep", new=AsyncMock()),
        ):
            assert await ig._free_comfyui_vram(_TEST_LEASE) is True
        assert post.await_count == 3


# ═══════════════════════════════════════════════════════════════════════════
# _try_generate — workflow construction + poll loop (lines 321-371)
# ═══════════════════════════════════════════════════════════════════════════


_FROZEN_UUID = MagicMock()
_FROZEN_UUID.int = 0xDEADBEEF_00000000_00000000_00000000


class TestTryGenerateWorkflow:
    @pytest.mark.asyncio
    async def test_workflow_carries_prompt_dimensions_and_seed(self):
        """Asserts the exact ComfyUI workflow: prompt -> node 6, w/h -> node 5,
        deterministic seed -> node 3, plus the baked NoobAI checkpoint, Klukai
        LoRA weights (0.75/0.75) and negative tags."""
        post = AsyncMock(return_value=_Resp(200, {"prompt_id": "pid-1"}))
        # history immediately reports a finished image
        hist = _Resp(
            200,
            {
                "pid-1": {
                    "outputs": {
                        "n8": {
                            "images": [
                                {
                                    "filename": "klukai_gen_0001.png",
                                    "subfolder": "",
                                    "type": "output",
                                }
                            ]
                        }
                    }
                }
            },
        )
        view = _Resp(200, content=b"PNGBYTES")
        get = AsyncMock(side_effect=[hist, view])

        with (
            patch.object(ig, "_http", _fake_client(get=get, post=post)),
            patch("app.image_gen.asyncio.sleep", new=AsyncMock()),
            patch("app.image_gen.uuid.uuid4", return_value=_FROZEN_UUID),
        ):
            result = await ig._try_generate("a klukai portrait", 832, 1216, _TEST_LEASE)

        assert result == b"PNGBYTES"

        # Inspect the workflow POSTed to /prompt.
        prompt_url = post.call_args.args[0]
        assert prompt_url.endswith("/prompt")
        assert post.call_args.kwargs["headers"][GPU_LEASE_HEADER] == "test-lease"
        assert all(
            call.kwargs["headers"][GPU_LEASE_HEADER] == "test-lease"
            for call in get.await_args_list
        )
        wf = post.call_args.kwargs["json"]["prompt"]
        assert wf["6"]["inputs"]["text"] == "a klukai portrait"
        assert wf["5"]["inputs"]["width"] == 832
        assert wf["5"]["inputs"]["height"] == 1216
        assert wf["3"]["inputs"]["seed"] == (_FROZEN_UUID.int % (2**32))
        # Template invariants must survive the round-trip copy.
        assert wf["4"]["inputs"]["ckpt_name"] == "noobai_xl_v1.safetensors"
        assert wf["10"]["inputs"]["lora_name"] == KLUKAI_LORA
        assert wf["10"]["inputs"]["strength_model"] == 0.75
        assert wf["10"]["inputs"]["strength_clip"] == 0.75
        assert wf["7"]["inputs"]["text"] == NEGATIVE_TAGS

    @pytest.mark.asyncio
    async def test_template_not_mutated_between_calls(self):
        """The template is deep-copied per call — a custom width must not leak
        into the shared WORKFLOW_TEMPLATE."""
        original_w = ig.WORKFLOW_TEMPLATE["5"]["inputs"]["width"]
        post = AsyncMock(return_value=_Resp(200, {"prompt_id": "pid"}))
        hist = _Resp(
            200, {"pid": {"outputs": {"o": {"images": [{"filename": "f.png"}]}}}}
        )
        view = _Resp(200, content=b"X")
        get = AsyncMock(side_effect=[hist, view])
        with (
            patch.object(ig, "_http", _fake_client(get=get, post=post)),
            patch("app.image_gen.asyncio.sleep", new=AsyncMock()),
            patch("app.image_gen.uuid.uuid4", return_value=_FROZEN_UUID),
        ):
            await ig._try_generate("p", 512, 768, _TEST_LEASE)
        assert ig.WORKFLOW_TEMPLATE["5"]["inputs"]["width"] == original_w

    @pytest.mark.asyncio
    async def test_view_params_use_image_fields(self):
        """The /view fetch must forward filename/subfolder/type from history."""
        post = AsyncMock(return_value=_Resp(200, {"prompt_id": "p"}))
        hist = _Resp(
            200,
            {
                "p": {
                    "outputs": {
                        "o": {
                            "images": [
                                {
                                    "filename": "img.png",
                                    "subfolder": "sub",
                                    "type": "temp",
                                }
                            ]
                        }
                    }
                }
            },
        )
        view = _Resp(200, content=b"DATA")
        get = AsyncMock(side_effect=[hist, view])
        with (
            patch.object(ig, "_http", _fake_client(get=get, post=post)),
            patch("app.image_gen.asyncio.sleep", new=AsyncMock()),
            patch("app.image_gen.uuid.uuid4", return_value=_FROZEN_UUID),
        ):
            await ig._try_generate("p", 832, 1216, _TEST_LEASE)
        view_params = get.call_args.kwargs["params"]
        assert view_params == {
            "filename": "img.png",
            "subfolder": "sub",
            "type": "temp",
        }

    @pytest.mark.asyncio
    async def test_returns_none_on_queue_non_200(self):
        post = AsyncMock(return_value=_Resp(500, text="queue exploded"))
        with (
            patch.object(ig, "_http", _fake_client(post=post)),
            patch("app.image_gen.asyncio.sleep", new=AsyncMock()),
            patch("app.image_gen.uuid.uuid4", return_value=_FROZEN_UUID),
        ):
            assert await ig._try_generate("p", 832, 1216, _TEST_LEASE) is None

    @pytest.mark.asyncio
    async def test_returns_none_when_no_prompt_id(self):
        post = AsyncMock(return_value=_Resp(200, {}))  # no prompt_id key
        with (
            patch.object(ig, "_http", _fake_client(post=post)),
            patch("app.image_gen.asyncio.sleep", new=AsyncMock()),
            patch("app.image_gen.uuid.uuid4", return_value=_FROZEN_UUID),
        ):
            assert await ig._try_generate("p", 832, 1216, _TEST_LEASE) is None

    @pytest.mark.asyncio
    async def test_returns_none_when_history_has_no_images(self):
        """prompt_id present in history but outputs carry no images -> None."""
        post = AsyncMock(return_value=_Resp(200, {"prompt_id": "p"}))
        hist = _Resp(200, {"p": {"outputs": {"o": {"images": []}}}})
        get = AsyncMock(return_value=hist)
        with (
            patch.object(ig, "_http", _fake_client(get=get, post=post)),
            patch("app.image_gen.asyncio.sleep", new=AsyncMock()),
            patch("app.image_gen.uuid.uuid4", return_value=_FROZEN_UUID),
        ):
            assert await ig._try_generate("p", 832, 1216, _TEST_LEASE) is None

    @pytest.mark.asyncio
    async def test_returns_none_when_view_fails(self):
        """Image present but /view returns non-200 -> the loop finishes the
        prompt_id branch and returns None."""
        post = AsyncMock(return_value=_Resp(200, {"prompt_id": "p"}))
        hist = _Resp(
            200, {"p": {"outputs": {"o": {"images": [{"filename": "f.png"}]}}}}
        )
        view = _Resp(404, content=b"")
        get = AsyncMock(side_effect=[hist, view])
        with (
            patch.object(ig, "_http", _fake_client(get=get, post=post)),
            patch("app.image_gen.asyncio.sleep", new=AsyncMock()),
            patch("app.image_gen.uuid.uuid4", return_value=_FROZEN_UUID),
        ):
            assert await ig._try_generate("p", 832, 1216, _TEST_LEASE) is None

    @pytest.mark.asyncio
    async def test_polls_until_history_ready(self):
        """First history poll lacks the prompt_id; the second has the image.
        Confirms the loop keeps polling rather than bailing early."""
        post = AsyncMock(return_value=_Resp(200, {"prompt_id": "p"}))
        not_ready = _Resp(200, {})  # prompt_id not in history yet
        ready = _Resp(
            200, {"p": {"outputs": {"o": {"images": [{"filename": "f.png"}]}}}}
        )
        view = _Resp(200, content=b"LATE")
        get = AsyncMock(side_effect=[not_ready, ready, view])
        with (
            patch.object(ig, "_http", _fake_client(get=get, post=post)),
            patch("app.image_gen.asyncio.sleep", new=AsyncMock()),
            patch("app.image_gen.uuid.uuid4", return_value=_FROZEN_UUID),
        ):
            result = await ig._try_generate("p", 832, 1216, _TEST_LEASE)
        assert result == b"LATE"
        assert get.await_count == 3  # two history polls + one view

    @pytest.mark.asyncio
    async def test_history_non_200_keeps_polling_then_times_out(self):
        """History always returns non-200; with the loop capped, the function
        must fall through to the timeout return (None)."""
        post = AsyncMock(return_value=_Resp(200, {"prompt_id": "p"}))
        get = AsyncMock(return_value=_Resp(503, {}))
        with (
            patch.object(ig, "_http", _fake_client(get=get, post=post)),
            patch("app.image_gen.asyncio.sleep", new=AsyncMock()),
            patch("app.image_gen.uuid.uuid4", return_value=_FROZEN_UUID),
            patch("app.image_gen.range", return_value=range(3)),
        ):
            assert await ig._try_generate("p", 832, 1216, _TEST_LEASE) is None
        assert get.await_count == 3  # polled the (shrunk) loop, never found image

    @pytest.mark.asyncio
    async def test_returns_none_on_post_exception(self):
        post = AsyncMock(side_effect=ig.httpx.ConnectError("comfy down"))
        with (
            patch.object(ig, "_http", _fake_client(post=post)),
            patch("app.image_gen.asyncio.sleep", new=AsyncMock()),
            patch("app.image_gen.uuid.uuid4", return_value=_FROZEN_UUID),
        ):
            assert await ig._try_generate("p", 832, 1216, _TEST_LEASE) is None


# ═══════════════════════════════════════════════════════════════════════════
# _generate_image_inner + generate_image — retry + semaphore (300-316)
# ═══════════════════════════════════════════════════════════════════════════


class TestGenerateImageInner:
    @pytest.mark.asyncio
    async def test_returns_first_attempt_result_no_retry_needed(self):
        with (
            patch(
                "app.image_gen._try_generate", new=AsyncMock(return_value=b"IMG")
            ) as tg,
            patch("app.image_gen._free_comfyui_vram", new=AsyncMock()) as free,
            patch("app.image_gen._interrupt_comfyui", new=AsyncMock()) as interrupt,
        ):
            result = await ig._generate_image_inner(
                "p", 832, 1216, retry=True, lease=_TEST_LEASE
            )
        assert result == b"IMG"
        assert tg.await_count == 1  # success on first try, no retry
        interrupt.assert_not_called()
        free.assert_awaited_once_with(_TEST_LEASE)  # always freed in finally

    @pytest.mark.asyncio
    async def test_retries_after_interrupt_on_first_failure(self):
        tg = AsyncMock(side_effect=[None, b"RETRY_IMG"])
        with (
            patch("app.image_gen._try_generate", new=tg),
            patch("app.image_gen._free_comfyui_vram", new=AsyncMock()),
            patch("app.image_gen._interrupt_comfyui", new=AsyncMock()) as interrupt,
        ):
            result = await ig._generate_image_inner(
                "p", 832, 1216, retry=True, lease=_TEST_LEASE
            )
        assert result == b"RETRY_IMG"
        assert tg.await_count == 2
        interrupt.assert_awaited_once_with(
            _TEST_LEASE
        )  # stale job interrupted before retry

    @pytest.mark.asyncio
    async def test_no_retry_when_disabled(self):
        tg = AsyncMock(return_value=None)
        with (
            patch("app.image_gen._try_generate", new=tg),
            patch("app.image_gen._free_comfyui_vram", new=AsyncMock()),
            patch("app.image_gen._interrupt_comfyui", new=AsyncMock()) as interrupt,
        ):
            result = await ig._generate_image_inner(
                "p", 832, 1216, retry=False, lease=_TEST_LEASE
            )
        assert result is None
        assert tg.await_count == 1  # retry disabled -> single attempt
        interrupt.assert_not_called()

    @pytest.mark.asyncio
    async def test_frees_vram_even_when_generation_raises(self):
        tg = AsyncMock(side_effect=RuntimeError("boom"))
        free = AsyncMock()
        with (
            patch("app.image_gen._try_generate", new=tg),
            patch("app.image_gen._free_comfyui_vram", new=free),
            patch("app.image_gen._interrupt_comfyui", new=AsyncMock()),
        ):
            with pytest.raises(RuntimeError):
                await ig._generate_image_inner(
                    "p", 832, 1216, retry=True, lease=_TEST_LEASE
                )
        free.assert_awaited_once_with(_TEST_LEASE)  # finally still runs

    @pytest.mark.asyncio
    async def test_failed_cleanup_rejects_result(self):
        with (
            patch("app.image_gen._try_generate", new=AsyncMock(return_value=b"IMG")),
            patch(
                "app.image_gen._free_comfyui_vram", new=AsyncMock(return_value=False)
            ),
        ):
            with pytest.raises(GPULeaseError, match="cleanup could not be confirmed"):
                await ig._generate_image_inner(
                    "p", 832, 1216, retry=False, lease=_TEST_LEASE
                )


class TestGenerateImagePublic:
    def test_render_deadline_leaves_two_minute_lease_safety_margin(self):
        assert ig._IMAGE_LEASE_WORK_SECONDS == 480
        assert ig._IMAGE_LEASE_WORK_SECONDS <= GPU_LEASE_TTL_SECONDS - 120

    @pytest.mark.asyncio
    async def test_delegates_through_semaphore(self):
        with patch(
            "app.image_gen._generate_image_inner",
            new=AsyncMock(return_value=b"WRAPPED"),
        ) as inner:
            result = await ig.generate_image(
                "a prompt", width=512, height=768, retry=False
            )
        assert result == b"WRAPPED"
        # Args forwarded verbatim to the inner implementation.
        inner.assert_awaited_once_with("a prompt", 512, 768, False, _TEST_LEASE)

    @pytest.mark.asyncio
    async def test_default_dimensions_are_portrait(self):
        with patch(
            "app.image_gen._generate_image_inner", new=AsyncMock(return_value=b"X")
        ) as inner:
            await ig.generate_image("p")
        # Defaults: 832x1216 portrait, retry=True.
        inner.assert_awaited_once_with("p", 832, 1216, True, _TEST_LEASE)

    @pytest.mark.asyncio
    async def test_holds_lm_gate_and_gateway_lease_for_entire_render(self):
        events = []

        class OrderedGate:
            async def __aenter__(self):
                events.append("gate-enter")

            async def __aexit__(self, *_args):
                events.append("gate-exit")

        @asynccontextmanager
        async def ordered_lease(workload):
            assert workload == "comfyui"
            events.append("lease-acquire")
            try:
                yield GPULease(ttl_seconds=600, token="ordered")
            finally:
                events.append("lease-release")

        async def render(*_args):
            events.append("render")
            return b"IMAGE"

        with (
            patch("app.llm_router.get_lm_gate", return_value=OrderedGate()),
            patch("app.image_gen.gpu_lease", new=ordered_lease),
            patch("app.image_gen._generate_image_inner", new=render),
        ):
            assert await ig.generate_image("p") == b"IMAGE"

        assert events == [
            "gate-enter",
            "lease-acquire",
            "render",
            "lease-release",
            "gate-exit",
        ]

    @pytest.mark.asyncio
    async def test_refuses_comfy_when_gateway_cannot_lease(self):
        @asynccontextmanager
        async def refused(_workload):
            raise GPULeaseError("GPU lease acquire failed with HTTP 503")
            yield  # pragma: no cover - required async-generator shape

        inner = AsyncMock(return_value=b"must-not-run")
        with (
            patch("app.image_gen.gpu_lease", new=refused),
            patch("app.image_gen._generate_image_inner", new=inner),
        ):
            assert await ig.generate_image("p") is None
        inner.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_timeout_interrupts_and_frees_before_lease_release(self):
        events = []

        @asynccontextmanager
        async def ordered_lease(_workload):
            events.append("lease-acquire")
            try:
                yield GPULease(ttl_seconds=600, token="ordered")
            finally:
                events.append("lease-release")

        async def timeout_render(*_args):
            raise TimeoutError

        async def interrupt(lease):
            assert lease.token == "ordered"
            events.append("interrupt")
            return True

        async def free(lease):
            assert lease.token == "ordered"
            events.append("free")
            return True

        with (
            patch("app.image_gen.gpu_lease", new=ordered_lease),
            patch("app.image_gen._generate_image_inner", new=timeout_render),
            patch("app.image_gen._interrupt_comfyui", new=interrupt),
            patch("app.image_gen._free_comfyui_vram", new=free),
        ):
            assert await ig.generate_image("p") is None

        assert events == ["lease-acquire", "interrupt", "free", "lease-release"]


# ═══════════════════════════════════════════════════════════════════════════
# Integration-ish: full generate_image path through the fake HTTP boundary
# ═══════════════════════════════════════════════════════════════════════════


class TestGenerateImageEndToEnd:
    @pytest.mark.asyncio
    async def test_full_path_returns_image_bytes(self):
        hist = _Resp(
            200, {"p": {"outputs": {"o": {"images": [{"filename": "f.png"}]}}}}
        )
        view = _Resp(200, content=b"FINALPNG")
        # generate_image -> _try_generate (history+view) -> _free_comfyui_vram
        get = AsyncMock(side_effect=[hist, view])
        free_post = AsyncMock(return_value=_Resp(200))

        # _free_comfyui_vram posts to /free; route post calls by URL.
        async def _post(url, *a, **kw):
            if url.endswith("/free"):
                return await free_post(url, *a, **kw)
            return _Resp(200, {"prompt_id": "p"})

        with (
            patch.object(
                ig, "_http", _fake_client(get=get, post=AsyncMock(side_effect=_post))
            ),
            patch("app.image_gen.asyncio.sleep", new=AsyncMock()),
            patch("app.image_gen.uuid.uuid4", return_value=_FROZEN_UUID),
        ):
            result = await ig.generate_image("klukai", retry=False)

        assert result == b"FINALPNG"
        free_post.assert_awaited_once()  # VRAM freed after a successful gen
