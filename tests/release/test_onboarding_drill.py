from __future__ import annotations

from pathlib import Path

from tools.ci import onboarding_drill as drill

DOC = Path(__file__).resolve().parents[2] / "docs/onboarding.md"


def test_every_drill_command_is_still_in_the_onboarding_doc():
    doc = DOC.read_text()
    for section, command in drill.STEPS:
        if command:
            assert drill.documented(doc, command), (section, command)


def test_a_command_that_drifted_from_the_doc_is_detected():
    assert not drill.documented(
        "docker compose up -d core\n", "docker compose up -d companion-core"
    )
    assert drill.documented("docker compose ps   # note\n", "docker compose ps")


def test_fill_env_replaces_and_appends_without_touching_other_lines(tmp_path):
    env = tmp_path / ".env"
    env.write_text("# comment\nPOSTGRES_PASSWORD=\nOTHER=keep\n")
    drill.fill_env(env, {"POSTGRES_PASSWORD": "a", "VOICE_API_TOKEN": "b"})
    assert (
        env.read_text()
        == "# comment\nPOSTGRES_PASSWORD=a\nOTHER=keep\nVOICE_API_TOKEN=b\n"
    )


def test_built_images_follow_the_pinned_compose_project_name(tmp_path):
    (tmp_path / "docker-compose.yml").write_text(
        "# header\nname: companion\nservices: {}\n"
    )
    assert drill.built_images(tmp_path) == [
        "companion-companion-core",
        "companion-events-bridge",
    ]
    repo = drill.built_images(Path(__file__).resolve().parents[2])
    assert repo[0] == "companion-companion-core"
