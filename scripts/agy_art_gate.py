"""PreToolUse gate; copied into the isolated worker's read-only configuration."""
import json
from pathlib import Path
import sys


def decide(call, policy):
    name = call.get('name')
    if name == 'finish':
        return 'allow'
    if name == 'view_file' and call.get('args', {}).get('AbsolutePath') in policy['images']:
        return 'allow'
    return 'deny'


def main():
    try:
        payload = json.load(sys.stdin)
        policy = json.loads(Path('/work/.agents/review-policy.json').read_text())
        call = payload['toolCall']
        decision = decide(call, policy)
        record = {'tool':call.get('name'), 'path':call.get('args',{}).get('AbsolutePath'),
                  'decision':decision, 'step':payload.get('stepIdx')}
        with Path('/work/gate-audit.ndjson').open('a') as handle:
            handle.write(json.dumps(record)+'\n')
        print(json.dumps({'decision':decision, 'reason':'Klukai permits only selected image reads and final output.'}))
    except Exception:
        print('{"decision":"deny","reason":"Invalid artwork gate input"}')


if __name__ == '__main__':
    main()
