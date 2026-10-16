"""Generate or verify canonical SLO operational source; never activates services."""

from __future__ import annotations
import argparse
from pathlib import Path

from .slo_render import RULES, load, render_dashboard, render_docs, render_rules
from .slo_validation import validate_assets


def render(root: Path) -> dict[str, str]:
    policy = load(root)
    rules = render_rules(policy)
    return {
        RULES: rules,
        "docs/alerts/klukai.yaml": rules,
        "docs/slos.md": render_docs(policy),
        "docs/dashboards/overview.json": render_dashboard(policy),
        "docs/dashboards/chat-path.json": render_dashboard(policy, chat=True),
    }


def write(root: Path) -> None:
    assets = render(root)
    validate_assets(root, assets)
    for relative, content in assets.items():
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)


def check(root: Path) -> None:
    assets = render(root)
    for relative, content in assets.items():
        target = root / relative
        if not target.is_file():
            raise ValueError(f"Generated asset missing: {relative}")
        if target.read_text() != content:
            raise ValueError(f"Generated asset drift: {relative}")
    validate_assets(root, assets)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--root", type=Path, default=Path(__file__).resolve().parents[2]
    )
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--write", action="store_true")
    mode.add_argument("--check", action="store_true")
    args = parser.parse_args()
    try:
        (write if args.write else check)(args.root)
    except (OSError, ValueError) as error:
        parser.exit(1, f"SLO assets failed: {error}\n")
    print(
        "SLO operational assets verified"
        if args.check
        else "SLO operational assets generated"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
