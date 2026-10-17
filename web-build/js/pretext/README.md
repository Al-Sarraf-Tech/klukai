# Vendored text analysis and layout

This directory retains the existing Pretext browser API used by
`web/index.html` and `pretext_bridge.js`. The bridge exposes text preparation,
measurement and canvas rendering even though current chat bubbles use Flutter
presentation. Removing this directory would break that shipped interface.

On 2026-10-15, `analysis.js` was replaced byte for byte with the official
`@chenglou/pretext` **0.0.5** `dist/analysis.js` to fix
[GHSA-5478-66c3-rhxr](https://github.com/chenglou/pretext/security/advisories/GHSA-5478-66c3-rhxr).
The prior file matched the official vulnerable 0.0.4 artifact. The replacement
removes the accumulating repeated-character scan and defers materialization
using run metadata. It preserves the exports/schema consumed by this layout.

[PROVENANCE.json](PROVENANCE.json) records per-file SHA-256 values, the verified
npm archive SHA-512/SHA-256, release identities and the advisory-specific source
VEX justification. This is a targeted security backport into an inherited mixed
component, not a claim that the entire directory is release 0.0.5. The inherited
`layout.js`/`inline-flow.js` variants keep their original Klukai source identity;
the exact upstream release of those variants is unverified. [LICENSE](LICENSE)
contains the upstream MIT license.

These upstream compiled algorithms are exempt from the application module size
bound: `analysis.js` and `line-break.js` remain identifiable vendor artifacts,
with their complete source digests recorded. Do not split or format them as
application code. Klukai's handwritten adapter is `../pretext_bridge.js`.

`tools/companion/test/pretext_security.test.mjs` checks literal multilingual
segmentation/cursor expectations and a bounded hostile-input worker. The
character-visit budget fails the previous code without hanging the test process
and passes the backport without truncating input. The actual Chrome integration
in `tools/avatar/test/module-graph.test.mjs` exercises the real bridge through
preparation, layout, visible canvas pixels and handle release.
