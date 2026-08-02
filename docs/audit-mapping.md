# Audit mapping — klukai controls vs SOC2-lite

> klukai is a personal product; full SOC2 certification isn't a goal. But the
> structure required to *survive* an external audit is the same structure that
> makes the system maintainable. This document maps klukai's existing controls
> to the SOC2 Trust Services Criteria so the audit-readiness gap is visible.
>
> **Status:** Phase 5 deliverable (per `docs/superpowers/specs/2026-05-16-s-plus-uplift.md` §9.4). Last reviewed: 2026-05-17.

## Trust Services Criteria — mapping summary

| Criterion | Coverage | Evidence | Gap |
|---|---|---|---|
| **CC1** Control environment | partial | `docs/adr/`, CODEOWNERS, PR template, conventional commits | Single-operator; no formal "control owner" assignments |
| **CC2** Communication & information | yes | `docs/runbooks/`, CHANGELOG.md, ADRs, post-incident notes template | n/a |
| **CC3** Risk assessment | partial | `docs/superpowers/specs/2026-05-16-s-plus-uplift.md` §10 risks, `docs/perf-baseline.md` | No quarterly risk-review cadence |
| **CC4** Monitoring activities | yes | Grafana dashboards + 13 alerts + `runbook_url` on each | n/a |
| **CC5** Control activities | yes | `scripts/s-tier-audit.sh`, CI gates (lint/typecheck/test/sec/scan) | n/a |
| **CC6.1** Logical access | yes | Bearer tokens + per-user rate limit + admin role; loopback bind | Quarterly token rotation runbook (Phase 5 calendar gate) |
| **CC6.2** New user provisioning | yes | Seed users hardcoded; documented in `.env.example` | No self-service onboarding (intentional non-goal) |
| **CC6.3** User access changes | partial | Manual via `app/auth.py`; admin endpoints audited | No formal change ticket trail |
| **CC6.6** External access | yes | Cloudflare TLS in front; nginx loopback bind; Tailscale to dominus-nobara | n/a |
| **CC6.7** Access removal | partial | `revoke_token` endpoint exists; no auto-expiry yet | TTL on bearer tokens |
| **CC6.8** Malicious software prevention | yes | trivy + grype + bandit + safety + gitleaks in CI | n/a |
| **CC7.1** Vulnerability mgmt | yes | Renovate weekly; safety in CI; trivy HIGH/CRITICAL fail | n/a |
| **CC7.2** Anomaly detection | yes | RED metrics per endpoint + 13 Prom alerts + burn-rate SLO alerts | n/a |
| **CC7.3** Incident response | yes | 12 runbooks; post-incident template in `docs/runbooks/README.md` | No on-call rotation (single operator) |
| **CC7.4** Incident recovery | partial | Offsite backup; `scripts/restore-from-backup.sh`; DR drill (Phase 4) | Quarterly DR drill cadence (Phase 5 calendar gate) |
| **CC7.5** Change management | yes | PR-gated CI; CODEOWNERS; conventional commits + git-cliff CHANGELOG | n/a |
| **CC8.1** Change management — software | yes | Multi-stage Dockerfile; SHA-pinned deps; SBOM + cosign signing (Phase 4) | n/a once Phase 4 lands |
| **CC9.1** Risk mitigation — disruption | partial | Circuit breakers per dep (Phase 4); graceful shutdown; offsite backup | Multi-region (explicit non-goal) |
| **CC9.2** Risk mitigation — fraud / abuse | partial | Rate-limit, audit chain (HMAC), admin endpoints audited | No formal abuse-detection ruleset |
| **A1.1** Availability commitments | yes | `docs/slos.md` codifies per-endpoint SLOs and error budgets | n/a |
| **A1.2** Capacity planning | partial | `docs/perf-baseline.md`; nightly perf collection (Phase 4) | No formal headroom-target doc |
| **A1.3** Backup & recovery | yes | Offsite backup nightly; restore script; DR drill quarterly (calendar gate) | n/a once calendar gate ages in |
| **C1.1** Confidentiality | yes | TPM-sealed secrets at rest (systemd-creds); TLS in transit; loopback bind | n/a |
| **C1.2** Confidentiality — disposal | yes | Audit log purge runbook; PG `VACUUM FULL` cadence | n/a |
| **P1** Privacy commitments | yes | Memory immutability invariant documented + tested; user-export endpoint (`/api/user/export`) | n/a |

## Klukai-specific controls

### K1 — Character integrity gate (golden tests)

**Why:** Klukai's character is the product. A regression in speech pattern, mood handling, or affection-level transition is functionally an outage.

**Control:** Golden test suite snapshots ~3000 (50 prompts × 10 affection levels × 6 mood categories) outputs. Drift = test fail; rotation requires explicit `--update-snapshots` flag.

**Evidence:** `docker/core/tests/golden/`.

**Reference:** `feedback_speech_routing_bug.md`, `feedback_commander_human.md`.

### K2 — Memory immutability invariant

**Why:** Chat messages, episodes, affection rows, Qdrant vectors are **SACRED**. Compaction summarizes; never deletes. Per absolute CLAUDE.md directive.

**Control:** `scripts/audit-memories.sh` counts PG rows + Qdrant points + Redis sessions; alerts on >5% drop.

**Evidence:** `scripts/audit-memories.sh`, `feedback_never_delete_chat.md`.

### K3 — Audit chain HMAC tamper-detection

**Why:** Admin actions and significant state changes must be tamper-evident.

**Control:** Every audit row signed with HMAC chain (prev_hash + payload); a single mutation breaks the chain at verification time.

**Evidence:** `app/audit_chain.py`, `tests/test_signed_urls_and_chain.py`, ADR-0008.

### K4 — Identity guards

**Why:** Klukai ≠ Kairi (`feedback_klukai_kairi_separate.md`); Commander is HUMAN (`feedback_commander_human.md`). Cross-identity contamination = persona regression.

**Control:** Separate DB tables, Qdrant collections, Redis prefixes. ADRs 0011, 0013.

**Evidence:** `app/personality/`, ADR-0011, ADR-0013.

## Audit-readiness gap summary

To pass an external audit today, the following would need to be added (none are S+-blocking; they're audit-specific):

1. **Quarterly risk-review minutes** — formal cadence in `docs/risk-reviews/<date>.md`.
2. **Per-control owner assignments** — even if "single operator," documented.
3. **Token TTL** — bearer tokens auto-expire (today they're long-lived).
4. **Headroom-target doc** — capacity-plan threshold (e.g., "scale-up when p99 > 80% of SLO for 7 days").

None of these change klukai's tier — they're audit-specific paperwork that comes when (if) an audit is contracted.

## Cadence

This doc is reviewed quarterly (90-day cycle, aligned to Phase 5 calendar gates). The review answers:

- Have any controls degraded?
- Have any new threats appeared (new dependency, new attack surface)?
- Have any locked decisions been reversed (ADR supersession)?

A review with no changes is still a review — append a row:

```json
{"review_date": "YYYY-MM-DD", "reviewer": "<name>", "changes": "none", "next": "YYYY-MM-DD"}
```

…to `docs/audit-mapping-history.json`.

## See also

- `docs/superpowers/specs/2026-05-16-s-plus-uplift.md` — the plan that produced these controls.
- `docs/slos.md` — the availability commitment.
- `docs/runbooks/` — the operational response surface.
- `docs/adr/` — the decision history.
