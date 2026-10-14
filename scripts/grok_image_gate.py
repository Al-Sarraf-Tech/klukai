"""Native Grok PreToolUse hook: one edit, exactly the approved references."""
import json
from pathlib import Path
import sys


def decide(call, policy, claim=Path('/work/generation-started')):
    if not isinstance(call, dict) or call.get('toolInputTruncated'):
        return 'deny'
    args = call.get('toolInput')
    if call.get('toolName') != 'image_edit' or not isinstance(args, dict):
        return 'deny'
    refs = args.get('image')
    if (not isinstance(refs, list) or not refs or
            any(not isinstance(x, str) for x in refs) or
            sorted(refs) != sorted(policy['images']) or
            not isinstance(args.get('prompt'), str) or not args['prompt'].strip()):
        return 'deny'
    try:
        with claim.open('x') as handle:
            handle.write('claimed')
        return 'allow'
    except OSError:
        return 'deny'


def main():
    decision = 'deny'
    try:
        call = json.loads(sys.stdin.read(65537))
        policy = json.loads(Path('/gate/policy.json').read_text())
        decision = decide(call, policy)
        record = {'tool': call.get('toolName'), 'decision': decision,
                  'session': call.get('sessionId'), 'id': call.get('toolUseId')}
        with Path('/work/gate-audit.ndjson').open('a') as handle:
            handle.write(json.dumps(record) + '\n')
    except Exception:
        decision = 'deny'
    print(json.dumps({'decision': decision, 'reason': 'Only one approved reference edit is permitted.'}))
    return 0 if decision == 'allow' else 2


if __name__ == '__main__':
    raise SystemExit(main())
