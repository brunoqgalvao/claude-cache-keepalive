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
SIZE_EDGES = [20_000, 80_000, 200_000, 500_000]   # hazard buckets: 20-80k, 80-200k, 200-500k, 500k+
MAX_K = 24                                       # checkpoints: about every hour, up to 24 h idle

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

def bucket(P):
    b = -1
    for i, e in enumerate(SIZE_EDGES):
        if P >= e:
            b = i
    return b

def fit_hazard(streams, end):
    """P(you come back within the next hour | idle about k hours, chat size), from your history.
    At checkpoint k (idle k*STEP s) the chat is at risk if you have not come back yet; it is an event
    if you come back before the refreshed cache would expire (k*STEP + 1 h). Shrunk toward the
    all-sizes rate for small samples."""
    nb = len(SIZE_EDGES)
    ev = [[0] * MAX_K for _ in range(nb)]; risk = [[0] * MAX_K for _ in range(nb)]
    for v in streams.values():
        for i, x in enumerate(v):
            b = bucket(x['P'])
            if b < 0:
                continue
            nxt = v[i + 1] if i + 1 < len(v) else None
            g = nxt['t'] - x['t'] if nxt else None
            for k in range(1, MAX_K + 1):
                t = k * STEP
                if g is not None and g <= t:
                    break
                if g is None and end - x['t'] < t + TTL:
                    break            # outcome not known yet
                risk[b][k - 1] += 1
                if g is not None and g <= t + TTL:
                    ev[b][k - 1] += 1
    pooled = [(sum(ev[b][k] for b in range(nb)) + 1) / (sum(risk[b][k] for b in range(nb)) + 2) for k in range(MAX_K)]
    a = 8.0
    p = [[round((ev[b][k] + a * pooled[k]) / (risk[b][k] + a), 4) for k in range(MAX_K)] for b in range(nb)]
    return {'size_edges': SIZE_EDGES, 'step_s': STEP, 'p': p, 'at_risk': risk, 'returns': ev}

def needed(x, floor):
    reuse = max(0.0, 0.97 * (x['P'] + x['out']) - floor)
    return ping_cost(x) / ((W60 - rmult(x['model'])) * reuse) if reuse > 0 else 1.0

def evaluate_hazard(streams, hz, floor, end, max_idle_h=24):
    saved = spent = 0.0; pings = rescues = 0
    for v in streams.values():
        for i, x in enumerate(v):
            b = bucket(x['P'])
            if b < 0:
                continue
            need = needed(x, floor)
            nxt = v[i + 1] if i + 1 < len(v) else None
            g = nxt['t'] - x['t'] if nxt else None
            if g is not None and g <= TTL + 30:
                continue
            n = 0; covered = False
            for k in range(1, MAX_K + 1):
                t = k * STEP
                if (g is not None and t >= g) or (g is None and t > end - x['t']) or t + TTL > max_idle_h * 3600 + 60:
                    break
                if hz['p'][b][k - 1] <= need:
                    break
                n += 1
                if g is not None and t + TTL >= g:
                    covered = True
                    break
            spent += n * ping_cost(x); pings += n
            if covered:
                saved += (W60 - rmult(x['model'])) * max(0.0, 0.98 * min(x['P'], nxt['P']) - floor); rescues += 1
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
    hz = fit_hazard(streams, end)
    hnet, hpings, hres = evaluate_hazard(streams, hz, floor, end)
    span_days = max(1e-6, (end - min(r['t'] for v in streams.values() for r in v)) / 86400)
    use_hazard = hnet >= best['net_tokens'] and hnet > 0
    chosen_pings = hpings if use_hazard else best['pings']
    policy = {
        'mode': 'hazard' if use_hazard else 'rule',
        'max_idle_s': 24 * 3600 if use_hazard else int(best['max_idle_h'] * 3600),
        'min_prompt_tokens': SIZE_EDGES[0] if use_hazard else best['min_prompt_tokens'],
        'daily_cap': max(100, int(math.ceil(chosen_pings / span_days * 1.5 / 50) * 50)),
        'expected_saving_pct': round(100 * (hnet if use_hazard else best['net_tokens']) / base, 1),
        'shared_floor_tokens': floor,
        'hazard': hz,
        'hazard_result': {'saving_pct': round(100 * hnet / base, 2), 'pings': hpings, 'rescues': hres},
        'rule_result': {'max_idle_h': best['max_idle_h'], 'min_prompt_tokens': best['min_prompt_tokens'], 'saving_pct': round(best['saving_pct'], 2), 'pings': best['pings'], 'rescues': best['rescues']},
        'trained_at': time.strftime('%Y-%m-%dT%H:%M:%S'),
        'trained_on': {'days': a.days, 'requests': n, 'conversations': len(streams), 'baseline_input_tokens': round(base)},
        'grid': rows,
    }
    print(f"learned from {n:,} requests in {len(streams)} conversations (last {a.days:g} days)")
    print(f"{'keep alive up to':>16} {'chats over':>11} {'saving vs 1h-only':>18} {'pings':>7} {'rescued returns':>16}")
    for r in sorted(rows, key=lambda r: -r['net_tokens'])[:8]:
        print(f"{r['max_idle_h']:>14} h {r['min_prompt_tokens'] // 1000:>9}k {r['saving_pct']:>17.1f}% {r['pings']:>7} {r['rescues']:>16}")
    print(f"probability policy (keep alive while P(return within the hour) beats break-even): {100 * hnet / base:.1f}%, {hpings} pings, {hres} rescued")
    if max(hnet, best['net_tokens']) <= 0:
        print('keep-alives would not have paid on your usage; policy keeps them off')
        policy['max_idle_s'] = 0
    if not a.dry_run:
        D.mkdir(parents=True, exist_ok=True)
        (D / 'policy.json').write_text(json.dumps(policy, indent=1))
        what = 'probability policy' if use_hazard else f"keep alive up to {best['max_idle_h']} h for chats over {best['min_prompt_tokens'] // 1000}k tokens"
        print(f"wrote {D / 'policy.json'}: {what}, cap {policy['daily_cap']}/day")
    return policy

if __name__ == '__main__':
    main()
