"""Security evidence is recomputed; scanner and candidate assertions are not authority."""

from copy import deepcopy
from datetime import UTC, datetime
import io
import json
import tarfile

import pytest

from tools.ci import image_security as security


def valid_evidence(count=3):
    policy = security.load_policy()
    subject = {
        "revision": "a" * 40,
        "source_tree": "b" * 40,
        "run": "123",
        "layers": ["sha256:" + "c" * 64],
        "config_sha256": "d" * 64,
        "image_id": "sha256:" + "e" * 64,
    }
    stamp = "2026-11-08T00:00:00+00:00"
    packages = []
    for p in policy["packages"]:
        version = p["version"].split(":")[-1]
        ver, rel = version.rsplit("-", 1)
        pkg = {
            "ID": p["package"] + "@" + p["version"],
            "Name": p["package"],
            "Version": ver,
            "Release": rel,
            "Arch": "amd64",
            "SrcName": p["source"],
            "Identifier": {"PURL": p["purl"]},
            "AnalyzedBy": "dpkg",
        }
        if ":" in p["version"]:
            pkg["Epoch"] = int(p["version"].split(":")[0])
        packages.append(pkg)
    findings = [
        {
            "VulnerabilityID": cve,
            "PkgID": packages[0]["ID"],
            "PkgName": "libsqlite3-0",
            "PkgIdentifier": packages[0]["Identifier"],
            "InstalledVersion": "3.54.1-2",
            "Status": "affected",
            "Severity": "HIGH",
            "Layer": {"DiffID": subject["layers"][0]},
        }
        for cve in policy["cves"][:count]
    ]
    raw = {
        "SchemaVersion": 2,
        "Trivy": {"Version": "0.71.0"},
        "CreatedAt": stamp,
        "ArtifactType": "container_image",
        "ArtifactName": subject["image_id"],
        "ArtifactID": "sha256:" + "f" * 64,
        "Metadata": {
            "OS": {"Family": "debian", "Name": "13.7"},
            "ImageID": subject["image_id"],
            "DiffIDs": subject["layers"],
        },
        "Results": [
            {
                "Target": "image (debian 13.7)",
                "Class": "os-pkgs",
                "Type": "debian",
                "Packages": packages,
                "Vulnerabilities": findings,
            }
        ],
    }
    filtered = deepcopy(raw)
    filtered["Results"][0].pop("Vulnerabilities")
    filtered["Results"][0]["ExperimentalModifiedFindings"] = [
        {
            "Type": "vulnerability",
            "Status": "fixed",
            "Statement": "",
            "Source": "fixed.openvex.json",
            "Finding": deepcopy(v),
        }
        for v in findings
    ]
    native = {
        "packages": [],
        "sqlite_version": "3.54.1",
        "loaded_sqlite": ["/opt/runtime-libs/libsqlite3.so.0"],
        "loader_environment": policy["loader_environment"],
        "os_release": {"ID": "debian", "VERSION_ID": "13"},
    }
    for p in policy["packages"]:
        native["packages"].append(
            {
                "package": p["package"],
                "version": p["version"],
                "source": p["source"],
                "architecture": "amd64",
                "archive_sha256": p["sha256"],
                "resolved_path": p["runtime_library"],
                "library_sha256": p["library_sha256"],
                "copies": {p["runtime_library"]: p["library_sha256"]},
                "embedded": {k: p[k] for k in security.EMBEDDED_KEYS},
            }
        )
    dbmeta = {
        "Version": 2,
        "UpdatedAt": "2026-11-07T18:00:00Z",
        "NextUpdate": "2026-11-08T18:00:00Z",
        "DownloadedAt": "2026-11-07T20:00:00Z",
    }
    scanner = {
        "version": "0.71.0",
        "executable_sha256": policy["scanner"]["executable_sha256"],
        "database": {
            "sha256": "1" * 64,
            "metadata": dbmeta,
            "metadata_sha256": security.digest(dbmeta),
        },
        "database_after": {
            "sha256": "1" * 64,
            "metadata": dbmeta,
            "metadata_sha256": security.digest(dbmeta),
        },
        "sbom_sha256": "5" * 64,
        "started_at": stamp,
        "finished_at": stamp,
        "raw_exit": 0,
        "filtered_exit": 0,
        "invocation": security.invocations(subject["image_id"]),
    }
    envelope = {
        "schema": 1,
        "policy": {"document": policy, "sha256": security.digest(policy)},
        "subject": subject,
        "scanner": scanner,
        "native": native,
        "raw": raw,
        "filtered": filtered,
        "vex": security.make_vex(raw, subject, policy, stamp),
        "verdict": {"passed": True},
        "errors": [],
    }
    manifest = {k: v for k, v in subject.items() if k != "image_id"}
    manifest["materials"] = {"sbom.cdx.json": "5" * 64}
    return envelope, manifest


@pytest.mark.parametrize("count", [0, 1, 2, 3])
def test_recomputes_exact_observed_fixed_findings(count):
    e, m = valid_evidence(count)
    e["verdict"] = {"passed": False, "fixed": 9000}
    result = security.verify_evidence(e, m)
    assert result == {
        "passed": True,
        "raw_high_critical": count,
        "fixed": count,
        "unresolved_high_critical": 0,
    }


@pytest.mark.parametrize(
    "path,value",
    [
        (("schema",), 99),
        (("extra",), True),
        (("errors",), ["scanner crashed"]),
        (("subject", "revision"), "0" * 40),
        (("subject", "source_tree"), "0" * 40),
        (("subject", "run"), "wrong"),
        (("subject", "config_sha256"), "0" * 64),
        (("subject", "layers"), ["sha256:" + "0" * 64]),
        (("scanner", "version"), "0.70.0"),
        (("scanner", "executable_sha256"), "0" * 64),
        (("scanner", "raw_exit"), 1),
        (("scanner", "filtered_exit"), 1),
        (("scanner", "started_at"), "2026-01-01T00:00:00Z"),
        (("scanner", "finished_at"), "2027-01-01T00:00:00Z"),
        (("scanner", "database_after", "sha256"), "0" * 64),
        (("scanner", "database", "metadata_sha256"), "0" * 64),
        (("native", "sqlite_version"), "3.46.1"),
        (("native", "loaded_sqlite"), ["/tmp/unused.so"]),
        (("native", "loader_environment"), {"LD_PRELOAD": "/tmp/bad.so"}),
        (("native", "os_release", "ID"), "ubuntu"),
        (("native", "packages", 0, "archive_sha256"), "0" * 64),
        (("native", "packages", 0, "library_sha256"), "0" * 64),
        (("native", "packages", 0, "version"), "3.46.1-7"),
        (("native", "packages", 0, "architecture"), "arm64"),
        (("native", "packages", 0, "embedded", "sha256"), "0" * 64),
        (("policy", "document", "cves"), ["CVE-*"]),
        (("raw", "Metadata", "ImageID"), "sha256:" + "0" * 64),
        (("raw", "Metadata", "OS", "Name"), "sid"),
        (("raw", "Results", 0, "Packages", 0, "Arch"), "arm64"),
        (
            ("raw", "Results", 0, "Vulnerabilities", 0, "PkgIdentifier", "PURL"),
            "pkg:deb/debian/libsqlite3-0",
        ),
        (
            ("raw", "Results", 0, "Vulnerabilities", 0, "VulnerabilityID"),
            "CVE-2026-119586",
        ),
        (
            ("filtered", "Results", 0, "ExperimentalModifiedFindings", 0, "Status"),
            "not_affected",
        ),
        (
            ("filtered", "Results", 0, "ExperimentalModifiedFindings", 0, "Source"),
            ".trivyignore",
        ),
        (
            (
                "filtered",
                "Results",
                0,
                "ExperimentalModifiedFindings",
                0,
                "Finding",
                "Severity",
            ),
            "LOW",
        ),
        (("vex", "statements", 0, "status"), "not_affected"),
        (("vex", "statements", 0, "products", 0, "@id"), "pkg:deb/debian/libsqlite3-0"),
    ],
)
def test_substitution_is_rejected(path, value):
    e, m = valid_evidence()
    node = e
    for key in path[:-1]:
        node = node[key]
    node[path[-1]] = value
    with pytest.raises(ValueError):
        security.verify_evidence(e, m)


@pytest.mark.parametrize(
    "change", ["drop", "duplicate", "extra", "unresolved", "missing"]
)
def test_reconciles_all_findings(change):
    e, m = valid_evidence()
    modified = e["filtered"]["Results"][0]["ExperimentalModifiedFindings"]
    if change == "drop":
        modified.pop()
    elif change == "duplicate":
        modified.append(deepcopy(modified[0]))
    elif change == "extra":
        modified.append(deepcopy(modified[0]))
        modified[-1]["Finding"]["VulnerabilityID"] = "CVE-2099-1234"
    elif change == "unresolved":
        e["filtered"]["Results"][0]["Vulnerabilities"] = [modified.pop()["Finding"]]
    else:
        del e["native"]
    with pytest.raises(ValueError):
        security.verify_evidence(e, m)


def test_policy_expiry_applies_to_issuance_not_historical_verification():
    e, m = valid_evidence()
    assert security.verify_evidence(e, m)["passed"]
    with pytest.raises(ValueError):
        security.check_issuance(
            security.load_policy(), datetime(2099, 1, 1, tzinfo=UTC)
        )
    with pytest.raises(ValueError):
        security.check_issuance(
            security.load_policy(), datetime(2020, 1, 1, tzinfo=UTC)
        )


def test_runtime_image_proof_is_rechecked_not_believed(monkeypatch):
    e, m = valid_evidence()
    from tools.ci import release_artifact

    monkeypatch.setattr(release_artifact, "identity", lambda image: e["subject"])
    forged = deepcopy(e["native"])
    forged["packages"][0]["library_sha256"] = "0" * 64
    monkeypatch.setattr(security, "_native_proof", lambda image, policy: forged)
    with pytest.raises(ValueError):
        security.verify_image(e["subject"]["image_id"], e, m)


def test_tar_inventory_rejects_duplicate_and_escaping_paths(tmp_path):
    for names in [("a", "a"), ("../escape",)]:
        archive = tmp_path / "export.tar"
        with tarfile.open(archive, "w") as tar:
            for name in names:
                member = tarfile.TarInfo(name)
                member.size = 1
                tar.addfile(member, io.BytesIO(b"x"))
        with pytest.raises(ValueError):
            security._read_export(archive)


def test_scan_writes_failure_material_even_when_scanner_is_missing(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(security.shutil, "which", lambda name: None)
    output = tmp_path / "image-security.json"
    with pytest.raises((ValueError, RuntimeError)):
        security.scan("image", "a" * 40, "b" * 40, "123", output)
    evidence = json.loads(output.read_text())
    assert evidence["errors"] and evidence["verdict"]["passed"] is False


def test_sbom_is_bound_to_the_paired_scan_database():
    e, m = valid_evidence()
    m["materials"] = {"sbom.cdx.json": "0" * 64}
    with pytest.raises(ValueError):
        security.verify_evidence(e, m)


@pytest.mark.parametrize("target", ["../../escape", "/missing", "self"])
def test_library_links_cannot_escape_or_cycle(target):
    entries = {"self": {"kind": "symlink", "link": target}}
    with pytest.raises(ValueError):
        security._resolve(entries, "/self")


def test_an_alternate_vulnerable_sqlite_copy_is_not_certified():
    e, m = valid_evidence()
    e["native"]["packages"][0]["copies"]["/usr/local/lib/libsqlite3.so.0"] = "0" * 64
    with pytest.raises(ValueError):
        security.verify_evidence(e, m)


@pytest.mark.parametrize("identifier", ["demo@1.0", "substituted@2.0"])
def test_trivy_vex_only_synthesizes_redundant_language_package_ids(identifier):
    e, m = valid_evidence()
    package = {
        "Name": "demo",
        "Version": "1.0",
        "Identifier": {"PURL": "pkg:pypi/demo@1.0"},
        "FilePath": "opt/venv/demo.dist-info/METADATA",
    }
    result = {
        "Target": "Python",
        "Type": "python-pkg",
        "Class": "lang-pkgs",
        "Packages": [package],
    }
    e["raw"]["Results"].append(deepcopy(result))
    result["Packages"][0]["ID"] = identifier
    e["filtered"]["Results"].append(result)
    if identifier == "demo@1.0":
        assert security.verify_evidence(e, m)["passed"]
    else:
        with pytest.raises(ValueError):
            security.verify_evidence(e, m)
