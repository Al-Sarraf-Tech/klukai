from __future__ import annotations

import asyncio
from dataclasses import dataclass
import unittest

import dominus_speaches as hardening


@dataclass
class FakeDependant:
    call: object


@dataclass
class FakeRoute:
    path: str
    dependant: FakeDependant | None = None
    endpoint: object | None = None


@dataclass
class FakeRouter:
    routes: list[FakeRoute]


@dataclass
class FakeApplication:
    router: FakeRouter


class SpeachesHardeningTests(unittest.TestCase):
    def test_matching_bounded_stt_tts_ttl_is_required(self) -> None:
        self.assertEqual(
            hardening.validate_ttl_contract(
                {"WHISPER__TTL": "600", "DOMINUS_SPEACHES_TTS_TTL": "600"}
            ),
            600,
        )
        with self.assertRaisesRegex(RuntimeError, "matching"):
            hardening.validate_ttl_contract(
                {"WHISPER__TTL": "600", "DOMINUS_SPEACHES_TTS_TTL": "599"}
            )
        with self.assertRaisesRegex(RuntimeError, "895-second"):
            hardening.validate_ttl_contract(
                {"WHISPER__TTL": "896", "DOMINUS_SPEACHES_TTS_TTL": "896"}
            )

        self.assertEqual(hardening.MAX_MODEL_IDLE_TTL_SECONDS, 900)
        self.assertEqual(hardening.MODEL_IDLE_SAFETY_CUTOFF_SECONDS, 895)

    def test_vad_realtime_chat_and_diarization_routes_are_removed(self) -> None:
        routes = [
            FakeRoute("/health"),
            FakeRoute("/v1/audio/speech/timestamps"),
            FakeRoute("/v1/audio/diarization"),
            FakeRoute("/v1/realtime"),
            FakeRoute("/v1/realtime/rtc"),
            FakeRoute("/v1/chat/completions"),
        ]
        app = FakeApplication(FakeRouter(routes))
        hardened = hardening.harden_application(app)
        self.assertEqual([route.path for route in hardened.router.routes], ["/health"])

    def test_transcription_route_forces_vad_false(self) -> None:
        captured: list[dict[str, object]] = []

        async def transcription(**arguments: object) -> dict[str, object]:
            captured.append(arguments)
            return arguments

        route = FakeRoute(
            "/v1/audio/transcriptions",
            dependant=FakeDependant(transcription),
            endpoint=transcription,
        )
        app = FakeApplication(FakeRouter([route]))
        hardening.harden_application(app)
        assert route.dependant is not None
        result = asyncio.run(route.dependant.call(vad_filter=True, model="locked"))
        self.assertEqual(result["vad_filter"], False)
        self.assertEqual(captured, [{"vad_filter": False, "model": "locked"}])


if __name__ == "__main__":
    unittest.main()
