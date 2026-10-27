# Retired for Klukai — 2026-10-26

The owner moved Klukai entirely off Dominus-Nobara, including CI. These files
are historical deployment material. Do not install or start this stack.

The five old AI containers were stopped and removed without deleting volumes,
models, images or application data. The user stack and publish-repair units are
masked; both installed repair scripts honor
`/mnt/nvmer0/services/ai-stack/.retired-klukai`. Their previous files are retained
in `~/.local/state/klukai-retired-20261026` and the root script's dated backup.

The dedicated Klukai CI controller/network are disabled. The shared UCI host
configuration denies `Al-Sarraf-Tech/klukai`. Amarillo no longer pulls Dominus
CI records; all Klukai jobs and release artifacts use its existing local pool.

The owner's separate `local-llm` services are unrelated and remain available.
