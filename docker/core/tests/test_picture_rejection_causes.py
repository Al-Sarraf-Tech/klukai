"""The prompt words that made her checker reject a picture twice and fail the job.

Owner goal 2026-11-01. Both pictures the checker rejected twice were Elmo scenes:

- "Large red 'ELMO'S' text is visible on the ceiling", then 'ELMO' lettering on a
  vehicle, with "tank treads and hull inside the room". The prompt said "inside
  the Elmo, a colossal armored land vehicle on tank treads".
- "Open sky, desert landscape ... through the viewports", on a scene that also
  said to avoid outdoor scenery. The prompt named "the armored viewports" and
  "the dark wasteland".
"""

from __future__ import annotations

import re

import pytest

from app import image_scene as sc
from app.image_provider import cloud_prompt

INSIDE = [p for p in sc.PLACES.values() if p.elmo and p.indoor]
OUTSIDE = [p for p in sc.PLACES.values() if p.elmo and not p.indoor]
IDS = lambda p: p.key  # noqa: E731


def test_the_elmo_has_rooms_and_an_outside():
    assert len(INSIDE) >= 5 and len(OUTSIDE) >= 1


@pytest.mark.parametrize("place", sc.PLACES.values(), ids=IDS)
def test_no_picture_is_told_the_ships_name(place):
    assert "elmo" not in place.tags.lower()


@pytest.mark.parametrize("place", INSIDE, ids=IDS)
def test_a_room_never_mentions_its_outside(place):
    assert not re.search(
        r"\b(treads?|hull|colossal|viewports?|windows?)\b", place.tags, re.I
    )
    assert sc.ELMO_INTERIOR in place.tags


@pytest.mark.parametrize("place", OUTSIDE, ids=IDS)
def test_the_outside_is_still_a_land_vehicle(place):
    assert sc.ELMO in place.tags


@pytest.mark.parametrize("place", INSIDE, ids=IDS)
@pytest.mark.parametrize("time", ["morning", "afternoon", "evening", "night"])
def test_a_room_is_lit_from_inside_at_every_hour(place, time):
    tags = sc.time_tags(place, time, indoor=True)
    assert tags == f"{time}, {place.light}"


@pytest.mark.parametrize("place", INSIDE, ids=IDS)
@pytest.mark.parametrize("weather", ["rain", "snow"])
def test_a_room_shows_no_weather(place, weather):
    assert sc.weather_tags(place, weather, indoor=True) == ""


def test_a_room_with_a_window_still_has_one():
    place = sc.PLACES["cafe"]
    assert "through" in sc.time_tags(place, "morning", indoor=True)
    assert sc.weather_tags(place, "rain", indoor=True).startswith("rain on ")


@pytest.mark.parametrize(
    ("scene", "expected"),
    [
        ("on the Elmo deck at dusk", "on the mobile base deck at dusk"),
        ("in the Elmo's hangar", "in the mobile base's hangar"),
        ("aboard Elmo", "aboard the mobile base"),
        ("the ELMO's roof", "the mobile base's roof"),
        ("a shirt from Elmore", "a shirt from Elmore"),
    ],
)
def test_the_name_never_reaches_the_image_model_from_any_path(scene, expected):
    assert f"Scene and appearance: {expected}." in cloud_prompt(scene)


def test_the_name_is_gone_from_exclusions_too():
    assert "elmo" not in cloud_prompt("a room", negative_extra="the Elmo").lower()


@pytest.mark.parametrize("hour", [9, 15, 19, 23])
def test_a_room_on_the_elmo_never_reaches_the_model_with_its_outside(hour):
    brief = sc.resolve_scene(
        "draw yourself in the command center in the rain", [], hour=hour, couple=False
    )
    assert brief.place.key == "command_center"
    prompt = cloud_prompt(f"{brief.sentence()} {brief.tags()}")
    for word in ("elmo", "tread", "viewport", "wasteland", "rain"):
        assert word not in prompt.lower(), word
    assert "stays indoors" in prompt


def test_the_outside_still_says_it_is_a_land_vehicle_without_the_name():
    brief = sc.resolve_scene("draw yourself on the Elmo deck", [], hour=15, couple=False)
    assert brief.place.key == "elmo_topside"
    prompt = cloud_prompt(brief.sentence())
    assert "tank treads" in prompt and "dry land" in prompt
    assert "elmo" not in prompt.lower()
