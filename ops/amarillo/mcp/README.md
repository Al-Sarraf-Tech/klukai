# Klukai gateway credential pickup

The existing gateway rotates keys weekly. This host-side system service reads
only the newest valid `klukai-web-tools_YYYYMMDDTHHMMSSZ.txt` drop under
`/docker/mcp/gateway/key_drops` and publishes it to
`/home/jalsarraf/.local/share/mcp-stack/klukai/gateway.token`. The source directory
is never mounted into Klukai. Only the destination directory is mounted read-only
into the core, which rereads the token for each request.

The Python standard-library script validates the exact filename and key format,
rejects symlinks (including path ancestors), nonregular files and hard links,
and refuses to replace an equal-or-newer destination. It preserves source mtime
and atomically replaces the credential as UID/GID 1000, mode 0600, inside a 0700
directory. Missing drops leave the current token unchanged. Invalid newest drops
fail without falling back to an older key. Successful runs are silent; errors
contain no file paths or credentials. No other client's key is opened or read.

Install the script root-owned as
`/usr/local/libexec/klukai-mcp-token-pickup.py` (mode 0755) and the service/timer
root-owned under `/etc/systemd/system` (mode 0644). Precreate the destination
directory owned by UID/GID 1000 with mode 0700. These are **system** units: the
gateway's UID 65532, mode 0400 drops require a root reader. After reviewing the
installation, reload systemd, run the oneshot once, and enable/start the timer.
It checks every 15 minutes and catches missed runs after boot. The service has
no network and grants host write access only to the destination directory.
Disabling the timer
stops pickup without deleting the current credential.

Run fixture-only tests from the repository root:

```sh
python3 -m unittest discover -s ops/amarillo/mcp -p 'test_*.py' -v
systemd-analyze verify ops/amarillo/mcp/klukai-mcp-token-pickup.service ops/amarillo/mcp/klukai-mcp-token-pickup.timer
```

For fixture use, the script accepts `--source-dir`, `--destination`, `--owner-uid`
and `--owner-gid`. Parent directories above the destination's immediate parent
must already exist. The script does not generate keys, alter gateway schedules,
restart the core, or activate its own timer.
