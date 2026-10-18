#!/usr/bin/env python3
"""Walk-forward backtest: the three cache policies on the same requests, out of sample.

For each test day, the return-probability model is fitted only on the days before it (like the
daily retrain), then scored on the pauses that start that day. The 5-minute and 1-hour rules are
replayed over the same requests. Quota = input-side cost in base-token equivalents (5-min write
1.25x, 1-hour write 2x, read 0.05x on Opus 5.5) plus output at 5x.

  python3 analysis/backtest_walkforward.py --days 14 --test-days 7
"""
import argparse, pathlib, sys
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / 'tools'))
import tune as T

W5 = 1.25

def cached_read(r, prev, ttl, floor):
    """Tokens a request reads from cache under a given lifetime, modelled the same way for both
    lifetimes: within the lifetime it reads the previous request's prompt (the part it shares);
    after it, only the shared system prompt and tools. Uses the log's real cacheable size, not its
    real reads, because those depend on the lifetime that was in force when it was recorded."""
    cacheable = r['cr'] + r['cc']
    if prev is None:
        return min(cacheable, floor)
    if r['t'] - prev['t'] <= ttl:
        return min(cacheable, prev['P'])
    return min(cacheable, floor)

def replay(streams, t_from, t_to, floor):
    tot = dict(m5=0.0, h1=0.0, out=0.0, requests=0)
    for v in streams.values():
        prev = None
        for r in v:
            if t_from <= r['t'] < t_to:
                m = T.rmult(r['model']); u = max(0, r['P'] - r['cr'] - r['cc']); cacheable = r['cr'] + r['cc']
                r1 = cached_read(r, prev, T.TTL + 30, floor)
                r5 = cached_read(r, prev, 300, floor)
                tot['h1'] += u + T.W60 * (cacheable - r1) + m * r1
                tot['m5'] += u + W5 * (cacheable - r5) + m * r5
                tot['out'] += 5 * r['out']; tot['requests'] += 1
            prev = r
    return tot

def calibrate(streams, floor, switched_at):
    """How well the model reproduces the reads actually logged: 5-min model on 5-min-era traffic,
    1-hour model on 1-hour-era traffic."""
    res = {'5-min era': [0.0, 0.0], '1-hour era': [0.0, 0.0]}
    for v in streams.values():
        prev = None
        for r in v:
            era = '1-hour era' if r['t'] >= switched_at else '5-min era'
            ttl = T.TTL + 30 if era == '1-hour era' else 300
            res[era][0] += r['cr']; res[era][1] += cached_read(r, prev, ttl, floor)
            prev = r
    return {k: (round(a / 1e6, 1), round(b / 1e6, 1)) for k, (a, b) in res.items()}

def keepalive_net(streams, hz, floor, t_from, t_to, end):
    window = {k: v for k, v in streams.items()}
    saved = spent = 0.0; pings = rescues = 0
    for v in window.values():
        for i, x in enumerate(v):
            if not (t_from <= x['t'] < t_to):
                continue
            b = T.bucket(x['P'])
            if b < 0:
                continue
            need = T.needed(x, floor)
            nxt = v[i + 1] if i + 1 < len(v) else None
            g = nxt['t'] - x['t'] if nxt else None
            if g is not None and g <= T.TTL + 30:
                continue
            n = 0; covered = False
            for k in range(1, T.MAX_K + 1):
                t = k * T.STEP
                if (g is not None and t >= g) or (g is None and t > end - x['t']) or t + T.TTL > 24 * 3600 + 60:
                    break
                if hz['p'][b][k - 1] <= need:
                    break
                n += 1
                if g is not None and t + T.TTL >= g:
                    covered = True
                    break
            spent += n * T.ping_cost(x); pings += n
            if covered:
                saved += (T.W60 - T.rmult(x['model'])) * max(0.0, 0.98 * min(x['P'], nxt['P']) - floor); rescues += 1
    return saved - spent, saved, spent, pings, rescues

def main():
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    ap.add_argument('--days', type=float, default=14)
    ap.add_argument('--test-days', type=int, default=7)
    ap.add_argument('--switched-at', type=float, default=None, help='unix time you moved to the 1-hour cache, for the calibration check')
    a = ap.parse_args()
    streams = T.load(a.days)
    end = max(r['t'] for v in streams.values() for r in v)
    floor = T.shared_floor(streams)
    start = end - a.test_days * 86400
    agg = dict(m5=0.0, h1=0.0, out=0.0, requests=0, net=0.0, saved=0.0, spent=0.0, pings=0, rescues=0)
    weeks = {}
    for d in range(a.test_days):
        t0 = start + d * 86400; t1 = t0 + 86400
        train = {k: [r for r in v if r['t'] < t0] for k, v in streams.items()}
        train = {k: v for k, v in train.items() if v}
        hz = T.fit_hazard(train, t0)
        net, saved, spent, pings, rescues = keepalive_net(streams, hz, floor, t0, t1, end)
        rp = replay(streams, t0, t1, floor)
        for k in ('m5', 'h1', 'out', 'requests'):
            agg[k] += rp[k]
        agg['net'] += net; agg['saved'] += saved; agg['spent'] += spent; agg['pings'] += pings; agg['rescues'] += rescues
        w = weeks.setdefault(d // 7, dict(m5=0.0, h1=0.0, net=0.0))
        w['m5'] += rp['m5'] + rp['out']; w['h1'] += rp['h1'] + rp['out']; w['net'] += net
    m5 = agg['m5'] + agg['out']; h1 = agg['h1'] + agg['out']; learned = h1 - agg['net']
    pc = lambda a_, b_: 100 * (b_ - a_) / b_
    sign = lambda x: f"{abs(x):.1f}% {'less' if x >= 0 else 'more'}"
    print(f"out-of-sample: last {a.test_days} days, {agg['requests']:,} requests (model retrained daily on the days before)")
    print(f"quota (input + output, base-token equivalents):")
    print(f"  5-min cache          {m5/1e6:9.1f}M")
    print(f"  1-hour cache         {h1/1e6:9.1f}M   {sign(pc(h1, m5))} than 5-min")
    print(f"  1 h + keep-alive     {learned/1e6:9.1f}M   {sign(pc(learned, m5))} than 5-min, {sign(pc(learned, h1))} than 1-hour")
    print(f"  more usage from the same quota: {100*(m5/learned-1):.1f}% vs 5-min, {100*(h1/learned-1):.1f}% vs 1-hour")
    for i, w in sorted(weeks.items()):
        if w['m5'] <= 0 or w['h1'] <= 0:
            print(f'  week {i+1}: no usage')
            continue
        l = w['h1'] - w['net']
        print(f"  week {i+1}: 1-hour {sign(pc(w['h1'], w['m5']))} than 5-min; keep-alive {sign(pc(l, w['m5']))} than 5-min, {sign(pc(l, w['h1']))} than 1-hour")
    print(f"keep-alives: {agg['pings']:,} sent, {agg['rescues']:,} rescues, cost {agg['spent']/1e6:.1f}M, avoided {agg['saved']/1e6:.1f}M")
    if a.switched_at:
        print('calibration, cache reads logged vs modelled (M tokens):', calibrate(streams, floor, a.switched_at))

if __name__ == '__main__':
    main()

