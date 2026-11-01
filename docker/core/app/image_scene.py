"""Where and when a picture happens: the scene brief (pure, deterministic).

Before any tag is written, the brief settles the picture's setting, in this
strict order:

1. a place named in HIS current request ("draw us in the kitchen");
2. else the newest place HE established in the recent conversation ("we're in
   my room", "let's go to the beach", "come over"), scanning his turns newest
   first;
3. else, only for a picture of her alone, where SHE is: the place she states
   in her latest reply ("I'm in the rec room"), else her Her Day roster block;
4. else a neutral setting that fits the hour, picked by seed so that no single
   place keeps coming back.

Her replies never override a place he stated. A negation vetoes a place ("not
outside", "we're not at the beach anymore"), a newer move beats an older one,
and wishes, memories, questions and other people's whereabouts establish
nothing. The brief also fixes indoors/outdoors, the time of day (his words,
else the Commander's local hour) and the weather (his words only). Every later
step follows it: scene tags, scene dressing, the candid pose, the cloud
renderer's setting line and avoid list. No LLM call, no I/O.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass

from .image_gen_constants import (
    COMMANDER_OUTFIT_MAP,
    IMAGE_MOOD_MAP,
    SITUATION_KEYWORDS,
    SQUAD_KEYWORDS,
)
from .image_prompts import lighting_period
from .wardrobe_variation import derive_seed, variation_rng

REQUEST = "request"
HIS_HISTORY = "his-history"
HER_REPLY = "her-reply"
HER_DAY = "her-day"
DEFAULT = "default"

# His recent turns that may still say where he is.
HIS_TURNS = 8

TIMES = ("morning", "afternoon", "evening", "night")
_ALL = frozenset(TIMES)
_DAYLIGHT = frozenset({"morning", "afternoon", "evening"})
_LATE = frozenset({"evening", "night"})

# The Elmo is a LAND vessel. Its wording never borrows from ships.
ELMO = "a colossal armored land vehicle on tank treads"
# A room never mentions the vehicle's outside or its name. Told "inside the Elmo,
# a colossal armored land vehicle on tank treads", the image model painted "ELMO"
# on the ceiling and treads in the room, and the checker failed the job. Told "the
# armored viewports", it painted a desert through them. The rooms have no windows.
ELMO_INTERIOR = "windowless riveted steel interior of an armored land base"
ELMO_AVOID = "ship, boat, ocean, sea, harbor, naval vessel"

_HOME = COMMANDER_OUTFIT_MAP["home"]
_CASUAL = COMMANDER_OUTFIT_MAP["casual"]


@dataclass(frozen=True)
class Place:
    """One setting: how it is matched, described, lit and dressed for."""

    key: str
    label: str
    indoor: bool
    phrases: tuple[str, ...]
    tags: str
    wardrobe: str | None = None  # an app/wardrobe_scene.py scene key
    elmo: bool = False  # aboard or on the Elmo
    specificity: int = 1  # 0 generic, 1 area, 2 room or spot
    light: str = "warm lamp light"
    window: str | None = "the window"  # None: no window, so no sky or weather
    night_view: str = "city lights"
    actions: tuple[str, ...] = ()  # candid solo actions that belong here
    commander: str | None = None  # his outfit when his words set none
    history: bool = True  # may be established from his earlier turns


def _place(
    key: str, label: str, indoor: bool, phrases: str, tags: str, **kw: object
) -> Place:
    words = tuple(p.strip() for p in phrases.split(",") if p.strip())
    return Place(key, label, indoor, words, tags, **kw)  # type: ignore[arg-type]


_ELMO_INSIDE = {
    "elmo": True,
    "light": "warm work lights",
    "window": None,
}

PLACES: dict[str, Place] = {
    p.key: p
    for p in (
        # ── His home ──────────────────────────────────────────────────────
        _place(
            "his_bedroom",
            "his bedroom",
            True,
            "my room, my bedroom, my bed, our room, our bedroom, our bed",
            "the Commander's bedroom, a lived-in modern bedroom, bed with pillows, "
            "desk with a lamp, shelves",
            specificity=2,
            commander=_HOME,
            actions=("sitting on the desk chair, legs crossed", "hugging a pillow"),
        ),
        _place(
            "bedroom",
            "a bedroom",
            True,
            "the bedroom, bedroom, in bed, into bed, to bed, on the bed, "
            "under the covers, under the blankets",
            "a cozy bedroom, bed with pillows, bedside lamp",
            specificity=2,
            commander=_HOME,
            actions=("hugging a pillow",),
        ),
        _place(
            "his_home",
            "his home",
            True,
            "my place, my house, my apartment, my flat, my home, our place, our home, "
            "our house, our apartment, at home, back home, home, come over, at mine, "
            "i'm home, im home, we're home",
            "the Commander's apartment, a cozy modern living space, sofa, bookshelves",
            commander=_HOME,
        ),
        _place(
            "living_room",
            "living room",
            True,
            "living room, family room, tv room, the couch, the sofa, couch, sofa",
            "a cozy living room, sofa with cushions, low coffee table, television",
            specificity=2,
            commander=_HOME,
            actions=(
                "sitting on the sofa, legs tucked under her",
                "sitting on the sofa arm",
            ),
        ),
        _place(
            "kitchen",
            "kitchen",
            True,
            "kitchen, the stove",
            "a home kitchen, counters, stovetop, hanging utensils",
            specificity=2,
            commander=_HOME,
            actions=(
                "leaning against the counter, holding a mug",
                "stirring a pot at the stove",
            ),
        ),
        # Request only: a washroom is never inferred from an earlier turn, and
        # the nudity rules stay with his own words (app/image_provider.py).
        _place(
            "washroom",
            "washroom",
            True,
            "bathroom, bath, bathtub, the tub, shower",
            "a steamy washroom, tiled walls, soft warm light",
            specificity=2,
            history=False,
        ),
        _place(
            "spring",
            "natural spring",
            False,
            "onsen, hot spring, hot springs",
            "a natural spring among rocks, rising steam, trees around",
            specificity=2,
            history=False,
        ),
        _place(
            "office",
            "office",
            True,
            "the office, my office, office, my desk, at my desk, the desk, at work, "
            "my work, workplace, cubicle",
            "an office, desk with monitors and paperwork, office chair",
            specificity=2,
            light="overhead office lighting",
            commander=_CASUAL,
            actions=(
                "sitting on the edge of the desk",
                "leaning over the desk, looking at a monitor",
            ),
        ),
        _place(
            "car",
            "car",
            True,
            "in the car, the car, my car, car, my truck, the truck, passenger seat, "
            "driver's seat, front seat",
            "inside a car, front seats, dashboard, seatbelts",
            specificity=2,
            light="soft dashboard glow",
            window="the windshield",
            commander=_CASUAL,
            actions=(
                "sitting in the passenger seat, seatbelt on",
                "looking out the side window",
            ),
        ),
        # The same rooms in his own life: his gym and garage are not the Elmo's.
        _place(
            "gym",
            "gym",
            True,
            "gym, the gym, fitness center, workout room, weight room",
            "a gym, weight racks, treadmills, mirrored wall",
            specificity=2,
            wardrobe="training",
            light="bright overhead lights",
            commander=COMMANDER_OUTFIT_MAP["training"],
            actions=("drinking from water bottle", "stretching, warming up"),
        ),
        _place(
            "garage",
            "garage",
            True,
            "garage, my garage, workshop",
            "a garage workshop, a motorcycle on a work stand, tool wall, concrete floor",
            specificity=2,
            wardrobe="garage",
            light="harsh overhead work lights",
            window="the open garage door",
            commander=_CASUAL,
        ),
        # ── Outings ───────────────────────────────────────────────────────
        _place(
            "cafe",
            "cafe",
            True,
            "cafe, coffee shop, coffeeshop, coffee house",
            "a cozy cafe, small wooden table, coffee cups, large front windows",
            specificity=2,
            wardrobe="cafe",
            light="warm cafe lighting",
            night_view="streetlights",
            commander=_CASUAL,
            actions=(
                "holding a coffee cup with both hands",
                "sitting at a small table, chin on hand",
            ),
        ),
        _place(
            "restaurant",
            "restaurant",
            True,
            "restaurant, diner, bistro, steakhouse, pizza place, sushi place",
            "a restaurant, table with plates and candles, warm dim lighting",
            specificity=2,
            wardrobe="date",
            light="candlelight",
            night_view="streetlights",
            commander=COMMANDER_OUTFIT_MAP["date"],
            actions=("sitting at the table, chin on hand", "raising a glass"),
        ),
        _place(
            "bar",
            "bar",
            True,
            "the bar, a bar, pub, cocktail bar, nightclub",
            "a dim bar, counter with backlit bottle shelves, bar stools",
            specificity=2,
            light="low amber lighting",
            night_view="streetlights",
            commander=_CASUAL,
            actions=("sitting on a bar stool", "holding a glass"),
        ),
        _place(
            "mall",
            "shops",
            True,
            "mall, shopping mall, the store, a store, the shops, shopping, supermarket, "
            "grocery store, groceries, bookstore, convenience store, the market",
            "a shopping mall, glass shop fronts with plain awnings, polished floors",
            wardrobe="cafe",
            light="bright store lighting",
            window="the storefront glass",
            night_view="streetlights",
            commander=_CASUAL,
            actions=("holding shopping bags", "browsing a shelf"),
        ),
        _place(
            "cinema",
            "cinema",
            True,
            "cinema, movie theater, movie theatre, the theater, the theatre, theater, "
            "theatre, the movies",
            "a dim movie theater, rows of red seats, glow of the screen",
            specificity=2,
            wardrobe="date",
            light="glow of the movie screen",
            window="the lobby windows",
            night_view="streetlights",
            commander=_CASUAL,
            actions=("sitting in the theater seats, holding popcorn",),
        ),
        _place(
            "library",
            "library",
            True,
            "library, the library",
            "a quiet library, tall bookshelves, reading tables",
            specificity=2,
            wardrobe="reading",
            light="warm reading lamps",
            commander=_CASUAL,
            actions=("reading a book", "pulling a book from the shelf"),
        ),
        _place(
            "hotel",
            "hotel room",
            True,
            "hotel, hotel room, motel",
            "a hotel room, neatly made bed, armchair, large window",
            specificity=2,
            commander=_CASUAL,
        ),
        _place(
            "transit",
            "train or plane",
            True,
            "on the train, the train, train station, subway, on the bus, the bus, "
            "on the plane, the plane, airport, flight",
            "a passenger cabin, rows of seats, window seat",
            specificity=2,
            light="soft cabin lighting",
            window="the cabin window",
            commander=_CASUAL,
            actions=("sitting by the window", "resting her head against the window"),
        ),
        _place(
            "hospital",
            "hospital",
            True,
            "hospital, the clinic, clinic, infirmary, medbay, med bay",
            "a quiet hospital room, white sheets, monitors, visitor chair",
            specificity=2,
            light="soft fluorescent light",
            commander=_CASUAL,
        ),
        _place(
            "park",
            "park",
            False,
            "the park, a park, park bench, playground, picnic",
            "a green park, trees, lawn, a bench, winding path",
            commander=_CASUAL,
            actions=("sitting on a park bench", "walking, mid-stride"),
        ),
        _place(
            "beach",
            "beach",
            False,
            "beach, beaches, seaside, seashore, shore, shoreline, coast, boardwalk, "
            "the ocean, ocean, the sea, sea",
            "a sandy beach, ocean waves, wide open shore",
            wardrobe="beach",
            commander=COMMANDER_OUTFIT_MAP["beach"],
        ),
        _place(
            "pool",
            "pool",
            False,
            "pool, poolside, swimming pool",
            "a swimming pool, clear blue water, poolside tiles, lounge chairs",
            specificity=2,
            wardrobe="swim",
            commander=COMMANDER_OUTFIT_MAP["beach"],
        ),
        _place(
            "city",
            "city street",
            False,
            "city, the city, downtown, the street, the streets, city street, street, "
            "town, city center, city centre, main street, sidewalk, crosswalk, chicago",
            "a city street, shops with plain awnings, sidewalk, passing traffic",
            commander=_CASUAL,
            actions=("walking, mid-stride", "hands in pockets, waiting at a crosswalk"),
        ),
        _place(
            "rooftop",
            "rooftop",
            False,
            "rooftop, the roof, roof",
            "a city rooftop, railing, water tanks, skyline view",
            specificity=2,
            commander=_CASUAL,
            actions=("leaning on the railing", "wind, floating hair"),
        ),
        _place(
            "garden",
            "garden",
            False,
            "garden, backyard, back yard, the yard, front yard, flower garden",
            "a garden, flowers, green hedges",
            commander=_CASUAL,
            actions=("smelling a flower", "walking, mid-stride"),
        ),
        _place(
            "balcony",
            "balcony",
            False,
            "balcony, terrace, porch, patio, back deck, my deck, veranda",
            "a balcony, railing, potted plants, view over the rooftops",
            specificity=2,
            commander=_HOME,
            actions=("leaning on the railing", "holding a mug at the railing"),
        ),
        _place(
            "forest",
            "forest",
            False,
            "forest, the woods, woods, trail, hiking, hike, campsite, camping, campfire",
            "a forest trail, tall trees, ferns, dappled light",
            commander=_CASUAL,
            actions=("walking along the trail", "resting against a tree trunk"),
        ),
        _place(
            "mountains",
            "mountains",
            False,
            "mountain, mountains, the hills, hills, hilltop, summit, lookout, cliff, "
            "overlook, scenic overlook",
            "a mountain overlook, rocky path, valley far below",
            commander=_CASUAL,
            actions=("shading eyes, looking at the view", "wind, floating hair"),
        ),
        _place(
            "lakeside",
            "lakeside",
            False,
            "lake, lakeside, river, riverside, riverbank, pond, creek",
            "a quiet lakeside, still water, reeds, distant treeline",
            commander=_CASUAL,
            actions=("skipping a stone", "sitting on a large rock"),
        ),
        _place(
            "road",
            "open road",
            False,
            "motorcycle, motorbike, on the bike, on your bike, on my bike, on her bike, "
            "bike ride, the highway, highway, the road, road trip, on the road, riding, "
            "ride, night ride, joyride",
            "an open highway, motorcycle, asphalt stretching to the horizon",
            wardrobe="ride",
            commander=COMMANDER_OUTFIT_MAP["motorcycle"],
        ),
        _place(
            "battlefield",
            "battlefield",
            False,
            "battlefield, battle, combat zone, front line, front lines, frontline, "
            "warzone, war zone, firefight, on mission, on a mission, deployed",
            "a ruined battlefield in a contaminated zone, rubble, drifting smoke",
            commander=COMMANDER_OUTFIT_MAP["battle"],
        ),
        _place(
            "wasteland",
            "wasteland",
            False,
            "outskirts, wasteland, the wastes, yellow zone, red zone, contaminated zone",
            "a post-apocalyptic wasteland, cracked earth, ruined structures on the horizon",
        ),
        _place(
            "outside",
            "outdoors",
            False,
            "outside, outdoors, out here, out there, fresh air, in the open, "
            "under the stars, under the sky, under the moon, in the rain, out in the rain, "
            "in the snow, in the sun, in the sunshine, meteor shower, stargazing",
            "outdoors, open air, trees and a quiet street",
            specificity=0,
        ),
        _place(
            "inside",
            "indoors",
            True,
            "inside, indoors, in here, stay in, staying in, stayed in",
            "indoors, a cozy interior",
            specificity=0,
        ),
        # ── The Elmo: the Commander's mobile base, a LAND vessel ────────────
        _place(
            "elmo",
            "the Elmo",
            True,
            "elmo, aboard, on board, onboard, mobile base, the base, mbv",
            f"{ELMO_INTERIOR}, steel corridors",
            specificity=0,
            **_ELMO_INSIDE,
        ),
        _place(
            "elmo_topside",
            "the Elmo's armored roof",
            False,
            "elmo deck, elmo's deck, deck of the elmo, top deck, upper deck, outer deck, "
            "observation deck, on deck, the deck, topside, elmo's roof, on the roof of the elmo, "
            "roof of the elmo, on top of the elmo, top of the elmo, elmo's hull, hull of the elmo",
            f"on the armored roof of {ELMO}, steel walkway with railings, "
            "antennas, dusty plains stretching to the horizon",
            specificity=2,
            elmo=True,
            actions=(
                "leaning on the railing, looking at the horizon",
                "wind, floating hair",
                "stretching, warming up",
            ),
        ),
        _place(
            "command_center",
            "the Elmo's command center",
            True,
            "command deck, command center, command centre, command room, control room, "
            "command bridge, nav console, tactical room, ops room, operations room, war room",
            f"a command center, {ELMO_INTERIOR}, banks of monitors glowing with abstract maps, "
            "holographic map table",
            specificity=2,
            **{**_ELMO_INSIDE, "light": "glow of tactical monitors"},
            actions=(
                "leaning over the map table",
                "arms crossed, watching the monitors",
                "holding a tablet, reading reports",
            ),
        ),
        _place(
            "hangar",
            "the Elmo's hangar",
            True,
            "hangar, hangar bay, vehicle bay, motor pool, repair bay",
            f"the vehicle hangar, {ELMO_INTERIOR}, a motorcycle on a work stand, tool racks, "
            "oil-stained steel floor",
            specificity=2,
            wardrobe="garage",
            **{**_ELMO_INSIDE, "light": "harsh overhead work lights"},
        ),
        _place(
            "firing_range",
            "the Elmo's firing range",
            True,
            "firing range, shooting range, the range, gun range, target practice",
            f"an enclosed indoor firing range, {ELMO_INTERIOR}, steel lane dividers, "
            "plain round paper targets, ear protection on hooks",
            specificity=2,
            wardrobe="training",
            **{**_ELMO_INSIDE, "light": "bright range lights"},
            actions=(
                "aiming a rifle down the lane, ear protection on",
                "loading a magazine at the lane bench",
            ),
        ),
        _place(
            "training_room",
            "the Elmo's training room",
            True,
            "training room, sparring mat, dojo, drill hall",
            f"the training room, {ELMO_INTERIOR}, padded mats, weight racks, punching bags",
            specificity=2,
            wardrobe="training",
            **{**_ELMO_INSIDE, "light": "bright overhead lights"},
        ),
        _place(
            "armory",
            "the Elmo's armory",
            True,
            "armory, armoury, weapons locker, gun rack, weapons bay",
            f"the armory, {ELMO_INTERIOR}, racks of rifles, ammunition crates, "
            "workbench with a cleaning kit",
            specificity=2,
            **_ELMO_INSIDE,
            actions=("cleaning a rifle at the workbench", "inspecting a rifle"),
        ),
        _place(
            "rec_room",
            "the Elmo's rec room",
            True,
            "rec room, recreation room, game room, gaming room, arcade, pool table",
            f"the rec room, {ELMO_INTERIOR}, worn couch, big screen glowing with abstract colour, "
            "scattered controllers",
            specificity=2,
            **{**_ELMO_INSIDE, "light": "glow of the big screen"},
            actions=(
                "holding a game controller",
                "sitting on the couch arm, watching the screen",
            ),
        ),
        _place(
            "logistics_bay",
            "the Elmo's logistics bay",
            True,
            "logistics bay, supply bay, cargo bay, storage bay, supply room, storeroom, "
            "quartermaster",
            f"the logistics bay, {ELMO_INTERIOR}, stacked supply crates, shelving, "
            "a dark wall terminal",
            specificity=2,
            **_ELMO_INSIDE,
            actions=("checking a clipboard", "leaning against supply crates"),
        ),
        _place(
            "mess_hall",
            "the Elmo's mess hall",
            True,
            "mess hall, the mess, canteen, cafeteria, dining hall",
            f"the mess hall, {ELMO_INTERIOR}, long metal tables, food trays, serving counter",
            specificity=2,
            **_ELMO_INSIDE,
            actions=("holding a food tray", "sitting at a long table"),
        ),
        _place(
            "tea_counter",
            "the Tea Time counter",
            True,
            "tea counter, tea time counter, tea time, behind the counter, racing calm",
            f"a tea counter in a lounge, {ELMO_INTERIOR}, teapots and cups, "
            "warm pendant lights",
            specificity=2,
            wardrobe="tea",
            **_ELMO_INSIDE,
        ),
        _place(
            "elmo_lounge",
            "the Elmo's lounge",
            True,
            "lounge, the lounge, common room, break room",
            f"the lounge, {ELMO_INTERIOR}, couches, low coffee table, warm pendant lights",
            specificity=2,
            **_ELMO_INSIDE,
            actions=("sitting on the couch, legs crossed", "pretending to read a book"),
        ),
        _place(
            "her_quarters",
            "her quarters",
            True,
            "your quarters, your room, your bedroom, your bed, your bunk, your place, "
            "her quarters, her room, quarters, bunk",
            f"an officer's private quarters, {ELMO_INTERIOR}, neatly made bunk, small desk with a lamp, "
            "sketchbook",
            specificity=2,
            **{**_ELMO_INSIDE, "light": "dim desk lamp"},
            actions=("sitting at her desk, sketching", "leaning against the doorframe"),
        ),
        _place(
            "perimeter",
            "the Elmo's perimeter",
            False,
            "perimeter, the perimeter, patrol route",
            f"the perimeter around {ELMO}, parked on dusty ground, floodlights",
            elmo=True,
        ),
        # ── Neutral settings (only ever a default) ──────────────────────────
        _place(
            "neutral_room",
            "a cozy room",
            True,
            "",
            "a cozy room, soft furnishings, shelves of books, a large window",
            specificity=0,
        ),
        _place(
            "window_seat",
            "a window seat",
            True,
            "",
            "a window seat with cushions, potted plants",
            specificity=0,
        ),
    )
}

# Neutral settings when nothing places the picture, with the hours they suit.
DEFAULTS: tuple[tuple[str, frozenset[str]], ...] = (
    ("neutral_room", _ALL),
    ("window_seat", frozenset({"morning", "afternoon"})),
    ("elmo_lounge", _ALL),
    ("park", _DAYLIGHT),
    ("city", _ALL),
    ("lakeside", _DAYLIGHT),
    ("rooftop", _LATE),
    ("elmo_topside", _DAYLIGHT),
    ("balcony", _LATE),
    ("cafe", _DAYLIGHT),
)

# Her Day roster locations (config/personality.yaml her_day) → places.
_HER_DAY: dict[str, str] = {
    "elmo deck": "elmo_topside",
    "command deck": "command_center",
    "firing range": "firing_range",
    "armory": "armory",
    "hangar": "hangar",
    "mess hall": "mess_hall",
    "lounge": "elmo_lounge",
    "training room": "training_room",
    "logistics bay": "logistics_bay",
    "perimeter": "perimeter",
    "highway": "road",
    "quarters": "her_quarters",
    "rec room": "rec_room",
    "deployed": "battlefield",
    "outskirts": "wasteland",
    "off duty": "elmo",  # her private life, below the bond that shows it
    "the elmo": "elmo",
}

# Possessives flip when SHE speaks: her "my room" is her quarters.
_HER_PERSPECTIVE: dict[str, str] = {
    "my room": "her_quarters",
    "my bedroom": "her_quarters",
    "my bed": "her_quarters",
    "my place": "her_quarters",
    "your room": "his_bedroom",
    "your bedroom": "his_bedroom",
    "your bed": "his_bedroom",
    "your place": "his_home",
    "home": "elmo",
    "at home": "elmo",
    "back home": "elmo",
    "i'm home": "elmo",
    "im home": "elmo",
}

_GENERIC = frozenset({"inside", "outside"})


def _phrase_table() -> dict[str, str]:
    table: dict[str, str] = {}
    for p in PLACES.values():
        for phrase in p.phrases:
            if table.setdefault(phrase, p.key) != p.key:
                raise ValueError(f"phrase {phrase!r} names two places")
    return table


_PHRASES = _phrase_table()
_PHRASE_RE = re.compile(
    r"(?<![a-z0-9'])(?:"
    + "|".join(re.escape(p) for p in sorted(_PHRASES, key=len, reverse=True))
    + r")(?![a-z0-9])"
)

# ── Clause rules ─────────────────────────────────────────────────────────────

_CLAUSE_BREAK = re.compile(
    r"[.!?;,:\n()\[\]\"]+|\s[-–—]+\s|—|\b(?:but|then|now|so|because|though|although|anyway|except)\b"
)
_SENTENCE_END = re.compile(r"[.!?\n]")
_TOKEN = re.compile(r"[a-z0-9']+")

_NEGATORS = frozenset(
    {
        "not", "no", "never", "nowhere", "without", "nor", "neither",
        "cant", "dont", "wont", "isnt", "arent", "wasnt", "werent", "didnt", "aint",
    }
)  # fmt: skip
_NEGATOR_PHRASES = (
    "no longer",
    "instead of",
    "rather than",
    "anywhere but",
    "as if",
    "yeah right",
    "done with",
    "sick of",
    "tired of",
)
_NOT_A_NEGATION = re.compile(r"\b(?:can't|cant|can not|cannot) wait\b")
# Leaving a place: counted in his earlier turns ("we left the beach").
_LEAVING = frozenset(
    {"left", "leave", "leaving", "leaves", "exit", "exited", "exiting"}
)
_DESTINATION = frozenset({"for", "to"})
# "Got back from the beach" left it; "the sunset from the rooftop" is there.
_RETURNING = frozenset(
    {
        "back",
        "home",
        "returned",
        "returning",
        "came",
        "coming",
        "got",
        "drove",
        "walked",
    }
)
_AWAY = re.compile(r"\b(?:away|far) from\b")
_OUT_OF = re.compile(r"\b(?:out of|got out|get out|getting out)\b")
_STALE_AFTER = re.compile(r"^\W*(?:[a-z']+\W+){0,3}?(?:anymore|any more|no more)\b")

# Not where he is now: wishes, memories, plans for later.
_HYPOTHETICAL = re.compile(
    r"\b(?:wish|wished|someday|some day|one day|yesterday|tomorrow|later|sometime|"
    r"remember|remembered|dream|dreamed|dreamt|last night|last time|last week|"
    r"last year|next time|next week|would love|i'd love|would be nice|used to|"
    r"if we|if i|if you|back when|the other day|earlier)\b"
)
# "We were at the beach" is over; a bare "were in my room" is a typed "we're".
_PAST = frozenset({"was", "been", "had"})
_PAST_SUBJECT = frozenset({"we", "you", "they", "i"})
_PROPOSAL = re.compile(
    r"\b(?:let's|lets|shall we|should we|want to|wanna|how about|what about|"
    r"why don't we|why dont we|can we|could we|come)\b"
)
_THIRD_PERSON = frozenset(
    {"he", "she", "they", "him", "them", "his", "their", "her"} | set(SQUAD_KEYWORDS)
)
_FIRST_SECOND = frozenset(
    {
        "i", "i'm", "im", "i'll", "we", "we're", "we'll", "us", "let's", "lets", "me",
        "my", "our", "you", "you're", "your", "here",
    }
)  # fmt: skip
# A place he is at or going to: a preposition, a being or a moving verb.
_CUES = frozenset(
    {
        "in", "at", "on", "into", "onto", "to", "inside", "by", "near", "around",
        "under", "through", "across", "toward", "towards", "over", "here", "there",
        "back", "i'm", "im", "we're", "am", "are", "you're", "go", "going", "goes",
        "went", "gone", "come", "coming", "came", "head", "heading", "headed", "get",
        "getting", "got", "arrive", "arrived", "arriving", "stay", "staying", "stayed",
        "visit", "visiting", "hit", "walk", "walking", "drive", "driving", "sit",
        "sitting", "sat", "chill", "chilling", "hang", "hanging", "relax", "relaxing",
        "lounging", "welcome", "join", "meet", "step", "stepping", "stepped", "from",
    }
)  # fmt: skip
_BARE_START = frozenset(
    {"in", "at", "on", "into", "to", "under", "out", "back", "inside", "outside",
     "outdoors", "indoors", "aboard", "onboard", "come", "stay", "staying", "stayed",
     "i'm", "im", "we're", "downtown", "topside"}
)  # fmt: skip
# "It's raining outside" is weather, not where he is.
_WEATHERISH = frozenset(
    {
        "raining", "rain", "snowing", "snow", "cold", "hot", "warm", "dark", "bright",
        "sunny", "nice", "windy", "stormy", "storming", "freezing", "pouring", "wet",
        "look", "looking", "see", "window", "lovely", "beautiful", "gross", "humid",
    }
)  # fmt: skip
_HER_CUES = frozenset({"i'm", "im", "i", "here", "down", "up", "still", "we're"})


def _normalize(text: str) -> str:
    return text.lower().replace("’", "'").replace("‘", "'").replace("é", "e")


def _tokens(text: str) -> list[str]:
    return _TOKEN.findall(text)


@dataclass(frozen=True)
class _Mention:
    place: Place
    start: int
    clause: int
    negated: bool
    current: bool  # where he is or is going now (always True in a request)


def _negated(before: str, after: str, *, history: bool) -> bool:
    tail_tokens = _tokens(_NOT_A_NEGATION.sub(" ", before))[-6:]
    tail = " ".join(tail_tokens)
    general = any(t in _NEGATORS or t.endswith("n't") for t in tail_tokens) or any(
        p in tail for p in _NEGATOR_PHRASES
    )
    away = bool(_AWAY.search(tail))
    leaving = False
    if history:
        for i, t in enumerate(tail_tokens):
            following = tail_tokens[i + 1] if i + 1 < len(tail_tokens) else ""
            if t in _LEAVING and following not in _DESTINATION:
                leaving = True
            elif t == "from" and _RETURNING & set(tail_tokens[max(0, i - 2) : i]):
                leaving = True
        leaving = leaving or bool(_OUT_OF.search(tail))
    if general or away or leaving:
        # "I'm not leaving my room" stays in the room.
        return general != (away or leaving)
    return bool(_STALE_AFTER.match(after))


def _clause_bounds(
    breaks: list[tuple[int, int]], start: int, end: int, size: int
) -> tuple[int, int, int]:
    index, lo, hi = 0, 0, size
    for b_start, b_end in breaks:
        if b_end <= start:
            index, lo = index + 1, b_end
        elif b_start >= end:
            hi = b_start
            break
    return index, lo, hi


def _is_current(
    lower: str,
    start: int,
    end: int,
    before: str,
    after: str,
    phrase: str,
    place: Place,
    mode: str,
) -> bool:
    """His earlier turn (or her reply) says this is where they are now."""
    sentence_start = max(
        (m.end() for m in _SENTENCE_END.finditer(lower, 0, start)), default=0
    )
    stop = _SENTENCE_END.search(lower, end)
    sentence = lower[sentence_start : stop.end() if stop else len(lower)]
    if _HYPOTHETICAL.search(f"{before} {phrase} {after}"):
        return False
    if stop is not None and stop.group(0) == "?" and not _PROPOSAL.search(sentence):
        return False
    lead = _tokens(lower[sentence_start:start])
    if any(t in _THIRD_PERSON for t in lead) and not any(
        t in _FIRST_SECOND for t in lead
    ):
        return False
    lead4 = _tokens(before)[-4:]
    if (
        any(t in _PAST for t in lead4)
        or any(
            t == "were" and i and lead4[i - 1] in _PAST_SUBJECT
            for i, t in enumerate(lead4)
        )
        or set(_tokens(after)[:2]) & {"was", "were"}  # "the highway was awful"
    ):
        return False
    near = lead4[-3:]
    if place.key in _GENERIC:
        after_tokens = _tokens(after)[:2]
        if any(t in _WEATHERISH for t in near[-2:] + after_tokens):
            return False
    if mode == "her":
        return any(t in _HER_CUES for t in _tokens(before)[-5:])
    if place.key in _GENERIC:
        return any(t in _CUES for t in near[-2:]) or phrase.startswith(
            ("in here", "out here", "stay")
        )
    return phrase.split()[0] in _BARE_START or any(t in _CUES for t in near)


def _mentions(text: str, mode: str) -> list[_Mention]:
    """Every place named in ``text``; mode is request, history or her."""
    lower = _normalize(text)
    breaks = [(m.start(), m.end()) for m in _CLAUSE_BREAK.finditer(lower)]
    found: list[_Mention] = []
    for m in _PHRASE_RE.finditer(lower):
        phrase = m.group(0)
        key = (
            _HER_PERSPECTIVE.get(phrase, _PHRASES[phrase])
            if mode == "her"
            else _PHRASES[phrase]
        )
        place = PLACES[key]
        clause, lo, hi = _clause_bounds(breaks, m.start(), m.end(), len(lower))
        before, after = lower[lo : m.start()], lower[m.end() : hi]
        negated = _negated(before, after, history=mode != REQUEST)
        if mode == REQUEST:
            current = not (
                place.key in _GENERIC
                and any(
                    t in _WEATHERISH for t in _tokens(before)[-2:] + _tokens(after)[:2]
                )
            )
        else:
            current = place.history and _is_current(
                lower, m.start(), m.end(), before, after, phrase, place, mode
            )
        found.append(_Mention(place, m.start(), clause, negated, current))
    return found


def _choose(
    mentions: Iterable[_Mention], vetoed: set[str], need: bool | None
) -> _Mention | None:
    """The latest clause's most specific place that is still allowed."""
    ordered = sorted(
        (m for m in mentions if m.current and not m.negated),
        key=lambda m: (m.clause, m.place.specificity, m.start),
        reverse=True,
    )
    for m in ordered:
        if m.place.key in vetoed or (need is not None and m.place.indoor != need):
            continue
        return m
    return None


def infer_place(text: str | None) -> Place | None:
    """The place a picture's own tags name, if any: a tag list leads with its
    setting, so the most specific, earliest place wins."""
    if not text:
        return None
    named = [m for m in _mentions(text, REQUEST) if m.current and not m.negated]
    if not named:
        return None
    return min(named, key=lambda m: (-m.place.specificity, m.start)).place


def her_day_place(location: str, activity: str = "") -> Place:
    """Her roster block's place; the Elmo when the block names nothing known."""
    loc = " ".join(_normalize(location).split())
    if loc == "lounge" and "tea time" in _normalize(activity):
        return PLACES["tea_counter"]
    key = _HER_DAY.get(loc)
    if key is None:
        found = infer_place(loc)
        key = found.key if found else "elmo"
    return PLACES[key]


# ── Time and weather ────────────────────────────────────────────────────────

_TIME_WORDS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("morning", re.compile(r"\b(?:morning|dawn|sunrise|breakfast|daybreak)\b")),
    ("afternoon", re.compile(r"\b(?:afternoon|midday|noon|lunch|daytime)\b")),
    ("evening", re.compile(r"\b(?:evening|sunset|dusk|twilight|golden hour|dinner)\b")),
    (
        "night",
        re.compile(
            r"\b(?:night|tonight|midnight|nighttime|moonlight|moonlit|starry|stars)\b"
        ),
    ),
)
_WEATHER_WORDS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "rain",
        re.compile(
            r"\b(?:rain|raining|rainy|drizzle|drizzling|storm|stormy|thunderstorm|downpour|pouring)\b"
        ),
    ),
    ("snow", re.compile(r"\b(?:snow|snowing|snowy|snowfall|blizzard|snowflakes)\b")),
)


def _time_from(text: str) -> str | None:
    lower = _normalize(text)
    best: tuple[int, str] | None = None
    for name, pattern in _TIME_WORDS:
        for m in pattern.finditer(lower):
            if best is None or m.start() > best[0]:
                best = (m.start(), name)
    return best[1] if best else None


def _weather_from(text: str, *, history: bool) -> str | None:
    lower = _normalize(text)
    if history and _HYPOTHETICAL.search(lower):
        return None
    best: tuple[int, str] | None = None
    for name, pattern in _WEATHER_WORDS:
        for m in pattern.finditer(lower):
            lead = _tokens(lower[max(0, m.start() - 30) : m.start()])[-3:]
            if any(t in _NEGATORS or t.endswith("n't") for t in lead):
                continue
            if best is None or m.start() > best[0]:
                best = (m.start(), name)
    return best[1] if best else None


_OUTDOOR_TIME = {
    "morning": "morning, soft sunrise light, clear sky",
    "afternoon": "afternoon, bright daylight, blue sky",
    "evening": "evening, golden hour, sunset sky",
    "night": "night, moonlight, night sky",
}
_OUTDOOR_TIME_GREY = {
    "morning": "morning, soft grey light",
    "afternoon": "afternoon, overcast daylight",
    "evening": "evening, grey dusk",
    "night": "night, glowing streetlights",
}


def time_tags(
    place: Place, time: str, *, indoor: bool, weather: str | None = None
) -> str:
    """Light for the hour; indoors it never shows sky, moon or stars."""
    if not indoor:
        return (_OUTDOOR_TIME_GREY if weather else _OUTDOOR_TIME)[time]
    w = place.window
    if w is None:
        return f"{time}, {place.light}"
    return {
        "morning": f"morning, soft daylight through {w}",
        "afternoon": f"afternoon, daylight through {w}",
        "evening": f"evening, {place.light}, dusk glow through {w}",
        "night": f"night, {place.light}, {place.night_view} through {w}",
    }[time]


def weather_tags(place: Place, weather: str | None, *, indoor: bool) -> str:
    """Weather seen from where she is: on the window indoors, on her outdoors."""
    if weather is None or (indoor and place.window is None):
        return ""
    if indoor:
        return (
            f"rain on {place.window}"
            if weather == "rain"
            else f"snow falling beyond {place.window}"
        )
    return {
        "rain": "rain, wet ground, umbrella, overcast",
        "snow": "snow, falling snowflakes, cold breath",
    }[weather]


# ── The brief ────────────────────────────────────────────────────────────────

# Scene dressing that only makes sense outdoors, or only indoors.
_OUTDOOR_ONLY_SCENES = frozenset(
    {"rain", "snow", "beach", "swim", "stargazing", "fireworks", "lanterns", "ride"}
)
_INDOOR_ONLY_SCENES = frozenset({"bedtime", "tea", "cat_cafe"})
_BED_PLACES = frozenset({"his_bedroom", "bedroom", "her_quarters"})


def scene_fits(scene: str | None, indoor: bool) -> bool:
    """Whether app/wardrobe_scene.py's ``scene`` can happen in this setting."""
    if scene is None:
        return True
    return scene not in (_OUTDOOR_ONLY_SCENES if indoor else _INDOOR_ONLY_SCENES)


@dataclass(frozen=True)
class SceneBrief:
    """Where and when one picture happens, and why."""

    place: Place
    source: str
    time: str
    time_source: str  # "words" or "clock"
    weather: str | None = None
    about_her: bool = False

    @property
    def key(self) -> str:
        return self.place.key

    @property
    def indoor(self) -> bool:
        return self.place.indoor

    @property
    def commander(self) -> str | None:
        return self.place.commander

    def tags(self) -> str:
        parts = [
            self.place.tags,
            time_tags(self.place, self.time, indoor=self.indoor, weather=self.weather),
            weather_tags(self.place, self.weather, indoor=self.indoor),
        ]
        return ", ".join(p for p in parts if p)

    def sentence(self) -> str:
        """The setting line a cloud renderer reads before the tag list."""
        where = "Indoors" if self.indoor else "Outdoors"
        light = time_tags(
            self.place, self.time, indoor=self.indoor, weather=self.weather
        )
        weather = weather_tags(self.place, self.weather, indoor=self.indoor)
        text = f"Setting: {where} - {self.place.tags}; {light}"
        text += f"; {weather}." if weather else "."
        if self.indoor and self.place.window is None:
            text += (
                " The whole picture stays indoors: solid walls and a low ceiling "
                "fill the background."
            )
        elif self.indoor:
            text += " The whole picture stays indoors, with the room's walls in view."
        if self.place.elmo and not self.indoor:
            text += (
                " The vehicle is a colossal armored mobile base on tank treads that "
                "drives over dry land."
            )
        return text

    def avoid(self) -> str:
        # Keep a constraint only for a specific object: "A long avoid list is easy to
        # ignore, and it spends the prompt on objects that then show up anyway" (xAI).
        return ELMO_AVOID if self.place.elmo and not self.indoor else ""

    def wardrobe_scene(self, request: str | None) -> str | None:
        """The scene she dresses for: his request's, when it fits the setting,
        else the place's own (only for a place he chose)."""
        from .wardrobe_scene import scene_of

        asked = scene_of(request)
        if asked is not None and scene_fits(asked, self.indoor):
            return asked
        if self.source in (REQUEST, HIS_HISTORY) and scene_fits(
            self.place.wardrobe, self.indoor
        ):
            return self.place.wardrobe
        return None

    def request_scope(self) -> str | None:
        """What a repeat of the same sentence must also match to be the same
        picture: the place, when it came from outside his sentence."""
        return self.key if self.source in (HIS_HISTORY, HER_REPLY, HER_DAY) else None

    def log_fields(self) -> str:
        """Place words only — never message text."""
        return (
            f"place={self.key} ({self.place.label}) source={self.source} "
            f"setting={'indoor' if self.indoor else 'outdoor'} "
            f"time={self.time}({self.time_source}) weather={self.weather or 'clear'}"
        )


def _turns(history: Iterable[Mapping[str, object]] | None) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    for turn in history or ():
        role, content = turn.get("role"), turn.get("content")
        if role in ("user", "assistant") and isinstance(content, str):
            out.append((str(role), content))
    return out


def turns_from_context(chat_context: str | None) -> list[dict[str, str]]:
    """Recover ``role: content`` lines (chat_followups' chat_context)."""
    turns: list[dict[str, str]] = []
    for line in (chat_context or "").splitlines():
        m = re.match(r"\s*(user|assistant)\s*:\s?(.*)", line, re.IGNORECASE)
        if m:
            turns.append({"role": m.group(1).lower(), "content": m.group(2)})
        elif turns:
            turns[-1]["content"] += "\n" + line
    return turns


def resolve_scene(
    request: str,
    history: Iterable[Mapping[str, object]] | None = None,
    *,
    hour: int,
    couple: bool = False,
    her_place: tuple[str, str] | None = None,
    seed: str | None = None,
) -> SceneBrief:
    """The brief for one picture. ``history`` is the recent turns, oldest
    first (it may end with this request and her reply to it); ``her_place``
    is her Her Day (location, activity), when known."""
    request = request or ""
    turns = _turns(history)
    his = [text for role, text in turns if role == "user"]
    if his and " ".join(his[-1].split()) == " ".join(request.split()):
        his = his[:-1]
    his = his[-HIS_TURNS:]
    her_last = next(
        (text for role, text in reversed(turns) if role == "assistant"), None
    )

    vetoed: set[str] = set()
    need: bool | None = None  # True: must be indoors; False: outdoors
    fallback: tuple[Place, str] | None = None

    def consider(text: str, mode: str, source: str) -> tuple[Place, str] | None:
        nonlocal need, fallback
        mentions = _mentions(text, mode)
        pick = _choose(mentions, vetoed, need)
        chosen: tuple[Place, str] | None = None
        if pick is not None and pick.place.key in _GENERIC:
            # "Let's stay inside" keeps an older room; "go outside" needs open air.
            fallback = fallback or (pick.place, source)
            need = pick.place.indoor if need is None else need
        elif pick is not None:
            chosen = (pick.place, source)
        vetoed.update(m.place.key for m in mentions if m.negated)
        if need is None:
            if any(m.negated and m.place.key == "outside" for m in mentions):
                need = True
            elif any(m.negated and m.place.key == "inside" for m in mentions):
                need = False
        return chosen

    found = consider(request, REQUEST, REQUEST)
    for text in reversed(his):
        if found:
            break
        found = consider(text, "history", HIS_HISTORY)
    about_her = not couple
    if found is None and about_her:
        if her_last:
            found = consider(her_last, "her", HER_REPLY)
        if found is None and her_place is not None:
            place = her_day_place(*her_place)
            if place.key not in vetoed and (need is None or place.indoor == need):
                found = (place, HER_DAY)
    if found is None and fallback is not None:
        found = fallback

    words = _time_from(request)
    time, time_source = (words, "words") if words else (lighting_period(hour), "clock")
    weather = _weather_from(request, history=False)
    for text in reversed(his):
        if weather is not None:
            break
        weather = _weather_from(text, history=True)

    if found is None:
        if seed is None:
            seed = derive_seed(request, *(text for _, text in turns), hour)
        pool = [
            k
            for k, hours in DEFAULTS
            if time in hours and (need is None or PLACES[k].indoor == need)
        ] or [k for k, _ in DEFAULTS if need is None or PLACES[k].indoor == need]
        found = (PLACES[variation_rng(seed, "place").choice(pool)], DEFAULT)

    place, source = found
    return SceneBrief(place, source, time, time_source, weather, about_her)


# ── His request's own moods and actions ─────────────────────────────────────

# The brief owns place, time and weather: these never come from keywords.
_SETTING_TOKENS = frozenset(
    {
        "bedroom", "kitchen", "restaurant", "training ground", "onsen", "hot spring",
        "outdoors", "indoor", "battlefield", "road", "night", "night sky", "dark sky",
        "stars", "moonlight", "morning light", "sunrise", "sunset", "orange sky",
        "golden hour lighting", "rain", "umbrella", "wet", "overcast", "shelter",
        "snow", "winter", "cold breath",
    }
)  # fmt: skip
_BED_TOKENS = frozenset({"bed", "lying on bed", "pillows", "blankets", "waking up"})
_OUTDOOR_TOKENS = frozenset(
    {
        "wind", "floating hair", "speed lines", "speed", "city skyline",
        "sunlight through leaves", "natural lighting", "riding", "riding together",
        "motorcycle", "explosions", "debris", "smoke",
    }
)  # fmt: skip


def _keyword_pattern(keyword: str) -> re.Pattern[str]:
    """The keyword as a whole word with its usual endings: "smiling" and
    "hugging" count; "bedroom", "ready", "cookie" and "great" do not."""
    if keyword == "sleep":
        return re.compile(r"\ba?sleep(?:s|ing|y)?\b")
    if keyword.endswith("e"):
        body = re.escape(keyword[:-1]) + "(?:e|es|ed|ing)"
    else:
        last = re.escape(keyword[-1])
        body = re.escape(keyword) + f"(?:s|es|ed|ing|{last}ing|{last}ed)?"
    return re.compile(rf"\b{body}\b")


# Situations that are only an hour or a weather ("night" → "night sky, stars,
# moonlight, quiet, intimate"): the brief owns those, whole entry and all.
_TIME_SITUATIONS = frozenset({"night", "morning", "rain"})
_REQUEST_KEYWORDS: tuple[tuple[re.Pattern[str], str], ...] = tuple(
    (_keyword_pattern(keyword), tags)
    for table in (IMAGE_MOOD_MAP, SITUATION_KEYWORDS)
    for keyword, tags in table.items()
    if table is IMAGE_MOOD_MAP or keyword not in _TIME_SITUATIONS
)


def request_tags(request: str, brief: SceneBrief, *, couple: bool = False) -> str:
    """Moods and actions in HIS request only, made to fit the brief's setting.

    Whole words only ("great" is not "eat", "update" is not a date, "ready"
    is not "read"), and nothing from older turns or her replies is read.
    """
    lower = _normalize(request or "")
    found = [tags for pattern, tags in _REQUEST_KEYWORDS if pattern.search(lower)]
    for member, member_tags in SQUAD_KEYWORDS.items():
        if re.search(rf"\b{re.escape(member)}\b", lower):
            found.append(member_tags)
            if not couple:
                found.append("multiple girls")
    kept: list[str] = []
    for tag in (t.strip() for chunk in found for t in chunk.split(",")):
        if not tag or tag in kept or tag in _SETTING_TOKENS:
            continue
        if tag in _BED_TOKENS and brief.key not in _BED_PLACES:
            continue
        if brief.indoor and tag in _OUTDOOR_TOKENS:
            continue
        kept.append(tag)
    return ", ".join(kept)
