"""Explicit picture-request routing only: pure parsing, never a render call."""

import pytest

from app.image_prompts import is_couple_scene, needs_image


@pytest.mark.parametrize(
    "message",
    [
        "Send me a selfie",
        "How about a selfie?",
        "Another picture of us, please",
        "One more photo",
        "I would like a photo of you",
        "I want a selfie",
        "Could I have a photo of you?",
        "Show me your picture",
        "Show me what you're wearing",
        "Draw yourself by the garden fence",
        "Show me yourself by the wall",
        "Take a picture of you by the wall",
        "Send a selfie in front of the fence",
        "Show me your outfit next to the logs",
        "Send me a photo of the code on the screen",
        "Could you send me a selfie?",
        "Klukai, please take a picture",
        "Take a photo",
        "Can you take a picture?",
        "Please send a selfie if possible",
        "Take a photograph of us",
        "Snap a pic",
        "Send me a pic",
        "Please send us a photo of you",
        "Show me yourself",
        "SHOW ME YOURSELF",
        "Show me a picture",
        "Can you show me your outfit?",
        "Let me see your outfit",
        "I'd like to see your outfit",
        "I want to see a picture of you",
        "Can I see you?",
        "Show us together",
        "Show me us together",
        "Take a picture with me",
        "Could you take a photo of the two of us?",
        "Draw us on the beach",
        "Can you draw a scene?",
        "A drawing of you",
        "Paint me something",
        "Paint a rooftop shot",
        "Render that again",
        "Generate an image of a sunset",
        "Create a picture of yourself",
        "Make a picture of us",
        "Visualize us",
        "  Please   send\nme a photo!  ",
    ],
)
def test_explicit_picture_requests(message):
    assert needs_image(message) is True


@pytest.mark.parametrize(
    "message",
    [
        "",
        "Show me",
        "SHOW ME!",
        "Show me code",
        "Show me how this works",
        "Show me the logs",
        "Show me your code",
        "Let me see the logs",
        "How about tomorrow?",
        "How about dinner?",
        "Try again",
        "Try again, the network disconnected",
        "One more",
        "Don't take a picture",
        "Send me a selfie, actually don't",
        "I do not want a picture",
        "How about a code example?",
        "Another network retry, please",
        "One more question",
        "I would like a photo feature added",
        "Show me your picture settings",
        "Please do not send me a selfie",
        "Don’t show me yourself",
        "No pictures, please",
        "Stop generating images",
        "Can you take pictures?",
        "Can you take a picture or only send text?",
        "Can you take a picture or are you text only?",
        "Can you send me a selfie if I ask for one?",
        "Take a picture only if I ask you to",
        "Send me a photo when I ask for it",
        "Show me your photo generation capabilities",
        "Draw attention to this error",
        "Do you take selfies?",
        "How do you take a picture?",
        "What keywords trigger a selfie?",
        "Does saying show me yourself create a picture?",
        'The phrase "send me a selfie" should work',
        '"take a picture"',
        "You sent me a selfie yesterday",
        "I took a picture of us yesterday",
        "I don't want to see your outfit",
        "I want to see how the code works",
        "Draw a conclusion from these logs",
        "Render this React component",
        "Paint the wall blue",
        "Check the drawer for my keys",
        "I surrender, you win",
        "Withdraw the troops",
        "I'm drawn to you",
        "Tell me about your day",
    ],
)
def test_conversation_is_not_permission_to_generate(message):
    assert needs_image(message) is False


@pytest.mark.parametrize(
    "message", ["show us together", "take a picture with me", "take a photo of us"]
)
def test_neutral_together_requests_keep_existing_scene_detection(message):
    assert needs_image(message) is True
    assert is_couple_scene(message) is True


@pytest.mark.parametrize(
    "scene",
    [
        "We are sitting together on the balcony at sunset",
        "We're walking together on the beach at dawn",
        "Imagine us standing together in the garden",
        "You are relaxing in the bedroom",
        "You and I are sitting together in the cafe",
    ],
)
def test_bare_followup_preserves_the_latest_user_scene_verbatim(scene):
    from app.image_request_intent import resolve_picture_request

    turns = [
        {"role": "user", "content": scene},
        {"role": "assistant", "content": "We are in a different room now."},
    ]
    assert (
        resolve_picture_request("Please show me", turns)
        == f"Please show me\n\nScene: {scene}"
    )
    assert resolve_picture_request(scene, []) is None


@pytest.mark.parametrize(
    "scene",
    [
        "We are debugging code in the office",
        "We are sitting in the office discussing server logs",
        "We were sitting together on the balcony yesterday",
        "Could we be sitting together on the balcony?",
        "We are sitting together on the balcony but no pictures",
        "We are sitting together on the balcony if I ask for a photo",
        "We are sitting together",
        "The server is in the office",
        "A picture feature could show us on the balcony",
    ],
)
def test_ambiguous_followup_does_not_turn_non_scene_context_into_request(scene):
    from app.image_request_intent import resolve_picture_request

    assert (
        resolve_picture_request("show me", [{"role": "user", "content": scene}]) is None
    )


def test_effective_scene_keeps_existing_participants_setting_and_time():
    from app.image_request_intent import resolve_picture_request
    from app.image_scene import resolve_scene

    request = resolve_picture_request(
        "show me",
        [
            {
                "role": "user",
                "content": "We are sitting together on the balcony at sunset",
            }
        ],
    )
    assert request is not None
    assert is_couple_scene(request)
    scene = resolve_scene(request, hour=8, couple=True)
    assert scene.key == "balcony"
    assert scene.time == "evening"  # Existing resolver's sunset period.
    assert scene.time_source == "words"
