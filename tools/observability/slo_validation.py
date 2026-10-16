"""Validate source links and Grafana identities without contacting services."""

from __future__ import annotations
import json
from pathlib import Path
import re
from typing import Any
from urllib.parse import urlparse
import yaml


def validate_anchor(root: Path, url: str) -> None:
    parsed = urlparse(url)
    marker = "/blob/main/"
    if (
        parsed.netloc != "github.com"
        or marker not in parsed.path
        or not parsed.fragment
    ):
        raise ValueError(f"Unsupported runbook URL/anchor: {url}")
    relative = parsed.path.split(marker, 1)[1]
    target = (root / relative).resolve()
    if (
        not target.is_relative_to(root.resolve() / "docs/runbooks")
        or not target.is_file()
    ):
        raise ValueError(f"Missing runbook: {relative}")
    text = target.read_text()
    explicit = set(re.findall(r'<a\s+id="([^"]+)"', text))
    headings = {
        re.sub(r"[^a-z0-9 -]", "", match.lower()).replace(" ", "-")
        for match in re.findall(r"^#+\s+(.+)$", text, re.M)
    }
    if parsed.fragment not in explicit | headings:
        raise ValueError(f"Missing runbook anchor: {url}")


def validate_assets(root: Path, assets: dict[str, str]) -> None:
    rule_file = yaml.safe_load(assets["obs/prometheus/klukai-slo.rules.yml"])
    for group in rule_file["groups"]:
        for rule in group["rules"]:
            if "alert" in rule:
                validate_anchor(root, rule["annotations"]["runbook_url"])
    provision = yaml.safe_load(
        (root / "obs/grafana/provisioning/datasources/datasources.yml").read_text()
    )
    uids = {
        item.get("uid"): item.get("type") for item in provision.get("datasources", [])
    }
    for relative, text in assets.items():
        if relative.startswith("docs/dashboards/"):
            dashboard: dict[str, Any] = json.loads(text)
            for panel in dashboard["panels"]:
                if uids.get(panel["datasource"]["uid"]) != panel["datasource"]["type"]:
                    raise ValueError(f"Missing datasource UID in {relative}")
                for link in panel.get("links", []):
                    validate_anchor(root, link["url"])
