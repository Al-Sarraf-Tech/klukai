# PR reviewer

Claude reviews every ready PR against `main` and, within fixed rules, approves
it. An approval satisfies branch protection's required review and the
deployer's `require_approved_pr`, so the merge then deploys by itself
([deploy-on-merge](deploy-on-merge.md)).

- **Model:** Claude Opus 5.5 at `xhigh` effort for every review (owner,
  2026-10-22).
- **API key:** the owner's existing paid Anthropic key, the one production
  uses. It is read in memory from the encrypted `core-override` and never
  stored or logged.
- **Spend:** a daily cap (default 15 reviews) bounds it. Each head is reviewed
  once, and a broken reply is retried at most once.
- **Schedule:** a timer runs `klukai-pr-reviewer.timer` every five minutes. It
  uses the same owner-pinned code as the deployer (`install --revision`).

## What it can and cannot do

The model reads the PR and returns a JSON verdict. It has no tools, cannot run
the PR's code and cannot post anything itself. The PR's title, description,
commits and diff are framed as untrusted data, and any text in them that
addresses the reviewer is itself a blocking finding.

Whether an approval is posted is decided by fixed rules outside the model:

| Rule | Otherwise |
| --- | --- |
| Open, not a draft, against `main`; head unchanged during the review | not posted / comment |
| A full CI run of exactly that head passed (the deployer's certification) | waits; never approves |
| The whole diff fit the review (no truncation) | comment |
| No protected path changed: deploy, CI and ops; migrations and recovery plans; auth and security; personality; sacred memory and affection code; dependencies and lockfiles; secrets and `.env` | full review and comments, **owner approves** |
| The model's verdict is `approve` with no blocking or major finding | request changes / comment |
| The reviewer identity did not author the PR | comment |

The protected list is `PROTECTED` in
`ops/amarillo/scripts/klukai-pr-reviewer.py`.

## Identity and modes

Approvals come from a dedicated GitHub identity, so CyberWolf's PRs (authored
as `jalsarraf0`) can be approved by someone other than their author.

1. The owner creates a free machine account (for example `klukai-reviewer`)
   with write access to `Al-Sarraf-Tech/klukai` and no admin rights, plus a
   fine-grained token for it with Pull requests read/write and
   Contents/Actions/Metadata read.
2. Store the token without echoing it:

   ```sh
   install -m 600 /dev/null "$XDG_RUNTIME_DIR/t" && read -rs T && printf %s "$T" > "$XDG_RUNTIME_DIR/t"; unset T
   python3 ~/git/klukai/ops/amarillo/scripts/klukai_secrets.py migrate reviewer-github-token "$XDG_RUNTIME_DIR/t" --remove-source
   ```

3. Name it in `~/.config/klukai/reviewer.json`:
   `{"reviewer_login": "klukai-reviewer"}`.
4. Let its approval gate deploys, in `~/.config/klukai/deployer.json`:
   `{"require_approved_pr": true, "reviewers": ["jalsarraf0", "klukai-reviewer"]}`.

Without the token the reviewer runs **dry**: it reviews, records the verdict in
`~/.local/state/klukai/reviewer/` and sends the owner a bus note, but posts
nothing to GitHub.

## Operating it

```sh
R=~/.local/share/klukai/deployer/current/ops/amarillo/scripts/klukai-pr-reviewer.py
python3 $R status                          # mode, today's count against the cap, verdicts
python3 $R pause | resume
python3 $R review <pr> --dry-run           # one review now, nothing posted
python3 $R review <pr> --dry-run --ignore-ci   # before CI passed (can never approve)
journalctl --user -u klukai-pr-reviewer -n 50 --no-pager
ls ~/.local/state/klukai/reviewer/receipts/
```

Your own review always wins:

- A "Request changes" from you blocks the merge under branch protection.
- `klukai-deploy-on-merge.py pause` stops deploys whatever is approved.

## Enable

```sh
install -m 0644 ops/amarillo/systemd/klukai-pr-reviewer.{service,timer} ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now klukai-pr-reviewer.timer
```
