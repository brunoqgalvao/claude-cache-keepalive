#!/usr/bin/env python3
"""Learn a keep-alive policy from your own opencodex usage log and write policy.json.

Replays your Anthropic conversations: for every pause longer than the 1-hour cache, it computes
what a keep-alive policy would have cost (re-reads every ~59 min) and saved (no full rewrite when
you came back). It tries a grid of "keep alive up to N hours" x "only chats above M tokens" and
keeps the best on your recent data. Deliberately overfits to you; monitor.py re-runs it daily.

Costs use Anthropic's price ratios in base-input-token equivalents: 1 h cache write 2x, cache read
0.05x on Opus/Sonnet 5.5 (0.025x Fable 5.1, 0.1x others), output 5x.
"""
import argparse, collections, json, math, os, pathlib, statistics, sys, time

HOME = pathlib.Path(os.environ.get('OPENCODEX_HOME', pathlib.Path.home() / '.opencodex')).expanduser()
D = pathlib.Path(os.environ['KEEPALIVE_DIR']).expanduser() if os.environ.get('KEEPALIVE_DIR') else HOME / 'cache-keepalive'
USAGE = pathlib.Path(os.environ['KEEPALIVE_USAGE']).expanduser() if os.environ.get('KEEPALIVE_USAGE') else (D / 'usage.jsonl' if (D / 'usage.jsonl').exists() else HOME / 'usage.jsonl')
READ = {'claude-opus-5-5': 0.05, 'claude-sonnet-5-5': 0.05, 'claude-fable-5-1': 0.025}
W60, TTL, STEP = 2.0, 3600, 3540
IDLE_GRID_H = [0, 1, 2, 3, 4, 6, 8, 12, 24]
SIZE_GRID = [20_000, 40_000, 80_000, 150_000]

def rmult(model): return READ.get(model, 0.1)

def load(days):
    since = time.time() - days * 86400
    streams = collections.defaultdict(list)
    path = USAGE
    if not path.exists():
        sys.exit(f'no usage log at {path}')
    with open(path) as f:
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
            if t < since:
                continue
            streams[(d['conversationId'], d.get('model'), d.get('provider'))].append(
                dict(t=t, model=d.get('model'), P=u['inputTokens'], cr=u['cacheReadInputTokens'],
                     cc=u['cacheCreationInputTokens'], out=u['outputTokens']))
    for v in streams.values():
        v.sort(key=lambda r: r['t'])
    return streams

def shared_floor(streams):
    """Tokens that stay cached across conversations (system prompt + tools): what a cold return still reads."""
    misses = [y['cr'] for v in streams.values() for x, y in zip(v, v[1:])
              if y['t'] - x['t'] > TTL + 30 and y['P'] > 20_000 and y['cr'] < 0.5 * min(x['P'], y['P'])]
    return int(statistics.median(misses)) if len(misses) >= 5 else 30_000

def ping_cost(x):
    return rmult(x['model']) * (x['P'] + x['out']) + W60 * 30 + 5 * 5

def evaluate(streams, idle_h, min_size, floor, end):
    n_allowed = max(0, math.floor((idle_h * 3600 + 60 - TTL) / STEP)) if idle_h else 0
    saved = spent = 0.0
    pings = rescues = 0
    for v in streams.values():
        for i, x in enumerate(v):
            if x['P'] < min_size or n_allowed == 0:
                continue
            y = v[i + 1] if i + 1 < len(v) else None
            if y is None:
                k = min(n_allowed, math.floor((end - x['t']) / STEP))
                spent += max(0, k) * ping_cost(x); pings += max(0, k)
                continue
            g = y['t'] - x['t']
            if g <= TTL + 30:
                continue
            need = math.ceil((g - TTL) / STEP)
            if need <= n_allowed:
                saved += (W60 - rmult(x['model'])) * max(0.0, 0.98 * min(x['P'], y['P']) - floor)
                spent += need * ping_cost(x); pings += need; rescues += 1
            else:
                spent += n_allowed * ping_cost(x); pings += n_allowed
    return saved - spent, pings, rescues

def baseline_cost(streams):
    return sum(max(0, r['P'] - r['cr'] - r['cc']) + W60 * r['cc'] + rmult(r['model']) * r['cr'] for v in streams.values() for r in v)

def main():
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    ap.add_argument('--days', type=float, default=14, help='how much recent history to learn from (default 14)')
    ap.add_argument('--dry-run', action='store_true', help='print the result without writing policy.json')
    a = ap.parse_args()
    streams = load(a.days)
    n = sum(len(v) for v in streams.values())
    if n < 200:
        sys.exit(f'only {n} Anthropic requests in the last {a.days:g} days; use opencodex a bit more and re-run')
    end = max(r['t'] for v in streams.values() for r in v)
    floor = shared_floor(streams)
    base = baseline_cost(streams)
    rows = []
    for h in IDLE_GRID_H:
        for m in SIZE_GRID:
            net, pings, rescues = evaluate(streams, h, m, floor, end)
            rows.append(dict(max_idle_h=h, min_prompt_tokens=m, net_tokens=net, saving_pct=100 * net / base, pings=pings, rescues=rescues))
    best = max(rows, key=lambda r: r['net_tokens'])
    span_days = max(1e-6, (end - min(r['t'] for v in streams.values() for r in v)) / 86400)
    per_day = best['pings'] / span_days
    policy = {
        'max_idle_s': int(best['max_idle_h'] * 3600),
        'min_prompt_tokens': best['min_prompt_tokens'],
        'daily_cap': max(100, int(math.ceil(per_day * 1.5 / 50) * 50)),
        'expected_saving_pct': round(best['saving_pct'], 1),
        'shared_floor_tokens': floor,
        'trained_at': time.strftime('%Y-%m-%dT%H:%M:%S'),
        'trained_on': {'days': a.days, 'requests': n, 'conversations': len(streams)},
        'grid_top': sorted(rows, key=lambda r: -r['net_tokens'])[:5],
    }
    print(f"learned from {n:,} requests in {len(streams)} conversations (last {a.days:g} days)")
    print(f"{'keep alive up to':>16} {'chats over':>11} {'saving vs 1h-only':>18} {'pings':>7} {'rescued returns':>16}")
    for r in sorted(rows, key=lambda r: -r['net_tokens'])[:8]:
        print(f"{r['max_idle_h']:>14} h {r['min_prompt_tokens'] // 1000:>9}k {r['saving_pct']:>17.1f}% {r['pings']:>7} {r['rescues']:>16}")
    if best['max_idle_h'] == 0 or best['net_tokens'] <= 0:
        print('keep-alives would not have paid on your usage; policy keeps them off')
        policy['max_idle_s'] = 0
    if not a.dry_run:
        D.mkdir(parents=True, exist_ok=True)
        (D / 'policy.json').write_text(json.dumps(policy, indent=1))
        print(f"wrote {D / 'policy.json'}: keep alive up to {best['max_idle_h']} h for chats over {best['min_prompt_tokens'] // 1000}k tokens, cap {policy['daily_cap']}/day")
    return policy

if __name__ == '__main__':
    main()
