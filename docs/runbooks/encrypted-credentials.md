# Encrypted Klukai credentials

## Active store (since 2026-10-17)

Production Compose secrets are encrypted at rest and never kept in plaintext:

| Credential | Was | Holds |
| --- | --- | --- |
| `compose-env` | `~/git/klukai/.env` | Compose interpolation variables (database, signing, API, VAPID, voice, broker) |
| `core-override` | the private `preserve-runtime.private.json` | the preserved companion-core environment |

Both live in `~/.config/klukai/credentials/` (directory 0700, files 0600, owner
UID 1000) as `systemd-creds --user` credentials: host key plus TPM2, bound to
this machine, this user and the embedded name `klukai-<name>`. Each has a
`.cred.prev` rollback copy and a host-key-only copy under `recovery/` that
survives a TPM reset on this host. Losing the host itself (disk and host key)
needs the owner to re-enter the values; the files are not a portable backup.

`ops/amarillo/scripts/klukai_compose.py` is the only supported way to run Compose
for this stack. For one command it decrypts both credentials into a private
directory under `$XDG_RUNTIME_DIR` (tmpfs, 0700), runs `docker compose
--env-file <env> -f docker-compose.yml -f <override> [-f <image pin>]`, then
deletes the directory (SIGTERM is turned into a normal exit so this still
happens; a directory left by SIGKILL is swept after an hour by the next command
or by `check`). A missing, corrupt, wrongly named or group/world-readable
credential stops the command before Compose runs; there is no fallback to a
plaintext file. Plain `docker compose` in the repository no longer sees the
secrets and must not be used for companion-core. The release deployer and
`scripts/disaster-recovery.sh` use the same path. `klukai_compose.py config`
prints resolved values, secrets included, so do not run it into logs.

The two internal bearer tokens (`metrics-token` for the core metrics scrape,
`alert-bridge-token` for the Alertmanager bridge) are in the same store since
2026-10-18; see "Runtime tokens" below.

### Commands (`ops/amarillo/scripts/klukai_secrets.py`)

Values are never printed; comparisons use SHA-256 in memory.

```sh
S=ops/amarillo/scripts/klukai_secrets.py
python3 $S check --plaintext ~/git/klukai/.env       # perms, every cred + recovery copy decrypts, no plaintext left
python3 $S rotate compose-env NEW_FILE --remove-source  # NEW_FILE in $XDG_RUNTIME_DIR; old version kept as .prev
python3 $S rollback compose-env                      # previous version back; recovery copy follows
python3 $S recover core-override                     # rebuild the primary from the host-key copy
```

After a rotation or rollback, recreate the consumer with
`klukai_compose.py up -d --no-build --no-deps <service>` (core: use the release
deployer) and read back health. Credential rollback never restores a database.

### Runtime tokens (since 2026-10-18, SEC-3)

`metrics-token.cred` and `alert-bridge-token.cred` sit in the same store, with
the same `.prev` and `recovery/` copies, and `klukai_secrets.py check` covers
them. Docker restarts companion-core, Prometheus and Alertmanager at boot,
before any user session, so these two cannot be decrypted per command. The
**system** unit `ops/amarillo/systemd/klukai-runtime-secrets.service` does it once
per boot:

- It is ordered `Before=docker.service` (ordering only; Docker and the other
  stacks still start if it fails) and `After=tpm2.target`, and it requires the
  home mount.
- It runs as `jalsarraf`, never as root, because the credentials are
  user-scoped. A system unit's `LoadCredentialEncrypted=` cannot decrypt
  user-scoped credentials, so `klukai_runtime_secrets.py install` calls
  `systemd-creds --user decrypt` through the system varlink service. It runs
  hardened: no capabilities, `ProtectSystem=strict`, read-only home,
  `AF_UNIX` only.
- systemd creates `/run/klukai-secrets/{metrics,alert-bridge}/` (tmpfs, 0700,
  owned by the user; a directory Docker created first as root is re-owned).
  Each token is written 0400 into its own directory:

| File | Mounted into | Reader |
| --- | --- | --- |
| `/run/klukai-secrets/metrics/metrics-token` | core `/run/klukai-secrets`, Prometheus `/etc/prometheus/secrets` | per request / per scrape |
| `/run/klukai-secrets/alert-bridge/alert-bridge-token` | Alertmanager `/etc/alertmanager/secrets`; the bridge user unit reads it directly | at start |

- Every consumer mounts a directory, never the file, so a rotated token is
  visible without recreating. No consumer sees the other token.
- Both credentials are decrypted and validated (`[A-Za-z0-9_-]{32,128}`)
  before either file is written.
- Stopping the unit deletes the files and keeps the directories
  (`RuntimeDirectoryPreserve=yes`), so running containers' bind mounts stay
  valid when it starts again.

Fail closed:

- Prometheus and Alertmanager start through a guard that exits 78 when their
  token is missing or empty. `restart: unless-stopped` retries until the unit
  has run.
- Core still starts without the token and answers `/internal/metrics` with
  401, because chat must not depend on monitoring.
- The bridge exits on a missing token, and `Restart=on-failure` retries.

Install or update (root via sudo; back up `/etc/systemd/system` first):

```sh
sudo install -m 0644 ops/amarillo/systemd/klukai-runtime-secrets.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now klukai-runtime-secrets.service
python3 ops/amarillo/scripts/klukai_runtime_secrets.py check   # present, 0400, equal to the store
```

The obs project runs through `klukai_compose.py --obs ...`. It uses no store
secrets and an empty env file, so a stray `.env` cannot move the mounts.

Rotate a token:

1. Write the new value to a private file in `$XDG_RUNTIME_DIR` and run
   `klukai_secrets.py rotate <name> FILE --remove-source`.
2. Run `sudo systemctl restart klukai-runtime-secrets.service`.
3. For the alert token, also restart Alertmanager
   (`docker restart klukai-alertmanager`) and
   `systemctl --user restart klukai-alert-bridge`.

`klukai_secrets.py rollback <name>` followed by the same restarts undoes a
rotation.

### Migration record (2026-10-17)

Both plaintext files were sealed with `migrate`, which encrypts, decrypts the
staged file and compares it with the input before replacing anything. The
resolved `docker compose config` from the encrypted path was byte-identical to
the plaintext path (5,721 bytes, compared by SHA-256 only). A rotation drill on
a scratch store with dummy values passed rotate, rollback, recover, and
fail-closed checks for a missing, corrupt and world-readable credential. The
plaintext files were removed only after the encrypted path had deployed core
(see `docs/runbooks/release.md`).

### Migration record (2026-10-18, runtime tokens)

Both token files were sealed with `migrate`, keeping the plaintext source until
the end. The unit was then installed. Next, Prometheus and Alertmanager were
recreated (`--obs`) and the bridge was restarted. Core went out as release
`e37e22b` with `--accept-manual-rollback`, because only its mount line changed
and the previous image reads the same in-container path.

Only after the scrape worked from `/run` were `.runtime-secrets/` and its files
deleted. A content scan of the 35,667 files under `~/git/klukai` found no copy.

Checks after the switch:

- The test alert was delivered and resolved.
- e2e passed 8/10 with her-pov skipped.
- With the unit stopped, both obs containers exited 78. Starting it again
  recovered them.

The backup, with the old token files, is in
`~/.local/state/klukai/backup-20261018-sec3/` (0700). A reboot test of the boot
ordering is pending an owner OK.

## Alternative design: service-owned credential (not activated)

Codex's earlier design below, a `companion.service` user unit using
`LoadCredentialEncrypted=` and `scripts/release/secret_store.py`, is kept for
reference. It was never installed. The active store above supersedes it for the
Compose-driven deployment; adopting it later would replace `klukai_compose.py`
and must migrate from the active store, not from plaintext.

## Storage and execution

The Amarillo user service loads a user-scoped systemd encrypted credential with
the embedded name `klukai-runtime`. Its production path is
`~/.config/credstore.encrypted/klukai-runtime.cred`, inside a mode-0700 directory,
with the encrypted file mode 0600. Encryption uses the existing host key and
TPM2, user identity 1000, machine identity and PCR7. It does not depend on the
desktop login keyring. No new service, subscription or paid facility is needed.

`LoadCredentialEncrypted=` exposes the decrypted payload only in systemd's
private credential directory while the service runs. The payload has exactly:

- `schema_version`: integer 1.
- `environment`: the preserved production Compose interpolation variables.
- `metrics_token`: a separate, cryptographically random bearer token.

The consumer is `scripts/release/secret_store.py`. It checks actual file owner,
regular-file status, private permissions, bounded size and schema before
executing a command. Symlinks, FIFOs, malformed payloads, NUL values and tool
environment overrides such as `LD_PRELOAD`, `PATH` or `DOCKER_HOST` fail closed.
Validation failures produce a fixed message without payload contents.

Values are passed directly to the child environment. There is no shell, dotenv
serialization or interpolation of secret contents. Compose must use
`--env-file /dev/null` so a stale repository `.env` cannot override or mask the
sealed configuration. The source unit template is
`ops/amarillo/systemd/companion.service`.

Only the dedicated metrics token is materialized, atomically with mode 0400,
inside the service's mode-0700 `RuntimeDirectory=klukai-secrets`. Core and
Prometheus must mount that directory read-only, not bind the token inode alone,
so token replacement remains visible. Neither receives the encrypted payload
or a directory containing the other API/account credentials. Account-provider
auth files retain their existing separate worker boundary.

## Provisioning and migration

Perform this locally with the existing administrator account. Never put payload
values into terminal arguments, command output, Git, RabbitMQ or CI. Preserve
all existing production values; creating a store does not authorize rotating
API keys, passwords, signing secrets or provider selection.

1. Prepare the JSON payload in a private temporary directory under the current
   user's runtime directory. Validate it with the consumer's `--check` mode,
   pointing `CREDENTIALS_DIRECTORY` at that directory and naming the input
   `klukai-runtime`. The check prints only a success marker.
2. Seal the input to a separate candidate file using the following options;
   `INPUT` and `OUTPUT` are local file paths, never secret values:

   ```sh
   sudo systemd-creds --name=klukai-runtime --uid=1000 \
     --with-key=host+tpm2 --tpm2-pcrs=7 encrypt INPUT OUTPUT
   ```

3. Explicitly set the candidate file owner to UID/GID 1000 and mode 0600.
   `systemd-creds` can create mode-0644 ciphertext by default; the private
   parent directory and explicit permission step remain required.
4. Load the candidate through a disposable user unit's
   `LoadCredentialEncrypted=klukai-runtime:OUTPUT`, then run the consumer in
   check mode. Compare the decrypted environment against the captured baseline
   in memory and emit only equality results/counts, not values or raw hashes of
   individual low-entropy passwords. Verify a dedicated mode-0400 metrics token.
5. Retain the previous sealed store as a private encrypted rollback copy.
   Atomically select the candidate only after validation. Keep the provider
   credentials and runtime environment equivalent through the rollout.
6. Install the unit template only once the verified active release overlay
   exists. The overlay must select the reviewed, tested image; `--no-build`
   prevents an accidental startup rebuild. The deployer must verify artifact
   identity and activation integrity before switching to it.
7. Verify the actual service, image identity, public authentication and private
   metric scrape. Retire obsolete plaintext production startup inputs only
   after their complete encrypted round-trip and recovery path are confirmed.
   Archive necessary private deployment snapshots in encrypted form.

The service checks that the system Docker daemon is available. It does not
depend on a nonexistent per-user `docker.service`. It preserves the existing
user `aichat.service` ordering without modifying that project's configuration.

## Rotation and rollback

Create a new candidate sealed file; do not edit the active ciphertext or write
plaintext into its persistent directory. Repeat the disposable consumer test
and compare the intended changes. For a metrics-only rotation, preserve every
other value, activate the new store and reload/restart the consumers as needed.
Verify that the new token succeeds and the previous token is rejected.

If activation fails, restore the previous verified sealed store and compatible
code/artifacts, then restart and read back live health. Do not restore a database
backup over newer conversations as part of credential/code rollback. A failure
to decrypt or validate must stop startup rather than fall back to `.env` or an
empty/default secret.

The sealed rollback copy requires the same host/TPM/PCR binding. Before clearing
the TPM or changing Secure Boot policy, arrange recoverable source credentials
through the owner's existing secret-recovery process and re-seal for the new
binding. The encrypted file alone is not a portable cross-host recovery key.

## Verification evidence

The disposable source tests exercise exact values containing dollars, quotes,
backslashes and newlines, permissions, ownership, symlinks/FIFOs, invalid schema,
execution blocking, token atomic replacement and rollback. Run:

```sh
python -m pytest tests/release/test_secret_store.py -q
```

On Amarillo, real temporary systemd user services successfully loaded the
original, rotated and rollback dummy stores through host+TPM2/PCR7 encryption.
Each preserved exact environment values and verified the dedicated token's
mode 0400. A wrong embedded credential name failed before command execution
with `243/CREDENTIALS`. Private evidence is under
`~/.local/state/klukai/s-uplift-20261015/credential-consumer-probe/` and
`credential-probe/`. No production values were used by these probes.

## Shutdown budget

The proposed release unit allows 720 seconds to stop; the core Compose service
allows 600 seconds. Producer owners stop admission together and join before
shared HTTP clients or the database close. The conservative local-image timeout
path can spend up to 573 seconds in declared asynchronous cleanup phases:
three 65-second workload interrupt/free deadlines, 372 seconds for GPU release,
one second for its best-effort event and five seconds for owner drain.
These are deadlines on the relevant remote operations, not a universal bound
on a stalled filesystem/native thread or database driver. An unconfirmed release
stays uncertain; it must never clear a guard or permit conflicting GPU work.
Do not shorten the service budgets without revalidating cancellation, accepted
image recovery and database-close ordering. The unit is not installed by adding
this file; activation still requires the encrypted credential and verified release
overlay described above.
