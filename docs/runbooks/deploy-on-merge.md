# Deploy-on-merge

Once activated, Klukai deploys itself. The flow is: an approved PR is merged
into protected `main`; full CI passes for that exact merge commit; amarillo
deploys that commit; health verification passes, or production is rolled back.

Nothing here is active until the owner completes **Activation** below.
Preparing the pipeline does not authorize merging, deploying, changing branch
protection or creating credentials.

## How it works

The deployer is pull-based. A systemd user timer on amarillo
(`klukai-deploy-on-merge.timer`, every two minutes) runs one cycle of
`ops/amarillo/scripts/klukai-deploy-on-merge.py run-once`:

1. **Candidate.** The current head of `main`, read with a read-only token.
2. **Order.** The candidate must descend from the last deployed commit. An
   older commit, or a rewritten history, is never deployed, so an older run can
   never overwrite a newer release.
3. **Approval** (optional; on after activation). The candidate must be the
   merge commit of a PR approved on its final head by an allowed reviewer. A
   self-approval or a direct push does not count.
4. **Certification.** There must be a successful Klukai CI run for exactly
   this commit in which every full gate passed: Flutter VM and Chrome tests,
   `dart format`, `flutter analyze --fatal-infos`, the production web build,
   core tests, real backends, lint, mypy, security, the twice-built and
   Trivy-scanned image, and the perf gate. "Required CI gates" runs only for
   full plans. Every push to `main` is now planned full (`ci.yml`), so a
   selective or skipped check can never certify a release.
   - While CI is running, the deployer waits.
   - If CI failed, the commit is held and the owner gets one bus note. Each
     cycle re-checks, so a green re-run deploys it.
5. **Artifacts.** These steps change nothing in production. The core image
   (when `docker/core`, `docker-compose.yml` or `config/` changed) and the CI-built PWA are
   read from amarillo's own CI tree (`/docker/klukai/ci-artifacts`). The image
   is built on amarillo. A PWA built on Dominus arrives through
   `klukai-ci-pull`, and the deployer never reads Dominus directly.
   - Each is checked against the digest that run printed in its own GitHub
     job log:
     - `KLUKAI_RELEASE_MANIFEST_SHA256` for the image;
     - `KLUKAI_PWA_SHA256` for the PWA.
   - Every file is verified.
   - The PWA gets the deterministic content-hash transform
     (`tools/ci/web_release.py`).
   - Both are signed with the amarillo release key.
6. **Re-check.** If `main` moved meanwhile, this cycle stops; the next one
   takes the newer head. Cheap local checks run before any artifact is copied,
   and refusals are retried with growing gaps (1, 2, 4 … 30 minutes). Those
   checks are:
   - the checkout is detached;
   - no local file would be overwritten (ignored files included);
   - any migration or compose change has reviewed recovery plans
     ([docs/releases/recovery](../releases/recovery/README.md)) and is the
     merge of an approved PR.
7. **Production.** The deployer never cancels this part.
   - **Checkout.** It moves the production checkout `~/git/klukai` (detached)
     to the commit.
   - **Core.** It runs `klukai-release.py deploy`:
     - checks the signature;
     - takes a fresh `pg_dump` and restore-verifies it in an isolated database;
     - rolls out companion-core;
     - requires 180 s to ready, a 180 s hold and the 5xx gate;
     - rolls back automatically on any failure.
   - **Web.** It runs the atomic web release (below).
   - **Post-deploy checks.** It confirms readiness, then runs `scripts/e2e_live.py`
     checks `health,auth-required,bad-token,authed-read`. These run as the
     `claude` test user, are read-only and send no chat.
   - **Any failure.** It puts back the previous web release, then the core
     image, then the checkout.
8. **Record.** The deployer writes:
   - `~/.local/state/klukai/release/deployer/state.json` (the deployed commit,
     the outcome per commit, `inflight`);
   - `deployments.jsonl`;
   - a receipt per attempt;
   - a bus note to `owner`.

### Serialization, stale runs and retries

- A deployment lock, `~/.local/state/klukai/release/deploy.lock` (flock), is
  shared by the deployer, `klukai-release.py deploy` and
  `klukai_web_release.py`. A second deployment is refused rather than queued.
  A manual deploy can never interleave with an automatic one.
- Merges during a deployment do not cancel it. The running deployment finishes
  its commit, and the next cycle deploys the newest certified head.
  Intermediate commits are skipped. CI's own `cancel-in-progress` affects only
  CI runs, never a deployment.
- SIGTERM (shutdown, `systemctl stop`) is a stop request. The deployer
  finishes the deployment in progress, or restores it if a step fails, then
  exits. It never kills docker compose, pg_dump or git halfway.
  `TimeoutStopSec=20min` bounds the wait.
- If a run is killed outright, the next cycle first restores the recorded
  previous state (`inflight` in `state.json`, written with fsync): the web
  release, then the checkout, then the core image and its pin. If that
  restore cannot be completed (a left-over `.git/index.lock` is the usual
  cause), the deployer pauses, keeps the record and sends one loud note.
- A commit gets at most two production attempts. After an ordinary failure
  (`rolled_back`, `web_rolled_back`, `verify_failed`) it is held until a newer
  commit arrives or the owner runs `retry`.
- `ROLLBACK_FAILED` and `RESTORE_FAILED` pause the whole deployer at once.
  Nothing more is deployed, not even a newer commit, until the owner checks
  and runs `resume`.
- `pause` needs no lock. It works even while a deployment is running, and
  takes effect from the next cycle.
- A CI run whose failed jobs were re-run with "Re-run failed jobs" mixes
  artifacts from two attempts, and the relay refuses it. Use "Re-run all
  jobs".

### Atomic web releases

companion-core serves `/app` and `/` from `/app/static`. After activation that
is a read-only bind mount of `~/.local/share/klukai/web`:

```
.releases/<id>/              one complete verified release each
.current -> .releases/<id>   the live release
<name> -> .current/<name>    one link per top-level entry
```

- **Switching.** A release switches by renaming a new `.current` over the old
  one. That is one atomic step, so a browser never sees a mix of two releases.
  Each new release also carries the previous release's two content-hashed
  entry bundles, so a page that loaded the old `index.html` a moment before
  still gets its bootstrap.
- **Verification.** After the switch, the entry files are fetched through core
  (`127.0.0.1:8300`) and must match the signed digests, including the
  `immutable` cache header on hashed bundles.
- The existing `main.dart.<md5[:8]>.js` and
  `flutter_bootstrap.<md5[:8]>.js` cache names must match their bytes,
  including bundles carried from the previous release. This is an accidental
  corruption check, not authenticated provenance; the current release still
  requires signed SHA-256 evidence. Inconsistent legacy bundles are refused
  during adoption or verification, not renamed, deleted or silently repaired.
- **Rollback.** Any failure is the same rename back, and the previous release
  is checked as well.
- **Why links inside the mount.** A bind mount follows the directory inode.
  The switch therefore happens inside the mount, which keeps the existing
  read-only Docker mount and needs no recreate per release. Core's
  StaticFiles (`follow_symlink=False`) accepts the relative links because
  they resolve inside the mount.

The old `scripts/deploy-web.sh` rsync into `web-build/` is retired once the web
root is active: it refuses, because `web-build/` is no longer served.

### Trust model

- **No production credentials outside amarillo.** GitHub holds no production
  credential, and no CI job runs with production rights. Untrusted PR code
  only ever runs in CI's one-job VMs; dispatch requires repository write access.
  CyberWolf can submit PRs without any production access.
- **Pinned deployer.** The deployer runs from a pinned copy of its own code
  (`~/.local/share/klukai/deployer/<sha>`, installed by the owner with
  `install --revision`). A merged change to the deployer or its safety checks
  takes effect only when the owner installs it. Production still deploys the
  merged commit itself.
- **Least privilege.** The token is a fine-grained GitHub token with read-only
  access to the public repository. It is stored with systemd-creds (TPM2 plus
  host key, `klukai-secrets migrate deployer-github-token`) and decrypted in
  memory only, never on disk and never logged. The release signing key never
  leaves amarillo. Code is fetched anonymously over HTTPS.
- **No paid services.** No Nobara or Dominus is involved.

## Activation (owner approval required for each step)

Run these on amarillo as `jalsarraf`, in order. Steps 1–4 change GitHub or
credentials and need the owner's explicit approval. Step 5 recreates
companion-core once.

1. **Merge this pipeline** through a reviewed PR, after full CI passes on its
   branch.

2. **Create the read-only token** in GitHub settings. This is the owner's
   action, and it is free. Until it exists, the deployer reads the owner's
   existing GitHub CLI login at runtime (`gh auth token`). That is broader
   than read-only, so `status` reports `token_source`. It is held in memory
   only, never stored and never logged, the same as manual releases already
   do.
   - Type: fine-grained personal access token.
   - Resource owner: `Al-Sarraf-Tech`.
   - Repository access: *Public repositories (read-only)*, or only
     `Al-Sarraf-Tech/klukai` with Actions/Contents/Metadata/Pull requests set
     to read.
   - Expiry: 1 year.

   Store it without echoing it:

   ```sh
   install -m 600 /dev/null "$XDG_RUNTIME_DIR/t" && read -rs T && printf %s "$T" > "$XDG_RUNTIME_DIR/t"; unset T
   python3 ~/git/klukai/ops/amarillo/scripts/klukai_secrets.py migrate deployer-github-token "$XDG_RUNTIME_DIR/t" --remove-source
   ```

3. **Branch protection on `main`.** The exact changes are in the PR
   description: require a PR with one approving review from a code owner, and
   dismiss stale reviews. Keep the required checks. Decide whether
   administrators are included.

4. **CyberWolf identity.** Its PRs are now authored as `jalsarraf0`, and GitHub
   does not let you approve your own PR. The [PR reviewer](pr-reviewer.md)
   (Claude Opus 5.5, xhigh) approves them from its own identity, within fixed
   rules that keep protected paths for you. Otherwise choose one:
   - give CyberWolf its own machine account or GitHub App with write access to
     branches and PRs and no admin; or
   - keep it under your account and set `require_approved_pr` to false. Branch
     protection is then the only approval gate.

5. **Move the web root and pin the deployer.** Do this in this order. Until
   `activate` succeeds, `web-build/` is what core serves, so leave it untouched.

   ```sh
   cd ~/git/klukai && git fetch -q origin && git checkout --detach origin/main   # deployer owns this checkout from now on
   M=$(git rev-parse HEAD)
   python3 ops/amarillo/scripts/klukai-deploy-on-merge.py install --revision "$M"
   D=~/.local/share/klukai/deployer/current/ops/amarillo/scripts
   python3 $D/klukai_web_release.py adopt --from ~/git/klukai/web-build   # copies what is served; nothing changes
   python3 $D/klukai_web_release.py activate     # one core recreate onto the identical files; rolls back on failure
   git -C ~/git/klukai restore web-build/index.html   # only now: web-build/ is no longer served
   ```

   The first automatic deploy then replaces the adopted files with the
   CI-built PWA of main's head, atomically.

6. **Baseline and settings:**

   ```sh
   python3 $D/klukai-deploy-on-merge.py baseline "$(docker inspect -f '{{index .Config.Labels "org.opencontainers.image.revision"}}' companion-core)"
   printf '{"require_approved_pr": true, "reviewers": ["jalsarraf0"]}\n' > ~/.config/klukai/deployer.json
   ```

   Use `false` until step 4 is settled.

7. **Install and start the timer:**

   ```sh
   install -m 0644 ops/amarillo/systemd/klukai-deploy-on-merge.{service,timer} ~/.config/systemd/user/
   systemctl --user daemon-reload
   systemctl --user enable --now klukai-deploy-on-merge.timer
   ```

   The first cycle deploys main's head if it is certified and descends from
   the baseline.

## Operating it

```sh
D=~/.local/share/klukai/deployer/current/ops/amarillo/scripts
python3 $D/klukai-deploy-on-merge.py status            # deployed commit, held commits, in-flight run
python3 $D/klukai-deploy-on-merge.py pause             # stop deploying (next cycle on; no lock needed)
python3 $D/klukai-deploy-on-merge.py resume            # also clears a pause set by a loud failure
python3 $D/klukai-deploy-on-merge.py retry <sha>       # one more attempt for a held commit
journalctl --user -u klukai-deploy-on-merge -n 50 --no-pager
ls ~/.local/state/klukai/release/deployer/receipts/
```

To update the deployer's own code after a reviewed merge:

```sh
python3 $D/klukai-deploy-on-merge.py install --revision <sha on main>
```

## Rollback

- **Automatic.** Failed health checks, failed web checks or failed post-deploy
  checks restore everything the attempt changed. The bus note says
  `rolled_back`, `web_rolled_back` or `verify_failed`. `ROLLBACK_FAILED` and
  `RESTORE_FAILED` are loud and need a person.
- **Web only, by hand:**

  ```sh
  python3 $D/klukai_web_release.py rollback                 # previous release
  python3 $D/klukai_web_release.py rollback --to <id>       # any staged release
  ```

  `status` lists the staged releases.
- **Core, by hand.** Pause the deployer, then pin the newest rollback tag:

  ```sh
  docker image ls klukai-core   # klukai-core:rollback-<time>-<id>
  ```

  Write it into `~/.local/state/klukai/release/core-image.json`, then:

  ```sh
  python3 ~/git/klukai/ops/amarillo/scripts/klukai_compose.py up -d --no-build --no-deps companion-core
  ```

  After a migration release, follow that migration's recovery plan instead.
  Restore the verified dump in `/mnt/nvmeINT/backups/klukai/releases/` only as
  that plan describes. Data written since the dump matters, and chat history
  is sacred.
- **Turn deploy-on-merge off entirely:**

  ```sh
  systemctl --user disable --now klukai-deploy-on-merge.timer
  ```

  Manual releases (`docs/runbooks/release.md`) work as before. To move the web
  mount back, delete `~/.local/state/klukai/release/web-root.json` and
  recreate core with `klukai_compose.py`.

## Testing

- **Unit tests** in `ops/amarillo/tests`:
  - `test_deploy_on_merge.py` uses real git and fake targets;
  - `test_web_release_atomic.py`;
  - `test_ci_web_relay.py`;
  - `test_deploy_plumbing.py`;
  - `tests/quality/test_web_release.py` and the CI contract tests.
- **Disposable staging:**

  ```sh
  GH_TOKEN=... python3 ops/amarillo/scripts/klukai-deploy-staging.py --root <empty dir> all
  ```

  It runs the real controller against real CI records and artifacts with
  throwaway targets:
  - a stand-in core running the CI-built images;
  - a throwaway PostgreSQL for the backup gate;
  - a web root served by the real core image through a read-only bind mount.

  Covered: success, failed and selective CI, invalid artifacts, a rewound
  main, concurrent merges, unhealthy core, unreadable web, failed post-deploy
  checks, a run killed mid-hold, and web rollback. It writes
  `staging-evidence.json` and touches nothing in production.
