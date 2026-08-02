# ADR-0015: Windows and WSL2 are not klukai deployment targets

- **Date:** 2026-04-20 (formalized 2026-05-16)
- **Updated:** 2026-08-01 (lost Windows host recorded)
- **Status:** Accepted
- **Authors:** jalsarraf

## Context

Early in Klukai's history, WSL2 was considered for server-side components and
the RTX 3090 services ultimately ran on a Windows machine named `dominus`.
That Windows installation and its WSL2 environment are now a total loss. They
cannot provide live configuration, model files, a rollback target, or a
network endpoint.

The replacement workstation is `dominus-nobara`, running Nobara Linux with
the RTX 3090 and NVMe RAID 0. The always-on application and data services
remain on the Linux server `amarillo`.

## Decision

Klukai's deployment topology has exactly two hosts:

- **amarillo** — core, edge gateway, PostgreSQL, Redis, Qdrant, primary backup
  staging, and non-GPU application services.
- **dominus-nobara** — the canonical RAID-backed, containerized LLM, voice,
  speech, image, and transcription sidecar plus the preserved RAID-contained
  native-vLLM exception.

Windows `dominus` and WSL2 are both retired historical evidence. No script,
Compose file, DNS entry, copy step, or rollback plan may depend on either.
Connections between the two live hosts use Tailscale only. A future deployment
host requires a new ADR.

## Consequences

- Operations cover two live failure domains and one canonical service owner
  per workload.
- `ops/dominus-nobara/compose.yaml`, the associated user units, and the
  immutable model lock define the GPU-side deployment.
- All durable GPU-side data lives on `/mnt/nvmer0`; host packages are limited
  to the storage, Docker/NVIDIA, Tailscale, and service-manager substrate.
- Recovery copies may be staged on `amarillo`, but the dead Windows/WSL2 host
  is never queried or used as a source.
- RAID 0 is not a backup, so unique data requires an independent copy.

## Alternatives considered

- **Rebuild the Windows/WSL2 topology:** rejected because both environments
  are lost and the clean Nobara host provides a simpler container runtime.
- **Treat WSL2 as a third target:** rejected because it adds an unsupported
  failure domain without a deployment need.
- **Run every service on amarillo:** rejected because the supported CUDA GPU
  and recovered large model fleet are on `dominus-nobara`.

## Related

- `ops/dominus-nobara/RUNBOOK.md`
- `ops/dominus-nobara/models.lock.json`
- ADR-0002 (canonical two-host split)
- ADR-0014 (off-host recovery copy)
