"""The chat benchmark only calls documented text models, and it keeps the key."""
import importlib.util
import json
import urllib.error
from pathlib import Path


def _bench():
    path = Path(__file__).resolve().parents[1] / "scripts" / "klukai-xai-chat-benchmark.py"
    spec = importlib.util.spec_from_file_location("klukai_xai_chat_benchmark", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_default_pair_and_reasoning_flags():
    bench = _bench()
    assert bench.DEFAULT_MODELS == ("grok-4.7", "grok-4.20-0309-non-reasoning")
    assert bench.wants_reasoning("grok-4.7")
    assert bench.wants_reasoning("grok-4.20-0309-reasoning")
    assert not bench.wants_reasoning("grok-4.20-0309-non-reasoning")
    assert not bench.wants_reasoning("grok-build-0.1")
    assert not bench.wants_reasoning("grok-4.20-multi-agent-0309")
    plain = bench.request_body("grok-4.7", "sys", "hi")
    fast = bench.request_body("grok-4.20-0309-non-reasoning", "sys", "hi")
    assert plain["reasoning_effort"] == "low"
    assert "reasoning_effort" not in fast
    assert plain["model"] == "grok-4.7"
    assert fast["stream"] is True


def test_unknown_models_are_rejected_and_dry_run_spends_nothing(capsys):
    bench = _bench()
    assert bench.models_from_arg(None) == bench.DEFAULT_MODELS
    assert bench.models_from_arg(" grok-4.3 , grok-4.7 ") == ("grok-4.3", "grok-4.7")
    try:
        bench.models_from_arg("not-a-model")
    except bench.PlanError:
        pass
    else:
        raise AssertionError("undocumented model was accepted")
    assert bench.main(["--models", "not-a-model"]) == 2
    assert "undocumented" in capsys.readouterr().err
    assert bench.main(["--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "model=grok-4.7 prompt=greeting reasoning=yes" in out
    assert "model=grok-4.20-0309-non-reasoning prompt=stage reasoning=no" in out


def test_price_prefers_ticks_and_ignores_a_bool():
    bench = _bench()
    assert bench.usd(1_000_000, 0, 0, None) == 2.0
    assert bench.usd(100, 80, 10, None) == (20 * 2 + 80 * 0.5 + 10 * 6) / 1_000_000
    assert bench.usd(100, 80, 10, 20_000_000) == 0.002
    assert bench.usd(0, 0, 0, True) == 0.0


def test_stream_lines_keep_the_first_token_and_usage():
    bench = _bench()
    parts = []
    state = {"first": None, "usage": {}}
    bench.consume("event: ping", parts, 0.0, state)
    bench.consume("data: [DONE]", parts, 0.0, state)
    bench.consume("data: [1]", parts, 0.0, state)
    bench.consume(
        'data: {"choices":[{"delta":{"content":"Hi"}}]}',
        parts,
        0.0,
        state,
    )
    bench.consume(
        'data: {"usage":{"prompt_tokens":4,"completion_tokens":1,'
        '"prompt_tokens_details":{"cached_tokens":"no"},"cost_in_usd_ticks":false}}',
        parts,
        0.0,
        state,
    )
    assert parts == ["Hi"]
    assert state["first"] is not None
    prompt, cached, output, ticks = bench._counts(state["usage"])
    assert (prompt, cached, output, ticks) == (4, 0, 1, False)


def test_a_run_saves_replies_and_never_prints_the_key(monkeypatch, tmp_path, capsys):
    bench = _bench()
    monkeypatch.setenv("KLUKAI_XAI_API_KEY", "super-secret-key")

    def run_one(key, model, name, system, user):
        assert key == "super-secret-key"
        if name == "memory":
            raise urllib.error.HTTPError("https://api.x.ai", 429, "no", {}, None)
        if name == "stage":
            raise RuntimeError("super-secret-key")
        return {
            "model": model,
            "prompt": name,
            "ttft_s": 0.2,
            "total_s": 0.5,
            "input_tokens": 10,
            "cached_tokens": 0,
            "output_tokens": 4,
            "usd": 0.0001,
            "reply": "Commander.",
        }

    monkeypatch.setattr(bench, "run_one", run_one)
    assert bench.main(["--out", str(tmp_path), "--models", "grok-4.7"]) == 1
    saved = (tmp_path / "grok-4.7--greeting.txt").read_text()
    assert saved == "Commander.\n"
    summary = json.loads((tmp_path / "summary.json").read_text())
    assert [row["prompt"] for row in summary["runs"]] == ["greeting", "memory", "stage"]
    assert summary["runs"][1]["error"] == "http 429"
    assert summary["runs"][2]["error"] == "RuntimeError"
    captured = capsys.readouterr()
    assert "super-secret-key" not in captured.out
    assert "super-secret-key" not in captured.err
    assert "http=429" in captured.err


def test_a_live_run_without_a_key_or_directory_does_not_call_out(monkeypatch, capsys):
    bench = _bench()
    monkeypatch.delenv("KLUKAI_XAI_API_KEY", raising=False)

    def run_one(*_args):
        raise AssertionError("network")

    monkeypatch.setattr(bench, "run_one", run_one)
    assert bench.main([]) == 2
    assert "pass --out" in capsys.readouterr().err
    assert bench.main(["--out", "/tmp/klukai-bench-unused"]) == 2
    assert "KLUKAI_XAI_API_KEY is not set" in capsys.readouterr().err
