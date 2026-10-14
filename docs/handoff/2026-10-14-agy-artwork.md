# Gemini artwork review through Agy

The owner authorized both a Klukai artwork-review workflow and Gemini fleet
membership, then explicitly requested a generation capability test. This adds
an operator-run advisory reviewer, separate from production image rendering.

## Use

Run on Amarillo in the owner's unlocked desktop/keyring session. Required host
programs are Python 3 with Pillow/PyYAML, Bubblewrap, xdg-dbus-proxy, and the
existing pinned Antigravity CLI 1.3.5. No new package or paid API is installed.
Sign in through `agy` if needed; `agy models` should list
`gemini-3.8-flash-medium`.

The host shortcut `klukai-art-review` invokes the same script from the main
checkout. Its six-image preview was verified after installation.

Prepare a manifest from selected local wardrobe renders (one to six):

```sh
python3 scripts/gemini_art_review.py \
  --prepare /absolute/path/to/wardrobe-renders \
  --reference /home/jalsarraf/git/klukai/docs/images/klukai-portrait.png \
  --outfits night_ride off_duty_cap hangar_coveralls winter_patrol dress_uniform drawing_night \
  --manifest /absolute/path/to/new-manifest.json
```

Inspect the selected images and manifest. Preview is the default and performs
no account or cloud request:

```sh
python3 scripts/gemini_art_review.py --manifest /absolute/path/to/new-manifest.json
```

Explicitly submit only approved nonprivate fictional artwork:

```sh
python3 scripts/gemini_art_review.py \
  --manifest /absolute/path/to/new-manifest.json \
  --allow-cloud-review --output /absolute/path/to/new-report.json
```

The report path must be new. Each attempt is recorded before invocation;
processing stops on the first failure and never retries automatically. A
`started` or `failed` result may have consumed account quota. Inspect it before
considering another attempt. Reports contain visible evidence and uncertain or
occluded details, with no automatic pass/fail, repair, or archive modification.

## Account and data boundaries

The worker uses the official pinned client and existing Google keyring login.
It does not copy tokens, use the retired Gemini CLI, or call an image API.
`useG1Credits=false` and no provider override are supplied in a fresh profile;
API keys, ADC selectors, alternate XDG configuration, preload variables and
proxy settings are excluded from the environment. Exhaustion is a failure,
not a paid fallback. CLI sparse persistence can omit false after execution.

Bubblewrap preserves the HOME string but mounts a new temporary profile at
that path. Only the pinned binary, runtime system files, staged images and a
filtered Secret Service socket are exposed. Original home files, global
rules, hooks, plugins, MCP configuration and repository context are absent.
The native client remains trusted: Secret Service access is not restricted to
one keyring item, and Google network traffic is not an egress allowlist.

Images are checked against SHA256, validated as bounded single-frame
PNG/JPEG/WebP, and normalized to RGB PNG without source metadata. Only selected
images, their outfit expectations, and a fixed review instruction are supplied.
Vendor processing/retention still applies; this is not an offline reviewer.

A custom agent selects `view_file` and `finish`. Its read-only PreToolUse gate
permits only the exact staged image paths and final output, and logs decisions.
Agy 1.3.5's `init.tools` lists the global tool registry even for this custom
agent; that list is not evidence of effective isolation. Acceptance requires
gate evidence, exact successful image calls, the requested agent/model, terminal
SUCCESS and locally validated review fields. No shell, generation, delegation,
MCP, URL, or writing tool is authorized by the review gate. CLI exit zero alone
is insufficient. An external watchdog terminates the process group.

`finish` is essential: a view_file-only custom agent can describe pixels but
loop waiting for a missing finish tool. The first synthetic probe exposed that
behavior; it timed out and was not accepted as a completed review. With finish
included, a second probe correctly described a red square, blue circle and
small text `Q7M`, with verified image-read events and terminal SUCCESS.

## Pilot evidence

Private local evidence directory:
`~/.local/share/klukai/gemini-review/pilot-20261014/`.
It contains the manifest, six nonprivate local ComfyUI renders, independent
Codex observations recorded before Gemini, the advisory report, and the
separate generation experiment. These are not owner-approved quality ratings.

All six reviews completed successfully with Gemini 3.8 Flash Medium; the local
report is `reviews-v1.json` and the independent comparison is
`pilot-comparison.json`. This small pilot establishes workflow operation, not
an accuracy rate.

| Outfit | Useful finding | Limitation from independent inspection |
| --- | --- | --- |
| Night ride | Open jacket and absent checker pattern | Overconfident hand assessment |
| Off-duty cap | Outfit/identity consistency | Hidden hands still grouped into a match verdict |
| Hangar coveralls | Missing face grease | Missed a reversed tear marking |
| Winter patrol | Reversed tear marking | Turtleneck obscured by scarf was called a match |
| Dress uniform | Visible glove/finger distortion; uncertain medals | Ponytail interpretation remains uncertain |
| Drawing night | Missing or barely visible tear marking | Tiny-feature absence needs human confirmation |

The parser was subsequently hardened after independent review to reject failed
finish events, unfinished repeated reads, cross-conversation steps and output
after the terminal result. Regression tests passed and the retained successful
pixel stream replayed through the hardened parser without another cloud call.
The six-image report was produced before that parser hardening; no visual
judgments were regenerated or edited.

The generation experiment succeeded through `generate_image`: one top-level
request, one output JPEG, 768 × 1376, 28.25 seconds for the complete Agy turn.
The returned file was decoded and visually inspected. It depicts Klukai in a
long winter coat and white scarf holding a sketchbook in a snowy courtyard,
using the public portrait as a reference. Identity details and the requested
scene are visible. This is concept-art evidence, not a canon approval.

- Output: `~/.local/share/klukai/gemini-review/pilot-20261014/klukai-agy-winter-patrol.jpg`
- SHA256: `da6d6ffcf14c77b57aed664854f33b494937c9635b14a326806c80be2c1ff65f`
- Conversation: `8fbd392c-8ff8-4b03-93ae-909c55085f54`
- Prompt and gate/stream evidence: `generation-test/` in the evidence directory.

Credit fallback was disabled and no API key or paid provider was used. This
proves included-account generation worked for this test; it does not establish
unlimited quota or a billing audit. Agy may perform internal generation work
within one tool call, so one top-level call is not a universal one-attempt cap.
Production ComfyUI settings and zero cloud-image allowances remain unchanged.
No unattended image generation has been enabled.

## Fleet

Gemini registration was supplied by concurrent fleet work in rabbitmq-ops
commit `aa15cad19d1221be2f796818773602b4d6d22609`. Verified broker subscriptions:
20 identities, 19 peers each, 380 directed bindings. Use `gemini@amarillo` for
an actual Gemini session on this host. Other host identities are durable inboxes;
registration does not prove a remote installation or running model worker.
There is no unattended Gemini responder. See `~/git/rabbitmq-ops/bus/GEMINI.md`
and `~/git/localai/docs/gemini-fleet.md` for fleet use and supported account setup.

## References

- [Account authentication](https://www.antigravity.google/docs/cli/install/)
- [Credit controls](https://www.antigravity.google/docs/cli/credits/)
- [Headless stream contract](https://www.antigravity.google/docs/cli/headless/)
- [Custom agents](https://www.antigravity.google/docs/subagents/)
- [Lifecycle hooks](https://www.antigravity.google/docs/hooks)
- [Google's old consumer CLI retirement notice](https://developers.google.com/gemini-code-assist/docs/deprecations/code-assist-individuals)

## Completion checks

Implementation commit `ca6f652` was fast-forwarded into the original main
checkout. The merged tree passed all 46 tests in `ops/amarillo/tests`; the
independent review closed with no unresolved material findings. The six-image
preview also passed through the installed host command.

Final fleet status publication `6553fdd6cc1b` was sent to all 19 peer feeds via
`bus pub` under project `klukai`, topic `klukai.gemini.status`. This proves durable
publication, not that every agent has read it. An earlier progress publication
was `e6f463c279a9`.
