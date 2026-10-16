# Component CI environments

The `Isolated component suites` job retains exact-source admission, pinned
checkout, the existing Node action and `[self-hosted, Linux, X64, ci-heavy]`
capacity. No runner provisioning or host security changes occur in the job.

`tools.ci.component_environment prepare` reserves a new private output outside
the checkout, captures committed source, records native command files/hashes
and actual versions, and tests the required Bubblewrap namespaces. It downloads
only Compose5.6.0 into that output's private Docker configuration and checks
SHA256 `40343e21ca777173e69cff5dbafeb37c6f81f3b0d57d9e597f036e95eb63e76a`
before execution. It also verifies Docker selects that renderer. The prerequisite
commands are pinned Node24.22.0 plus npm, ShellCheck, Docker, curl and
`/usr/bin/bwrap`. The existing Node action selects its checksum-verified archive.
Missing tools or denied namespace capabilities fail with private command logs
and an operator prerequisite message. Working Bubblewrap does not require
AppArmor tools, and the helper does not change AppArmor or sysctls.

The existing complete Python tools action remains bound to
`docker/core/requirements-ci.txt`, currently131 distributions. Bot, operations
and voice tests share that complete environment and remain separate pytest
processes. The new explicit `bridge` bootstrap role instead uses the complete
11-distribution `docker/events-bridge/requirements-ci.txt`; its runtime remains
pika1.3.2 and redis5.2.1, separate from core redis7.4.0. Its test pins match the
complete CI lock: pytest9.0.3, pytest-asyncio1.4.0 and pytest-timeout2.4.0.
Typing-extensions4.16.0 is retained unconditionally for the plugin's Python3.12
marker dependency. Both roles use the existing hashed build tools, offline
Bubblewrap wheel build, exact installed inventory, installed file/metadata
integrity, source/HEAD checks and bounded child supervisor. No caller selects an
arbitrary lock. Tools consumers use the fixed default role and reject bridge
manifests. Bridge selection explicitly requests its role.

The export operation revalidates native materials/source and both Python
roles before opening `GITHUB_ENV`. It exports only `DOCKER_CONFIG`,
`KLUKAI_COMPONENT_PYTHON` and `KLUKAI_BRIDGE_PYTHON`; the tools action continues
to export its validated complete tools environment. Component pytest runs use
warnings as errors, explicit function loop defaults for pytest-asyncio and no
bytecode writes to preserve installed integrity.
The existing production-provider-disabled variables, JS tests,
`npm ci --ignore-scripts` and warning-level ShellCheck suite remain in place.

## Evidence boundaries

Root acquired the bridge lock's exact primary PyPI version metadata and all
artifact hashes. The frozen lock SHA256 is
`cd9253b09902d389f74d3633455c0aed7935c676746582a1bd351369c3eba34c`.
Private receipts are under
`~/.local/state/klukai/s-uplift-20261015/bridge-lock-metadata-20261016/`:
`receipt.json` retains Requires-Dist and marker environments for Linux
CPython3.12–3.14; `canonicalization.json` records validation by the pinned pip
parser. This is metadata/closure evidence, not artifact execution or validation
on all supported interpreter versions.

Author boundary tests execute real Git/files/hash parsers/selection and finite
subprocess supervision with explicit pip/native/download doubles. The existing
real small namespace controls are retained. No author network acquisition,
install, full bootstrap, hosted CI, provider, GPU or production operation is
implied. Root owns independent source review, bounded real native acceptance
and exact-source CI acceptance. Original Task6 and all S acceptance IDs remain
open beyond this tranche.
