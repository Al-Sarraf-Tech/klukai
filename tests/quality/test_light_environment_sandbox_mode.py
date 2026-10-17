"""Runners without user namespaces must opt out of the wheel sandbox explicitly."""

import pytest

from tools.ci import component_environment, light_environment_sandbox as sandbox


def test_sandbox_is_required_by_default(monkeypatch):
    monkeypatch.delenv(sandbox.MODE_VARIABLE, raising=False)
    assert sandbox.mode() == "required"


def test_unknown_sandbox_mode_is_refused(monkeypatch):
    monkeypatch.setenv(sandbox.MODE_VARIABLE, "off")
    with pytest.raises(ValueError, match="Unknown native sandbox mode"):
        sandbox.mode()


def test_unavailable_record_needs_the_explicit_opt_out(monkeypatch):
    monkeypatch.delenv(sandbox.MODE_VARIABLE, raising=False)
    with pytest.raises(ValueError, match="opt-out was not selected"):
        sandbox.unchanged(dict(sandbox.UNAVAILABLE))
    monkeypatch.setenv(sandbox.MODE_VARIABLE, "unavailable")
    sandbox.unchanged(dict(sandbox.UNAVAILABLE))


def test_altered_unavailable_record_is_not_accepted(monkeypatch):
    monkeypatch.setenv(sandbox.MODE_VARIABLE, "unavailable")
    forged = {**sandbox.UNAVAILABLE, "capability": True}
    with pytest.raises(KeyError):
        sandbox.unchanged(forged)


def test_cleared_environment_matches_the_sandbox_environment():
    prefix = sandbox.cleared_environment()
    assert prefix[:2] == ["/usr/bin/env", "-i"]
    assert "PIP_CONFIG_FILE=/dev/null" in prefix
    assert "SOURCE_DATE_EPOCH=0" in prefix
    assert not any(item.startswith(("PIP_INDEX", "HTTP")) for item in prefix)


def test_component_namespace_skips_the_probe_only_when_opted_out(monkeypatch, tmp_path):
    class Refuse:
        def run(self, argv):
            raise AssertionError("no namespace probe in unavailable mode")

    monkeypatch.setenv(sandbox.MODE_VARIABLE, "unavailable")
    assert component_environment.namespace(Refuse(), tmp_path) == sandbox.UNAVAILABLE
    monkeypatch.delenv(sandbox.MODE_VARIABLE)
    monkeypatch.setattr(sandbox, "BWRAP", tmp_path / "missing-bwrap")
    with pytest.raises(ValueError, match="bwrap"):
        component_environment.namespace(Refuse(), tmp_path)
