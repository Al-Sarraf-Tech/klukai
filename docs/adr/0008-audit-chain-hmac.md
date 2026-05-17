# ADR-0008: Audit log with HMAC tamper-detection chain

- **Date:** 2026-04 (formalized 2026-05-16)
- **Status:** Accepted
- **Authors:** jalsarraf

## Context

klukai stores sensitive actions: logins, gifts given, costume changes,
mission events, memory keeps/discards. A plain audit table is
write-only-by-policy but technically mutable — a sufficiently
motivated DB-side attacker could rewrite history. For S-tier audit
defensibility, the log needs cryptographic tamper detection.

## Decision

Implement a hash-chained audit log in `app/audit_chain.py`:

- Every `companion_audit_log` row carries `prev_hash` (the previous
  row's `hmac`) and `hmac` (HMAC-SHA256 of `prev_hash || serialized
  payload`).
- The chain secret is derived from `AUDIT_CHAIN_SECRET` env (defaults
  to `dev-audit-chain-secret` for dev; production sets a strong value
  via systemd-creds per Phase 2 secrets work).
- `GET /api/audit/verify-chain` walks the chain end-to-end and reports
  the first row where `hmac` doesn't match the recomputed value.
- Any insertion/modification mid-chain breaks every subsequent row's
  hash — undetectable mutation is impossible without also rewriting
  all subsequent HMACs (and knowing the secret).

## Consequences

- **Write performance impact**: each audit insert reads the latest
  row's hmac, computes the new hmac, then writes. Within budget per
  `docs/slos.md` (audit log write p99 ≤ 100ms target).
- **Tamper-detection but not tamper-prevention**: an attacker with
  the secret AND DB write access could still rewrite cleanly. Defense
  is secret protection (systemd-creds TPM-sealed, Phase 2).
- **Recovery from corruption**: a broken chain is non-fatal —
  subsequent rows still record, just don't verify. Recovery is
  forensic, not operational.
- **Phase 4 stretch**: per-row signatures with off-host verification
  for stronger non-repudiation.

## Alternatives considered

- **Append-only DB (no UPDATE/DELETE)**: PG has no append-only mode;
  enforcement at the application layer is bypassed by direct DB
  access. The hash chain catches application-bypass too.
- **External audit service (CloudTrail, Splunk)**: vendor lock,
  cost, leaves klukai's privacy boundary. Out of scope.
- **No audit chain (just timestamped rows)**: rejected — defeats
  the S-tier defensibility goal.

## Related

- `app/audit_chain.py` — HMAC chain implementation
- `app/audit.py` — audit logger + `/api/audit/verify-chain` route
- `docs/runbooks/db-down.md`
- `docs/slos.md` (audit endpoints SLO)
- ADR-0003 (PG is the durable backend for audit)
