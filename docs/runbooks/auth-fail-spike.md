# Runbook: Auth Failure Spike

**Severity:** P2 (potential brute-force or auth regression)
**SLO breach:** Bearer rejection rate > 5%; user-facing login failures.

## Symptom

- Grafana panel "Auth failures rate" exceeds 5% of total requests
- Spike of `401 Unauthorized` responses
- Users report "can't log in" or "session keeps expiring"
- companion-core logs show many `invalid_token` or `bcrypt_verify_failed`

## Immediate action (< 10 min)

1. Check whether it's a single source or distributed:
   ```bash
   docker logs companion-core --since=1h | \
     grep -E "401|invalid_token" | \
     awk '{print $NF}' | sort | uniq -c | sort -rn | head
   ```
2. If single IP dominates → likely brute-force attempt:
   ```bash
   # Block via Cloudflare or amarillo firewall
   sudo firewall-cmd --add-rich-rule='rule family="ipv4" source address="X.X.X.X" reject' --timeout=3600
   ```
3. If distributed → likely client-side bug, expired tokens, or auth regression:
   - Check recent deploys for auth code changes:
     ```bash
     git log --oneline --since="1 day ago" -- docker/core/app/auth.py
     ```
   - Verify `app/auth.py` is intact:
     ```bash
     curl -X POST http://localhost:8300/api/user/login \
       -H "Content-Type: application/json" \
       -d '{"username":"jalsarraf","password":"<test>"}'
     ```

## Root-cause investigation

| Pattern | Likely cause | Action |
|---|---|---|
| All seed users failing | Password storage corruption | Check PG `users` table; restore from backup |
| One seed user failing | Password mismatch or lockout | Per `feedback_no_password_changes.md` — DO NOT reset; ask user |
| Many unknown users failing | Brute-force | Block source, increase rate-limit aggressiveness |
| Token verifications failing | Session secret rotated mid-flight | Restart core; rebuild sessions |
| All requests 401 after deploy | Auth route broken in PR | Roll back, fix forward |

## CRITICAL: Password handling

Per global CLAUDE.md `feedback_no_password_changes.md`: **NEVER rotate
passwords autonomously**. If users are locked out, contact them — do
not unilaterally reset.

Per `feedback_auth_passwords.md`: passwords live in env vars
(`SEED_PASSWORD_*`), not in git. If creds appear leaked, escalate to
user for manual rotation.

## Verification after fix

1. Auth failure rate returns to baseline (<1%) within 30 min.
2. Test login as each seed user.
3. Grafana auth-rate panel returns to green.

## Post-incident

- If brute-force: file ticket to tighten rate limit on `/api/user/login`
  (currently 10/min — consider 3/min with exponential backoff).
- If auth regression: PR with regression test in
  `tests/integration/test_auth_resilience.py`.

## Related

- `feedback_no_password_changes.md`
- `feedback_auth_passwords.md`
- `app/auth.py`
- `app/rate_limit.py` — bucket: `auth_login`
