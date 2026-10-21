"""PreToolUse gate; copied into each run's .agents folder beside its policy."""
import json
from pathlib import Path
import sys

GATE = Path(__file__).resolve().parent
WORK = GATE.parent


def decide(call, policy, claim=None):
    claim = WORK / 'generation-started' if claim is None else claim
    name = call.get('name')
    if name == 'generate_image' and policy.get('generation') is True:
        refs = call.get('args', {}).get('ImagePaths')
        if not isinstance(refs, list) or sorted(refs) != sorted(policy['images']):
            return 'deny'
        try:
            with claim.open('x') as handle:
                handle.write('claimed')
            return 'allow'
        except FileExistsError:
            return 'deny'
    if name == 'finish':
        return 'allow'
    if name == 'view_file' and call.get('args', {}).get('AbsolutePath') in policy['images']:
        return 'allow'
    return 'deny'


def main():
    try:
        payload = json.load(sys.stdin)
        policy = json.loads((GATE / 'review-policy.json').read_text())
        call = payload['toolCall']
        decision = decide(call, policy)
        record = {'tool':call.get('name'), 'path':call.get('args',{}).get('AbsolutePath'),
                  'decision':decision, 'step':payload.get('stepIdx')}
        with (WORK / 'gate-audit.ndjson').open('a') as handle:
            handle.write(json.dumps(record)+'\n')
        print(json.dumps({'decision':decision, 'reason':'Klukai permits only the selected artwork operation.'}))
    except Exception:
        print('{"decision":"deny","reason":"Invalid artwork gate input"}')


if __name__ == '__main__':
    main()
