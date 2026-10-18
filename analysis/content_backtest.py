#!/usr/bin/env python3
"""Does the content of the last exchange predict when you come back? Walk-forward test on pauses that can be
matched to a Codex session log. Compares, on the same pauses: the live table, a timing model (idle + size + this
chat's past pauses), + structural content signals, + a local bag-of-words classifier, and perfect hindsight."""
import json, math, re, sys, pathlib, collections, zlib
import numpy as np
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / 'tools'))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import tune as T
from feature_backtest import build, fit, simulate, oracle
from backtest_walkforward import replay

IDX = sys.argv[1] if len(sys.argv) > 1 else '/tmp/cache-shots/codex_index.json'
LLM = '/tmp/cache-shots/llm_labels.json'
LLMF = '/tmp/cache-shots/llm_features.json'
NH = 512
ASK = re.compile(r'\?\s*$|\b(approve|aprova|confirm|reply|respond|let me know|waiting for you|enviar|review page|your call|which one)\b', re.I)
WAIT = re.compile(r'\b(waiting|aguard|in progress|running|queued|will check|check back|monitor|next run|heartbeat)\b', re.I)
DONE = re.compile(r'\b(done|pronto|published|publicad|merged|deployed|shipped|finished|complete[d]?|live)\b', re.I)
WORD = re.compile(r"[a-zà-ú0-9']{3,}", re.I)

def context_map(streams):
    idx = json.load(open(IDX))
    byin = collections.defaultdict(list)
    for f, s in idx.items():
        for c in s['calls']:
            if c[1]: byin[c[1]].append((c[0], f, c[3]))
    ctx = {}
    for v in streams.values():
        for x in v:
            if T.bucket(x['P']) < 0: continue
            cands = [c for c in byin.get(x['P'], []) if abs(c[0] - x['t']) < 180]
            if not cands: continue
            _, f, turn = cands[0]; s = idx[f]; tr = s['turns'].get(turn, {})
            ended = tr.get('end') is not None and -5 <= tr['end'] - x['t'] <= 300
            msg = tr.get('msg', '') if ended else ''
            users = [u for u in s['users'] if u[0] <= x['t'] + 5]
            last_user = users[-1] if users else None
            tail = msg[-400:]
            struct = [1.0 if tr.get('trigger') == 'automation_heartbeat_scheduled' else 0.0,
                      1.0 if ended else 0.0,
                      1.0 if ASK.search(tail) else 0.0,
                      1.0 if WAIT.search(tail) else 0.0,
                      1.0 if DONE.search(tail) else 0.0,
                      math.log1p(len(msg) / 200),
                      math.log1p((x['t'] - last_user[0]) / 3600) if last_user else 3.0,
                      1.0 if s['meta'].get('thread_source') in ('subagent', 'agent_created_thread') else 0.0]
            vec = [0.0] * NH
            words = WORD.findall((msg[-1500:] + ' ' + (last_user[1][-800:] if last_user else '')).lower())
            for w in set(words):
                vec[zlib.crc32(w.encode()) % NH] = 1.0
            ctx[(x['t'], x['P'])] = {'struct': struct, 'text': vec}
    return ctx

def llm_hazard(p, k):
    """Chance of a return in (k, k+1] hours given none by k, from the model's 1 h / 3 h / 12 h answers."""
    p1, p3, p12 = [min(max(v, 0.001), 0.999) for v in p]
    p3 = max(p3, p1); p12 = max(p12, p3)
    xs, ys = [0, 1, 3, 12, 48], [0.0, p1, p3, p12, max(p12, min(0.999, p12 + 0.5 * (1 - p12)))]
    F = lambda h: next(ys[i] + (ys[i + 1] - ys[i]) * (h - xs[i]) / (xs[i + 1] - xs[i]) for i in range(len(xs) - 1) if xs[i] <= h <= xs[i + 1])
    a, b = F(min(k, 47)), F(min(k + 1, 48))
    return min(max((b - a) / max(1e-6, 1 - a), 1e-4), 0.999)

def main():
    streams = T.load(21)
    end = max(r['t'] for v in streams.values() for r in v)
    floor = T.shared_floor(streams)
    pauses = build(streams, floor)
    ctx = context_map(streams)
    pauses = [pz for pz in pauses if (pz['x']['t'], pz['x']['P']) in ctx and pz['rows']]
    labels = json.load(open(LLM)) if pathlib.Path(LLM).exists() else {}
    feats = json.load(open(LLMF)) if pathlib.Path(LLMF).exists() else {}
    pauses = [pz for pz in pauses if f"{pz['x']['t']}|{pz['x']['P']}" in labels and f"{pz['x']['t']}|{pz['x']['P']}" in feats]
    lg = lambda q: math.log(q / (1 - q))
    for pz in pauses:
        c = ctx[(pz['x']['t'], pz['x']['P'])]
        lab = labels[f"{pz['x']['t']}|{pz['x']['P']}"]
        pz['llm'] = [llm_hazard(lab, k) for (k, _, _, _) in pz['rows']]
        for (k, f, _, _), hk in zip(pz['rows'], pz['llm']):
            f['struct'] = c['struct']; f['text'] = c['text']
            f['llm'] = [lg(hk)] + [lg(min(max(v, 0.001), 0.999)) for v in lab]
            f['llmfeat'] = feats[f"{pz['x']['t']}|{pz['x']['P']}"]
    test_days = 14; start = end - test_days * 86400
    sets = {
        'timing: idle + size + past pauses': ['k', 'size', 'chat'],
        '+ content signals (heartbeat, ended, asks, waiting, done)': ['k', 'size', 'chat', 'struct'],
        '+ local text classifier (words of last exchange)': ['k', 'size', 'chat', 'text'],
        '+ both': ['k', 'size', 'chat', 'struct', 'text'],
        '+ cheap LLM reading the last exchange': ['k', 'size', 'chat', 'llm'],
        '+ cheap LLM + content signals': ['k', 'size', 'chat', 'struct', 'llm'],
        '+ LLM yes/no features (follow-up, thank-you, ...)': ['k', 'size', 'chat', 'llmfeat'],
        '+ LLM yes/no features + content signals': ['k', 'size', 'chat', 'struct', 'llmfeat'],
    }
    res = {n: [0.0, 0.0] for n in ['live table (hour x size)'] + list(sets) + ['cheap LLM alone (no training)', 'perfect hindsight']}
    base = 0.0; n_test = 0
    from feature_backtest import matrix
    for d in range(test_days):
        t0 = start + d * 86400; t1 = t0 + 86400
        test = [pz for pz in pauses if t0 <= pz['x']['t'] < t1]
        n_test += len(test)
        train_rows = [r for pz in pauses for r in pz['rows'] if r[3] < t0]
        rp = replay(streams, t0, t1, floor); base += rp['h1'] + rp['out']
        if not test or len(train_rows) < 50: continue
        for name, groups in sets.items():
            X = matrix(train_rows, groups); y = np.array([r[2] for r in train_rows], float)
            model = fit(X, y, l2=10.0 if 'text' in groups else 1.0)
            prob = [model(matrix(pz['rows'], groups)) for pz in test]
            s, c = simulate(test, prob, floor); res[name][0] += s; res[name][1] += c
        train = {k: [r for r in v if r['t'] < t0] for k, v in streams.items()}
        hz = T.fit_hazard({k: v for k, v in train.items() if v}, t0)
        hzp = [np.array([hz['p'][T.bucket(pz['x']['P'])][k - 1] for (k, _, _, _) in pz['rows']]) for pz in test]
        s, c = simulate(test, hzp, floor); res['live table (hour x size)'][0] += s; res['live table (hour x size)'][1] += c
        s, c = simulate(test, [np.array(pz['llm']) for pz in test], floor); res['cheap LLM alone (no training)'][0] += s; res['cheap LLM alone (no training)'][1] += c
        s, c = oracle(test, floor); res['perfect hindsight'][0] += s; res['perfect hindsight'][1] += c
    print(f'matched pauses: {len(pauses)} (test days: {n_test}); net saving on them, as % of total quota under the 1-hour cache')
    o = res['perfect hindsight'][0] - res['perfect hindsight'][1]
    for name, (s, c) in res.items():
        print(f'  {name:58s} {100 * (s - c) / base:5.2f}%   ({100 * (s - c) / o:5.1f}% of perfect)')

if __name__ == '__main__':
    main()

