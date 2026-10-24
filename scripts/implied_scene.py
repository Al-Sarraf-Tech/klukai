"""Implied intimacy for fal (owner, 2026-10-24: "make it implied nudity ... make sure it doesn't
trip the flag").

Seedream on fal refuses anything its classifier reads as sexual (content_policy_violation,
category "sexual"); this account may not lift that. The core still writes scenes as diffusion
tags, joined with "; " on one "Scene and appearance:" line, and at a high bond those carry body
and arousal tags that trip the filter even in fully clothed scenes (a zipped onesie kiss was
refused twice on 2026-10-24). So for fal the scene line is rewritten:

- implied(): body and arousal tags go, undress tags become a quilt, blanket or shirt that covers,
  "intimate" becomes "tender". Romance itself (a kiss, an embrace, a shared bed) stays.
- plain(): the redraw after a refusal. Also drops touch, bed and close-up cues and dresses
  both of them, so the same moment comes back fully clothed.

Only the scene line changes; any other line (instructions, a checker's fix hint) is kept.
Neither function adds a word the filter keys on.
"""

from __future__ import annotations

SCENE = "Scene and appearance: "

DROP = frozenset({
    "breasts", "small breasts", "medium breasts", "large breasts", "bare breasts", "cleavage",
    "detailed skin", "perfect anatomy", "hetero", "1other", "sexy", "lewd", "seductive",
    "seductive pose", "bedroom eyes", "sweat", "sweating", "parted lips", "flushed",
    "flushed face", "trembling", "ecstasy", "moaning", "explicit", "nsfw", "spread legs",
    "satisfied", "satisfied expression", "afterglow", "after sex", "wet skin",
    "bare midriff", "no pants", "towel barely covering",
})
RENAME = {
    "intimate": "tender",
    "intimate lighting": "warm lighting",
    "intimate setting": "cozy setting",
    "half-lidded eyes": "soft gaze",
    "half-closed eyes": "soft gaze",
    "vulnerable": "emotional",
    "vulnerability": "emotional",
    "sheer nightgown": "soft cotton nightgown",
}
HER_UNDRESS = frozenset({
    "lingerie", "black lace lingerie", "elegant lingerie", "garter belt", "black lace bra", "bra",
    "panties", "underwear", "negligee", "sheer negligee", "nude", "naked", "fully nude",
    "artistic nude", "topless", "bare skin",
})
HIS_UNDRESS = frozenset({"shirtless", "bare chest", "boxers", "nude", "naked", "fully nude",
                         "bare skin"})
BATH = frozenset({"bath", "bathing", "onsen", "hot spring", "shower", "bathtub", "steam",
                  "towel", "wet hair"})
BED = frozenset({"bed", "on bed", "in bed", "lying on bed", "lying in bed", "under covers",
                 "bedroom", "sheets", "blankets", "pillows"})
# Removed as well on the redraw after a refusal.
TOUCH = frozenset({
    "kiss", "kissing", "kissing wrist", "embrace", "hug", "hugging", "cuddling", "clinging",
    "arms around neck", "hand on waist", "hand on stomach", "hand on another's jaw",
    "fingers in hair", "hand in hair", "face in shoulder", "forehead to forehead", "lap pillow",
    "close distance", "close together", "close-up", "close up", "lying down", "lying on bed",
    "lying in bed", "under covers", "bed", "on bed", "in bed", "dim lighting", "romantic",
    "bare shoulders", "bare legs", "off shoulder",
})


def _split(line: str) -> list[str]:
    return [tag.strip() for tag in line.rstrip().rstrip(".").split(";") if tag.strip()]


def _rewrite(prompt: str, *, plain: bool) -> str:
    lines = prompt.split("\n")
    index = next((i for i, line in enumerate(lines) if line.startswith(SCENE)), None)
    if index is None:
        return prompt
    tags = _split(lines[index][len(SCENE):])
    lowered = [tag.lower() for tag in tags]
    couple = "one man" in lowered or "couple" in lowered
    in_bed = any(tag in BED for tag in lowered)
    in_bath = any(tag in BATH for tag in lowered)
    her = any(tag in HER_UNDRESS for tag in lowered)
    his = couple and any(tag in HIS_UNDRESS for tag in lowered)
    kept: list[str] = []
    for tag, low in zip(tags, lowered):
        if low in DROP or low in HER_UNDRESS or low in HIS_UNDRESS or (plain and low in TOUCH):
            continue
        if plain and low == "bedroom":
            tag = "cozy room"
        elif low in RENAME:
            tag = RENAME[low]
        if tag.lower() not in {k.lower() for k in kept}:
            kept.append(tag)
    if plain:
        notes = ["Klukai is fully clothed in soft everyday clothes" if her else
                 "Klukai is fully clothed"]
        if couple:
            notes.append("the Commander wears a casual shirt and trousers")
            notes.append("they sit side by side, smiling gently")
    else:
        notes = []
        if in_bath and (her or his):
            notes.append("Klukai is wrapped in a large white towel")
            notes.append("thick steam drifts across the water")
            if his:
                notes.append("the Commander has a towel around his waist")
        elif in_bed and (her or his):
            who = "them both" if couple else "her"
            notes.append(f"a thick quilt is pulled up to the shoulders over {who}")
            notes.append("only shoulders and arms show")
        elif her:
            notes.append("Klukai is wrapped in a large soft blanket around her shoulders")
        if his and not (in_bed or in_bath):
            notes.append("the Commander wears a loose open-collared shirt")
    notes.append("modest and tasteful, a tender moment between adults")
    seen = {tag.lower() for tag in kept}
    kept += [note for note in notes if note.lower() not in seen]
    lines[index] = SCENE + "; ".join(kept) + "."
    return "\n".join(lines)


def implied(prompt: str) -> str:
    """Covered and tender: what fal is sent first."""
    return _rewrite(prompt, plain=False)


def plain(prompt: str) -> str:
    """Fully clothed, no touch: the one redraw after fal refuses the implied version."""
    return _rewrite(prompt, plain=True)
