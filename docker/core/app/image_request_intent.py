"""Resolve explicit picture followups without inventing or persisting scene state."""

from collections.abc import Callable, Mapping, Sequence
import re

from .image_prompts import needs_image
from .image_scene import infer_place

# A bare followup is only unambiguous after a user's concrete embodied scene.
# This deliberately does not attempt general natural-language understanding.
_SCENE = re.compile(
    r"^(?:imagine |picture )?(?:we(?:'re| are)?|us|you(?:'re| are)?|"
    r"you and i(?: are)?|klukai(?: is)?) "
    r"(?:sitting|standing|walking|lying|relaxing|resting|watching|holding|"
    r"leaning|smiling|posing|dancing|cuddling)\b"
)
_TECHNICAL = re.compile(
    r"\b(?:code|coding|debugging|logs?|server|function|algorithm|html|css|sql|"
    r"api|error|capabilit(?:y|ies)|settings?|keywords?)\b"
)
_PREFIX = re.compile(
    r"^(?:(?:hey|hi)[ ,]+)?(?:klukai[ ,]+)?(?:please )?"
    r"(?:(?:can|could|would|will) you )?(?:please )?"
)
_FOLLOWUP = re.compile(r"(?:show me|let me see)(?:,? please)?[.!?]*")


def _concrete_scene(text: str) -> bool:
    normalized = " ".join(text.casefold().replace("’", "'").split())
    return bool(
        _SCENE.match(normalized)
        and not _TECHNICAL.search(normalized)
        # Reuse the ordinary request's opt-out/discussion checks, not its
        # admission result for a narrative (which alone is never a request).
        and needs_image(f"take a picture of {normalized}")
        and infer_place(normalized) is not None
    )


def resolve_picture_request(
    content: str,
    prior_turns: Sequence[Mapping[str, object]],
    *,
    detector: Callable[[str], bool] = needs_image,
) -> str | None:
    """Return only the render request; callers must retain raw chat content.

    ``prior_turns`` excludes the current user turn. Only the most recent user
    turn can supply a scene, never assistant text or an older topic. Resolution
    happens before her reply so the hint and render admission cannot diverge.
    """
    if detector(content):
        return content
    normalized = " ".join(content.casefold().replace("’", "'").split())
    request = _PREFIX.sub("", normalized, count=1)
    if request.startswith("show ") and _concrete_scene(request[5:]):
        return content
    if not _FOLLOWUP.fullmatch(request):
        return None
    for turn in reversed(prior_turns):
        if turn.get("role") != "user":
            continue
        scene = turn.get("content")
        if isinstance(scene, str) and _concrete_scene(scene):
            return f"{content}\n\nScene: {scene}"
        break
    return None
