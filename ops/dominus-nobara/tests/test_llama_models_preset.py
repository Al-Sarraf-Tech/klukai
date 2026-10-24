"""The llama-router model presets Dominus loads (config/llama-models.ini)."""

from __future__ import annotations

from pathlib import Path
import unittest


PRESET = Path(__file__).resolve().parents[1] / "config/llama-models.ini"


def sections(text: str) -> dict[str, dict[str, str]]:
    """llama.cpp preset INI: ';' comments, '[*]' shared defaults, 'key = value'."""
    found: dict[str, dict[str, str]] = {"": {}}
    current = ""
    for raw in text.splitlines():
        line = raw.split(";", 1)[0].strip()
        if not line:
            continue
        if line.startswith("[") and line.endswith("]"):
            current = line[1:-1]
            found.setdefault(current, {})
            continue
        key, _, value = line.partition("=")
        found[current][key.strip()] = value.strip()
    return found


class EmbeddingPresetTests(unittest.TestCase):
    def test_embedding_models_take_a_full_context_in_one_micro_batch(self) -> None:
        # llama.cpp embeds an input in a single micro-batch (non-causal
        # attention), so n_ubatch caps the input length. Inherited from '[*]',
        # 256 rejected every input over 256 tokens on Dominus ("input (616
        # tokens) is too large to process. increase the physical batch size").
        presets = sections(PRESET.read_text())
        shared = presets.get("*", {})
        embedders = {
            name: {**shared, **values}
            for name, values in presets.items()
            if values.get("embedding") == "true"
        }
        self.assertIn("text-embedding-nomic-embed-text-v1.5", embedders)
        for name, values in embedders.items():
            with self.subTest(model=name):
                ctx = int(values["ctx-size"])
                self.assertGreaterEqual(int(values["ubatch-size"]), ctx)
                self.assertGreaterEqual(int(values["batch-size"]), int(values["ubatch-size"]))


if __name__ == "__main__":
    unittest.main()
