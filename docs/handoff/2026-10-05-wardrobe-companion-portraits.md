# Handoff: wardrobe, Her Day, the companion stage, live portraits and 3D

Dates: 2026-10-04 → 2026-10-05. For Claude, Codex and the owner. This covers 47 commits on
`main`, from `acf3375` to `562063e`. Everything below is deployed on Amarillo and pushed.

Read this before touching the wardrobe, the portrait pipeline, the companion page or the 3D
avatar. Read `docs/handoff/2026-09-25-dominus-tailscale-publish.md` before touching the GPU host.

---

## 1. What exists now

### Her wardrobe: one canon catalog
- `config/personality.yaml` `costumes:` is the **single source of truth** for the prompt, image
  tags, unlock gates and the PWA picker. It is read through `docker/core/app/wardrobe.py`.
- **19 outfits:**
  - 6 canon: Blazing Star, Speed Star, Astral Luminous, Cerulean Breaker, Immaculate Service,
    Indigo Oath. Verified against the official EXILIUM art; see
    `docs/research/2026-10-04-canon-wardrobe-dossier.md`.
  - 13 originals, each anchored in canon.
- **Before this work** the image tags for every canon outfit were wrong (Cerulean Breaker
  rendered as "silver plating armor"), and two ids were invented. Legacy ids map forward on read:
  - `starlit_vow` → `indigo_oath`
  - `midnight_sovereign` → `formal_commission`
- **Daily pick:** she chooses **today's outfit** herself, deterministically, from weather,
  occasions, weekday, mood, his favourite and novelty. Her day turns over at **05:00**, not
  midnight.
- **His requests** ("wear the maid outfit") are decided **by code** (`decide_request`), never
  by the LLM:
  - affection bands decide what she'll wear;
  - private and denied outfits stay off-limits;
  - the oath outfit only "on a day that matters";
  - she changes at most twice a day;
  - below affection 3 she won't even name the outfit.
- **The PWA wardrobe button** runs the same decision table, in UI mode.
- **Storage:** migration `190`, table `companion_her_day`, one row per Commander per day. It is
  additive and doubles as the wardrobe calendar.

### Her Day
- **Roster:** `app/her_day.py` builds a duty roster per day: PT, range, hangar, Tea Time counter,
  0200 reports. It is derived deterministically from day and weather, and **never stored**.
- **Where it shows:**
  - LOCATION and CURRENT OUTFIT lines in the prompt;
  - a status line in the chat header with a TODAY sheet;
  - `GET /api/her-day`;
  - the PWA dossier, with wardrobe and worn-this-month.
- **What it never does:** make her unavailable. It colours her first line at most.
- **NIGHT WATCH reads the same roster block,** so the prompt, the page and the 0200 lines agree.

### Features added
| Feature | Where |
|---|---|
| Clean goodbyes: she lets him go and holds pings until morning | `app/goodbyes.py` |
| The 0200 watch: insomnia-aware, protective, orders him to bed | `app/night_watch.py` |
| Operation brief before his real-life events (stored as `companion_promises` rows with `kind=event`) | `app/op_brief.py` |
| Command decisions: small calls she asks him to make, with later consequences | `app/decisions.py` |
| Tea Time counter duty: she serves *Racing Calm* | `app/her_day.py` |

### The companion stage (`/app/companion.html`)
- **Format:** a Grok-companion-style page in vanilla JS (`flutter_app/web/companion/`), opened from
  the PWA header.
- **Live portrait:** her portrait in today's outfit plus five expression frames (blink, talk,
  smile, blush, annoyed):
  - irregular blinks;
  - mouth flaps driven by her voice's loudness;
  - mood crossfades;
  - breathing and sway;
  - parallax.
- **3D window:** her GFL2 dorm model with 15 animations, toon-shaded, driven by mood, with a
  talking layer.
- **Voice:** each reply goes through her TTS: English text in her Japanese (Ai Nonaka) voice.
- **Music:** generated live in Web Audio. There are no recorded tracks. It follows Her Day's
  scene, her mood and the weather, and ducks under her voice.
- **Ambience:** rain, snow, hangar dust, stars, bokeh.
- **Chat dock:** on the same session as the PWA.
- **iOS:** one tap-to-begin gesture unlocks audio. Safe areas are respected, and the page
  suspends in the background.

### Live portrait pipeline (`app/portrait.py`, ComfyUI code only in `app/image_gen.py`)
- **Cache:** frames live at `/images/portraits/{STYLE_VERSION}/{user}/{outfit}/`.
  **Bump `STYLE_VERSION` whenever the composition or style changes**, so stale frames are never
  served. Current: `v3`.
- **Generation:**
  1. The base is txt2img.
  2. Expression frames are **masked inpaints of a 576 px face crop**, with the eyes found by her
     green irises, composited back.
  3. Nothing outside each frame's mask changes.
- **One GPU lease, one model load, one graph** for the whole set.
- **Speed (measured live):** ~18 s of GPU time per full set (base ~8 s, five expressions
  ~9.5 s), down from ~80 s.
- **Pre-drawing:** today's outfit is pre-drawn when it is picked or changes.
- **Chat-quiet gate:** a render waits for 60 s without a chat message, so it never delays a
  reply. This is by design.
- **Blink:** it is verified (the iris must be gone) and re-rolled up to 5 times. After that, the
  most-closed attempt is kept if it is at least half shut.
- **Status:** a set with a base reports `ready`, never a false "GPU busy".
- **Routes** (all Bearer auth):
  - `GET /api/portrait` returns signed frame URLs;
  - `POST /api/portrait/refresh`;
  - `GET /api/avatar/model`.

### Image settings: the "hollow" fix
- **The sampler was wrong for the model.** NoobAI v-pred wants **Euler at CFG ~4.5**; we ran
  `euler_ancestral` at CFG 7. That produced the flat, saturated-blue, washed-out look.
- **The fix applies to every render:** `WORKFLOW_TEMPLATE` is now euler / normal / CFG 4.5 / 24
  steps.
- **Portrait style v3:** shading, eye reflections and a dark gradient backdrop with depth, so
  her edges blend into the dark stage.
- **Outfit accuracy:** all 19 outfits render as themselves. The tag fixes are in YAML.
- **Nudity guard:**
  - every original outfit names its inner layers and closures;
  - wardrobe-driven renders below affection 8 add an SFW negative (`generate_image(sfw=True)`);
  - intimate scene outfits only open at 8+.

### 3D avatar
- **Build:** `tools/avatar/` builds `assets/build/klukai.glb` (1.41 MB, meshopt + webp,
  reproducible) from the extracted GFL2 dorm model and Mixamo clips. It needs Node only, no
  Blender.
- **The April blocker is solved.** The source face is a baked wink. The open eye is mirrored onto
  the closed one, and the original lids become a `blink` morph target.
- **"Hollow" on iPhone** was `polygonOffset` on double-sided clothes. Metal applies it far more
  strongly than desktop GL, so the jacket drew through itself. It is removed, and a test keeps it
  out.
- **The model is game-derived:**
  - it is **gitignored**;
  - it is mounted read-only at `/avatar` in `companion-core`;
  - it is served **only** via the authenticated, gzip-capable `GET /api/avatar/model`
    (`no-cache` + ETag);
  - it is never served as `/app` static.

### iOS / Safari / Brave
- **No third-party requests:** CanvasKit and the fallback fonts are served locally.
  `scripts/deploy-web.sh` fails the build if any third-party host appears in the output.
- **iOS app shell:**
  - meta tags, the 180 px icon and a manifest scoped to `/app/`;
  - safe areas and keyboard insets;
  - the socket redials on resume;
  - first-tap audio unlock;
  - push-to-talk works on Safari.
- **Caching:** `app/static_cache.py` sets explicit `Cache-Control` for `/app`:
  - hashed bundles: a year, immutable;
  - CanvasKit, fonts and images: a day;
  - HTML and unhashed JS: revalidate.

---

## 2. Bugs found and fixed along the way
- **Twelve wardrobe bugs from an adversarial review:**
  - lingerie tags leaked into images at low affection;
  - outfit-request detection fired on ordinary sentences;
  - the day turned over at midnight;
  - stand-in rows took requests;
  - the request cap wasn't atomic;
  - and others.
- **Original outfits rendered topless or nude** below affection 8, because open layers got filled
  with skin.
- **Voice letters had never worked** (0 ever saved), for two reasons: `language="ja"` (XTTS needs
  `cutlet`, and the text is English anyway) and a root-owned `/audio` volume.
- **An agent switched the PWA replay to `ja`.** That would have broken every replay.
  `tts_request.dart` now pins `en` with a test.
- **`make test-integration`:**
  - it nested stale test files;
  - it skipped `pytest.ini`;
  - per-test event loops hung the global DB/Redis clients;
  - leftover `testclient` ban and rate-limit state broke repeat runs.

  It is now repeatable, 35/35.
- **The perf gate** ran in every default test run and flaked under load. It is now opt-in:
  `pytest -m perf`.

---

## 3. How to test
```bash
cd docker/core && python3 -m pytest tests/ -q --cov=app --cov-fail-under=95   # 4483 passed, 96.30%
ruff check app/ --config ruff.toml && mypy app/ --config-file mypy.ini
make test-js            # companion stage (node --test) + 3D avatar build tests
make test-integration   # live container, 35 tests
python3 scripts/e2e_live.py   # 11 live checks as the `claude` test user (needs the GPU host)
cd flutter_app && flutter analyze && flutter test   # VM tests; chrome-platform tests hang on this host
```
- **Render checks:** `scripts/wardrobe_render_check.py` renders every outfit.
- **3D screenshots:** `tools/avatar/shot.py`.
- **Test user:** always the `claude` user, never `jalsarraf`.

## 4. Operations
- **Deploy core:**
  ```
  docker compose build companion-core && docker compose up -d companion-core
  ```
- **Deploy the PWA:** `scripts/deploy-web.sh`. Static companion files can also be copied into
  the bind-mounted `web-build/`.
- **Rollback images:**
  - `companion-companion-core:rollback-pre-wardrobe-20261004`
  - `companion-companion-core:rollback-pre-companion-20261005`
- **Rebuild the 3D model:** `cd tools/avatar && npm ci && npm run build`. It needs the gitignored
  source assets in `assets/source-models` and `assets/textures`.
- **Live YAML:** `config/` is bind-mounted and hot-reloaded. Whatever branch is checked out in the
  main tree, its YAML is live immediately, even on old code.

## 5. Known limits and next steps
- **Real devices:** iPhone, Brave and Safari behaviour is verified only by emulation and
  software WebGL. Check on a real device.
- **No 3D lip-sync:** the model has no mouth shapes, so she uses a talking clip plus head motion.
- **One 3D outfit:** the dorm kit. Sunborn publishes official MMD models of her other outfits,
  non-commercial with no redistribution, at ~1–2 days each to convert.
- **Faster drawing is possible:** a speed LoRA (DMD2 / Lightning / Hyper / LCM) could cut
  drawing further. None is installed on the GPU host and none has been tried with this model.
- **TTS slows the next reply:** each voice line takes a GPU lease, so the next reply may
  cold-load the chat model (~2 s).
- **CI runner label:** CI runs on `[self-hosted, dominus]`, not the fleet's
  `[self-hosted, unified-all]`. This predates this work and is left as the owner set it.
