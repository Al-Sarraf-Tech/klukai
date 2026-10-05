"""img2img + source upload + pinned seeds (Live Portrait support in image_gen)."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

import app.image_gen as ig
from app.gpu_lease import GPU_LEASE_HEADER, GPULeaseError
from app.image_gen_constants import IMG2IMG_TEMPLATE, WORKFLOW_TEMPLATE
from tests.test_image_gen_coverage import (  # noqa: F401 — the autouse lease fixture
    _TEST_LEASE,
    _Resp,
    _fake_client,
    _mock_gpu_handoff,
)

_HIST = _Resp(200, {"pid": {"outputs": {"9": {"images": [
    {"filename": "out.png", "subfolder": "", "type": "output"}]}}}})


class TestTemplate:
    def test_same_model_chain_with_an_encoded_source(self):
        assert "5" not in IMG2IMG_TEMPLATE  # no empty latent
        assert IMG2IMG_TEMPLATE["4"] == WORKFLOW_TEMPLATE["4"]
        assert IMG2IMG_TEMPLATE["10"] == WORKFLOW_TEMPLATE["10"]
        assert IMG2IMG_TEMPLATE["11"]["class_type"] == "LoadImage"
        assert IMG2IMG_TEMPLATE["12"]["inputs"] == {"pixels": ["11", 0], "vae": ["4", 2]}
        ks = IMG2IMG_TEMPLATE["3"]["inputs"]
        assert ks["latent_image"] == ["12", 0]
        assert ks["model"] == ["10", 0]
        assert WORKFLOW_TEMPLATE["3"]["inputs"]["latent_image"] == ["5", 0]  # untouched


class TestPinnedSeed:
    @pytest.mark.asyncio
    async def test_seed_reaches_the_sampler(self):
        post = AsyncMock(return_value=_Resp(200, {"prompt_id": "pid"}))
        get = AsyncMock(side_effect=[_HIST, _Resp(200, content=b"PNG")])
        with patch.object(ig, "_http", _fake_client(get=get, post=post)), \
             patch("app.image_gen.asyncio.sleep", new=AsyncMock()):
            assert await ig._try_generate("p", 832, 1216, _TEST_LEASE, seed=2**32 + 7) == b"PNG"
        assert post.call_args.kwargs["json"]["prompt"]["3"]["inputs"]["seed"] == 7

    @pytest.mark.asyncio
    async def test_generate_image_forwards_the_seed(self):
        with patch("app.image_gen._generate_image_inner", new=AsyncMock(return_value=b"X")) as inner:
            await ig.generate_image("p", seed=42)
        assert inner.await_args.kwargs["seed"] == 42


class TestUpload:
    @pytest.mark.asyncio
    async def test_multipart_upload_through_the_facade(self):
        post = AsyncMock(return_value=_Resp(200, {"name": "src.png", "subfolder": "", "type": "input"}))
        with patch.object(ig, "_http", _fake_client(post=post)):
            assert await ig._upload_image(b"PNGDATA", _TEST_LEASE) == "src.png"
        url = post.call_args.args[0]
        assert url == f"{ig.COMFYUI_URL}/upload/image"
        kw = post.call_args.kwargs
        assert kw["headers"][GPU_LEASE_HEADER] == "test-lease"
        name, data, ctype = kw["files"]["image"]
        assert name.startswith("klukai_src_") and data == b"PNGDATA" and ctype == "image/png"
        assert kw["data"] == {"type": "input", "overwrite": "true"}

    @pytest.mark.asyncio
    async def test_subfolder_is_kept(self):
        post = AsyncMock(return_value=_Resp(200, {"name": "a.png", "subfolder": "sub"}))
        with patch.object(ig, "_http", _fake_client(post=post)):
            assert await ig._upload_image(b"x", _TEST_LEASE) == "sub/a.png"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("resp", [_Resp(500), _Resp(200, {"name": ""})])
    async def test_rejected_or_empty_upload(self, resp):
        with patch.object(ig, "_http", _fake_client(post=AsyncMock(return_value=resp))):
            assert await ig._upload_image(b"x", _TEST_LEASE) is None

    @pytest.mark.asyncio
    async def test_transport_error(self):
        with patch.object(ig, "_http", _fake_client(post=AsyncMock(side_effect=OSError("down")))):
            assert await ig._upload_image(b"x", _TEST_LEASE) is None


class TestImg2Img:
    @pytest.mark.asyncio
    async def test_full_path_builds_the_workflow_and_frees_vram(self):
        post = AsyncMock(side_effect=[
            _Resp(200, {"name": "src.png"}),          # upload
            _Resp(200, {"prompt_id": "pid"}),         # /prompt
            _Resp(200),                               # /free
        ])
        get = AsyncMock(side_effect=[_HIST, _Resp(200, content=b"FRAME")])
        with patch.object(ig, "_http", _fake_client(get=get, post=post)), \
             patch("app.image_gen.asyncio.sleep", new=AsyncMock()):
            out = await ig.generate_img2img(b"SRC", "closed eyes", denoise=0.4, seed=11, sfw=True)
        assert out == b"FRAME"
        wf = post.await_args_list[1].kwargs["json"]["prompt"]
        assert wf["11"]["inputs"]["image"] == "src.png"
        assert wf["6"]["inputs"]["text"] == "closed eyes"
        assert wf["7"]["inputs"]["text"] == ig.negative_prompt(True)
        assert wf["3"]["inputs"]["seed"] == 11
        assert wf["3"]["inputs"]["denoise"] == 0.4
        assert post.await_args_list[2].args[0].endswith("/free")
        assert IMG2IMG_TEMPLATE["11"]["inputs"]["image"] == ""  # template not mutated

    @pytest.mark.asyncio
    async def test_denoise_is_clamped(self):
        with patch("app.image_gen._upload_image", new=AsyncMock(return_value="s.png")), \
             patch("app.image_gen._run_workflow", new=AsyncMock(return_value=b"F")) as run, \
             patch("app.image_gen._free_comfyui_vram", new=AsyncMock(return_value=True)):
            await ig._img2img_inner(b"S", "p", 3.0, 1, False, _TEST_LEASE)
            assert run.await_args.args[0]["3"]["inputs"]["denoise"] == 1.0
            await ig._img2img_inner(b"S", "p", -1, 1, False, _TEST_LEASE)
            assert run.await_args.args[0]["3"]["inputs"]["denoise"] == 0.0

    @pytest.mark.asyncio
    async def test_failed_upload_renders_nothing_but_still_frees(self):
        free = AsyncMock(return_value=True)
        with patch("app.image_gen._upload_image", new=AsyncMock(return_value=None)), \
             patch("app.image_gen._run_workflow", new=AsyncMock()) as run, \
             patch("app.image_gen._free_comfyui_vram", new=free):
            assert await ig._img2img_inner(b"S", "p", 0.4, 1, False, _TEST_LEASE) is None
        run.assert_not_awaited()
        free.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_unconfirmed_cleanup_rejects_the_result(self):
        with patch("app.image_gen._upload_image", new=AsyncMock(return_value="s.png")), \
             patch("app.image_gen._run_workflow", new=AsyncMock(return_value=b"F")), \
             patch("app.image_gen._free_comfyui_vram", new=AsyncMock(return_value=False)):
            with pytest.raises(GPULeaseError):
                await ig._img2img_inner(b"S", "p", 0.4, 1, False, _TEST_LEASE)

    @pytest.mark.asyncio
    async def test_public_call_survives_a_refused_lease(self):
        with patch("app.image_gen._img2img_inner", new=AsyncMock(side_effect=GPULeaseError("no"))):
            assert await ig.generate_img2img(b"S", "p", denoise=0.4, seed=1) is None


class TestMaskedInpaint:
    @pytest.mark.asyncio
    async def test_mask_wires_the_inpaint_and_composite_nodes(self):
        uploads = AsyncMock(side_effect=["src.png", "mask.png"])
        with patch("app.image_gen._upload_image", new=uploads), \
             patch("app.image_gen._run_workflow", new=AsyncMock(return_value=b"F")) as run, \
             patch("app.image_gen._free_comfyui_vram", new=AsyncMock(return_value=True)):
            assert await ig._img2img_inner(b"S", "p", 0.85, 3, False, _TEST_LEASE, mask_png=b"M") == b"F"
        assert [c.args[0] for c in uploads.await_args_list] == [b"S", b"M"]
        wf = run.await_args.args[0]
        assert wf["13"]["inputs"] == {"image": "mask.png", "channel": "red"}
        assert wf["14"]["inputs"] == {"samples": ["12", 0], "mask": ["13", 0]}
        assert wf["3"]["inputs"]["latent_image"] == ["14", 0]
        assert wf["3"]["inputs"]["model"] == ["16", 0]
        assert wf["16"]["inputs"]["model"] == ["10", 0]
        assert wf["15"]["inputs"]["destination"] == ["11", 0]
        assert wf["15"]["inputs"]["mask"] == ["13", 0]
        assert wf["9"]["inputs"]["images"] == ["15", 0]  # the composite is what's saved
        assert ig.INPAINT_NODES["13"]["inputs"]["image"] == "{mask}"  # not mutated

    @pytest.mark.asyncio
    async def test_failed_mask_upload_renders_nothing(self):
        with patch("app.image_gen._upload_image", new=AsyncMock(side_effect=["src.png", None])), \
             patch("app.image_gen._run_workflow", new=AsyncMock()) as run, \
             patch("app.image_gen._free_comfyui_vram", new=AsyncMock(return_value=True)):
            assert await ig._img2img_inner(b"S", "p", 0.85, 3, False, _TEST_LEASE, mask_png=b"M") is None
        run.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_public_call_forwards_the_mask(self):
        with patch("app.image_gen._img2img_inner", new=AsyncMock(return_value=b"F")) as inner:
            await ig.generate_img2img(b"S", "p", denoise=0.8, seed=1, mask_png=b"M")
        assert inner.await_args.kwargs["mask_png"] == b"M"

    @pytest.mark.asyncio
    async def test_upload_names_are_content_addressed(self):
        post = AsyncMock(return_value=_Resp(200, {"name": "x.png"}))
        with patch.object(ig, "_http", _fake_client(post=post)):
            await ig._upload_image(b"same", _TEST_LEASE)
            await ig._upload_image(b"same", _TEST_LEASE)
            await ig._upload_image(b"other", _TEST_LEASE)
        names = [c.kwargs["files"]["image"][0] for c in post.await_args_list]
        assert names[0] == names[1] != names[2]


def test_build_prompt_can_drop_the_affection_tags():
    from app.image_gen_constants import AFFECTION_MOOD_TAGS
    with_tags = ig.build_prompt("upper body", affection_level=4)
    without = ig.build_prompt("upper body", affection_level=4, affection_tags=False)
    assert AFFECTION_MOOD_TAGS[4] in with_tags
    assert AFFECTION_MOOD_TAGS[4] not in without
    assert without.endswith("upper body")
