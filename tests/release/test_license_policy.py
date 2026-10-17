from __future__ import annotations

import json

import pytest

from tools.ci.license_policy import POLICY, judge


def inventory(*rows: tuple[str, str, str]) -> dict:
    return {
        "Results": [
            {
                "Licenses": [
                    {"PkgName": pkg, "Name": name, "Category": category}
                    for pkg, name, category in rows
                ]
            }
        ]
    }


@pytest.fixture
def policy() -> dict:
    return json.loads(POLICY.read_text())


def test_permissive_gpl_family_and_reviewed_licences_pass(policy):
    verdict = judge(
        inventory(
            ("fastapi", "MIT", "notice"),
            ("libc6", "LGPL-2.1-or-later", "restricted"),
            ("psycopg", "LGPL-3.0-only", "restricted"),
            ("certifi", "MPL-2.0", "reciprocal"),
            ("libc6", "FSFAP", "unknown"),
        ),
        policy,
    )
    assert verdict["passed"] is True and verdict["findings"] == 5


@pytest.mark.parametrize(
    "row",
    [
        ("mongo-thing", "SSPL-1.0", "restricted"),
        ("web-thing", "AGPL-3.0-only", "restricted"),
        ("new-lib", "MPL-2.0", "reciprocal"),  # MPL is reviewed per package only
        ("odd-lib", "Some-Custom-Licence", "unknown"),
        ("blob", "", "unknown"),
    ],
)
def test_unreviewed_copyleft_or_unknown_licences_fail(policy, row):
    verdict = judge(inventory(("fastapi", "MIT", "notice"), row), policy)
    assert verdict["passed"] is False
    assert verdict["violations"] == [
        {"package": row[0], "license": row[1], "category": row[2]}
    ]


def test_an_empty_inventory_cannot_certify(policy):
    with pytest.raises(ValueError):
        judge({"Results": []}, policy)
