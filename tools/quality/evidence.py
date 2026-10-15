"""Validate evidence identity and integrity before inspecting observations."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


class InvalidEvidence(ValueError):
    """A report cannot establish the requested requirement."""


def timestamp(value: Any, *, now: datetime, max_age_days: int) -> datetime:
    if not isinstance(value, str):
        raise InvalidEvidence("missing observation timestamp")
    try:
        observed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise InvalidEvidence("invalid observation timestamp") from error
    if observed.tzinfo is None:
        raise InvalidEvidence("observation timestamp has no timezone")
    if observed > now + timedelta(minutes=5):
        raise InvalidEvidence("observation timestamp is in the future")
    if now - observed > timedelta(days=max_age_days):
        raise InvalidEvidence("observation is stale")
    return observed


def load_bundle(
    directory: Path, requirement: str, revision: str, max_age: int
) -> dict[str, Any]:
    """Require same-revision assertions and intact raw executed-command output."""
    payload = json.loads((directory / f"{requirement}.json").read_text())
    if not isinstance(payload, dict) or payload.get("schema_version") != 1:
        raise InvalidEvidence("unsupported evidence schema")
    if (
        payload.get("requirement") != requirement
        or payload.get("source_revision") != revision
    ):
        raise InvalidEvidence("evidence belongs to another requirement or revision")
    timestamp(
        payload.get("observed_at"), now=datetime.now(timezone.utc), max_age_days=max_age
    )
    commands, assertions = payload.get("commands"), payload.get("assertions")
    if not isinstance(commands, list) or not commands:
        raise InvalidEvidence("no executed verification commands")
    if not isinstance(assertions, list) or not assertions:
        raise InvalidEvidence("no requirement assertions")
    policy = json.loads((Path(__file__).parent / "policy.json").read_text())
    required = set(policy["assertions"].get(requirement, []))
    names = {a.get("name") for a in assertions if isinstance(a, dict)}
    if not required or not required.issubset(names) or len(names) != len(assertions):
        raise InvalidEvidence(
            "required assertions are missing, duplicated or undefined"
        )
    for assertion in assertions:
        if (
            not isinstance(assertion, dict)
            or not assertion.get("name")
            or assertion.get("passed") is not True
        ):
            raise InvalidEvidence("a requirement assertion is missing or failed")
    for command in commands:
        if (
            not isinstance(command, dict)
            or type(command.get("exit_code")) is not int
            or command["exit_code"] != 0
        ):
            raise InvalidEvidence("verification command failed or did not finish")
        argv = command.get("argv")
        if (
            not isinstance(argv, list)
            or not argv
            or not all(isinstance(v, str) and v for v in argv)
        ):
            raise InvalidEvidence("missing actual verification command")
        artifact = command.get("output")
        if not isinstance(artifact, str) or Path(artifact).is_absolute():
            raise InvalidEvidence(
                "output path must be relative to the evidence directory"
            )
        raw = (directory / artifact).resolve()
        if not raw.is_relative_to(directory.resolve()):
            raise InvalidEvidence("output escapes the evidence directory")
        data = raw.read_bytes()
        if not data or hashlib.sha256(data).hexdigest() != command.get("sha256"):
            raise InvalidEvidence("raw evidence is missing, empty or altered")
    return payload
