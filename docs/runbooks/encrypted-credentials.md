# Encrypted Klukai credentials

Status: the consumer and disposable TPM-backed startup/rotation/rollback probes
are implemented. Production migration is part of the verified release rollout;
this file does not claim it has happened. Do not install the unit template until
the reviewed release and its pinned activation overlay have been verified.

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
