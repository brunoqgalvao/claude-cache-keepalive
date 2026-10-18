#!/usr/bin/env python3
"""Index Codex session logs (last N days) into a compact file for the content backtest.
Keeps, per session: model calls (time, input tokens), turns (start time, trigger, end time, final agent message)
and user messages (time, text). Message text stays on this machine."""
import json, os, pathlib, sys, time, datetime
days = float(sys.argv[1]) if len(sys.argv) > 1 else 22
out = sys.argv[2] if len(sys.argv) > 2 else '/tmp/cache-shots/codex_index.json'
roots = [pathlib.Path.home() / '.codex' / 'sessions', pathlib.Path.home() / '.codex' / 'archived_sessions']
since = time.time() - days * 86400
def ts(s): return datetime.datetime.fromisoformat(s.replace('Z', '+00:00')).timestamp()
sessions = {}
files = [p for r in roots if r.exists() for p in r.rglob('rollout-*.jsonl') if p.stat().st_mtime >= since]
for p in files:
    s = dict(meta={}, calls=[], turns={}, users=[])
    try:
        for line in open(p, errors='ignore'):
            if '"session_meta"' in line[:200]:
                d = json.loads(line); m = d['payload']; s['meta'] = {k: m.get(k) for k in ('id', 'cwd', 'thread_source', 'source', 'originator')}
            elif '"token_usage_record"' in line[:200]:
                d = json.loads(line); u = d['payload'].get('usage') or {}
                s['calls'].append([ts(d['timestamp']), u.get('input_tokens'), u.get('output_tokens'), d['payload'].get('turn_id')])
            elif '"task_started"' in line[:300]:
                d = json.loads(line); pl = d['payload']
                s['turns'].setdefault(pl.get('turn_id'), {})['start'] = ts(d['timestamp'])
                s['turns'][pl.get('turn_id')]['trigger'] = (pl.get('turn_attribution') or {}).get('turn_trigger')
            elif '"task_complete"' in line[:300]:
                d = json.loads(line); pl = d['payload']
                t = s['turns'].setdefault(pl.get('turn_id'), {})
                t['end'] = ts(d['timestamp']); t['msg'] = (pl.get('last_agent_message') or '')[-1500:]
            elif '"role":"user"' in line or '"role": "user"' in line:
                d = json.loads(line); pl = d.get('payload') or {}
                if pl.get('type') == 'message' and pl.get('role') == 'user':
                    txt = ' '.join(c.get('text', '') for c in pl.get('content', []) if isinstance(c, dict))
                    if '## My request:' in txt: txt = txt.split('## My request:')[-1]
                    if txt.strip().startswith('<') and 'environment_context' in txt[:200]: continue
                    s['users'].append([ts(d['timestamp']), txt.strip()[-1200:]])
    except Exception as e:
        continue
    if s['calls']:
        sessions[str(p)] = s
json.dump(sessions, open(out, 'w'))
print(len(files), 'files,', len(sessions), 'sessions with calls,', sum(len(s['calls']) for s in sessions.values()), 'calls')

