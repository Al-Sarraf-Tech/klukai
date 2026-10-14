"""One retained XTTS instance; stdin/stdout are a private JSON-lines protocol.

This process owns all Torch and XPU state. Terminating its process group is the
supervisor's cancellation, deadline, thermal, and idle-unload primitive.
"""
from __future__ import annotations

import argparse
import array
import base64
from contextlib import redirect_stdout
import io
import json
import math
import os
import hashlib
import importlib.metadata
import platform
from pathlib import Path
import sys
import traceback
import wave


ROOT = Path(__file__).resolve().parent


def encode_wav(samples, rate: int) -> str:
    if rate != 24000:
        raise ValueError("XTTS output rate must be 24000 Hz")
    values = [float(value) for value in samples]
    if not values or len(values) > 24000 * 300 or not all(math.isfinite(value) for value in values):
        raise ValueError("invalid or oversized synthesized audio")
    if math.sqrt(sum(value * value for value in values) / len(values)) <= 0.0001:
        raise ValueError("synthesized audio is silent")
    pcm = array.array("h", (int(max(-1.0, min(1.0, value)) * 32767) for value in values))
    if sys.byteorder != "little":
        pcm.byteswap()
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(rate)
        wav.writeframes(pcm.tobytes())
    return base64.b64encode(buffer.getvalue()).decode("ascii")


def install_position_adapter(model, torch) -> None:
    # TTS 0.22.0 null_position_embeddings returns FP32 zeros. In FP16 XTTS
    # those promote the embeddings and break GPT's half-precision LayerNorm.
    # Preserve the mathematically identical zeros in this model's dtype.
    def positions(indices):
        return torch.zeros((indices.shape[0], indices.shape[1], model.gpt.model_dim),
                           device=indices.device, dtype=next(model.gpt.gpt.parameters()).dtype)
    model.gpt.gpt.wpe = positions


class Engine:
    def __init__(self, model_dir: Path, reference: Path, lock_path: Path):
        # Local modules are explicitly loaded from this release, not PYTHONPATH.
        if __package__:
            from .artifacts import verify_artifacts, verify_packages
        else:
            from artifacts import verify_artifacts, verify_packages
        lock = json.loads(lock_path.read_text())
        if platform.python_version() != lock["worker_python"]:
            raise ValueError("worker Python version differs from verified release")
        verify_packages(lock["worker_packages"])
        distribution = importlib.metadata.distribution("TTS")
        for name, digest in lock["worker_sources"].items():
            source = Path(distribution.locate_file(name))
            if hashlib.sha256(source.read_bytes()).hexdigest() != digest:
                raise ValueError(f"worker dependency source drift: {name}")
        verify_artifacts(lock, model_dir, reference)
        # No pickle or Torch import occurs before the bytes have been checked.
        import torch
        from TTS.tts.configs.xtts_config import XttsConfig
        from TTS.tts.models.xtts import Xtts
        torch.set_num_threads(2)
        torch.set_num_interop_threads(1)
        if not torch.xpu.is_available():
            raise RuntimeError("Intel XPU is unavailable")
        if "Arc" not in torch.xpu.get_device_name(0):
            raise RuntimeError("expected Intel Arc device 0")
        # Refuse insufficient global device memory instead of evicting Granite.
        free, _total = torch.xpu.mem_get_info(0)
        if free < 1280 * 1024 * 1024:
            raise RuntimeError("insufficient free GPU memory for XTTS")
        config = XttsConfig()
        config.load_json(str(model_dir / "config.json"))
        model = Xtts.init_from_config(config)
        original_load = torch.load
        def trusted_load(*args, **kwargs):
            kwargs["weights_only"] = False
            return original_load(*args, **kwargs)
        torch.load = trusted_load
        try:
            model.load_checkpoint(config, checkpoint_dir=str(model_dir), eval=True)
        finally:
            torch.load = original_load
        with torch.inference_mode():
            latents, speaker = model.get_conditioning_latents(audio_path=[str(reference)])
        model.half().to("xpu")
        install_position_adapter(model, torch)
        self.latents = latents.to(device="xpu", dtype=torch.float16)
        self.speaker = speaker.to(device="xpu", dtype=torch.float16)
        torch.xpu.synchronize()
        if {str(parameter.device) for parameter in model.parameters()} != {"xpu:0"}:
            raise RuntimeError("XTTS parameters are not on XPU")
        self.torch = torch
        self.model = model
        self.config = config

    def synthesize(self, text: str, language: str) -> dict:
        if not isinstance(text, str) or not text.strip() or len(text) > 500 or language != "en":
            raise ValueError("unsupported speech input")
        config = self.config
        with self.torch.inference_mode():
            result = self.model.inference(text, language, self.latents, self.speaker,
                temperature=config.temperature, length_penalty=config.length_penalty,
                repetition_penalty=config.repetition_penalty, top_k=config.top_k,
                top_p=config.top_p, enable_text_splitting=True)
        self.torch.xpu.synchronize()
        return {"audio": encode_wav(result["wav"], config.audio.output_sample_rate), "sample_rate": 24000}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--model-dir", required=True, type=Path)
    parser.add_argument("--reference", required=True, type=Path)
    parser.add_argument("--lock", required=True, type=Path)
    args = parser.parse_args()
    os.environ.update(COQUI_TOS_AGREED="1", HF_HUB_OFFLINE="1", TRANSFORMERS_OFFLINE="1")
    engine = None
    for line in sys.stdin:
        try:
            if len(line) > 8192:
                raise ValueError("oversized worker request")
            request = json.loads(line)
            # Third-party status output must never corrupt the private protocol.
            with redirect_stdout(sys.stderr):
                if engine is None:
                    engine = Engine(args.model_dir, args.reference, args.lock)
                result = engine.synthesize(request["text"], request["language"])
            print(json.dumps(result, separators=(",", ":")), flush=True)
        except Exception:
            traceback.print_exc(file=sys.stderr)
            return 1  # A failed model is discarded, never silently reused.
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
