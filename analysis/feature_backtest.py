#!/usr/bin/env python3
"""Do more features help the keep-alive? Walk-forward comparison against the live hour x size table.

For every pause longer than the 1-hour cache and every hourly checkpoint in it, builds features known
at that moment, fits a logistic model each day on earlier days only, and replays the keep-alive
decisions on the next day. Reports net saving (rewrites avoided minus keep-alive cost) as a share of
total quota under the 1-hour cache, for several feature sets, the live table, and perfect hindsight.

  python3 analysis/feature_backtest.py --days 21 --test-days 14
"""
import argparse, bisect, datetime, math, pathlib, statistics, sys, zoneinfo
import numpy as np
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / 'tools'))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import tune as T
from backtest_walkforward import replay, keepalive_net

TZ = zoneinfo.ZoneInfo('America/Sao_Paulo')
KBINS = [1, 2, 3, 4, 7, 13]          # idle-hour bins: 1,2,3,4-6,7-12,13-24
HBINS = [0, 6, 12, 18]               # hour of day bins

def onehot(v, edges):
    o = [0.0] * len(edges)
    i = max(j for j, e in enumerate(edges) if v >= e)
    o[i] = 1.0
    return o

def build(streams, floor):
    allt = sorted(r['t'] for v in streams.values() for r in v)
    by_chat_start = {}
    pauses = []
    for key, v in streams.items():
        gaps = []
        for i, x in enumerate(v):
            b = T.bucket(x['P'])
            nxt = v[i + 1] if i + 1 < len(v) else None
            g = nxt['t'] - x['t'] if nxt else None
            if b >= 0 and (g is None or g > T.TTL + 30):
                prior = [q for q in gaps if q > 300]
                longs = [q for q in gaps if q > T.TTL + 30][-6:]
                per = statistics.median(longs) if len(longs) >= 3 else None
                regular = 1.0 if per and statistics.pstdev(longs) / per < 0.2 else 0.0
                rows = []
                for k in range(1, T.MAX_K + 1):
                    t = x['t'] + k * T.STEP
                    if g is not None and k * T.STEP >= g:
                        break
                    hod = datetime.datetime.fromtimestamp(t, TZ).hour
                    j = bisect.bisect_left(allt, t)
                    last_any = allt[j - 1] if j > 0 else x['t']
                    n_hour = j - bisect.bisect_left(allt, t - 3600)
                    feats = {
                        'k': onehot(k, KBINS),
                        'size': onehot(b, [0, 1, 2, 3]),
                        'hour': onehot(hod, HBINS),
                        'chat': [math.log1p(len(prior)), math.log1p(statistics.median(prior) / 60) if prior else 0.0,
                                 1.0 if not prior else 0.0, math.log1p(i), math.log1p(x['out'] / 100)],
                        'global': [math.log1p(max(0.0, t - last_any) / 60), math.log1p(n_hour),
                                   1.0 if t - last_any > 3 * 3600 else 0.0],
                        # schedule: does this chat usually come back in exactly this window?
                        'sched': [(sum(1 for q in longs if k * T.STEP < q <= k * T.STEP + T.TTL) + 0.0) / (len(longs) + 1),
                                  regular,
                                  regular * math.exp(-((k * T.STEP + T.TTL / 2 - per) / (0.25 * per)) ** 2) if per else 0.0,
                                  regular * (1.0 if per and k * T.STEP < per else 0.0)],
                    }
                    label = 1 if (g is not None and g <= k * T.STEP + T.TTL) else 0
                    known_at = t + T.TTL if g is None or g > k * T.STEP + T.TTL else x['t'] + g
                    rows.append((k, feats, label, known_at))
                pauses.append(dict(x=x, nxt=nxt, g=g, rows=rows, need=T.needed(x, floor), regular=regular, per=per))
            if g is not None:
                gaps.append(g)
    return pauses

def matrix(rows, groups):
    return np.array([[1.0] + sum((f[gr] for gr in groups), []) for (_, f, _, _) in rows])

def fit(X, y, l2=1.0, iters=25):
    mu = X[:, 1:].mean(0); sd = X[:, 1:].std(0) + 1e-6
    Z = X.copy(); Z[:, 1:] = (Z[:, 1:] - mu) / sd
    w = np.zeros(Z.shape[1])
    for _ in range(iters):
        p = 1 / (1 + np.exp(-Z @ w))
        g = Z.T @ (p - y) + l2 * np.r_[0, w[1:]]
        H = (Z * (p * (1 - p))[:, None]).T @ Z + l2 * np.diag(np.r_[0, np.ones(len(w) - 1)])
        w -= np.linalg.solve(H, g)
    return lambda X2: 1 / (1 + np.exp(-(np.c_[X2[:, :1], (X2[:, 1:] - mu) / sd] @ w)))

def simulate(pauses, prob, floor):
    saved = spent = 0.0
    for pz, ps in zip(pauses, prob):
        x, nxt, g = pz['x'], pz['nxt'], pz['g']
        n = 0; covered = False
        for (k, _, _, _), p in zip(pz['rows'], ps):
            if p <= pz['need']:
                break
            n += 1
            if g is not None and k * T.STEP + T.TTL >= g:
                covered = True
                break
        spent += n * T.ping_cost(x)
        if covered:
            saved += (T.W60 - T.rmult(x['model'])) * max(0.0, 0.98 * min(x['P'], nxt['P']) - floor)
    return saved, spent

def oracle(pauses, floor):
    saved = spent = 0.0
    for pz in pauses:
        x, nxt, g = pz['x'], pz['nxt'], pz['g']
        if g is None or g > 24 * 3600 + 60:
            continue
        n = math.ceil((g - T.TTL) / T.STEP)
        s = (T.W60 - T.rmult(x['model'])) * max(0.0, 0.98 * min(x['P'], nxt['P']) - floor); c = n * T.ping_cost(x)
        if s > c:
            saved += s; spent += c
    return saved, spent

def main():
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    ap.add_argument('--days', type=float, default=21)
    ap.add_argument('--test-days', type=int, default=14)
    a = ap.parse_args()
    streams = T.load(a.days)
    end = max(r['t'] for v in streams.values() for r in v)
    floor = T.shared_floor(streams)
    pauses = build(streams, floor)
    start = end - a.test_days * 86400
    sets = {
        'idle + size (same inputs as live)': ['k', 'size'],
        '+ time of day': ['k', 'size', 'hour'],
        '+ this chat\'s past pauses': ['k', 'size', 'chat'],
        '+ activity in all chats': ['k', 'size', 'global'],
        '+ schedule (when this chat usually fires)': ['k', 'size', 'sched'],
        'all of the above': ['k', 'size', 'hour', 'chat', 'global', 'sched'],
    }
    res = {name: [0.0, 0.0] for name in list(sets) + ['live table (hour x size)', 'perfect hindsight']}
    res['past pauses, schedule only on regular chats'] = [0.0, 0.0]
    reg = {name: [0.0, 0.0] for name in ['live table (hour x size)', '+ schedule (when this chat usually fires)', 'perfect hindsight']}
    nreg = ntest = 0
    base = 0.0
    for d in range(a.test_days):
        t0 = start + d * 86400; t1 = t0 + 86400
        test = [pz for pz in pauses if t0 <= pz['x']['t'] < t1 and pz['rows']]
        ntest += len(test); nreg += sum(1 for pz in test if pz['regular'])
        regtest = [pz for pz in test if pz['regular']]
        train_rows = [r for pz in pauses for r in pz['rows'] if r[3] < t0]
        rp = replay(streams, t0, t1, floor); base += rp['h1'] + rp['out']
        for name, groups in sets.items():
            X = matrix(train_rows, groups); y = np.array([r[2] for r in train_rows], float)
            model = fit(X, y)
            prob = [model(matrix(pz['rows'], groups)) for pz in test]
            if name == "+ this chat's past pauses": prob_chat = prob
            if name == '+ schedule (when this chat usually fires)': prob_sched = prob
            s, c = simulate(test, prob, floor); res[name][0] += s; res[name][1] += c
            if name in reg:
                s2, c2 = simulate(regtest, [p_ for pz, p_ in zip(test, prob) if pz['regular']], floor); reg[name][0] += s2; reg[name][1] += c2
        mix = [ps if pz['regular'] else pc for pz, pc, ps in zip(test, prob_chat, prob_sched)]
        s, c = simulate(test, mix, floor); res['past pauses, schedule only on regular chats'][0] += s; res['past pauses, schedule only on regular chats'][1] += c
        train = {k: [r for r in v if r['t'] < t0] for k, v in streams.items()}
        hz = T.fit_hazard({k: v for k, v in train.items() if v}, t0)
        _, s, c, _, _ = keepalive_net(streams, hz, floor, t0, t1, end)
        res['live table (hour x size)'][0] += s; res['live table (hour x size)'][1] += c
        hzprob = [np.array([hz['p'][T.bucket(pz['x']['P'])][k - 1] for (k, _, _, _) in pz['rows']]) for pz in regtest]
        s2, c2 = simulate(regtest, hzprob, floor); reg['live table (hour x size)'][0] += s2; reg['live table (hour x size)'][1] += c2
        s, c = oracle(test, floor); res['perfect hindsight'][0] += s; res['perfect hindsight'][1] += c
        s2, c2 = oracle(regtest, floor); reg['perfect hindsight'][0] += s2; reg['perfect hindsight'][1] += c2
    print(f'walk-forward, last {a.test_days} days; net saving as % of quota under the 1-hour cache')
    for name, (s, c) in res.items():
        print(f'  {name:36s} {100 * (s - c) / base:5.1f}%   (avoided {s/1e6:6.0f}M, keep-alives {c/1e6:5.0f}M)')
    print(f'scheduled chats (regular gaps): {nreg} of {ntest} pauses; net saving on those pauses, % of total quota:')
    for name, (s, c) in reg.items():
        print(f'  {name:36s} {100 * (s - c) / base:5.1f}%   (avoided {s/1e6:6.0f}M, keep-alives {c/1e6:5.0f}M)')

if __name__ == '__main__':
    main()

