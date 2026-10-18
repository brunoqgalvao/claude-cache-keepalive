#!/usr/bin/env python3
"""Show that 5-minute and 1-hour prompt caches are separate (uses the claude CLI and Haiku).

Session "upgrade": 5m write, then a 1h request 10 s later, then another 1h request ~8 min later.
Session "control": same, but every request stays 5m.
A unique ~7k-word blob in the first message makes each session's prefix measurable.
"""
import json, os, random, subprocess, tempfile, time, uuid

D = tempfile.mkdtemp(prefix='ttl-test-')
words = 'amber birch cobalt delta ember fjord granite harbor indigo juniper kestrel lantern meadow nectar onyx prairie quartz raven sierra tundra'.split()

def run(sid, prompt, ttl, new):
    env = dict(os.environ, CLAUDE_CODE_PROMPT_CACHE_TTL=ttl)
    args = ['claude', '-p', prompt, '--output-format', 'json', '--model', 'haiku', '--max-turns', '1']
    args += ['--session-id', sid] if new else ['--resume', sid]
    j = json.loads(subprocess.run(args, cwd=D, env=env, capture_output=True, text=True, timeout=300).stdout)
    u = j.get('usage', {})
    return {'ttl': ttl, 'read': u.get('cache_read_input_tokens'), 'write': u.get('cache_creation')}

res = {}
for name, second in (('upgrade', '1h'), ('control', '5m')):
    blob = ' '.join(random.choice(words) + str(random.randint(0, 999)) for _ in range(5000))
    sid = str(uuid.uuid4())
    res[name] = {'sid': sid, 'steps': [run(sid, 'Reference list, ignore it: ' + blob + '\nReply with exactly: one', '5m', True)]}
    time.sleep(10)
    res[name]['steps'].append(run(sid, 'Reply with exactly: two', second, False))
print('waiting 8 minutes...', flush=True)
time.sleep(480)
for name, ttl in (('upgrade', '1h'), ('control', '5m')):
    res[name]['steps'].append(run(res[name]['sid'], 'Reply with exactly: three', ttl, False))
for name, r in res.items():
    print(name)
    for s in r['steps']:
        print('  ', s)
