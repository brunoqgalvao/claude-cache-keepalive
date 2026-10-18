#!/usr/bin/env python3
"""Daily check: re-tune the policy on recent usage, then measure what keep-alives actually did.

Realized saving = for every return after more than 1 h idle that found its cache warm, the full
rewrite it avoided (2x minus the read price, above the always-cached system prompt), minus the
cost of every keep-alive sent. If keep-alives lost tokens over the last 72 h (with at least 20
sent), this writes the file "disabled" and they stop until you run install.py --enable. If the
tuner finds they would not pay on your recent usage, they stop too, and come back on by themselves
when a later retune finds they pay again.
"""
import json, os, pathlib, subprocess, sys, time

HERE = pathlib.Path(__file__).resolve().parent
HOME = pathlib.Path(os.environ.get('OPENCODEX_HOME', pathlib.Path.home() / '.opencodex')).expanduser()
D = pathlib.Path(os.environ['KEEPALIVE_DIR']).expanduser() if os.environ.get('KEEPALIVE_DIR') else HOME / 'cache-keepalive'
USAGE = pathlib.Path(os.environ['KEEPALIVE_USAGE']).expanduser() if os.environ.get('KEEPALIVE_USAGE') else (D / 'usage.jsonl' if (D / 'usage.jsonl').exists() else HOME / 'usage.jsonl')
READ = {'claude-opus-5-5': 0.05, 'claude-sonnet-5-5': 0.05, 'claude-fable-5-1': 0.025}
WINDOW_S = 72 * 3600

def realized(floor):
    since = time.time() - WINDOW_S
    try:
        installed = json.loads((D / 'state.json').read_text())['switched_at']
    except Exception:
        installed = since
    since = max(since, installed)
    spent = 0.0; pings = 0
    log = D / 'keepalive.jsonl'
    if log.exists():
        for line in open(log):
            try:
                e = json.loads(line)
            except Exception:
                continue
            if e.get('event') == 'ping' and e['ts'] / 1000 >= since and e.get('status') == 200:
                pings += 1
                spent += READ.get(e.get('model'), 0.1) * e.get('read', 0) + 2.0 * e.get('write', 0) + e.get('input', 0) + 10
    last = {}; saved = 0.0; rescues = 0; base = 0.0
    if not USAGE.exists():  # fresh install: nothing to measure yet, keep the default policy running
        return dict(pings=pings, rescues=0, saved=0, spent=round(spent), net=round(-spent), net_pct=None, since=since)
    with open(USAGE) as f:
        for line in f:
            if '"anthropic' not in line[:400]:
                continue
            try:
                d = json.loads(line)
            except Exception:
                continue
            u = d.get('usage')
            if d.get('status') != 200 or not u or not d.get('conversationId'):
                continue
            t = d['timestamp'] / 1000
            k = (d['conversationId'], d.get('model'), d.get('provider'))
            r = READ.get(d.get('model'), 0.1)
            if t >= since:
                base += max(0, u['inputTokens'] - u['cacheReadInputTokens'] - u['cacheCreationInputTokens']) + 2.0 * u['cacheCreationInputTokens'] + r * u['cacheReadInputTokens']
                p = last.get(k)
                if p and t - p[0] > 3630 and u['cacheReadInputTokens'] >= 0.8 * min(p[1], u['inputTokens']):
                    rescues += 1
                    saved += (2.0 - r) * max(0, u['cacheReadInputTokens'] - floor)
            last[k] = (t, u['inputTokens'])
    return dict(pings=pings, rescues=rescues, saved=round(saved), spent=round(spent), net=round(saved - spent),
                net_pct=round(100 * (saved - spent) / base, 1) if base else None, since=since)

def main():
    D.mkdir(parents=True, exist_ok=True)
    tune = subprocess.run([sys.executable, str(HERE / 'tune.py'), '--days', '14'], capture_output=True, text=True)
    policy = {}
    try:
        policy = json.loads((D / 'policy.json').read_text())
    except Exception:
        pass
    r = realized(policy.get('shared_floor_tokens', 30_000))
    action = 'ok'
    reason = None; kind = None
    if r['pings'] >= 20 and r['net'] < 0:
        kind = 'lost'
        reason = f"keep-alives lost {-r['net']:,} token-equivalents over the last 72 h ({r['pings']} sent, {r['rescues']} returns rescued)"
    elif policy.get('max_idle_s') == 0:
        kind = 'tuner_off'
        reason = 'the tuner found keep-alives would not pay on your last 14 days'
    flag = D / 'disabled'
    prev = {}
    if flag.exists():
        try:
            prev = json.loads(flag.read_text())
        except Exception:
            prev = {}
    if reason:
        action = 'disabled'
        flag.write_text(json.dumps({'by': 'monitor', 'kind': kind, 'at': time.strftime('%Y-%m-%dT%H:%M:%S'), 'reason': reason}) + '\n')
    elif flag.exists() and prev.get('by') == 'monitor' and prev.get('kind') == 'tuner_off':
        flag.rename(D / f'disabled.cleared-{int(time.time())}')
        action = 're-enabled: the tuner now finds keep-alives pay on your recent usage'
    elif flag.exists():
        action = 'still disabled (re-enable with install.py --enable)'
    status = dict(checked_at=time.strftime('%Y-%m-%dT%H:%M:%S'), action=action, reason=reason, realized_72h=r,
                  policy={k: policy.get(k) for k in ('mode', 'max_idle_s', 'min_prompt_tokens', 'daily_cap', 'expected_saving_pct', 'trained_at')},
                  tune_ok=tune.returncode == 0)
    (D / 'status.json').write_text(json.dumps(status, indent=1))
    print(json.dumps(status, indent=1))

if __name__ == '__main__':
    main()
