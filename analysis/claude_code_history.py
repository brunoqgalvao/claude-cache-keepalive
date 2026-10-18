#!/usr/bin/env python3
"""Convert Claude Code transcripts (token counts and timestamps only) into the usage-log format the tools read.
  python3 analysis/claude_code_history.py 90 /tmp/cache-shots/cc_usage.jsonl"""
import datetime, glob, hashlib, json, os, pathlib, sys, time
days = float(sys.argv[1]) if len(sys.argv) > 1 else 90
out = sys.argv[2] if len(sys.argv) > 2 else '/tmp/cache-shots/cc_usage.jsonl'
since = time.time() - days * 86400
rows = {}; 
for f in glob.glob(str(pathlib.Path.home() / '.claude' / 'projects' / '*' / '*.jsonl')):
    if os.path.getmtime(f) < since: continue
    for line in open(f, errors='ignore'):
        if '"usage"' not in line: continue
        try: d = json.loads(line)
        except Exception: continue
        if d.get('isSidechain'): continue
        m = d.get('message') or {}; u = m.get('usage') or {}
        if 'cache_read_input_tokens' not in u: continue
        t = datetime.datetime.fromisoformat(d['timestamp'].replace('Z', '+00:00')).timestamp()
        if t < since: continue
        key = m.get('id') or d.get('uuid')            # one API call can span several transcript lines
        cr, cc, inp = (int(u.get(k) or 0) for k in ('cache_read_input_tokens', 'cache_creation_input_tokens', 'input_tokens'))
        r = {'provider': 'anthropic-claude-code-history', 'timestamp': int(t * 1000), 'status': 200, 'model': m.get('model'),
             'conversationId': hashlib.sha256(str(d.get('sessionId')).encode()).hexdigest()[:32],
             'usage': {'inputTokens': inp + cr + cc, 'cacheReadInputTokens': cr, 'cacheCreationInputTokens': cc, 'outputTokens': int(u.get('output_tokens') or 0)}}
        if key not in rows or r['usage']['outputTokens'] > rows[key]['usage']['outputTokens']:
            rows[key] = r
rs = sorted(rows.values(), key=lambda r: r['timestamp'])
open(out, 'w').write(''.join(json.dumps(r) + '\n' for r in rs))
if rs:
    print(len(rs), 'API calls', datetime.datetime.fromtimestamp(rs[0]['timestamp'] / 1000), '->', datetime.datetime.fromtimestamp(rs[-1]['timestamp'] / 1000))

