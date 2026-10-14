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
adaptive thinking, with Dominus as the local secondary. English speech uses the
local Intel Arc XTTS service through core's submit/poll/cancel TTS jobs. Speech
transcription remains on its separate Dominus endpoint.

Scene pictures and dream/Her POV writing default to local providers. Production
now uses the existing Claude API key for dream/Her POV writing, initially capped
at 12 attempts per UTC month across all users. Pictures still render through
Dominus ComfyUI; the prepared OpenAI image adapter has zero allowance and remains
disabled. Dream entries remain text-only; animated live portraits retain their
local masked-image pipeline.
The app requires no API key or provider-specific build to switch core between
authorized modes. See the [provider policy and local GPU setup](../README.md#provider-policy)
and [creative-provider handoff](../docs/handoff/2026-10-14-dream-image-preparation.md).

## Companion camera

In the 3D companion view, drag with a mouse or one finger to rotate up to 120°
each way. Scroll or pinch to zoom, or use the rotation and zoom buttons above
the chat box. **Reset view** restores automatic framing. The chosen view stays
through animation, window resizing and a temporary switch to portrait mode.

## Development and tests

From this directory, using the project's installed Flutter SDK and dependencies:

```sh
flutter analyze
flutter test
flutter test --platform chrome
```

Both test commands are needed: browser-dependent screens use `@TestOn('browser')`,
while the socket service's local-server tests use `@TestOn('vm')`. The test
fixtures use fake transports and test tokens. Live testing against core is a
separate action: use the designated test account, and remember that a live
request can invoke whichever paid provider the operator has enabled.

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
