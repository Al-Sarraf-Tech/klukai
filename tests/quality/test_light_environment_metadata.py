"""Discovery aliases are one root; genuinely separate metadata stays exclusive."""

from __future__ import annotations

import json
import subprocess
import sys
from zipfile import ZipFile, ZipInfo

import pytest


@pytest.fixture
def subject():
    from tools.ci import light_environment

    return light_environment


@pytest.fixture
def discovery(tmp_path, subject):
    def probe(roots):
        # Keep the real stdlib while isolating this controlled metadata fixture
        # from the test runner's installed third-party distributions.
        prefix = """import json,sys
roots=json.loads(sys.argv[1])
sys.path=[*roots,*[p for p in sys.path if 'site-packages' not in p and 'dist-packages' not in p]]
"""
        argv = [
            sys.executable,
            "-I",
            "-B",
            "-c",
            prefix + subject.PACKAGE_PROBE,
            json.dumps([str(root) for root in roots]),
        ]
        result = subprocess.run(argv, capture_output=True, text=True, timeout=5)
        (tmp_path / "probe.json").write_text(
            json.dumps(
                {
                    "argv": argv,
                    "exit_code": result.returncode,
                    "stdout": result.stdout,
                    "stderr": result.stderr,
                },
                indent=2,
            )
        )
        assert result.returncode == 0, result.stderr
        return json.loads(result.stdout)

    return probe


def metadata(root, directory, name="Alias_Probe", version="1"):
    target = root / directory
    target.mkdir(parents=True)
    fields = "Metadata-Version: 2.1\n"
    if name is not None:
        fields += f"Name: {name}\n"
    if version is not None:
        fields += f"Version: {version}\n"
    (target / "METADATA").write_text(fields)
    return target


@pytest.fixture
def aliased_site(tmp_path):
    canonical = tmp_path / "lib/python3.14/site-packages"
    canonical.mkdir(parents=True)
    (tmp_path / "lib64").symlink_to("lib", target_is_directory=True)
    alias = tmp_path / "lib64/python3.14/site-packages"
    return canonical, alias


def test_shared_physical_discovery_roots_are_one_installation(
    subject, discovery, aliased_site
):
    canonical, alias = aliased_site
    target = metadata(canonical, "alias_probe-1.dist-info")
    assert (alias / target.name).samefile(target)
    result = discovery([canonical, alias, canonical / "../site-packages"])
    assert result["packages"] == [["Alias_Probe", "1"]]
    assert subject.packages(result, {"alias-probe": "1"}) == {"alias-probe": "1"}


@pytest.mark.parametrize("version", ["1", "2"])
@pytest.mark.parametrize("same_root", [False, True])
def test_distinct_metadata_with_duplicate_normalized_name_is_rejected(
    subject, discovery, aliased_site, tmp_path, version, same_root
):
    canonical, _alias = aliased_site
    first = metadata(canonical, "alias_probe-1.dist-info")
    foreign = canonical if same_root else tmp_path / "foreign root"
    second = metadata(foreign, "other-dist.dist-info", "alias.probe", version)
    assert not first.samefile(second)
    result = discovery([canonical] if same_root else [canonical, foreign])
    assert len(result["packages"]) == 2
    with pytest.raises(ValueError, match="Duplicate installed distribution"):
        subject.packages(result, {"alias-probe": "1"})


@pytest.mark.parametrize("damage", ["missing-name", "missing-version", "empty"])
def test_real_malformed_metadata_is_not_admitted(
    subject, discovery, aliased_site, damage
):
    canonical, alias = aliased_site
    target = metadata(
        canonical,
        "alias_probe-1.dist-info",
        None if damage == "missing-name" else "Alias_Probe",
        None if damage == "missing-version" else "1",
    )
    if damage == "empty":
        (target / "METADATA").write_text("")
    with pytest.raises(ValueError, match="Malformed installed distribution"):
        subject.packages(discovery([canonical, alias]), {"alias-probe": "1"})


@pytest.mark.parametrize("damage", ["missing", "extra", "wrong-version", "zip-extra"])
def test_every_real_discovery_root_preserves_complete_exclusive_package_closure(
    subject, discovery, aliased_site, tmp_path, damage
):
    canonical, alias = aliased_site
    metadata(
        canonical,
        "alias_probe-1.dist-info",
        version="2" if damage == "wrong-version" else "1",
    )
    roots = [canonical, alias]
    expected = {"alias-probe": "1"}
    if damage == "missing":
        expected["required-package"] = "1"
    elif damage == "extra":
        foreign = tmp_path / "foreign root"
        metadata(foreign, "foreign-9.dist-info", "Foreign_Project", "9")
        roots.append(foreign)
    elif damage == "zip-extra":
        archive = tmp_path / "foreign.zip"
        with ZipFile(archive, "w") as zipped:
            zipped.writestr(
                ZipInfo("foreign-9.dist-info/METADATA", (2020, 1, 1, 0, 0, 0)),
                "Name: Foreign_Project\nVersion: 9\n",
            )
        roots.append(archive)
    result = discovery(roots)
    if damage in {"extra", "zip-extra"}:
        assert ["Foreign_Project", "9"] in result["packages"]
    with pytest.raises(ValueError, match="exclusive complete lock"):
        subject.packages(result, expected)
