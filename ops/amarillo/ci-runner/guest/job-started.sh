#!/bin/sh
# Runner job-started hook: these VMs serve Klukai's own trusted events only.
case "${GITHUB_REPOSITORY:-}" in
  Al-Sarraf-Tech/klukai) ;;
  *) echo "klukai-ci: refusing a job for ${GITHUB_REPOSITORY:-unknown}" >&2; exit 1 ;;
esac
case "${GITHUB_EVENT_NAME:-}" in
  push|workflow_dispatch|schedule) ;;
  *) echo "klukai-ci: refusing event ${GITHUB_EVENT_NAME:-unknown}" >&2; exit 1 ;;
esac
