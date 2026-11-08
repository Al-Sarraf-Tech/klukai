"""Exact, artifact-bound SQLite adjudication; no ambient vulnerability ignores.

The installed policy is reviewed code, not a user-provided exception list.
Evidence is only accepted in the signed release chain. Native file hashes are
computed outside the candidate; a disposable probe corroborates the loaded path.
"""

from __future__ import annotations

import argparse
from collections import Counter
from datetime import UTC, datetime, timedelta
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import posixpath
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
from typing import Any

POLICY_PATH = Path(__file__).with_name("image_security_policy.json")
EMBEDDED_KEYS = (
    "package",
    "version",
    "architecture",
    "url",
    "sha256",
    "runtime_library",
    "library_sha256",
)
SUBJECT_KEYS = ("revision", "source_tree", "run", "layers", "config_sha256")
MAX_JSON = 32 * 1024 * 1024


def digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def _keys(value: Any, keys: tuple | set, name: str) -> None:
    _require(
        isinstance(value, dict) and set(value) == set(keys),
        f"{name}: malformed or unknown fields",
    )


def _hex(value: Any, prefix: str = "", size: int = 64) -> bool:
    return (
        isinstance(value, str)
        and re.fullmatch(re.escape(prefix) + f"[0-9a-f]{{{size}}}", value) is not None
    )


def _time(value: str) -> datetime:
    try:
        stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
        _require(stamp.tzinfo is not None, "Timestamp needs a timezone")
        return stamp
    except (TypeError, AttributeError, ValueError) as exc:
        raise ValueError("Invalid evidence timestamp") from exc


def _unique_pairs(pairs):
    result = {}
    for key, value in pairs:
        _require(key not in result, "Duplicate JSON key")
        result[key] = value
    return result


def _json(path: Path) -> dict:
    _require(path.stat().st_size <= MAX_JSON, "JSON evidence exceeds size limit")
    result = json.loads(path.read_text(), object_pairs_hook=_unique_pairs)
    _require(isinstance(result, dict), "JSON evidence must be an object")
    return result


def _sha(path: Path) -> str:
    with path.open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()


def load_policy() -> dict:
    policy = _json(POLICY_PATH)
    _keys(
        policy,
        (
            "id",
            "issued_at",
            "expires_at",
            "status",
            "cves",
            "packages",
            "os",
            "loader_environment",
            "scanner",
            "provenance",
            "fixed_evidence",
        ),
        "policy",
    )
    _require(
        policy["status"] == "fixed"
        and policy["cves"] == ["CVE-2026-119373", "CVE-2026-119374", "CVE-2026-119379"],
        "Unsupported adjudication scope",
    )
    _require(
        _time(policy["issued_at"]) < _time(policy["expires_at"]),
        "Invalid policy validity interval",
    )
    _require(
        policy["os"] == {"Family": "debian", "Name": "13.7"}, "Unsupported OS policy"
    )
    _require(len(policy["packages"]) == 2, "Incomplete native policy")
    for package, name, version, source in zip(
        policy["packages"],
        ["libsqlite3-0", "zlib1g"],
        ["3.54.1-2", "1:1.3.dfsg+really1.3.1-1+deb13u1"],
        ["sqlite3", "zlib"],
    ):
        _keys(package, (*EMBEDDED_KEYS, "source", "purl"), "package policy")
        _require(
            package["package"] == name
            and package["version"] == version
            and package["source"] == source
            and package["architecture"] == "amd64",
            "Unsupported package policy",
        )
        _require(
            _hex(package["sha256"]) and _hex(package["library_sha256"]),
            "Invalid policy hashes",
        )
        _require(
            package["purl"].startswith("pkg:deb/debian/" + name + "@")
            and "arch=amd64" in package["purl"]
            and "*" not in package["purl"],
            "Invalid policy PURL",
        )
    return policy


def check_issuance(policy: dict, now: datetime) -> None:
    _require(
        _time(policy["issued_at"]) <= now < _time(policy["expires_at"]),
        "Security adjudication policy is not valid at issuance",
    )


def invocations(image_id: str) -> dict:
    common = [
        "image",
        "--cache-dir",
        "cache",
        "--config",
        "empty.yaml",
        "--ignorefile",
        "empty.ignore",
        "--skip-db-update",
        "--skip-java-db-update",
        "--offline-scan",
        "--image-src",
        "docker",
        "--scanners",
        "vuln",
        "--list-all-pkgs",
        "--no-progress",
        "--timeout",
        "5m",
        "--severity",
        "HIGH,CRITICAL",
        "--format",
        "json",
    ]
    return {
        "raw": [
            *common,
            "--exit-code",
            "0",
            "--show-suppressed",
            "--output",
            "raw.json",
            image_id,
        ],
        "filtered": [
            *common,
            "--exit-code",
            "1",
            "--show-suppressed",
            "--vex",
            "fixed.openvex.json",
            "--output",
            "filtered.json",
            image_id,
        ],
        "sbom": [
            *common[:-2],
            "--format",
            "cyclonedx",
            "--output",
            "sbom.cdx.json",
            image_id,
        ],
    }


def _findings(report: dict) -> list[tuple[tuple, str, dict]]:
    found = []
    for result in report["Results"]:
        location = (result.get("Target"), result.get("Class"), result.get("Type"))
        for finding in result.get("Vulnerabilities", []):
            _require(isinstance(finding, dict), "Malformed vulnerability")
            found.append((location, digest(finding), finding))
    return found


def _approved(finding: dict, policy: dict) -> bool:
    pkg = policy["packages"][0]
    return (
        finding.get("VulnerabilityID") in policy["cves"]
        and finding.get("PkgName") == pkg["package"]
        and finding.get("InstalledVersion") == pkg["version"]
        and finding.get("PkgID") == pkg["package"] + "@" + pkg["version"]
        and (finding.get("PkgIdentifier") or {}).get("PURL") == pkg["purl"]
        and finding.get("Status") == "affected"
        and finding.get("Severity") in ("HIGH", "CRITICAL")
    )


def make_vex(raw: dict, subject: dict, policy: dict, timestamp: str) -> dict:
    cves = sorted(
        {v["VulnerabilityID"] for _, _, v in _findings(raw) if _approved(v, policy)}
    )
    return {
        "@context": "https://openvex.dev/ns/v0.2.0",
        "@id": "https://klukai.invalid/security/"
        + digest({k: subject[k] for k in SUBJECT_KEYS}),
        "author": "Klukai release engineering",
        "timestamp": timestamp,
        "version": 1,
        "statements": [
            {
                "vulnerability": {"name": cve},
                "products": [{"@id": policy["packages"][0]["purl"]}],
                "status": "fixed",
                "status_notes": "Exact reviewed Debian 3.54.1-2 amd64 bytes; "
                + policy["fixed_evidence"][cve],
            }
            for cve in cves
        ],
    }


def _native_valid(native: dict, policy: dict) -> None:
    _keys(
        native,
        (
            "packages",
            "sqlite_version",
            "loaded_sqlite",
            "loader_environment",
            "os_release",
        ),
        "native proof",
    )
    _require(native["sqlite_version"] == "3.54.1", "Unexpected loaded SQLite version")
    _require(
        native["loader_environment"] == policy["loader_environment"],
        "Unexpected loader override",
    )
    _require(
        native["os_release"] == {"ID": "debian", "VERSION_ID": "13"},
        "Untruthful OS identity",
    )
    _require(
        isinstance(native["packages"], list) and len(native["packages"]) == 2,
        "Missing native packages",
    )
    for actual, expected in zip(native["packages"], policy["packages"]):
        _keys(
            actual,
            (
                "package",
                "version",
                "source",
                "architecture",
                "archive_sha256",
                "resolved_path",
                "library_sha256",
                "copies",
                "embedded",
            ),
            "native package",
        )
        for key in ("package", "version", "source", "architecture", "library_sha256"):
            _require(
                actual[key] == expected[key],
                f"Native {key} differs from reviewed policy",
            )
        _require(
            actual["archive_sha256"] == expected["sha256"],
            "Unreviewed archive identity",
        )
        _require(
            actual["embedded"] == {k: expected[k] for k in EMBEDDED_KEYS},
            "Embedded provenance changed",
        )
        copies = actual["copies"]
        _require(
            isinstance(copies, dict) and copies and actual["resolved_path"] in copies,
            "Missing independently exported library",
        )
        _require(expected["runtime_library"] in copies, "Missing reviewed library path")
        _require(
            all(
                p.startswith("/")
                and ".." not in PurePosixPath(p).parts
                and h == expected["library_sha256"]
                for p, h in copies.items()
            ),
            "Unexpected library copy",
        )
        _require(
            copies[actual["resolved_path"]] == actual["library_sha256"],
            "Unverified resolved library",
        )
    _require(
        native["loaded_sqlite"] == [native["packages"][0]["resolved_path"]],
        "Python did not load exactly the independently verified SQLite library",
    )


def _report_valid(
    report: dict, subject: dict, policy: dict, started: datetime, finished: datetime
) -> None:
    _require(
        report.get("SchemaVersion") == 2
        and report.get("Trivy", {}).get("Version") == "0.71.0",
        "Unsupported Trivy report",
    )
    _require(
        report.get("ArtifactType") == "container_image"
        and report.get("ArtifactName") == subject["image_id"],
        "Wrong scan subject",
    )
    metadata = report.get("Metadata", {})
    _require(
        metadata.get("ImageID") == subject["image_id"]
        and metadata.get("DiffIDs") == subject["layers"],
        "Scanned image does not match release layers",
    )
    _require(metadata.get("OS") == policy["os"], "Unexpected scanned distribution")
    _require(
        started <= _time(report.get("CreatedAt")) <= finished,
        "Report timestamp outside scan interval",
    )
    _require(
        isinstance(report.get("Results"), list) and report["Results"],
        "Incomplete report",
    )
    inventory = []
    locations = []
    for result in report["Results"]:
        _require(
            isinstance(result, dict) and isinstance(result.get("Packages"), list),
            "Missing complete package inventory",
        )
        locations.append(
            (result.get("Target"), result.get("Class"), result.get("Type"))
        )
        inventory.extend(result["Packages"])
        for v in result.get("Vulnerabilities", []):
            _require(
                v.get("Severity") in ("HIGH", "CRITICAL"),
                "Unexpected finding severity scope",
            )
            _require(
                v.get("VulnerabilityID")
                and v.get("PkgID")
                and v.get("InstalledVersion"),
                "Incomplete finding",
            )
    _require(len(set(locations)) == len(locations), "Duplicate scan result")
    for expected in policy["packages"]:
        matches = [p for p in inventory if p.get("Name") == expected["package"]]
        _require(len(matches) == 1, "Missing or duplicate reviewed package inventory")
        package = matches[0]
        version = (
            str(package["Epoch"]) + ":" if package.get("Epoch") else ""
        ) + package.get("Version", "")
        if package.get("Release"):
            version += "-" + package["Release"]
        _require(
            version == expected["version"]
            and package.get("Arch") == "amd64"
            and package.get("SrcName") == expected["source"]
            and package.get("AnalyzedBy") == "dpkg"
            and (package.get("Identifier") or {}).get("PURL") == expected["purl"],
            "Package inventory differs from reviewed package",
        )


def _inventory(result: dict) -> dict:
    value = {
        k: v
        for k, v in result.items()
        if k not in ("Vulnerabilities", "ExperimentalModifiedFindings")
    }
    if result.get("Class") == "lang-pkgs":
        # Trivy 0.71.0 VEX assigns this redundant ID when the raw analyzer
        # omitted it. Preserve every other field and reject conflicting IDs.
        packages = []
        for package in result["Packages"]:
            item = dict(package)
            expected = item["Name"] + "@" + item["Version"]
            _require(
                item.get("ID", expected) == expected, "Language package ID changed"
            )
            item["ID"] = expected
            packages.append(item)
        value["Packages"] = packages
    return value


def verify_evidence(envelope: dict, manifest: dict) -> dict:
    """Recompute the verdict, using installed policy and signed-manifest bindings."""
    try:
        return _verify_evidence(envelope, manifest)
    except (KeyError, TypeError, IndexError, AttributeError) as exc:
        raise ValueError("Malformed image security evidence") from exc


def _verify_evidence(e: dict, manifest: dict) -> dict:
    _keys(
        e,
        (
            "schema",
            "policy",
            "subject",
            "scanner",
            "native",
            "raw",
            "filtered",
            "vex",
            "verdict",
            "errors",
        ),
        "envelope",
    )
    _require(
        e["schema"] == 1 and e["errors"] == [], "Incomplete or failed security evidence"
    )
    policy = load_policy()
    _require(
        e["policy"] == {"document": policy, "sha256": digest(policy)},
        "Policy differs from installed reviewed policy",
    )
    subject = e["subject"]
    _keys(subject, (*SUBJECT_KEYS, "image_id"), "subject")
    _require(
        all(subject[k] == manifest.get(k) for k in SUBJECT_KEYS),
        "Security subject differs from manifest",
    )
    _require(
        _hex(subject["revision"], size=40)
        and _hex(subject["source_tree"], size=40)
        and isinstance(subject["run"], str)
        and bool(subject["run"])
        and _hex(subject["config_sha256"])
        and _hex(subject["image_id"], "sha256:")
        and isinstance(subject["layers"], list)
        and subject["layers"]
        and all(_hex(v, "sha256:") for v in subject["layers"]),
        "Invalid immutable security subject",
    )
    scanner = e["scanner"]
    _keys(
        scanner,
        (
            "version",
            "executable_sha256",
            "database",
            "database_after",
            "sbom_sha256",
            "started_at",
            "finished_at",
            "raw_exit",
            "filtered_exit",
            "invocation",
        ),
        "scanner",
    )
    _require(
        scanner["version"] == policy["scanner"]["version"]
        and scanner["executable_sha256"] == policy["scanner"]["executable_sha256"],
        "Untrusted scanner executable",
    )
    _require(
        type(scanner["raw_exit"]) is int
        and scanner["raw_exit"] == 0
        and type(scanner["filtered_exit"]) is int
        and scanner["filtered_exit"] == 0,
        "Scanner failed or found unresolved findings",
    )
    _require(
        scanner["invocation"] == invocations(subject["image_id"]),
        "Scanner scope/configuration changed",
    )
    _require(
        _hex(scanner["sbom_sha256"])
        and scanner["sbom_sha256"]
        == manifest.get("materials", {}).get("sbom.cdx.json"),
        "SBOM differs from paired scanner evidence",
    )
    db = scanner["database"]
    _keys(db, ("sha256", "metadata", "metadata_sha256"), "database")
    _require(
        _hex(db["sha256"])
        and db["metadata_sha256"] == digest(db["metadata"])
        and db == scanner["database_after"],
        "Scanner database changed or is incomplete",
    )
    started, finished = _time(scanner["started_at"]), _time(scanner["finished_at"])
    check_issuance(policy, started)
    _require(
        started <= finished < _time(policy["expires_at"])
        and finished - started < timedelta(hours=1),
        "Invalid scan time interval",
    )
    meta = db["metadata"]
    _keys(
        meta,
        ("Version", "UpdatedAt", "NextUpdate", "DownloadedAt"),
        "database metadata",
    )
    _require(
        meta["Version"] == 2
        and _time(meta["UpdatedAt"]) <= _time(meta["DownloadedAt"]) <= finished
        and _time(meta["UpdatedAt"]) <= started < _time(meta["NextUpdate"]),
        "Stale or invalid scanner database",
    )
    _native_valid(e["native"], policy)
    for report in (e["raw"], e["filtered"]):
        _report_valid(report, subject, policy, started, finished)
    _require(
        e["raw"]["Metadata"] == e["filtered"]["Metadata"]
        and e["raw"].get("ArtifactID") == e["filtered"].get("ArtifactID"),
        "Scan image metadata changed",
    )
    raw_results = e["raw"]["Results"]
    filtered_results = e["filtered"]["Results"]
    _require(len(raw_results) == len(filtered_results), "Scan result inventory changed")
    modified = []
    for raw, filtered in zip(raw_results, filtered_results):
        _require(
            _inventory(raw) == _inventory(filtered),
            "Scan inventory changed",
        )
        _require(
            not raw.get("ExperimentalModifiedFindings"),
            "Raw scan contains suppressed findings",
        )
        location = (raw.get("Target"), raw.get("Class"), raw.get("Type"))
        for change in filtered.get("ExperimentalModifiedFindings", []):
            _keys(
                change,
                ("Type", "Status", "Statement", "Source", "Finding"),
                "modified finding",
            )
            _require(
                change["Type"] == "vulnerability"
                and change["Status"] == "fixed"
                and change["Statement"] == ""
                and change["Source"] == "fixed.openvex.json",
                "Unknown suppression",
            )
            _require(
                _approved(change["Finding"], policy), "Unreviewed modified finding"
            )
            modified.append((location, digest(change["Finding"]), change["Finding"]))
    raw = _findings(e["raw"])
    remaining = _findings(e["filtered"])
    original = Counter((loc, key) for loc, key, _ in raw)
    reconciled = Counter((loc, key) for loc, key, _ in remaining + modified)
    _require(
        all(n == 1 for n in original.values())
        and all(n == 1 for n in reconciled.values())
        and original == reconciled,
        "Lost, added, duplicated or changed finding",
    )
    _require(
        e["vex"] == make_vex(e["raw"], subject, policy, scanner["started_at"]),
        "Unsupported or changed VEX assertion",
    )
    _require(not remaining, "Unresolved HIGH/CRITICAL vulnerabilities")
    return {
        "passed": True,
        "raw_high_critical": len(raw),
        "fixed": len(modified),
        "unresolved_high_critical": 0,
    }


def _docker(*args: str, timeout: int = 300) -> str:
    result = subprocess.run(
        ["docker", *args], capture_output=True, text=True, timeout=timeout
    )
    if result.returncode:
        raise RuntimeError(f"docker {args[0]} failed: {result.stderr[:500]}")
    return result.stdout


def _read_export(path: Path) -> dict:
    """Read selected members without extracting untrusted paths on the host."""
    _require(path.stat().st_size <= 1024 * 1024 * 1024, "Image export exceeds bound")
    entries = {}
    with tarfile.open(path, "r:") as archive:
        for member in archive:
            name = member.name.rstrip("/")
            _require(
                name
                and not name.startswith("/")
                and ".." not in PurePosixPath(name).parts
                and name == posixpath.normpath(name)
                and name not in entries,
                "Ambiguous/escaping archive member",
            )
            _require(
                len(entries) < 100000 and member.size <= 128 * 1024 * 1024,
                "Image member exceeds bound",
            )
            entry = {"kind": "other", "link": member.linkname}
            if member.issym():
                entry["kind"] = "symlink"
            elif member.islnk():
                entry["kind"] = "hardlink"
            elif member.isdir():
                entry["kind"] = "directory"
            elif member.isfile():
                entry["kind"] = "file"
                selected = (
                    re.search(r"(^|/)(libsqlite3|libz)\.so", name) is not None
                    or name
                    in (
                        "etc/os-release",
                        "usr/lib/os-release",
                        "var/lib/dpkg/status",
                        "usr/share/klukai-runtime/native-security-packages.json",
                        "etc/ld.so.preload",
                    )
                    or name.startswith("var/lib/dpkg/status.d/")
                )
                if selected:
                    _require(
                        member.size <= 16 * 1024 * 1024,
                        "Native evidence member exceeds bound",
                    )
                    stream = archive.extractfile(member)
                    _require(stream is not None, "Unreadable native evidence")
                    data = stream.read()
                    entry.update(data=data, sha256=hashlib.sha256(data).hexdigest())
            entries[name] = entry
    return entries


def _resolve(entries: dict, path: str) -> str:
    parts = path.lstrip("/").split("/")
    for _ in range(40):
        changed = False
        for i in range(1, len(parts) + 1):
            name = "/".join(parts[:i])
            entry = entries.get(name)
            _require(entry is not None, "Missing exported library path")
            if entry["kind"] in ("symlink", "hardlink"):
                link = entry["link"]
                base = (
                    ""
                    if link.startswith("/") or entry["kind"] == "hardlink"
                    else posixpath.dirname(name)
                )
                target = posixpath.normpath(posixpath.join(base, link.lstrip("/")))
                _require(
                    target != ".." and not target.startswith("../"),
                    "Escaping library symlink",
                )
                parts = target.split("/") + parts[i:]
                changed = True
                break
        if not changed:
            resolved = "/".join(parts)
            _require(
                entries[resolved]["kind"] == "file",
                "Library path is not a regular file",
            )
            return "/" + resolved
    raise ValueError("Cyclic library symlink")


def _native_proof(image_id: str, policy: dict) -> dict:
    info = json.loads(_docker("image", "inspect", image_id))[0]
    _require(
        info.get("Os") == "linux" and info.get("Architecture") == "amd64",
        "Unreviewed image platform",
    )
    env = {}
    for item in (info.get("Config") or {}).get("Env", []):
        key, _, value = item.partition("=")
        if key.startswith("LD_"):
            _require(key not in env, "Duplicate loader environment")
            env[key] = value
    _require(env == policy["loader_environment"], "Unexpected loader environment")
    container = _docker("create", "--network", "none", image_id).strip()
    try:
        with tempfile.TemporaryDirectory(prefix="klukai-native-") as temporary:
            exported = Path(temporary) / "filesystem.tar"
            _docker("export", "--output", str(exported), container)
            entries = _read_export(exported)
    finally:
        _docker("rm", "-f", container, timeout=30)
    _require(
        "etc/ld.so.preload" not in entries,
        "Image contains loader preload configuration",
    )

    def data(path: str) -> bytes:
        return entries[_resolve(entries, path).lstrip("/")]["data"]

    os_release = {}
    for line in data("/etc/os-release").decode().splitlines():
        key, _, value = line.partition("=")
        if key in ("ID", "VERSION_ID"):
            _require(key not in os_release, "Duplicate OS identity")
            os_release[key] = value.strip('"')
    embedded = json.loads(
        data("/usr/share/klukai-runtime/native-security-packages.json"),
        object_pairs_hook=_unique_pairs,
    )
    _keys(embedded, ("packages",), "embedded provenance")
    _require(
        embedded["packages"]
        == [{k: p[k] for k in EMBEDDED_KEYS} for p in policy["packages"]],
        "Embedded package provenance changed",
    )
    installed = {}
    for name, entry in entries.items():
        if name == "var/lib/dpkg/status" or (
            name.startswith("var/lib/dpkg/status.d/")
            and not name.endswith(".md5sums")
            and entry["kind"] == "file"
        ):
            for paragraph in entry["data"].decode().split("\n\n"):
                fields = {}
                for line in paragraph.splitlines():
                    if line and not line[0].isspace() and ": " in line:
                        key, value = line.split(": ", 1)
                        _require(key not in fields, "Duplicate dpkg field")
                        fields[key] = value
                if fields.get("Package") in [p["package"] for p in policy["packages"]]:
                    _require(
                        fields["Package"] not in installed,
                        "Duplicate native dpkg package",
                    )
                    installed[fields["Package"]] = fields
    records = []
    for p in policy["packages"]:
        fields = installed.get(p["package"], {})
        _require(
            fields.get("Status") == "install ok installed",
            "Native package not installed",
        )
        resolved = _resolve(entries, p["runtime_library"])
        pattern = (
            r"(^|/)libsqlite3\.so"
            if p["package"] == "libsqlite3-0"
            else r"(^|/)libz\.so"
        )
        copies = {}
        for name in entries:
            if re.search(pattern, name):
                target = _resolve(entries, "/" + name)
                copies["/" + name] = entries[target.lstrip("/")]["sha256"]
        records.append(
            {
                "package": p["package"],
                "version": fields.get("Version"),
                "source": fields.get("Source", "").split(" ")[0],
                "architecture": fields.get("Architecture"),
                "archive_sha256": p["sha256"],
                "resolved_path": resolved,
                "library_sha256": entries[resolved.lstrip("/")]["sha256"],
                "copies": copies,
                "embedded": next(
                    x for x in embedded["packages"] if x["package"] == p["package"]
                ),
            }
        )
    probe = """import json,sqlite3
from pathlib import Path
sqlite3.connect(':memory:').execute('select 1').fetchone()
paths=sorted({line.split()[-1] for line in Path('/proc/self/maps').read_text().splitlines() if 'libsqlite3.so' in line})
print(json.dumps({'sqlite_version':sqlite3.sqlite_version,'loaded_sqlite':paths}))
"""
    name = "klukai-security-" + os.urandom(8).hex()
    try:
        output = _docker(
            "run",
            "--name",
            name,
            "--network",
            "none",
            "--read-only",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges",
            "--pids-limit",
            "32",
            "--memory",
            "256m",
            "--cpus",
            "1",
            "--entrypoint",
            "/opt/venv/bin/python",
            image_id,
            "-I",
            "-S",
            "-c",
            probe,
            timeout=45,
        )
        _require(len(output) < 4096, "Unbounded runtime probe output")
        loaded = json.loads(output, object_pairs_hook=_unique_pairs)
        _keys(loaded, ("sqlite_version", "loaded_sqlite"), "loaded path probe")
    finally:
        _docker("rm", "-f", name, timeout=30)
    proof = {
        "packages": records,
        **loaded,
        "loader_environment": env,
        "os_release": os_release,
    }
    _native_valid(proof, policy)
    return proof


def verify_image(image_id: str, envelope: dict, manifest: dict) -> dict:
    verify_evidence(envelope, manifest)
    _require(
        _hex(image_id, "sha256:"), "Runtime verification requires an immutable image ID"
    )
    from tools.ci.release_artifact import identity

    found = identity(image_id)
    _require(
        all(found[k] == manifest[k] for k in ("revision", "layers", "config_sha256")),
        "Loaded image differs from manifest",
    )
    proof = _native_proof(image_id, load_policy())
    _native_valid(proof, load_policy())
    _require(
        proof == envelope["native"],
        "Loaded image native proof differs from scanned artifact",
    )
    return proof


def _database(cache: Path) -> dict:
    metadata = _json(cache / "db" / "metadata.json")
    return {
        "sha256": _sha(cache / "db" / "trivy.db"),
        "metadata": metadata,
        "metadata_sha256": digest(metadata),
    }


def scan(image: str, revision: str, source_tree: str, run: str, output: Path) -> dict:
    """Collect both full HIGH/CRITICAL reports and persist failures before raising."""
    envelope = {
        "schema": 1,
        "policy": None,
        "subject": None,
        "scanner": {},
        "native": None,
        "raw": None,
        "filtered": None,
        "vex": None,
        "verdict": {"passed": False},
        "errors": [],
    }
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    try:
        policy = load_policy()
        check_issuance(policy, datetime.now(UTC))
        envelope["policy"] = {"document": policy, "sha256": digest(policy)}
        scanner = shutil.which("trivy")
        _require(scanner is not None, "Pinned Trivy is missing from PATH")
        _require(
            _sha(Path(scanner)) == policy["scanner"]["executable_sha256"],
            "Trivy executable is not the reviewed binary",
        )
        from tools.ci.release_artifact import identity

        found = identity(image)
        _require(
            found["revision"] == revision,
            "Candidate image revision label differs from full revision",
        )
        subject = {k: found[k] for k in ("layers", "config_sha256", "image_id")}
        subject.update(revision=revision, source_tree=source_tree, run=run)
        envelope["subject"] = subject
        started = datetime.now(UTC).isoformat()
        envelope["native"] = _native_proof(subject["image_id"], policy)
        with tempfile.TemporaryDirectory(prefix="klukai-security-") as temporary:
            work = Path(temporary)
            (work / "empty.ignore").write_text("")
            (work / "empty.yaml").write_text("{}\n")
            env = {
                "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
                "HOME": str(work),
                "DOCKER_CONFIG": str(work / "docker"),
                "XDG_CONFIG_HOME": str(work / "config"),
            }

            def invoke(arguments: list[str], label: str) -> int:
                result = subprocess.run(
                    [scanner, *arguments],
                    cwd=work,
                    env=env,
                    capture_output=True,
                    text=True,
                    timeout=600,
                )
                (output.parent / f"image-security-{label}.log").write_text(
                    result.stdout + result.stderr
                )
                return result.returncode

            code = invoke(
                [
                    "image",
                    "--cache-dir",
                    "cache",
                    "--config",
                    "empty.yaml",
                    "--download-db-only",
                    "--no-progress",
                ],
                "database",
            )
            _require(code == 0, "Trivy database download failed")
            database = _database(work / "cache")
            commands = invocations(subject["image_id"])
            record = {
                "version": policy["scanner"]["version"],
                "executable_sha256": _sha(Path(scanner)),
                "database": database,
                "started_at": started,
                "invocation": commands,
            }
            envelope["scanner"] = record
            record["raw_exit"] = invoke(commands["raw"], "raw")
            if (work / "raw.json").exists():
                shutil.copyfile(
                    work / "raw.json", output.parent / "image-security-raw.json"
                )
                envelope["raw"] = _json(work / "raw.json")
            _require(
                record["raw_exit"] == 0 and envelope["raw"] is not None,
                "Raw Trivy scan failed",
            )
            envelope["vex"] = make_vex(envelope["raw"], subject, policy, started)
            (work / "fixed.openvex.json").write_text(json.dumps(envelope["vex"]))
            record["filtered_exit"] = invoke(commands["filtered"], "filtered")
            if (work / "filtered.json").exists():
                shutil.copyfile(
                    work / "filtered.json",
                    output.parent / "image-security-filtered.json",
                )
                envelope["filtered"] = _json(work / "filtered.json")
            _require(invoke(commands["sbom"], "sbom") == 0, "Trivy SBOM failed")
            shutil.copyfile(work / "sbom.cdx.json", output.parent / "sbom.cdx.json")
            record["sbom_sha256"] = _sha(work / "sbom.cdx.json")
            record["database_after"] = _database(work / "cache")
            record["finished_at"] = datetime.now(UTC).isoformat()
        envelope["verdict"] = verify_evidence(
            envelope, {**subject, "materials": {"sbom.cdx.json": record["sbom_sha256"]}}
        )
        return envelope
    except (
        ValueError,
        RuntimeError,
        OSError,
        subprocess.SubprocessError,
        KeyError,
        TypeError,
    ) as exc:
        envelope["errors"].append(str(exc))
        raise
    finally:
        output.write_text(json.dumps(envelope, sort_keys=True, indent=2) + "\n")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    command = commands.add_parser("scan")
    for name in ("image", "revision", "source-tree", "run"):
        command.add_argument("--" + name, required=True)
    command.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        envelope = scan(
            args.image, args.revision, args.source_tree, args.run, args.output
        )
    except (
        ValueError,
        RuntimeError,
        OSError,
        subprocess.SubprocessError,
        KeyError,
        TypeError,
    ) as error:
        print(f"image security refused: {error}", file=sys.stderr)
        return 1
    print(json.dumps(envelope["verdict"], sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
