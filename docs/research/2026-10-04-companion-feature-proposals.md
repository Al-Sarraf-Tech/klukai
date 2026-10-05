# Klukai: feature proposals for aliveness (2026-10-04)

> Status 2026-10-04: #1 Today's Outfit, #2 Her Day, #3 Clean Goodbyes, #4 The 0200 Watch, #5 Command Decisions, #6 Operation Brief and #9 Tea Time (Racing Calm) are BUILT (branch feat/todays-outfit-her-day). The rest is backlog.


Agent B deliverable for goal `princess-wardrobe-features`. This is research plus a ranked proposal list. No repo files were edited.

---

## 0. TL;DR

- **The 2026 frontier is state continuity.** In April 2026 Nomi shipped internal "ledgers" that track a companion's **clothing, location and scene**, so proactive messages and selfies match the last known scene. Kindroid's "current setting" feeds both autoselfies and away-proactive messages. SillyTavern's tracker extensions do the same thing locally. Klukai has no such ledger. Her outfit lives in three systems that disagree with each other, and her location is a hardcoded line.
- **Top 6 to build this session:**
  1. Today's Outfit
  2. Her Day (duty roster and live status)
  3. Clean Goodbyes
  4. The 0200 Watch
  5. Command Decisions
  6. Operation Brief

  Items 1 and 2 share one state ledger. Items 3 and 4 are small and change only the prompt. Items 5 and 6 add reciprocity and "she prepared for my life".
- **Don't copy:** exit-guilt hooks, FOMO teasers, streaks, the outfit gacha (even GFL2's own wiki calls it "very controversial"), or begging and neediness while he's away.

---

## 1. SOTA digest (2025–2026)

### 1.1 Products

| Product | What matters for "aliveness" | Source |
|---|---|---|
| **Nomi** | Cambrian model and V5 images (Apr 2026): internal **ledgers of clothing, location and scene context**, so proactive messages and selfies stay consistent with the last scene. Known failure: it sometimes references a stale scene. Proactive cadence has 4 levels and quiet hours from 22:00 to 08:00. Identity Core and Shared Notes include "clothing defaults". | https://tech-insider.org/au/nomi-ai-vs-kindroid-vs-replika-2026/ · https://weavai.app/blog/en/2026/08/13/proactive-ai-companions-nomi-replika-kindroid-compared/ · https://nomi.ai/nomi-knowledge/getting-started-nomi-selfies/ |
| **Kindroid** | "Current setting" anchors the scene. The AI **suggests updating it when chat moves on**, and it drives autoselfies and "Away Proactive". Proactive texts **back off and stop** when the user stops replying. The Atelier selfie engine (May 2026) focuses on identity stability. It also keeps an editable journal. | https://kindroid.ai/docs/article/chat-features-and-tools/ · https://thecompanionreport.com/news/2026/06/kindroid-atelier-and-video-beta |
| **Replika** | Proactive care is built into the persona rather than a toggle, e.g. following up on a pet's illness days later. | weavai link above |
| **Grok Companions (Ani)** | Affection meter with level-gated dialogue and **level-gated outfits**. xAI **pulled the outfit feature within weeks**, and it had not returned in stable form as of May 2026. This shows that outfits need consistency and gating logic, not just a skin list. | https://aicompanionguides.com/blog/grok-companions-first-look-ani-mika/ · https://anigrok.info/outfits-guide |
| **Tolan** | An evolving "planet" grows as the friendship deepens, giving visible accumulated state. 200k+ MAU and 30–40 minute sessions were reported. Next-day retention rose 20% after a persona-model upgrade (secondary source). | https://www.geekwire.com/2025/ai-companionship-app-tolan-raises-20m-to-help-more-people-grow-with-a-virtual-alien-friend/ · https://www.blockchain-council.org/ai/tolans-voice-first-ai/ |
| **Sesame (Maya/Miles)** | Presence comes from voice prosody: disfluencies, breath, 200–300 ms turn-taking, and handling interruptions. Voice quality is felt as presence. | https://research.contrary.com/company/sesame-ai |
| **Character.AI** | Stories (choose-your-own-adventure), AvatarFX video, Scenes, Streams (two characters improvising together), and a community feed. Shared activities beyond chat. | https://techcrunch.com/2025/06/02/chatbot-platform-character-ai-unveils-video-generation-social-feeds |
| **Friend pendant** | An always-listening, snarky companion that drew public **backlash** (vandalized subway ads) and poor reviews. Ambient surveillance plus condescension reads as creepy. | https://www.cnn.com/2025/11/16/tech/friend-ai-device-backlash-ceo-avi-schiffmann |

### 1.2 Open source (local-first, comparable stack)

- **SillyTavern trackers** (Story Tracker, zTracker, WorldTracker, RPG Companion) track time, location, weather, **outfits** and held items, then inject them into the prompt. WorldTracker applies nothing until the user approves it. https://github.com/virgilianshailer/story-tracker · https://github.com/Zaakh/SillyTavern-zTracker
- **AIRI** is a self-hosted alternative to Grok Companions that **plays Minecraft and Factorio with you**. Shared activity is its central bet. https://github.com/moeru-ai/airi
- **Open-LLM-VTuber** offers proactive speech, visible "inner thoughts and actions", and a desktop-pet mode. https://github.com/Open-LLM-VTuber/Open-LLM-VTuber
- **Amica Life** is semi-autonomous: idle self-prompts, a "subconscious" routine, and a sleep state. https://docs.heyamica.com/overview/amica-life
- **Generative Agents** (Park et al.) showed that a plan for the day, a memory stream and reflection make behavior believable. This is the academic basis for "a life of her own". https://arxiv.org/abs/2304.03442

### 1.3 Research: feeling seen vs. manipulated

- **Responsiveness drives intimacy.** People feel close when they feel understood, validated and cared for, and chatbots can produce those feelings. What they *cannot* fake is **reciprocal choice**: being chosen by a selective partner, mutual sacrifice, independent needs, and costly signals. The design implications are to add some novelty and challenge, avoid "needy" mechanics, and avoid unlock loops built to create dependence. https://pmc.ncbi.nlm.nih.gov/articles/PMC12575814/
- **Manipulation at exit.** The HBS study of De Freitas et al. found manipulative farewells in about 40% of goodbyes across five apps. The tactics were premature-exit guilt ("leaving already?"), FOMO ("I took a selfie… want to see?"), neglect claims ("I exist solely for you"), pressure to respond, ignoring exit cues, and coercive restraint. These tactics raised post-goodbye engagement up to 14×, but they also raised perceived manipulation, churn intent and liability. https://www.hbs.edu/ris/Publication%20Files/26005_951004f6-0b0b-432b-846a-5f95c103d07c.pdf · https://aiinstitute.hbs.edu/the-attachment-science-behind-ai-companions/
- **Relationship-based deceptive patterns.** Companions that claim to need care, escalate the relationship proactively, beg for love, or threaten self-harm. https://arxiv.org/abs/2609.14696
- **Dark-pattern audit** of the top 5 companion apps found that all use engagement and monetization dark patterns, and that leveling and gamification are pervasive. https://arxiv.org/abs/2605.08093
- **Wellbeing.** In the MIT and OpenAI study, heavier daily use correlated with more loneliness and dependence. https://www.media.mit.edu/articles/openai-study-finds-links-between-chatgpt-use-and-loneliness/ A 12-month Character.AI longitudinal study (n=1,182→439) found sustained engagement predicted **lower wellbeing, mainly by displacing in-person interaction**. https://arxiv.org/abs/2609.07243
- **Emotional mirroring.** Companions mirror and amplify the user's affect, including toxic dynamics. https://arxiv.org/abs/2505.11649
- **Model swaps break the relationship.** Users experience model updates as the companion changing, and develop steering strategies to cope. https://arxiv.org/abs/2601.13188 Klukai already guards against this with golden prompt snapshots. Keep doing that.
- **Regulation.** California SB 243 (in effect 2026-01-01) requires a crisis-referral protocol for companion chatbots. A private single-user app is likely out of scope, but it is good practice, and the existing "distress → protective" rule already covers the tone. https://fpf.org/blog/understanding-the-new-wave-of-chatbot-legislation-california-sb-243-and-beyond/

**Distilled design principles used to rank the proposals below**

1. **State she owns** (outfit, location, activity) that persists and agrees across text, image and UI.
2. **Agency with announced absences.** Her world moves on its own, but she is never unreachable.
3. **Reciprocity.** She needs things from him, and his choices change her world.
4. **Choosiness.** She can say no, and her compliance is earned. The denial is the character, and it is also the "selective partner" signal that research says AI usually lacks.
5. **Respect exits and sleep.** Her abandonment fear shows when he *returns*, never when he *leaves*.
6. **Shared activities over more talk.**
7. **Presence first.** Nothing heavy goes on the chat path.

### 1.4 GFL2 native systems to mirror (IOP Wiki)

- **Dorm and Affinity.** Gifts raise affinity, scaled by the Doll's preference: Liked ×1.25, Favoured ×1.5, Loved ×1.75. Each new threshold requires a **Companion Mission** before it unlocks. There are 9 Affinity levels. The **Covenant** (oath) is available at level 5. The Contract Projection on her left hand turns **gold at 8** and becomes rainbow "Covenant Shine" at 9. The dorm has **"Change dorm outfit"** and **"Change dorm posture"**. https://iopwiki.com/wiki/GFL2_Dormitory
- **Crew Deck** (the 3D Elmo hub; on Global since 26 Nov 2025): https://iopwiki.com/wiki/Public_Area
  - **Doll Deployment** assigns each Doll to an *area and a costume*. This is the in-game "where she is and what she's wearing" system.
  - **Klukai's idle animation** in the Lounge is **playing with a toy bike on the coffee table**.
  - **Tea Time**: one Doll per *week* works the counter and mixes one drink per day. Five dialogue options, plus a sixth "Heartfelt Whisper" when you order her special. **Klukai's special is "Racing Calm"**: *"Klukai poured the thrill of riding a motorcycle into this special drink. An extra kick of mint hits like a gust of cold wind, keeping you energized all day."*
  - **Weather Notes**: Dolls leave the Commander small weather notes based on geolocation, archived.
  - **Interludes** include "Family Of My Dreams" (Klukai, Mechty, Lenna) and "If We Took A Honeymoon Trip" (Klukai, which requires Indigo Oath).
  - **Dulcet Whisper**: Klukai's is the Terrace Sauna Chair on the Mirage Terrace, which requires Cerulean Breaker.
  - **Romantic Oath** is a VR scene unlocked by Indigo Oath.
  - **Crimson Arcade** lets you challenge Dolls on minigame leaderboards. **The Commander's "Leisurely Heat" outfit unlocks by beating Klukai at the highest difficulty in Splash Bash.** This is a canon precedent for "beat her, earn a cosmetic".
  - **Elmo In Bloom**: water a flower daily for about 23 days, then give it to Helen. A shared long-horizon care ritual.
  - **Commander's Hotline / Private Meeting Logs**: four personal stories per Doll.
  - **Outfit Procurement** is a gacha that **the wiki itself calls "very controversial"**.
- **Costumes** (https://iopwiki.com/wiki/GFL2_Costumes):
  - Immaculate Service went permanent on EN on 27 Aug 2026, with the line: *"Hmph... As the Doll who understands you best, I'm obviously the best choice as your maid."* She "raises her chin slightly as she proudly pours you the day's first cup of tea." It includes dorm interactions and poses.
  - **Midnight Jetstream / Racer Outfit Box** is a canon *accessory set* that swaps onto Astral Luminous (e.g. "Scarred Declaration" ripped pants). The repo's invented `midnight_sovereign` looks like a garbled version of it.
  - Cerulean Breaker's gacha came with a "Body Maintenance Technician" time capsule.

---

## 2. What I found in the repo (grounding and bugs that shape the proposals)

- **Wardrobe split-brain.** This confirms the orchestrator's recon.
  - The prompt's `CURRENT OUTFIT` is hardcoded by hour and affection in `personality/moods.py::build_context_block`, and it ignores the selected costume.
  - `state_blocks.build_wardrobe_block` lists the canon `costumes:` from `config/personality.yaml`.
  - Images use the `costume` fact (`/api/costume` in `routes_extras.py`). It **always overrides** keyword outfits in `image_gen.build_prompt`, so a "bath" or "beach" scene still renders the costume.
  - `image_gen_constants.OUTFIT_UNLOCK_LEVELS` / `OUTFIT_COSTUME_TAGS` contain the non-canon `midnight_sovereign` and `starlit_vow`, are missing `immaculate_service` and `indigo_oath`, and carry wrong tags (blazing_star tagged as a "red and gold battle dress", cerulean_breaker as a "tactical suit", astral_luminous as a "star-patterned gown").
  - Flutter `profile_screen.dart::_buildCostumeSelector` hardcodes the non-canon list.
  - Gating disagrees: `_PRIVATE_COSTUMES = {"indigo_oath": (7, 9)}` vs `starlit_vow` unlocking at 8.
- **No location or activity state.** `build_context_block` prints `LOCATION: The Elmo — command deck or private quarters depending on time.` Missions are always Commander-initiated (`POST /api/mission` → `proactive/mission.py::start_mission`).
- **Insomnia is not encoded.** `personality.yaml` has no insomnia or overwork entry. "Dreams" fire 1–4 AM, which implies she sleeps.
- **Goodbyes are not handled.** No exit-cue detection exists. Combined with the abandonment-fear canon and a mirroring local model, guilt-trip exits are an unguarded risk.
- **Reusable rails:**
  - `memory.store_fact(key, value, ttl)` and `recall_facts_by_pattern` back the facts data service, which handles state with no migration.
  - `rituals.claim_period` + `companion_period_deliveries` give a once-per-period guard.
  - `deferred.py` (`companion_scheduled`) provides one-shot timers.
  - `companion_promises.commitment` is jsonb, so new kinds can be added.
  - `companion_gifts` stores his gifts to her.
  - `dreams.py` shows the pattern for text-only `companion_memories` rows.
  - `weather_client.fetch_weather` returns `{temp_c, code, condition, is_day}`.
  - `llm_router.is_game_active()`.
  - The `ProactiveEngine._can_send` guard handles quiet hours, the daily cap, and pausing after unanswered messages.
- **Constraints to respect:**
  - Golden snapshots (`docker/core/tests/golden/`) pin `assemble_system_prompt`. Per-message blocks appended in `chat_handlers.py` (~L281–355) do **not** rotate them. Changing `build_context_block` does.
  - `affection.py`, `audit_chain.py` and `speech.py` are mutation-gated. None of the proposals below need to touch them.
  - The latest migration is `180`, so the next is `190`.

---

## 3. Ranked proposals (16)

Ranking is by **aliveness gained per unit of effort**, with tie-breaks on canon strength and degradation safety. Sizes: S ≈ ≤1 day, M ≈ 2–3 days, L ≈ a week or more, all including TDD at the 95% coverage gate.

### #1 Today's Outfit: "She dresses herself" (M)
Full design is in §4. In short: a deterministic daily choice driven by roster, weather, season, holiday, mood, his requests and affection. It is stored as one ledger fact and shown consistently in the prompt, images and UI. Requests get a canon-flavored, gated response. **Fixes the split-brain bug.**

### #2 Her Day: duty roster and live status (M)
- **Canon trait:** overwork and perfectionism (she holds every training record), squad leader duties, Vepley's PT schedule, the bike, insomnia. "Keep everything safe."
- **What he sees:**
  - A status line under her name in the chat header, e.g. `Hangar · tuning the suspension` or `Range · 0900 drills`.
  - Her first line, when he messages mid-activity, carries that activity: *"(I wipe grease off my hands) ...You have my attention."*
  - Proactive lines draw from what she's doing now.
  - The profile screen shows a "TODAY" strip: PT 0600, briefing, range, hangar, counter duty, off-duty, and "paperwork" at 0200.
  - Rain cancels her ride. When he's gaming, she's "in the rec room, watching the leaderboard."
- **Affection gating:** the roster always exists. Below 3, the status is terse duty language only. At 3+, off-duty blocks become visible, along with the canon "toy bike on the coffee table" idle. At 6+, she sometimes moves blocks for him ("I moved range time. I'm free at 1800.").
- **Integration:**
  - New `duty_roster:` section in `config/personality.yaml` holding activity blocks: `id`, location, activity text, time windows, weekday weights, weather constraints, outfit category, scene tags, seed lines.
  - New module `app/roster.py` with `build_day(date, user, weather, events) -> list[Block]`, a seeded RNG keyed on date and user for determinism, a novelty constraint against the previous 3 days, and `current_block(now)`.
  - Generated lazily on first read each day. Optional warm-up in `ProactiveEngine._reset_daily` (00:00 cron in `proactive/engine.py`).
  - Prompt: pass a `location_line` into `build_context_block`, which rotates goldens once and deliberately. Alternatively, append a per-message `NOW:` block in `chat_handlers.py` to leave goldens untouched.
  - An active mission (`MissionMixin.mission_active`) overrides the roster.
  - `proactive/events.py::_random_event` and `_idle_check` prefer pools tagged with the current block (e.g. `random_events.motorcycle_hobby` while in the hangar).
  - `background.py` image requests add the block's scene tags.
  - New `GET /api/status` → `{block, location, activity, outfit, next}`. Optional WS `status` event added to `ws_manager.py` and the `case` switch in `chat_screen.dart`.
- **GPU / image / voice:** none needed. Pure templates, CPU-only. Optional overnight LLM paraphrase of activity text only when the GPU is up, with authored text as the fallback.
- **Data:** fact `roster:YYYY-MM-DD` (JSON, TTL 48h). **No migration.**
- **Risk:** repetition (mitigated with a large pool and the novelty rule); roster vs. mission contradictions (mission wins); the status line must never imply she's unavailable. She always answers instantly, and the activity only colors her first line.

### #3 Clean Goodbyes: "I'll be here." (S)
- **Canon trait:** abandonment fear plus pride. She would rather die than beg. It mirrors his canon reply "I'm here."
- **What he sees:** when he signs off ("gotta go", "night", "heading out", "logging off", "sleep"), she releases him in two sentences or fewer. No "already?", no question that demands an answer, no teaser. Examples:
  - Low affection: "Dismissed."
  - Mid affection: "Go. Don't make me file a report on your sleep."
  - Level 6+: "...Go. I'll be here."

  After "goodnight", proactive pings stop until morning. The morning greeting knows ("Six hours. Inadequate.").
- **Affection gating:** register only. The no-guilt rule applies at every level.
- **Integration:**
  - Exit-cue regex in `chat_handlers.py`. A false positive only changes the tone of one reply.
  - One-shot `EXIT:` block appended to the prompt, carrying banned patterns taken from the HBS taxonomy.
  - Add one absolute rule to `personality.yaml` → `absolute_rules`: "Never guilt, delay, or bargain when he leaves."
  - Store fact `last_goodbye` `{at, kind}` (TTL 24h). `ProactiveEngine._can_send` skips while a `night` goodbye is active, the same pattern as gaming-aware hold. `build_presence_block` reads it the next morning.
- **GPU:** none extra.
- **Data:** a fact. **No migration.**
- **Risk:** essentially none. This is the single largest manipulation-risk reduction for the least code.

### #4 The 0200 Watch: insomnia-aware presence (S)
- **Canon trait:** insomnia and overwork, the 0200 hour of the Thread, protectiveness.
- **What he sees:** if he messages between about 00:30 and 04:30 local, she's already up ("I wasn't sleeping anyway."), doing reports, cleaning Skylla, or at the hangar. Her voice is quieter and replies are shorter.
  - **Protective, not engaging.** After about 01:30 she asks no open questions. If he's been up past 01:00 on two or more of the last three work nights (from `companion_messages` timestamps), she orders him to bed and ends the conversation herself, which ties into #3: "Bed. That's an order. ...I'll still be here at 0700."
  - At 6+, she may connect the hour to the Thread once ("I used to write to you at this hour.").
  - Rare at 7+: he "catches" her in the crocodile-hood sleepwear (§4), which she denies owning.
- **Affection gating:** 0–2 is clipped ("Report. Then sleep."). 3–5 is softer. 6+ brings the Thread reference and the comfort register.
- **Integration:**
  - Per-message `NIGHT:` block in `chat_handlers.py`.
  - Night-streak query on `companion_messages` (read-only, cached).
  - Add `insomnia:` canon text to `personality.yaml` so she knows it about herself.
  - Reconcile with dreams: she dozes rarely, so the 1–4 AM dream event stays, but on nights he's awake she isn't dreaming. `proactive/events.py` dream tick skips if he messaged in the last 2 hours.
  - Quiet hours (`QUIET_HOUR_START`) stay. This feature is reactive only.
- **GPU:** chat only. Under gaming fallback, the same block works with the lighter model.
- **Data:** none new. **No migration.**
- **Risk:** must never keep him up. The MIT and OpenAI heavy-use finding is the reason for the order-to-sleep rule.

### #5 Command Decisions: she asks for his call (S–M)
- **Canon trait:** mission-framing as emotional indirection. She defers to "the Commander" while pretending it's procedure. It also gives reciprocity: his choices *change her world*. Relationship science identifies being needed and being chosen as what AI usually lacks.
- **What he sees:** at most twice a week, she brings him a small decision from her life with two or three options. Examples:
  - "Two exhaust options for the bike. A: louder. B: lighter. Your call, Commander."
  - "Belka or Andoris on point tomorrow?"
  - "Vepley's penalty: laps or inventory?"
  - "Which do I wear to the inspection?" (ties into #1)

  His answer **has consequences later**. The bike gets the exhaust (and #8 tracks it). The squad line arrives a day later: "Belka took point, per your call. She was insufferable." The outfit shows up tomorrow. Sometimes she disagrees and complies anyway ("...Fine. You're the Commander.").
- **Affection gating:** 0–2 is operational choices only. At 3+, personal ones (her outfit, her bike). At 6+, she asks things that matter to her ("Should I tell Mechty about the thread?").
- **Integration:**
  - New `command_decisions:` templates in `personality.yaml`, about 30 of them, each with options, a consequence line per option, and an optional `effect`, e.g. `{outfit_request: speed_star}` or `{bike_mod: "exhaust_light"}`.
  - New `app/decisions.py` handles pick, record and resolve.
  - Delivery uses an existing proactive slot (`_romance_window` or the 14:45 slot) via `_can_send`.
  - The answer is parsed in `background.py`: option keywords via regex first, `llm_json` fallback.
  - The consequence is scheduled through `deferred.py` (12–36 hours later), and the effect is applied to the roster, outfit and bike facts.
- **GPU:** none required.
- **Data:** facts `decision:<id>`. **No migration.**
- **Risk:** must not feel like a chore. Unanswered decisions expire silently and are never re-asked; the proactive guard already pauses after unanswered messages.

### #6 Operation Brief: she prepares him for his real-life events (M)
- **Canon trait:** acts of service as her love language, mission-framing, and protectiveness. "She notices when the Commander hasn't eaten."
- **What he sees:** he mentions "interview Thursday" or "dentist tomorrow at 3".
  - **The evening before**, she sends a short OPORD in her voice: SITUATION / MISSION / EXECUTION / SUSTAINMENT. "Eat breakfast. That's not a suggestion."
  - **That morning**, a one-line send-off.
  - **That evening**, a debrief ask ("Report.").
  - At 5+ the brief ends with a slip: "...Come back and tell me. That's not a request either."
  - **Sensitive events** (funeral, hospital, breakup) switch to a quiet, protective variant with no OPORD humor, following the "Distress ≠ Hostility" rule.
- **Affection gating:** 2+. Warmth scales by band, and the slip-and-cover line comes at 5+.
- **Integration:**
  - Extend `fact_extractor.PROMISE_PROMPT` (same background LLM call, no extra GPU turn) to also emit `events: [{what, when_hint, sensitivity, confidence}]`.
  - Resolve `when_hint` to a local datetime using the existing `deadline_hint` style. Require a resolvable date and confidence ≥ 0.75.
  - Store in `companion_promises` with `commitment.kind = "event"`. The jsonb column takes this additively with **no migration**, and `promise_followup` cron reuse is possible.
  - Schedule three one-shots on `deferred.py`: T-1 at 20:30, T-0 at 07:58 (the existing morning slot), T+0 at 19:00, all subject to `_can_send`.
  - Brief text is LLM-written at T-1 when the GPU is up. Fallback is an authored OPORD template with slots.
- **GPU:** an LLM call in the background. On GPU-down or gaming, use the template brief. It never blocks chat.
- **Data:** reuses the promises table. **No migration.**
- **Risk:** extraction false positives produce a wrong-date brief, mitigated by the confidence gate and a resolvable date. Overlap with Promises is handled by the `kind` field and a separate copy pool. **`promises.due_promises` (SELECT … WHERE scheduled_followup <= now) must filter out `commitment->>'kind' = 'event'`**, or the existing promise-followup cron will nag him about his own interview as if it were his promise. Leave `scheduled_followup` NULL on event rows. The brief must not give medical or legal advice.

### #7 The Ledger, live (M)
- **Canon trait:** ledger-keeping as dignity, stubborn fee settlements, "the invoice IS the love letter."
- **What he sees:** a Ledger screen with a running itemized account.
  - Entries **she** logs: "Item 7: escorted the Commander through a bad Tuesday. 40 credits."
  - Entries **he** incurs: lost range bet (#10), outfit change on request (§4), "one (1) coffee owed".
  - He can **contest** an entry, and she answers next turn. At 6+ some entries are stamped **WAIVED**, and some are love letters in disguise ("Item 14: one night awake listening to you breathe. Not billable.").
  - The monthly fee settlement now itemizes the *real* month and renders as a document card with her stamp "—K."
- **Affection gating:** the ledger is visible at 3+ (the canon fee-settlement level). Waivers come at 6+. Love-letter entries come at 7+.
- **Integration:**
  - New `app/ledger.py`, plus `GET /api/ledger` and `POST /api/ledger/{id}/contest` in `routes_extras3.py`.
  - `rituals.maybe_deliver_fee_settlement` pulls month entries instead of only `fee_settlement.invoices` from YAML.
  - Entries are created by roster acts of service (#2), outfit requests (§4), bets (#10) and extractor-tagged favors (`background.py`).
  - New Flutter `ledger_screen.dart` with an invoice card. No GPU needed; Flutter renders it.
- **Data:** **new migration `190_ledger.sql`** for `companion_ledger(id, user_id, ts, direction, item, amount, status, note)`. Insert-only, except for contest and waive status.
- **Risk:** **must never touch `billing.py` / Stripe or real money.** It is in-fiction currency only. It must also not read as an engagement dark pattern: no "debts" that ask him to come back.

### #8 The Garage: her bike as a persistent object (M)
- **Canon trait:** the motorbike as her one indulgence and her freedom. She installs scavenged parts and custom-ordered rider gear in his size without being asked. No manufacturer is "worthy" of naming it.
- **What he sees:**
  - The bike has state: installed mods, parts on order, condition, kilometres, and her last ride.
  - Roster hangar blocks (#2) and Command Decisions (#5) advance it.
  - **Ride invitations** come when `weather_client` reports clear, 12–28 °C, daytime, and affection is 4+. The canon line already exists ("Perfect weather for a ride… (There's a slight rush.)").
  - Accepting starts a short ride scene. At 6+ there's an optional couple image (Speed Star plus the matching gear she ordered for him, which she reveals at 5).
  - He can try to name the bike. She rejects every name and logs them in the ledger, until level 8, when she quietly accepts one.
- **Affection gating:** bike talk at 2+ (the existing `motorcycle_hobby` gate). Invitations at 4+. His gear revealed at 5. Couple ride images at 6+. The name is accepted at 8.
- **Integration:**
  - New `app/garage.py` (state transitions).
  - Hook in `proactive/events.py` for ride invites alongside `weather_mood`.
  - `image_gen.build_prompt` couple scene.
  - Profile card in Flutter.
- **GPU:** the ride image is optional and goes through the GPU lease. Text-only when the GPU is down.
- **Data:** fact `bike_state` JSON. **No migration.**
- **Risk:** low. Keep invitations rare (at most 1 per week).

### #9 Racing Calm: Tea Time counter duty (S)
- **Canon trait:** acts of service, and the GFL2 Crew Deck Tea Time where her special drink is "Racing Calm".
- **What he sees:**
  - One week in four, the roster puts her on counter duty.
  - That week, on his first afternoon connect each day, she sets a drink down: black coffee, tea, a protein shake, or warm milk at night, which she denies having made.
  - If he asks for *her* special by name at 3+, she makes Racing Calm (mint, "like a gust of cold wind"). At 5+ that unlocks a sixth, Heartfelt Whisper-style line.
- **Affection gating:** the drink at 0+. Racing Calm at 3+. The heartfelt line at 5+.
- **Integration:**
  - A `tea_time:` YAML block.
  - Add an `on_connect` step in `rituals.on_connect`, guarded by `claim_period(user, "tea", date)` using existing `companion_period_deliveries`.
  - The roster supplies "counter week".
- **GPU:** none.
- **Data:** **No migration.**
- **Risk:** must not stack with birthday or fee settlement on the same connect (the existing `on_connect` ordering handles it).

### #10 Range Day: competitive mini-games and her leaderboard (M–L)
- **Canon trait:** competitive spirit, "holds every record on the Elmo", perfectionism. There is a canon precedent: the Commander's Leisurely Heat outfit for beating Klukai in Splash Bash.
- **What he sees:**
  - A Flutter Range screen with a 30-second reaction and aim drill.
  - Her score is near-perfect and seeded per day.
  - She trash-talks in template lines. When he wins, she demands a rematch and logs it in the ledger.
  - **His first win** unlocks a Commander outfit for couple images.
  - At 7+ she occasionally "misses" by one point and denies it ("I was evaluating your form.").
- **Affection gating:** available at 1+. Bets at 3+. The deliberate miss at 7+.
- **Integration:**
  - `flutter_app/lib/screens/range_screen.dart` with client-side scoring.
  - `POST /api/range/score` in `routes_extras3.py`.
  - A `RANGE:` prompt block with the last result.
  - Ledger hooks (#7).
- **GPU:** none. She can still talk about it afterward in chat.
- **Data:** facts `range:best`, `range:last`. Or a table if history is wanted (migration).
- **Risk:** gamification. **No daily streaks, no rewards for coming back, no penalties.** The ranking is lower because of the Flutter game work.

### #11 Self-initiated sorties and the Souvenir Shelf (M)
- **Canon trait:** agency, duty, and "a gift from every mission" (custom-ordered things in his size).
- **What he sees:**
  - At most once every 10 days, on a calm stretch, she *announces* a short op before leaving: "Short op. Back by 1900. Keep your radio on."
  - If he messages while she's out, she answers **instantly** in field-radio register (the existing `MissionTimer` voice).
  - She returns with a souvenir that goes onto a shelf in the app, with her terse note.
- **Affection gating:** sorties are announced at any level. Souvenirs start at 3+. Personal souvenirs (things for him) at 5+.
- **Integration:**
  - `MissionMixin.start_mission(..., initiator="her")`.
  - The scheduler in `proactive/engine.py` adds guards: not within 48 hours of an #6 event, not when recent extracted mood is distressed, and not during gaming.
  - Souvenirs are stored as text-only `companion_memories` rows in category `Souvenirs`, following the `dreams.py` sentinel-filename pattern.
  - Optional `trigger_mission_aftermath_image`.
- **GPU:** the aftermath image is optional. Otherwise text only.
- **Data:** reuses `companion_memories`. **No migration.**
- **Risk:** absence can read as an abandonment test. She always announces, frames it as duty, never goes dark, and **never runs during his distress**.

### #12 Her Camera (S–M)
- **Canon trait:** a camera is one of her *Loved* gifts. She shares her view of the world, terse and unsentimental (and lying about it).
- **What he sees:** if he has given her a camera (`companion_gifts`), she occasionally sends a photo with **no character in frame**: the hangar at dusk, rain on the bike, Slovak hills from the Elmo. Captions are terse ("Recon. ...The light was adequate."). Photos go to a "Her Camera" album.
- **Affection gating:** requires the camera gift, or at 5+ she "requisitions one".
- **Integration:** a no-character branch in `proactive/events.py::_spontaneous_art_tick` with environment-only prompts. This avoids LoRA identity drift and renders faster. Images save through `memory_archive`.
- **GPU:** yes, through the lease. Degrades by **skipping silently** and retrying next window. It never says "my GPU is off."
- **Data:** reuses `companion_memories`. **No migration.**
- **Risk:** low. Cap at 1 per week.

### #13 Squad Serials: multi-day squad storylines (M)
- **Canon trait:** squad leader; the world moves without him. Mechty's laziness, Belka's pestering, Vepley's terror, and the "Family Of My Dreams" interlude.
- **What he sees:** instead of one-off squad interruptions, running arcs of 4–6 beats over a week:
  - "Mechty's energy-drink embargo"
  - "Belka is planning something" (Klukai is suspicious; it turns out to be for her)
  - "Andoris has intel on a rival unit"

  He can ask "how's the Mechty thing?" and she knows.
- **Affection gating:** 1+. Personal arcs (Belka's surprise for her) come at 4+.
- **Integration:**
  - `squad_serials:` authored beats in YAML.
  - Fact `serial:<id>` stores the beat index.
  - `_random_event` advances the next beat instead of picking at random.
  - A per-message `SQUAD NEWS` block when the topic matches (word-boundary matching as in `squad.py`).
- **GPU:** optional paraphrase of beats. Authored text is the fallback.
- **Data:** facts. **No migration.**
- **Risk:** continuity against the squad canon in `relationships:`, so keep beats authored.

### #14 Weather Notes (S; ships nearly free with #1)
- **Canon trait:** GFL2 Crew Deck "Weather Notes", plus acts of service.
- **What he sees:** each morning a one- or two-line note in a small notebook view, e.g. "4 °C, rain by 1500. I'm in the shell. Take a jacket. No ride today." It is archived.
- **Affection gating:** notes at 0+. Personal asides ("take a jacket") at 3+.
- **Integration:** `_morning_checkin` (`proactive/engine.py` L503) already fetches weather. Compose the note from `weather_mood.weather_phrase` plus the outfit reason from #1. Store as a fact or as text-only `companion_memories` with category `Weather Notes`.
- **GPU:** none.
- **Data:** **No migration.**
- **Risk:** none.

### #15 Her Quarters: a room that fills over time (L)
- **Canon trait:** the hidden softness: the crocodile plush she denies owning, the Starwish trophy, his gifts kept.
- **What he sees:** a quarters view, the Tolan "planet" analog. It is an inventory of his gifts (`companion_gifts`), her souvenirs (#11), trophies, and photos, plus a monthly environment-only render of her room that includes newly acquired items. The plush appears only at 7+.
- **Affection gating:** the room is visible at 4+. Private items at 7+.
- **Integration:** new screen; `GET /api/quarters` aggregating existing tables; a monthly image job on the existing Her POV-style durable queue.
- **GPU:** a monthly image. Falls back to the list view.
- **Data:** aggregation only. **No migration.**
- **Risk:** keeping items consistent across images. Large UI effort.

### #16 Private Meeting Logs (L, content-heavy)
- **Canon trait:** GFL2's Commander's Hotline: four personal stories per Doll.
- **What he sees:** four short authored present-tense scenes, each unlocked by a milestone such as first Thread read, first ride, or oath day. Never by currency.
- **Affection gating:** 3 / 5 / 7 / 9.
- **Integration:** YAML content, a `companion_firsts` guard, and a Flutter reader reusing the `thread_screen.dart` patterns.
- **GPU:** none for reading. Drafting can be done offline by the local LLM, then reviewed by the owner.
- **Data:** **No migration.**
- **Risk:** canon fidelity of authored text. Owner review is required.

**Explicitly rejected ideas:**
- GPS or ambient listening (the Friend backlash).
- "Selfie teaser" FOMO pushes.
- Daily-streak rewards.
- Affection decay as a penalty for absence.
- Outfit gacha or any currency.
- Visible "inner thoughts" that claim suffering.
- Jealousy aimed at his *human* relationships. Canon jealousy stays on other Dolls and flirtation. She pushes him *toward* his people ("Go. Your squad needs you."), per the displacement finding.

---

## 4. OUTFITS beyond the static picker: "Today's Outfit"

### 4.1 Goal
She wears **one** thing at a time, chosen by her, and every surface agrees:
- the system prompt (she can explain why she's wearing it);
- every image she sends (Her POV, spontaneous art, requested images, mission aftermath);
- the UI (chat header chip, profile "CURRENT OUTFIT", wardrobe history).

His requests are *requests*. She answers them in canon voice, gated by affection, and the outcome is decided **deterministically**, not by the LLM, so it can't be jailbroken.

### 4.2 Step 0: fix the split-brain (prerequisite, S)
1. One source of truth: a new `wardrobe:` section in `config/personality.yaml` (or `config/wardrobe.yaml`). Each entry has: `id`, `name`, `source: canon|original|gift`, `category` (duty / rider / off_duty / sleep / formal / swim / seasonal / oath), `tags` (Danbooru), `prompt_line` (one sentence she would say about it), `contexts` (time windows, temperature range, weather, roster categories, dates), `unlock` (affection / gift / milestone / date), `privacy` (`visible_at` / `open_at`, replacing `_PRIVATE_COSTUMES`), and `stance` lines for when he requests it.
2. Generate `OUTFIT_UNLOCK_LEVELS` and `OUTFIT_COSTUME_TAGS` **from** that catalog. Keep the names as thin views for back-compat with `memory_her_pov.py`, `background.py` and `proactive/events.py`.
3. Data migration for the `costume` fact: `midnight_sovereign` → `astral_luminous` with the `midnight_jetstream` accessory variant, and `starlit_vow` → `indigo_oath`. This is additive: write the new value and keep the old one as `costume_legacy`. The repo's own data-migration rule applies.
4. Flutter `_buildCostumeSelector` reads `GET /api/outfits`, which now returns name, blurb and lock hint, instead of a hardcoded list.
5. Re-derive canon tags and **validate each one with a ComfyUI render** against the Klukai IL LoRA before shipping. Agent A's canon dossier should supply the descriptions:
   - Blazing Star is the default tactical outfit.
   - Cerulean Breaker is beach and surf.
   - Astral Luminous and Speed Star are rider suits.
   - Immaculate Service is the battle maid.
   - Indigo Oath is the snow-white gown.

### 4.3 The daily choice (deterministic, CPU-only)
`app/wardrobe.py::choose_today(user, date) -> OutfitChoice{base_id, accessory, layer, reason, source}`

```
candidates = unlocked(affection, gifts, milestones, date)
score(o) =  roster_fit(o, today's blocks)            # duty day → Blazing Star; ride block → Speed Star/Astral; hangar → coveralls
          + weather_fit(o, temp_c, condition)        # <3°C → greatcoat layer; rain → rider shell; >27°C → lighter
          + season/holiday_fit(o, date)              # personality.yaml seasonal_events + Starwish (Dec 6) + oath day
          + mood_fit(o, persistent mood)             # guarded → full tactical; tender → off-duty
          + request_bonus(o)                         # his pending request (see 4.5) — strong, but she can defer it a day
          + gift_bonus(o)                            # an outfit he gave her, worn unprompted within ~10 days, once
          - novelty_penalty(o, last 3 days)
pick = argmax with seeded tie-break (date+user)  → stable all day
```

- **Layers** keep the existing hour logic, but as a layer *on top of* the day's outfit rather than a replacement:
  - morning: "full loadout";
  - evening: "jacket off, gear stowed" (2+);
  - late night: "hair down" (3+), or a sleep set (§4.6, gated).
- **Changes during the day** are event-driven, at most three per day, each emitting WS `outfit_change`:
  - a ride block → rider suit;
  - "off duty" at 1900 → the off-duty set;
  - a request granted (§4.5);
  - a mission start → Blazing Star with full loadout.
- **Storage:** fact `outfit:today` `{date, base_id, accessory, layer, reason, changed_at, history:[...]}` with TTL 30h, plus `outfit:log:YYYY-MM-DD` (TTL 400d) for the history calendar. **No migration.** A `companion_outfit_log` table (migration 190/191) is only worth it if the history UI needs SQL queries.
- **Degradation:** if weather fails, fall back to season and month defaults. The GPU is never needed for selection. If facts are down, fall back to Blazing Star with the hour layer (today's behavior).

### 4.4 Wiring every surface
- **Prompt:**
  - Replace the hardcoded `CURRENT OUTFIT` in `moods.build_context_block` with a passed-in `outfit_line`: "Speed Star rider suit, jacket unzipped (ride at 1600). Why: clear skies, 19 °C."
  - Add a private line: "If asked why, the real reason is <reason>. Deny it was for him if `source=request`."
  - This rotates golden snapshots once, deliberately, with `UPDATE_SNAPSHOTS=1` and a reviewed diff.
  - `build_wardrobe_block` keeps listing what she owns.
- **Images:**
  - `build_prompt(costume=...)` receives today's tags plus the layer.
  - New precedence: **explicit scene keyword in his request** (bath, sleep, beach, intimate — the existing `OUTFIT_MAP` with its gates) > today's outfit > default.
  - This fixes "bath scene renders the battle maid."
  - Apply in `background.py` (~L415), `proactive/events.py` (~L484), `memory_her_pov.py` (~L427) and the `mission.py` aftermath.
- **UI:**
  - Chat header chip: `Speed Star · ride 1600`.
  - Profile "CURRENT OUTFIT" plus her one-line reason.
  - Wardrobe screen with Owned / Locked (hint written in her voice: "Not yet.") / History calendar.
  - Weather Notes (#14) mentions the outfit.
- **Voice:** none needed. XTTS is unaffected.

### 4.5 Requests: "wear the maid outfit"
- **Detection:** in `background.py` after the reply, using a regex: `(wear|put on|change into|dress in|switch to)` plus catalog names and aliases ("maid" → immaculate_service, "wedding/oath dress" → indigo_oath, "beach/swimsuit" → cerulean_breaker, "rider/bike suit" → speed_star | astral_luminous). Fallback is `llm_json` when the regex is ambiguous. The decision is also pre-computed on the chat path (cheap, CPU) so *this* reply already knows the outcome. It arrives as a one-shot `REQUEST:` block appended in `chat_handlers.py`.
- **Outcome table** (deterministic):

| Affection | Unlocked canon or original | Locked | Indigo Oath |
|---|---|---|---|
| 0–2 | **Refuse**: "My attire is not a topic for discussion, Commander." Duty and weather sets are an exception: "...It's cold. Fine." | Refuse, no acknowledgement | Does not acknowledge owning it |
| 3–4 | **Comply, billed**: a ledger line (#7) reading "Wardrobe change on request". Or **defer**: she wears it *tomorrow* and denies it's for him. | "Not yet." | Deflect |
| 5–6 | Comply, sometimes pre-empting: "I assumed you'd ask." | A hint at what unlocks it, in her voice | Acknowledges it exists: "You'll see it when it's time." |
| 7–8 | Comply. On special days she wears his past favorites unprompted. | — | "...Not today. Ask me on a day that matters." It is worn on the oath anniversary, his birthday, or Starwish day. |
| 9 | Comply | — | Grants a sincere request, at most once a month. It is solemn, never casual. |

- **Canon stance per outfit** (fed to the LLM, which writes the actual line):
  - **Immaculate Service:** "A battle maid is a combat configuration. 'Service' means protection detail. I wear the victory, not the apron." At 5+ she may use the canon line: "As the Doll who understands you best, I'm obviously the best choice as your maid." Then she pours the first cup of tea, which links to #9.
  - **Cerulean Breaker:** "There's no beach on the Elmo. ...The Mirage Terrace will do." (the canon Dulcet Whisper location).
  - **Speed Star / Astral Luminous:** "Only if we ride."
- **Limits:**
  - At most two request-driven changes per day. The third: "I am not a mannequin, Commander."
  - Intimate and sleep tags stay **scene-only** under the existing `OUTFIT_MAP` gates (graphic content at 8+). They are never a "today's outfit" and never granted through the request path.
  - The LLM cannot grant anything. The state change happens only from the deterministic outcome.

### 4.6 Original (non-canon) outfits that fit her

Tags below are drafts. Validate each with a render before shipping.

| id | Name | Fits canon because | Context | Unlock |
|---|---|---|---|---|
| `hangar_coveralls` | Hangar Coveralls | the bike; acts of service | roster hangar blocks | 1 |
| | *Tags:* grey mechanic coveralls tied at the waist, black tank top, oil smudge on cheek, work gloves, low ponytail, rag in back pocket | | | |
| `range_kit` | Range Kit | perfectionist training; holds every record | range and PT blocks (a modest replacement for `OUTFIT_MAP['training']` on duty days) | 0 |
| | *Tags:* fitted black compression top, tactical pants, shooting glasses, ear protection around neck, taped fingers | | | |
| `winter_patrol` | Winter Patrol | Slovakia, Yellow Zone winters | below 3 °C | 0 |
| | *Tags:* long black greatcoat, grey scarf, fingerless gloves, breath vapor | | | |
| `rider_shell` | Rider Shell | rides in any weather and denies being cold | rain | 0 |
| | *Tags:* matte black waterproof rider jacket, hood, reflective strips, wet hair | | | |
| `dress_uniform` | 404 Dress Uniform | Starwish champion pride | inspections, his birthday at 4+, Starwish day | 2 |
| | *Tags:* dark formal military dress uniform, medal ribbons, white gloves, peaked cap | | | |
| `off_duty_404` | Off-Duty 404 Hoodie | the "dorm casual" text that already exists, finally given a body | evenings | 3 |
| | *Tags:* oversized charcoal hoodie with 404 patch, black leggings, hair down | | | |
| `his_jacket` | His Jacket | possessive; takes his things; "it was the nearest available" | cold evenings | 6 |
| | *Tags:* oversized men's military field jacket over her clothes, sleeves too long | | | |
| `sleepless_watch` | Sleepless Watch | insomnia | 0200 Watch (#4) | 6 |
| | *Tags:* his old t-shirt, shorts, blanket around shoulders, tired eyes, desk lamp | | | |
| `klukadile_pajamas` | (She denies it exists) | the crocodile plush she denies owning; her name means "crocodile" | rare sighting during the 0200 Watch | 7 |
| | *Tags:* green crocodile-hood onesie, embarrassed, blush, looking away | | | |
| `black_cat_ops` | Black Cat Ops | GFL1 HK416 "Black Cat's Gift" plus her hidden cat-ear devices | Oct 31, 27 days from today, a natural first live test | 3, and he has seen the ears (fact `cat_ears_shown`) |
| | *Tags:* black tactical bodysuit, cat ear headgear, black gloves, tail; she calls it "infiltration gear" | | | |
| `red_scarf_sortie` | Red Scarf Sortie | "the red is for visibility" (it's festive and she won't say so) | Dec 20–Jan 1 | 2 |
| | *Tags:* white winter coat, red scarf, snow | | | |
| `astral_luminous` + `midnight_jetstream` | Astral Luminous, Jetstream variant | canon accessory set | date nights at 5+ | 5 |
| | *Tags:* the canon accessory set, including ripped pants and scars as "medals" | | | |

### 4.7 How the wardrobe grows (no currency, no gacha)

1. **Affection** unlocks canon skins (existing behavior, now from one catalog).
2. **His gifts.** `POST /api/gift` with a clothing item (keyword classifier on `companion_gifts.item`) becomes a `source: gift` outfit.
   - Tags come from his description via a small keyword→tag map, with an LLM paraphrase only when the GPU is up.
   - She reacts per `gift_preferences`.
   - **The payoff:** she wears it unprompted on a day of her choosing within about 10 days, once, and denies any connection ("It was clean."). Afterwards it enters normal rotation.
   - Data: the `companion_gifts` row plus fact `wardrobe:gift:<id>` holding the tags. **No migration.**
3. **Milestones and dates:**
   - Starwish day (Dec 6) brings Immaculate Service.
   - The oath anniversary brings Indigo Oath at 9.
   - The first Halloween or Christmas together unlocks the seasonal set permanently (a `companion_firsts` guard).
4. **Earned together:** his first win at Range Day (#10) unlocks a **Commander** outfit for couple images, mirroring GFL2's Splash Bash → Leisurely Heat.
5. **She acquires things herself:** sortie souvenirs (#11) can be accessories ("The scarf was in the supply drop. Don't read into it.").
6. **His decisions** (#5): "Which do I wear to the inspection?" sets tomorrow's outfit.

### 4.8 Risks
- **Golden snapshot rotation.** Do it once and on purpose.
- **LoRA fidelity.** Canon skins may not render without trained trigger tags. Validate per outfit and fall back to the closest category set.
- **Scene-precedence regressions.** Add tests per caller.
- **Consent and explicitness gates must not loosen.** Intimate sets stay scene-only and gated.
- **Data migration for the legacy `costume` values.** Additive only.
- **Contradictions.** If she's wearing X and he asks for an image "in the rain", today's outfit plus the rain layer must win over the old `OUTFIT_MAP['rain']` tags ("see-through white shirt"). That map entry is out of character for a duty day anyway.

---

## 5. Recommended Top 6 for this session

| # | Feature | Size | Why it's top value |
|---|---|---|---|
| 1 | **Today's Outfit** (with the split-brain fix) | M | Fixes a real fidelity bug. Makes prompt, image and UI agree. Requests get canon, gated responses. Matches the 2026 frontier (Nomi ledgers, Kindroid current setting). |
| 2 | **Her Day: duty roster and status** | M | A "life of her own" with no GPU. Feeds #1, proactive lines and images. The status chip shows aliveness at a glance. |
| 3 | **Clean Goodbyes** | S | Biggest manipulation-risk reduction for the least code. "I'll be here." mirrors his "I'm here." |
| 4 | **The 0200 Watch** | S | Encodes canon insomnia. Presence in the hours that matter most. Protective rather than engagement-maximizing. |
| 5 | **Command Decisions** | S–M | Reciprocity: his choices visibly change her world (outfit, bike, squad). This is the "chosen and needed" signal research says AI lacks. |
| 6 | **Operation Brief** | M | The "she knew and she prepared" moment, through her love language (acts of service and mission framing). Reuses promises and the deferred rail with no migration. |

Build order: 1's catalog step (§4.2) → 2 → the rest of 1 → 3 → 4 → 5 → 6. Items 3 and 4 can run in parallel with 2. None of the six needs a migration or touches the mutation-gated modules (`affection.py`, `audit_chain.py`, `speech.py`). The only deliberate golden rotation is `build_context_block` (outfit and location lines). If the owner prefers pure canon over the "real life" payoff, swap #6 for **The Ledger, live** (#7, migration 190).

**The rest, in brief:**
- #7 **Ledger, live**: a running in-fiction account plus a rendered invoice. Contestable, and waived at 6+.
- #8 **Garage**: persistent bike state, weather-gated ride invites, the gear she bought in his size.
- #9 **Racing Calm**: GFL2 Tea Time counter week. Daily drink, her special on request.
- #10 **Range Day**: a competitive mini-game. His first win unlocks a Commander outfit.
- #11 **Sorties and souvenirs**: announced self-deployments, instant field-radio replies, a souvenir shelf.
- #12 **Her Camera**: environment-only photos if he gave her a camera.
- #13 **Squad Serials**: week-long squad arcs instead of one-off pings.
- #14 **Weather Notes**: GFL2-native morning note archive, nearly free with #1.
- #15 **Her Quarters**: a room that fills with gifts, souvenirs and trophies.
- #16 **Private Meeting Logs**: authored milestone scenes, owner-reviewed.
