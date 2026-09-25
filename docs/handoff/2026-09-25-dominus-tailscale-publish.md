# Handoff: Dominus power-on must bring the RTX 3090 publishes back

Date: 2026-09-25. For Claude and Codex. Keep reviewing this. Do not treat a green container healthcheck as proof that Amarillo can reach her.

## Current handoff state

At the end of the 2026-09-25 review, Dominus was reachable from Amarillo on
`100.107.121.5:1234`, `:8301`, and `:8390`; the gateway health endpoint returned
`status: ok` and `upstream: ok`. Game mode was inactive, the gateway GPU lease
was inactive, and `nvidia-smi` showed 0% GPU utilization. The idle chat model
entered llama.cpp's sleeping state after 298 seconds with no request. Keep the
existing five-minute model residency ceiling; it satisfies the requested
ten-minute maximum and gives the GPU back sooner.

The publish repair has been corrected and installed at both live executable
paths listed below. A dry run returned `noop-healthy`. No AI containers or
Docker daemon were restarted during this review. Local source and review tests
are in this checkout; the RAID source tree remains a separate older snapshot.

For the next operator: preserve the game marker gate, lazy model loading, and
local-only inference. On the next ordinary power cycle, check the three host
listeners from Amarillo. During a game, confirm the marker exists and the
gateway publish remains closed; after the game, use the canonical systemd unit
to restore the stack. See “What to review next” for the exact checks.

## What broke

Turning off `dominus-nobara` (the computer with the RTX 3090) is normal. On the boot at 09:45 CDT, dockerd restored the `restart: unless-stopped` AI containers before `tailscale0` had `100.107.121.5`. Docker logged:

`failed to bind host port 100.107.121.5:1234/tcp: cannot assign requested address`

and the same for `8301` and `8390`. The containers stayed up. Their healthchecks hit `127.0.0.1` inside the container, so Docker reported healthy. `NetworkSettings.Ports` was null. From amarillo, `100.107.121.5:1234` was connection refused. companion-core logged `LM Studio available: False` and `No local LLM backend available`.

The GPU was fine. No game marker was set. Tailscale to dominus was up by the time we looked. `docker restart` did not bind the ports. `docker compose up --force-recreate --no-deps` of `lmstudio-compat`, `companion-voice`, and `speaches`, run after the address existed, did.

`dominus-ai-stack.service` already refuses to start until `tailscale ip -4` prints `100.107.121.5`. That check never ran on the failure path. Docker's own restart policy had already started the containers, and a later `compose up` does not recreate a running container with an empty publish.

## What changed

- `ops/dominus-nobara/scripts/repair-tailscale-publish.sh`
  - No-ops while `/run/user/1000/dominus-gpu/game-active` exists.
  - No-ops when the Tailscale address, the RAID mount, or Docker is missing.
  - The timer no-ops while `dominus-ai-stack.service` is `activating`, so it does not race game-end.
  - `--from-unit` is the stack unit's own second `ExecStart`, and it does repair during that window.
  - Recreates only the missing publishes: `1234` gateway, `8301` voice, `8390` speaches. It does not recreate `llama-router` or `comfyui`.
  - `--wait-for-address` waits up to 60 seconds, then exits 0. A Tailscale outage must not wedge the rest of Docker.
- `systemd/dominus-publish-repair.timer` runs that script 45 seconds after boot and every 30 seconds.
- `systemd/docker.service.d/20-tailscale-address.conf` is the dockerd `ExecStartPre` for the wait. It does not restart Docker when installed.
- Tests: `ops/dominus-nobara/tests/test_repair_tailscale_publish.py` and the stack-unit assertion in `test_compose_contract.py`.

Installed on dominus for the next power-on (the RAID tree `/mnt/nvmer0/services/ai-stack/source/klukai` is an August snapshot, not this git checkout; the live unit lives in the user systemd directory):

- `/home/jalsarraf/.local/bin/dominus-repair-tailscale-publish`
- `~/.config/systemd/user/dominus-ai-stack.service` (repair is the second `ExecStart`; the running process was not restarted)
- `~/.config/systemd/user/dominus-publish-repair.{service,timer}` enabled
- `/usr/local/sbin/dominus-repair-tailscale-publish`
- `/etc/systemd/system/docker.service.d/20-tailscale-address.conf` (daemon-reload only; Docker was not restarted, so the wait applies on the next Docker start)

The previous user unit was copied to `dominus-ai-stack.service.bak-20260925` beside it. A live dry-run on dominus printed `noop-healthy`, and a real run left the gateway, voice, and speaches start times unchanged. `100.107.121.5:1234`, `:8301`, and `:8390` were listening.

## Reviewed and left alone

- companion-core on amarillo. `LLMRouter` rechecks a down gateway after 15 seconds and fails closed with no cloud fallback. It recovered on its own once `1234` was listening (`LM Studio recovered after 743s`). Do not add a cloud path. Do not restart core just because dominus was off.
- GameMode. `game-start.sh` creates the marker before it stops the stack. `unless-stopped` containers that were explicitly stopped stay stopped across a Docker restart. The repair script and the timer condition both refuse to run while the marker exists. Do not "help" by starting the stack during a game.
- Chat model when no game is active: `cognitivecomputations_dolphin-mistral-24b-venice-edition`. Gaming model stays `huihui-gemma-4-26b-a4b-abliterated`. The router had `model_path=none` while the port was down; Venice loads on the next real turn.

## What to review next

1. Next time dominus is powered off and back on, `ss` should show `100.107.121.5:1234`, `:8301`, and `:8390` without anyone recreating containers by hand. Allow the 60 second Docker wait plus one timer pass.
2. Start a game and confirm `1234` stays closed and the timer does not recreate the gateway. End the game and confirm the canonical unit brings the stack back, still with no model loaded until a real request.
3. Do not replace the recreate with `docker restart`. That was tested on 2026-09-25 and left `NetworkSettings.Ports` null.
4. Do not add `llama-router` or `comfyui` to the repair list.
5. The address `100.107.121.5` is intentional and checked by `verify-compose-contract.py`. A change has to update compose, the repair script, and the stack unit together.
6. `docker.service` was not restarted to pick up the new drop-in. `systemctl cat docker` shows it only after the next daemon-reload; it takes effect on the next Docker start. Do not restart Docker while she is in use just to prove the drop-in.
7. `web-build/index.html` may be dirty locally from the PWA deploy that hashed `flutter_bootstrap`. That is unrelated. Do not mix it into this fix.

## Prove a quiet repair

```bash
DOMINUS_PUBLISH_REPAIR_DRY_RUN=1 \
  /home/jalsarraf/.local/bin/dominus-repair-tailscale-publish
# expected while she is up: noop-healthy
# expected during a game: noop-game
```

## Follow-up review: idle residency and repair guards

- Owner requests GPU model release within 10 idle minutes. The live router
  already uses `--sleep-idle-seconds 298`, and gateway health advertises 300
  seconds. Router logs confirmed entry into sleeping state after 298 seconds;
  keep this stricter limit. No keepalive or cloud fallback was added.
- Fixed publish checks accepting port `12340` as evidence for `1234`.
- Added a game-marker recheck immediately before Compose recreation, after
  acquiring the repair lock. This catches game starts during earlier checks;
  it does not make the marker check and Docker operation atomic.
- Installed the reviewed script into both existing live executable paths,
  without restarting containers. Prior copies are in
  `/tmp/dominus-publish-review.RJDtShe8/` on Dominus (temporary backups).
- Verification: 31 operations tests, 7 GPU guard tests, and 48 gateway tests
  passed. Live repair dry-run returned `noop-healthy`; gateway health was OK.
- Runbook section 8 now enables and starts `dominus-publish-repair.timer`
  after the stack unit. The live timer was already enabled; this closes the
  gap for the next install. A condition skip does not stall `OnUnitActiveSec`
  on systemd 259: the timer stayed `waiting` and resumed after the marker
  condition cleared.
