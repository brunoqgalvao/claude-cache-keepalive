#!/usr/bin/env python3
"""Label each matched pause with yes/no facts about the last exchange, read by a cheap model, for the regression."""
import json, sys, pathlib, collections, datetime, zoneinfo, urllib.request, concurrent.futures, re
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / 'tools'))
import tune as T
IDX = '/tmp/cache-shots/codex_index.json'; OUT = '/tmp/cache-shots/llm_features.json'
KEYS = ['asked_user', 'followup_likely', 'user_closing', 'task_done', 'waiting_external', 'user_said_later', 'blocked', 'scheduled_run']
MODEL = sys.argv[1] if len(sys.argv) > 1 else 'anthropic/claude-haiku-4-5'
TZ = zoneinfo.ZoneInfo('America/Sao_Paulo')
idx = json.load(open(IDX))
byin = collections.defaultdict(list)
for f, s in idx.items():
    for c in s['calls']:
        if c[1]: byin[c[1]].append((c[0], f, c[3]))
S = T.load(21)
jobs = []
for v in S.values():
    for i, x in enumerate(v):
        if T.bucket(x['P']) < 0: continue
        nxt = v[i + 1] if i + 1 < len(v) else None
        g = nxt['t'] - x['t'] if nxt else None
        if g is not None and g <= T.TTL + 30: continue
        cands = [c for c in byin.get(x['P'], []) if abs(c[0] - x['t']) < 180]
        if not cands: continue
        _, f, turn = cands[0]; s = idx[f]; tr = s['turns'].get(turn, {})
        ended = tr.get('end') is not None and -5 <= tr['end'] - x['t'] <= 300
        users = [u for u in s['users'] if u[0] <= x['t'] + 5]
        lu = users[-1] if users else None
        when = datetime.datetime.fromtimestamp(x['t'], TZ).strftime('%A %H:%M')
        prompt = f"""You predict when a coding-agent chat will get its next message. The user is Bruno, who runs many agent chats in parallel, some on schedules (heartbeats).

Chat project: {pathlib.Path(s['meta'].get('cwd') or '').name}
This turn was started by: {tr.get('trigger') or 'unknown'} (automation_heartbeat_scheduled = a scheduled run)
Local time when the chat went idle: {when}
The agent {'finished its turn' if ended else 'was mid-turn (this may be a background step)'}.
Hours since Bruno last wrote in this chat: {((x['t'] - lu[0]) / 3600 if lu else -1):.1f}

Bruno's last message (tail):
{(lu[1][-600:] if lu else '(none)')}

Agent's last message (tail):
{(tr.get('msg') or '(none)')[-1200:] if ended else '(turn not finished)'}

Answer each with true or false:
- asked_user: the agent's last message asks Bruno a question or waits for his decision/approval
- followup_likely: there is an obvious next step Bruno is likely to ask for soon
- user_closing: Bruno's last message is a thank-you, "ok", "great" or similar closing
- task_done: the agent reports the task as finished
- waiting_external: the agent is waiting on something outside the chat (CI, deploy, another person, a set time)
- user_said_later: Bruno said he will come back later or gave a time
- blocked: the agent hit an error or is blocked
- scheduled_run: this looks like a scheduled/heartbeat run rather than Bruno typing
Reply with only JSON with those 8 keys."""
        jobs.append((f"{x['t']}|{x['P']}", prompt))
done = json.load(open(OUT)) if pathlib.Path(OUT).exists() else {}
todo = [j for j in jobs if j[0] not in done]
def call(job):
    key, prompt = job
    body = json.dumps({'model': MODEL, 'input': prompt, 'max_output_tokens': 150}).encode()
    for _ in range(3):
        try:
            req = urllib.request.Request('http://127.0.0.1:10100/v1/responses', data=body, headers={'content-type': 'application/json'})
            r = json.loads(urllib.request.urlopen(req, timeout=60).read())
            txt = ''.join(c.get('text', '') for o in r.get('output', []) for c in o.get('content', []) if isinstance(c, dict))
            m = re.search(r'\{.*\}', txt, re.S)
            d = json.loads(m.group(0))
            return key, [1.0 if d.get(k) in (True, 'true', 1) else 0.0 for k in KEYS]
        except Exception as e:
            err = str(e)
    return key, None
with concurrent.futures.ThreadPoolExecutor(8) as ex:
    for n, (key, val) in enumerate(ex.map(call, todo)):
        if val: done[key] = val
        if n % 100 == 0: json.dump(done, open(OUT, 'w'))
json.dump(done, open(OUT, 'w'))
print(len(jobs), 'pauses,', len(done), 'labelled')

