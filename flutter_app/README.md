# Klukai PWA

Flutter Web client for Klukai: streamed chat, her memory album and Her POV jobs,
The Thread, wardrobe and daily schedule, and cancellable voice playback.
Release **1.1.0+2** belongs to Klukai **v1.1.0**; [pubspec.yaml](pubspec.yaml)
is the version source and declares Dart `^3.11.4`. The existing artwork stays in
[assets/](assets/) and the server's persistent images volume.

## Runtime connection

Production serves this app at `/app/` on the core origin. [main.dart](lib/main.dart)
uses that origin for API requests; localhost development uses
`http://localhost:8300`. The login page stores the core session bearer in
`klukai_token` browser local storage. Use the normal login flow for the origin
being tested; never embed provider credentials or production session tokens in
the app, build arguments, fixtures, or screenshots.

The client connects to core over HTTP and WebSocket. Core alone selects the
inference providers. The deployed interactive primary is Sonnet 5.5 with medium
adaptive thinking, with Dominus as the local secondary. English speech uses
Dominus XTTS through core's submit/poll/cancel TTS jobs. When Dominus is offline,
text chat remains available without an automatic voice fallback. Speech
transcription remains on its separate Dominus endpoint.

Production uses the existing Claude API key for dream/Her POV writing, initially
capped at 12 attempts per UTC day and 12 per UTC month across all users. Pictures
use the private account worker's authorized Agy/Grok alternation and bounded
service-error failover. Policy refusals never fail over; paid image APIs, paid
credits and GPU images remain disabled. The app does not choose providers.
The app requires no API key or provider-specific build to switch core between
authorized modes. See the [provider policy and local GPU setup](../README.md#provider-policy)
and [creative-provider handoff](../docs/handoff/2026-10-14-dream-image-preparation.md).

## Companion camera

The 3D companion view starts with her whole body visible. Drag with a mouse or
one finger to rotate freely through 360°. Scroll or pinch to zoom, or use the
rotation and zoom buttons above the chat box. **Reset view** restores full-body
framing. The chosen view stays through animation, window resizing and a
temporary switch to portrait mode.

## Development and tests

Use Flutter 3.44.2 / Dart 3.12.2 and the committed dependency lock. From this
directory:

```sh
flutter pub get --offline --enforce-lockfile
dart format --output=none --set-exit-if-changed lib test
flutter analyze --no-pub --fatal-infos
```

Run VM and Chrome suites separately, selecting browser files by their
`@TestOn('browser')` annotation; socket local-server tests run on the VM.
The repository CI runner records machine events and requires a successful
terminal `done` event, at least one non-skipped test and no error events.
An interrupted Flutter process can return zero without executing any tests.
The browser journeys use deterministic transports, HTTP fixtures and test
credentials to check reconnect/history recovery, revocation, cancelled speech,
archive retry and terminal provider failure. Live testing against core is a
separate action: use the designated test account, and remember that a live
request can invoke whichever paid provider the operator has enabled.

On Amarillo, use `CHROME_EXECUTABLE="$PWD/tool/chrome_test.sh"` for Chrome
tests. Its basic password store avoids a headless desktop-keyring stall and is
restricted to Flutter-generated temporary profiles; existing browser profiles
are rejected. This does not change the desktop browser's credential settings.

## Screen ownership

Each screen keeps one mutable `State` owner and its lifecycle. Library parts
have private extension methods, share that state and never construct a second
transport or controller. They cannot be imported as independent public APIs.

| Owner | Parts and responsibility |
| --- | --- |
| `chat_screen.dart` | `chat_connection`: auth/history/reconnect; `chat_wire_events`: inbound protocol and reply finalization; `chat_composer`: outgoing text and waiting/error hints; `chat_voice`: permission/transcription epochs; `chat_playback`: queued voice and notification output; `chat_navigation`: screen changes/logout; `chat_starters` and `chat_view`: character prompts and presentation. |
| `memory_archive_screen.dart` | Owns filters and request generations; `archive_sidebar` and `archive_timeline` render navigation/results; `archive_detail` owns the separate authenticated detail dialog and confirmed discard. |
| `her_pov_screen.dart` | Owns accepted job, polling and gallery loading; `her_pov_layout` renders responsive layout/gallery; `her_pov_stage` renders phases, progress and action. |
| `profile_screen.dart` | Owns independent records/wardrobe requests; `profile_view` renders the dossier and invokes those actions. |

Existing constructor injection remains supported. Chat additionally accepts an
optional history client and login redirect callback; Her POV accepts an archive
service and job HTTP client. Defaults preserve production wiring, and callers
retain ownership of injected HTTP clients. Revocation disables chat and ignores
late frames before login navigation; a pending transcript cannot send afterward.

The companion's handwritten JavaScript also stays below the application size
bound. [Avatar ownership](web/companion/avatar/README.md) describes the public
entrypoint, single runtime owner, local module graph and pinned Three bundle.
[Pretext provenance](web/js/pretext/README.md) documents the exact upstream
security backport, inherited vendor exceptions, MIT license and regression
evidence.

## Build and publish

From the repository root:

```sh
./scripts/deploy-web.sh --build-only
```

The [build script](../scripts/deploy-web.sh) sets the `/app/` base path, bundles
CanvasKit, mirrors fallback fonts, and audits third-party runtime references.
An uncached font mirror can access the network during the build. Build-only
output stays in `flutter_app/build/web`; it does not alter `web-build/` or the
running app. Keep the normal local-font setting rather than opting into the
script's CDN exception.

When web publication is authorized, run `./scripts/deploy-web.sh` from the
repository root. It writes the core-mounted `web-build/`, preserves its login
page, and adds content hashes to the JavaScript filenames. This is a deployment,
not just compilation. It neither starts GPU services nor enables a paid provider.

General Flutter guidance: [learn Flutter](https://docs.flutter.dev/get-started/learn-flutter),
[first-app codelab](https://docs.flutter.dev/get-started/codelab),
[learning resources](https://docs.flutter.dev/reference/learning-resources), and
the [Flutter documentation](https://docs.flutter.dev/).
