#!/usr/bin/env python3
"""Keep-alive dashboard, recomputed from the local logs. http://127.0.0.1:8797/

  /           Savings & live: what keep-alives saved and cost over a chosen window, as big numbers and a
              chart, and the chats whose cache is warm right now with the chance the policy gives you
              return and what it will do at the next check.
  /decisions  Every idle pause graded after the fact: paid off, wasted, missed or no keep-alive needed,
              next to the best a policy that knew when you'd come back could have done.
  /policy     The learned rule, its break-even, your return probabilities and the cache-rule comparison.

Titles of Codex chats are read from ~/.codex when present (opencodex logs a hash of the thread id).
Nothing leaves the machine."""
import bisect, collections, datetime, hashlib, html, http.server, json, math, os, pathlib, sqlite3, subprocess, threading, time, urllib.parse

HOME = pathlib.Path(os.environ.get('OPENCODEX_HOME', pathlib.Path.home() / '.opencodex')).expanduser()
D = pathlib.Path(os.environ['KEEPALIVE_DIR']).expanduser() if os.environ.get('KEEPALIVE_DIR') else HOME / 'cache-keepalive'
USAGE = pathlib.Path(os.environ['KEEPALIVE_USAGE']).expanduser() if os.environ.get('KEEPALIVE_USAGE') else (D / 'usage.jsonl' if (D / 'usage.jsonl').exists() else HOME / 'usage.jsonl')
KA = D / 'keepalive.jsonl'
CODEX_HOME = pathlib.Path(os.environ.get('CODEX_HOME', pathlib.Path.home() / '.codex')).expanduser()
READ = {'claude-opus-5-5': 0.05, 'claude-sonnet-5-5': 0.05, 'claude-fable-5-1': 0.025}
PRICE = {'claude-opus-5-5': 4.0}            # US$ per million base input tokens (API list); others assumed equal
W5, W60, TTL, LEAD = 1.25, 2.0, 3600, 60
OUTW = 5.0   # output tokens cost 5x input; plan meters count them too
DEFAULT_POLICY = {'mode': 'rule', 'max_idle_s': 4 * 3600, 'min_prompt_tokens': 40000, 'daily_cap': 400, 'shared_floor_tokens': 30000}
RANGES = {'6h': 6 * 3600, '24h': 86400, '3d': 3 * 86400, '7d': 7 * 86400, '30d': 30 * 86400, 'all': None}
TZ = datetime.datetime.now().astimezone().tzinfo
esc = html.escape


def _json(p):
    try:
        return json.loads(p.read_text())
    except Exception:
        return {}

def _num(x): return x if isinstance(x, (int, float)) else 0
def rm(model): return READ.get(model, 0.1)
def usd(tokens, model=None): return tokens * PRICE.get(model, 4.0) / 1e6
def fmt_tok(x): return f"{x / 1e6:,.1f}M" if abs(x) >= 1e6 else f"{x / 1e3:,.0f}k"
def fmt_usd(x): return f"US$ {x:,.0f}" if abs(x) >= 10 else f"US$ {x:,.2f}"
def when(ts): return datetime.datetime.fromtimestamp(ts, TZ).strftime('%d/%m %H:%M')
def hm(ts): return datetime.datetime.fromtimestamp(ts, TZ).strftime('%H:%M')

def dur(s):
    s = max(0, s)
    if s < 3600:
        return f"{round(s / 60)} min"
    if s < 86400:
        h, m = divmod(round(s / 60), 60)
        return f"{h} h {m:02d}" if m else f"{h} h"
    return f"{s / 86400:.1f} d"


# ---------------------------------------------------------------- reading the logs

class Tail:
    """A JSONL file read incrementally: only lines appended since the last call are parsed."""
    CHUNK = 1 << 24

    def __init__(self, path, parse):
        self.path, self.parse = path, parse
        self.pos, self.ino, self.rows = 0, None, []

    def update(self):
        try:
            st = self.path.stat()
        except OSError:
            changed = bool(self.rows)
            self.pos, self.ino, self.rows = 0, None, []
            return changed
        if st.st_ino != self.ino or st.st_size < self.pos:
            self.pos, self.ino, self.rows = 0, st.st_ino, []
        if st.st_size <= self.pos:
            return False
        with open(self.path, 'rb') as f:
            f.seek(self.pos)
            base, rest = self.pos, b''
            while True:
                chunk = f.read(self.CHUNK)
                if not chunk:
                    break
                buf = rest + chunk
                cut = buf.rfind(b'\n')
                if cut < 0:
                    rest = buf
                    continue
                for line in buf[:cut].split(b'\n'):
                    r = self.parse(line)
                    if r is not None:
                        self.rows.append(r)
                base += cut + 1
                rest = buf[cut + 1:]
        self.pos = base
        return True


def parse_usage(since):
    def parse(line):
        if b'"anthropic' not in line[:400]:
            return None
        try:
            d = json.loads(line)
        except ValueError:
            return None
        u = d.get('usage')
        if d.get('status') != 200 or not u or not d.get('conversationId'):
            return None
        t = _num(d.get('timestamp')) / 1000
        if t < since:
            return None
        return dict(t=t, done=t + _num(d.get('durationMs')) / 1000, model=d.get('model'), prov=d.get('provider'), cid=d['conversationId'],
                    P=_num(u.get('inputTokens')), cr=_num(u.get('cacheReadInputTokens')), cc=_num(u.get('cacheCreationInputTokens')), out=_num(u.get('outputTokens')))
    return parse


def parse_ka(since):
    def parse(line):
        try:
            e = json.loads(line)
        except ValueError:
            return None
        if not isinstance(e, dict) or _num(e.get('ts')) / 1000 < since:
            return None
        return e
    return parse


class Titles:
    """Codex chat titles keyed by opencodex's conversation id, sha256(thread id)[:32]."""

    def __init__(self):
        self.map, self.checked = {}, 0.0

    def get(self):
        if time.time() - self.checked < 60:
            return self.map
        self.checked = time.time()
        names, rows = {}, []
        try:
            with open(CODEX_HOME / 'session_index.jsonl') as f:
                for line in f:
                    try:
                        d = json.loads(line)
                        names[d['id']] = d.get('thread_name')
                    except Exception:
                        pass
        except OSError:
            pass
        dbs = sorted(list(CODEX_HOME.glob('state_*.sqlite')) + list(CODEX_HOME.glob('sqlite/state_*.sqlite')), key=lambda p: p.stat().st_mtime)
        if dbs:
            try:
                con = sqlite3.connect(f"file:{dbs[-1]}?mode=ro", uri=True, timeout=1)
                rows = con.execute('select id, cwd, title, name, agent_nickname from threads').fetchall()
                con.close()
            except Exception:
                rows = []
        out = {}
        for tid, cwd, title, name, nick in rows:
            first = (title or '').strip().split('\n')[0][:90]
            label = names.get(tid) or name or first or tid
            if nick:
                label = f"{nick} · {label}"
            out[hashlib.sha256(tid.encode()).hexdigest()[:32]] = dict(title=label, project=pathlib.Path(cwd or '').name)
        for tid, nm in names.items():
            h = hashlib.sha256(tid.encode()).hexdigest()[:32]
            if h not in out and nm:
                out[h] = dict(title=nm, project='')
        self.map = out
        return out


class ProcStart:
    """Start time of the process holding keep-alive state in memory (opencodex or the Claude Code proxy).
    Chats last used before it started are no longer tracked."""

    def __init__(self):
        self.value, self.checked = None, 0.0

    def get(self):
        if time.time() - self.checked < 30:
            return self.value
        self.checked = time.time()
        pat = os.environ.get('KEEPALIVE_PROCESS') or ('keepalive_proxy.py' if (D / 'keepalive_proxy.py').exists() else 'opencodex')
        best = None
        try:
            out = subprocess.run(['ps', '-axo', 'lstart=,command='], capture_output=True, text=True, timeout=3).stdout
            for line in out.splitlines():
                cmd = line[24:]
                if pat not in cmd or 'dashboard' in cmd or (pat == 'opencodex' and ' start' not in cmd):
                    continue
                t = time.mktime(time.strptime(' '.join(line[:24].split()), '%a %b %d %H:%M:%S %Y'))
                best = t if best is None else max(best, t)
        except Exception:
            best = None
        self.value = best
        return best


# ---------------------------------------------------------------- the model of what happened

def decide(pol, model, prompt, out, idle):
    """Python twin of decide() in cache-keepalive.ts. Returns (keep, p_return, p_needed, reason, strip)."""
    r = rm(model)
    floor = pol.get('shared_floor_tokens', 30000)
    reuse = max(0.0, 0.97 * (prompt + out) - floor)
    need = (r * (prompt + out) + 85) / ((W60 - r) * reuse) if reuse > 0 else 1.0
    max_idle = pol.get('max_idle_s', 4 * 3600)
    hz = pol.get('hazard')
    if pol.get('mode') != 'hazard' or not hz:
        ok = idle + TTL <= max_idle + 60 and prompt >= pol.get('min_prompt_tokens', 40000)
        return ok, None, need, ('rule' if ok else 'max_idle'), None
    b = -1
    for i, e in enumerate(hz['size_edges']):
        if prompt >= e:
            b = i
    if b < 0:
        return False, None, need, 'too_small', None
    row, step = hz['p'][b], hz['step_s']
    k = max(1, min(len(row), int(idle / step + 0.5)))
    p = row[k - 1]
    ok = p > need and idle + TTL <= max_idle + 60
    strip = [bool(row[j] > need and (j + 1) * step + TTL <= max_idle + 60) for j in range(len(row))]
    return ok, p, need, ('worth_it' if ok else ('max_idle' if p > need else 'not_worth_it')), strip


def norm_decision(e, floor):
    m = rm(e.get('model'))
    t = _num(e.get('ts')) / 1000
    prompt, out = _num(e.get('prompt')), _num(e.get('out'))
    need = e.get('p_needed')
    if need is None and prompt:
        reuse = max(0.0, 0.97 * (prompt + out) - floor)
        need = round((m * (prompt + out) + 85) / ((W60 - m) * reuse), 4) if reuse > 0 else 1.0
    ev = e.get('event')
    ok = ev == 'ping' and e.get('status') == 200 and 'error' not in e
    cost = m * _num(e.get('read')) + W60 * _num(e.get('write')) + _num(e.get('input')) + 10 if ok else 0.0
    kind = 'kept' if ok else ('failed' if ev == 'ping' else 'stopped')
    return dict(t=t, start=t - _num(e.get('ms')) / 1000, idle=_num(e.get('idle_s')), last_done=t - _num(e.get('idle_s')), model=e.get('model'),
                prompt=prompt, p=e.get('p_return', e.get('p')), need=need, kind=kind, reason=e.get('reason'),
                error=str(e.get('error', e.get('status', '')))[:80] if kind == 'failed' else None, cost=cost, conv=e.get('conv'),
                read=(_num(e.get('read')) / prompt if ok and prompt else None))


STOP_WHY = {'not_worth_it': 'the policy let it go: chance of return below break-even', 'max_idle': 'the policy let it go: idle limit reached',
            'daily_cap': 'stopped: daily keep-alive cap reached', 'cache_changed': "stopped: the chat's opening changed",
            'too_small': 'not kept: chat too small', 'disabled': 'stopped: keep-alives were switched off', 'no_valid_token': 'stopped: login expired'}


class Store:
    def __init__(self):
        self.lock = threading.Lock()
        self.titles, self.proc = Titles(), ProcStart()
        self.state_sig = None
        self.checked = 0.0
        self.built = None
        self.sig = None
        self._reset()

    def _reset(self):
        st = _json(D / 'state.json')
        self.t0 = st.get('switched_at', time.time() - 86400)
        self.usage = Tail(USAGE, parse_usage(min(self.t0 - 86400, time.time() - 8 * 86400)))   # covers the weekly plan window
        self.ka = Tail(KA, parse_ka(self.t0))

    def model(self):
        with self.lock:
            if time.time() - self.checked > 5 or self.built is None:
                self.checked = time.time()
                try:
                    ssig = (D / 'state.json').stat().st_mtime
                except OSError:
                    ssig = None
                if ssig != self.state_sig:
                    self.state_sig = ssig
                    self._reset()
                self.usage.update()
                self.ka.update()
                try:
                    psig = (D / 'policy.json').stat().st_mtime
                except OSError:
                    psig = None
                sig = (self.usage.pos, self.ka.pos, psig, ssig)
                if sig != self.sig or self.built is None:
                    self.sig = sig
                    self.built = build(self)
            return self.built


def split_contexts(rows):
    """One conversation id can carry several cache contexts (side requests, forks, parallel calls), and the
    keep-alive tracks each one separately. A request continues the context whose last prompt it reads from
    the cache, or, when it reads nothing, the context of about its size that went quiet more than 5 min ago."""
    out, lasts = [], []
    for r in rows:
        best, bd = None, None
        for i, l in enumerate(lasts):
            if l is not None and r['cr'] >= 0.8 * min(l['P'], r['P']) and r['cr'] <= l['P'] + 4096:
                d = abs(r['cr'] - l['P'])
                if bd is None or d < bd:
                    best, bd = i, d
        if best is None:
            for i, l in enumerate(lasts):
                if l is not None and r['t'] - l['t'] > 300 and 0.9 * l['P'] <= r['P'] <= 1.5 * l['P'] + 50000:
                    d = abs(r['P'] - l['P'])
                    if bd is None or d < bd:
                        best, bd = i, d
        if best is None:
            out.append([r])
            lasts.append(r)
        else:
            out[best].append(r)
            lasts[best] = r
        for i, l in enumerate(lasts):   # contexts idle for over a day are done
            if l is not None and r['t'] - l['t'] > 26 * 3600:
                lasts[i] = None
    return out


def build(store):
    pol = _json(D / 'policy.json') or dict(DEFAULT_POLICY)
    floor = pol.get('shared_floor_tokens', 30000)
    t0 = store.t0
    chats = collections.defaultdict(list)
    for r in store.usage.rows:
        chats[(r['cid'], r['model'], r['prov'])].append(r)
    streams = {}
    for k, v in chats.items():
        v.sort(key=lambda r: r['t'])
        for j, ctx in enumerate(split_contexts(v)):
            streams[k + (j,)] = ctx
    by_cid = collections.defaultdict(list)
    idx = collections.defaultdict(list)
    for k, v in streams.items():
        by_cid[k[0]].append(k)
        for r in v:
            idx[(r['model'], r['P'])].append((r['done'], k))
    decs = collections.defaultdict(list)
    events = []
    for e in store.ka.rows:
        d = norm_decision(e, floor)
        # the keep-alive key hashes the context's opening; match it to the usage row with the same prompt size
        # that finished when the keep-alive says the chat went idle
        key, best = None, 300.0
        for done, k in idx.get((d['model'], d['prompt']), ()):
            if abs(done - d['last_done']) < best:
                best, key = abs(done - d['last_done']), k
        d['key'] = key
        events.append(d)
        if key:
            decs[key].append(d)
    latest = collections.defaultdict(float)   # last activity per chat, across its contexts
    for k, rows in streams.items():
        latest[k[:3]] = max(latest[k[:3]], rows[-1]['t'])
    episodes, open_ep = [], {}
    for k, rows in streams.items():
        ds = sorted(decs.get(k, []), key=lambda d: d['t'])
        dts = [d['t'] for d in ds]
        m = rm(k[1])
        for i, r in enumerate(rows):
            if r['t'] < t0:
                continue
            nxt = rows[i + 1] if i + 1 < len(rows) else None
            gap = nxt['t'] - r['t'] if nxt else None
            lo = bisect.bisect_right(dts, r['t'])
            hi = bisect.bisect_left(dts, nxt['t']) if nxt else len(dts)
            mine = ds[lo:hi]
            if nxt and not mine and gap <= TTL + 30:
                continue
            oks = [d for d in mine if d['kind'] == 'kept']
            ep = dict(key=k, cid=k[0], model=k[1], last_t=r['t'], start=r['done'], prompt=r['P'], out=r['out'], decisions=mine,
                      pings=len(oks), cost=sum(d['cost'] for d in oks), touch=max([r['t']] + [d['start'] for d in oks]),
                      ret=nxt['t'] if nxt else None, gap=gap, unit=m * (r['P'] + r['out']) + 85, ret_P=nxt['P'] if nxt else None,
                      side=nxt is None and latest[k[:3]] > r['t'] + 1,
                      warm=bool(nxt and gap > TTL + 30 and nxt['cr'] >= 0.8 * min(r['P'], nxt['P'])),
                      saved_if_warm=(W60 - m) * max(0, nxt['cr'] - floor) if nxt else 0.0,
                      value=(W60 - m) * max(0, min(r['P'], nxt['P']) - floor) if nxt else 0.0)
            for d in mine:
                d['returned'] = bool(nxt and d['t'] < nxt['t'] <= d['t'] + TTL)
                d['ret_in'] = nxt['t'] - d['t'] if nxt else None
            episodes.append(ep)
            if nxt is None:
                open_ep[k] = ep
    return dict(pol=pol, floor=floor, t0=t0, streams=streams, events=events, episodes=episodes, open_ep=open_ep,
                status=_json(D / 'status.json'), disabled=_json(D / 'disabled') if (D / 'disabled').exists() else None)


def classify(ep, now, pol, proc_start):
    """Grade a finished pause. Returns None while its cache is still warm and nobody has come back."""
    if ep['ret'] is None and now - ep['touch'] < TTL + 30:
        return None
    min_p = pol.get('min_prompt_tokens', 20000)
    max_idle = pol.get('max_idle_s', 4 * 3600)
    gap, n = ep['gap'], ep['pings']
    saved = ep['saved_if_warm'] if ep['warm'] and n else 0.0
    value = ep['value'] if gap is not None and gap > TTL + 30 and not ep['warm'] else saved
    compacted = gap is not None and not ep['warm'] and ep['ret_P'] is not None and ep['ret_P'] < 0.7 * ep['prompt']
    if compacted:
        value = 0.0
    need_n = math.ceil((gap - TTL) / (TTL - LEAD)) if gap is not None and gap > TTL + 30 else 0
    best = max(0.0, value - need_n * ep['unit']) if gap is not None and gap <= max_idle + 60 else 0.0
    actual = saved - ep['cost']
    best = max(best, actual)
    last = ep['decisions'][-1] if ep['decisions'] else None
    away = f"after {dur(gap)}" if gap is not None else ''
    kept = f"Kept alive {n}× ({fmt_tok(ep['cost'])})" if n else ''
    if saved > 0:
        outcome = 'paid'
        why = None
        story = f"{kept}. You came back {away} and the {fmt_tok(ep['prompt'])} cache was still warm: {fmt_tok(saved)} of rewrite avoided."
    elif best > 0:
        outcome = 'missed'
        if last and last['kind'] == 'stopped':
            why = STOP_WHY.get(last['reason'], 'stopped: ' + str(last['reason']))
        elif last and last['kind'] == 'failed':
            why = 'a keep-alive failed'
        elif last:
            why = 'dropped: keep-alives stopped without a decision (likely an opencodex restart)'
        elif ep['prompt'] < min_p:
            why = f"not tracked: under {min_p // 1000}k tokens"
        elif proc_start and ep['start'] < proc_start < ep['start'] + TTL:
            why = f"not tracked: opencodex restarted at {hm(proc_start)}"
        else:
            why = 'never checked: the keep-alive had no timer for it, usually because another chat with the same keep-alive key replaced it'
        story = (f"{kept + ', then ' if n else ''}{why}. You came back {away} and rewrote the cache; "
                 f"{need_n} keep-alive{'s' if need_n != 1 else ''} ({fmt_tok(need_n * ep['unit'])}) would have saved {fmt_tok(best)}.")
    elif n:
        outcome = 'wasted'
        why = None
        if last and last['kind'] == 'stopped':
            end = STOP_WHY.get(last['reason'], 'it stopped')
        elif last and last['kind'] == 'failed':
            end = 'a keep-alive failed'
        else:
            end = 'the hour ran out'
        tail = (f"You came back {away}, too late for keep-alives to pay." if gap is not None and gap > max_idle else
                f"You came back {away}, before the cache would have expired anyway." if gap is not None and gap <= TTL + 30 else
                f"You came back {away} but the context had changed, so the cache could not be reused." if compacted else
                f"You came back {away} to a cold cache." if gap is not None else
                "The chat carried on in another context, so this cache was never reused." if ep['side'] else "You haven't come back.")
        story = f"{kept}, then {end}. {tail}"
    else:
        outcome = 'none'
        why = None
        if gap is None:
            lead = STOP_WHY.get(last['reason'], 'not kept') if last else 'not kept'
            story = lead[0].upper() + lead[1:] + ('; the chat carried on in another context.' if ep['side'] else "; you haven't come back.")
        elif compacted:
            story = f"You came back {away}, but the context had changed (compacted), so a keep-alive could not have helped."
        elif ep['warm']:
            story = f"You came back {away} and the cache was still warm without a keep-alive."
        elif gap > max_idle + 60:
            story = f"You came back {away}, past the {dur(max_idle)} limit."
        else:
            story = f"You came back {away}; keeping it alive that long ({fmt_tok(need_n * ep['unit'])}) would have cost more than the rewrite ({fmt_tok(value)})."
    return dict(outcome=outcome, why=why, story=story, saved=saved, actual=actual, best=best, need_n=need_n)


# ---------------------------------------------------------------- API payloads

def pick_range(name, t0, now):
    span = RANGES.get(name, 86400) if name in RANGES else 86400
    t_from = max(t0, now - span) if span else t0
    width = now - t_from
    bucket = next(b for lim, b in ((7 * 3600, 900), (26 * 3600, 3600), (3.2 * 86400, 3 * 3600), (8 * 86400, 6 * 3600), (1e18, 86400)) if width <= lim)
    return t_from, bucket


def bucket_start(t, b):
    if b >= 86400:
        d = datetime.datetime.fromtimestamp(t, TZ)
        return d.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
    off = datetime.datetime.fromtimestamp(t, TZ).utcoffset().total_seconds()
    return math.floor((t + off) / b) * b - off


def header(store, M):
    pol = M['pol']
    st = M['status']
    return dict(disabled=(M['disabled'] or {}).get('reason', 'disabled') if M['disabled'] is not None else None,
                mode='probability' if pol.get('mode') == 'hazard' else 'fixed limit', trained_at=pol.get('trained_at'),
                proc_start=store.proc.get(), t0=M['t0'], monitor=st.get('checked_at'), monitor_action=st.get('action'),
                daily_cap=pol.get('daily_cap'), min_prompt=pol.get('min_prompt_tokens'))


def api_overview(store, rng):
    M = store.model()
    now = time.time()
    pol = M['pol']
    t_from, b = pick_range(rng, M['t0'], now)
    ps = store.proc.get()
    buckets = collections.OrderedDict()
    t = bucket_start(t_from, b)
    while t <= now:
        buckets[t] = dict(t=t, saved=0.0, cost=0.0, missed=0.0, rescues=0, pings=0, missed_n=0, c_live=0.0, c_cc_1h=0.0, c_old_5m=0.0)
        t = bucket_start(t + b + 1, b) if b >= 86400 else t + b
    def slot(ts):
        return buckets.get(bucket_start(ts, b))
    tot = collections.Counter()
    for d in M['events']:
        if d['t'] < t_from:
            continue
        if d['kind'] == 'kept':
            tot['cost'] += d['cost']; tot['cost_usd'] += usd(d['cost'], d['model']); tot['pings'] += 1
            s = slot(d['t'])
            if s:
                s['cost'] += d['cost']; s['pings'] += 1; s['c_live'] += d['cost']
        elif d['kind'] == 'failed':
            tot['failed'] += 1
    reasons = collections.Counter()
    for ep in M['episodes']:
        if ep['ret'] is None or ep['ret'] < t_from:
            continue
        g = classify(ep, now, pol, ps)
        if not g:
            continue
        s = slot(ep['ret'])
        if g['outcome'] == 'paid':
            tot['saved'] += g['saved']; tot['saved_usd'] += usd(g['saved'], ep['model']); tot['rescues'] += 1
            if s:
                s['saved'] += g['saved']; s['rescues'] += 1
        elif g['outcome'] == 'missed':
            tot['missed'] += g['best']; tot['missed_usd'] += usd(g['best'], ep['model']); tot['missed_n'] += 1
            reasons[g['why'].split(':')[0]] += 1
            if s:
                s['missed'] += g['best']; s['missed_n'] += 1
    use = collections.Counter()
    for t, model, vals, out in replay(M):
        if t < t_from:
            continue
        sl = slot(t)
        for k, val in vals.items():
            use[k] += val + out; use[k + '_usd'] += usd(val + out, model)
            if sl:
                sl['c_' + k] += val + out
    use['live'] += tot['cost']; use['live_usd'] += tot['cost_usd']
    spend = use['live']
    return dict(now=now, range=rng, t_from=t_from, bucket=b, head=header(store, M), spend=spend, consumption=dict(use),
                baseline=BASELINE, baseline_label=BASELINE_LABEL[BASELINE],
                totals=dict(tot), reasons=reasons.most_common(), series=list(buckets.values()))


QUOTA = HOME / 'provider-account-quota-cache.json'   # opencodex's cache of each Claude account's plan meters


def plan_window(M, name, start, reset, util):
    """Where a plan meter would be without keep-alives. The meter's used share is spread over everything this
    machine sent in the window (as priced in token-equivalents); the same requests under the cache you had
    before then give the estimate. Requests from before install count the same both ways."""
    now = time.time()
    ev = []
    for t, model, vals, out in replay(M):
        if start <= t <= now:
            ev.append((t, vals['live'] + out, vals[BASELINE] + out))
    for d in M['events']:
        if d['kind'] == 'kept' and start <= d['t'] <= now:
            ev.append((d['t'], d['cost'], 0.0))
    if M['t0'] > start:
        for v in M['streams'].values():
            for r in v:
                if start <= r['t'] < M['t0']:
                    c = max(0, r['P'] - r['cr'] - r['cc']) + W5 * r['cc'] + rm(r['model']) * r['cr'] + OUTW * r['out']
                    ev.append((r['t'], c, c))
    ev.sort()
    actual = sum(e[1] for e in ev)
    base = sum(e[2] for e in ev)
    out = dict(name=name, start=start, reset=reset, used=util, actual=actual, base=base, est=None, hit_at=None)
    if actual <= 0 or util <= 0:
        return out
    k = util / actual
    out['est'] = k * base
    if out['est'] >= 1:
        cum = 0.0
        for t, _, b in ev:
            cum += b
            if k * cum >= 1:
                out['hit_at'] = t
                break
    return out


def plan_usage(M):
    if (D / 'keepalive_proxy.py').exists():
        return None
    q = _json(QUOTA)
    rows = [(k, v) for k, v in (q.get('rows') or {}).items() if k.startswith('anthropic') and isinstance(v, dict)]
    if not rows:
        return None
    now = time.time()
    out = []
    for k, v in rows:
        acct = dict(id=(k[-32:] if len(k) > 40 else k)[:6], updated=_num(v.get('updatedAt')) / 1000, windows=[], estimated=len(rows) == 1)
        for name, pk, rk, span in (('This week', 'weeklyPercent', 'weeklyResetAt', 7 * 86400), ('5-hour window', 'fiveHourPercent', 'fiveHourResetAt', 5 * 3600)):
            if v.get(pk) is None or not v.get(rk) or v[rk] / 1000 < now:
                continue
            reset = v[rk] / 1000
            w = plan_window(M, name, reset - span, reset, v[pk] / 100)
            if not acct['estimated']:
                w['est'] = w['hit_at'] = None
            acct['windows'].append(w)
        if acct['windows']:
            out.append(acct)
    return out or None


def api_live(store):
    M = store.model()
    now = time.time()
    pol = M['pol']
    ps = store.proc.get()
    titles = store.titles.get()
    floor = M['floor']
    min_p = pol.get('min_prompt_tokens', 20000)
    disabled = M['disabled'] is not None
    rows, expired, small = [], [], 0
    for k, ep in M['open_ep'].items():
        expires = ep['touch'] + TTL
        idle = now - ep['start']
        if expires < now - 6 * 3600:
            continue
        m = rm(k[1])
        last = ep['decisions'][-1] if ep['decisions'] else None
        tracked = ps is None or ep['start'] >= ps or any(d['start'] >= ps for d in ep['decisions'] if d['kind'] == 'kept')
        next_check = ep['touch'] + TTL - LEAD
        keep, p, need, reason, strip = decide(pol, k[1], ep['prompt'], ep['out'], max(0, next_check - ep['start']))
        if disabled:
            verdict, note = 'off', 'Keep-alives are switched off'
        elif last and last['kind'] != 'kept':
            verdict, note = 'letgo', (STOP_WHY.get(last['reason'], 'stopped') if last['kind'] == 'stopped' else 'a keep-alive failed: ' + (last['error'] or ''))
        elif ep['prompt'] < min_p:
            verdict, note = 'small', f"under {min_p // 1000}k tokens: opencodex does not keep it"
        elif not tracked:
            verdict, note = 'untracked', f"last used before opencodex restarted at {hm(ps)}: no keep-alive will be sent"
        elif now > next_check + 180:
            verdict, note = 'untracked', 'check is overdue: opencodex may have dropped it'
        else:
            verdict, note = ('keep' if keep else 'expire'), None
        kt = 0
        if strip:
            j = max(1, min(len(strip), int(max(0, next_check - ep['start']) / pol['hazard']['step_s'] + 0.5)))
            while j - 1 < len(strip) and strip[j - 1]:
                j += 1
                kt += 1
        t = titles.get(ep['cid'], {})
        row = dict(cid=ep['cid'], title=t.get('title'), project=t.get('project'), model=k[1], prompt=ep['prompt'], start=ep['start'], idle=idle, side=ep['side'],
                   touch=ep['touch'], expires=expires, next_check=next_check, pings=ep['pings'], cost=ep['cost'], p=p, need=need,
                   verdict=verdict, note=note, strip=strip, keep_hours=kt, idle_h_next=round(max(0, next_check - ep['start']) / 3600, 1),
                   at_stake=(W60 - m) * max(0, ep['prompt'] - floor), in_use=now - ep['start'] < 300, last=dict(t=last['t'], kind=last['kind']) if last else None)
        if expires < now:
            if ep['prompt'] >= min_p:
                expired.append(row)
        elif ep['prompt'] < min_p:
            small += 1
        else:
            rows.append(row)
    rows.sort(key=lambda r: r['expires'])
    expired.sort(key=lambda r: -r['expires'])
    return dict(now=now, head=header(store, M), chats=rows, expired=expired[:30], small=small, plan=plan_usage(M))


def api_decisions(store, rng):
    M = store.model()
    now = time.time()
    pol = M['pol']
    ps = store.proc.get()
    titles = store.titles.get()
    t_from, _ = pick_range(rng, M['t0'], now)
    tot = collections.Counter()
    out, cal = [], collections.defaultdict(lambda: [0, 0.0, 0])
    reasons = collections.Counter()
    edges = [0, 0.03, 0.06, 0.10, 0.15, 0.20, 0.25, 0.30, 0.40, 1.01]
    for ep in M['episodes']:
        if ep['start'] < t_from:
            continue
        g = classify(ep, now, pol, ps)
        if not g:
            continue
        o = g['outcome']
        tot[o] += 1
        tot[o + '_tok'] += g['saved'] - ep['cost'] if o in ('paid', 'wasted') else (g['best'] if o == 'missed' else 0)
        tot['actual'] += g['actual']; tot['best'] += g['best']; tot['cost'] += ep['cost']; tot['pings'] += ep['pings']
        tot['actual_usd'] += usd(g['actual'], ep['model']); tot['best_usd'] += usd(g['best'], ep['model'])
        if o == 'missed':
            reasons[g['why'].split(':')[0]] += 1
        for d in ep['decisions']:
            if d['p'] is None:
                continue
            i = bisect.bisect_right(edges, d['p']) - 1
            c = cal[i]
            c[0] += 1; c[1] += d['p']; c[2] += d['returned']
        t = titles.get(ep['cid'], {})
        out.append(dict(start=ep['start'], cid=ep['cid'], title=t.get('title'), project=t.get('project'), model=ep['model'], prompt=ep['prompt'],
                        gap=ep['gap'], ret=ep['ret'], pings=ep['pings'], cost=ep['cost'], outcome=o, why=g['why'], story=g['story'],
                        net=g['actual'], best=g['best'], warm=ep['warm'],
                        checks=[dict(t=d['t'], idle=d['idle'], p=d['p'], need=d['need'], kind=d['kind'], reason=d['reason'], error=d['error'],
                                     returned=d['returned'], ret_in=d['ret_in'], cost=d['cost']) for d in ep['decisions']]))
    out.sort(key=lambda e: -e['start'])
    calib = [dict(lo=edges[i], hi=min(1, edges[i + 1]), n=c[0], said=c[1] / c[0], happened=c[2] / c[0]) for i, c in sorted(cal.items()) if c[0]]
    nd = sum(c['n'] for c in calib)
    return dict(now=now, range=rng, t_from=t_from, head=header(store, M), totals=dict(tot), reasons=reasons.most_common(),
                calibration=calib, checks=nd, said=sum(c['said'] * c['n'] for c in calib) / nd if nd else None,
                happened=sum(c['happened'] * c['n'] for c in calib) / nd if nd else None, episodes=out[:600], total_episodes=len(out))


def api_badge(store):
    """Small summary for the menu bar item: is it on, what it saved, what it is doing right now."""
    M = store.model()
    now = time.time()
    pol = M['pol']
    ps = store.proc.get()
    titles = store.titles.get()
    midnight = datetime.datetime.now(TZ).replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
    spans = dict(today=max(M['t0'], midnight), d24=max(M['t0'], now - 86400), all=M['t0'])
    out = {k: collections.Counter() for k in spans}
    for d in M['events']:
        if d['kind'] != 'kept':
            continue
        for k, t in spans.items():
            if d['t'] >= t:
                out[k]['cost'] += d['cost']; out[k]['cost_usd'] += usd(d['cost'], d['model']); out[k]['pings'] += 1
    last = None
    for ep in M['episodes']:
        if ep['ret'] is None or not ep['warm'] or not ep['pings']:
            continue
        g = classify(ep, now, pol, ps)
        if not g or g['outcome'] != 'paid':
            continue
        for k, t in spans.items():
            if ep['ret'] >= t:
                out[k]['saved'] += g['saved']; out[k]['saved_usd'] += usd(g['saved'], ep['model']); out[k]['rescues'] += 1
        if last is None or ep['ret'] > last['t']:
            last = dict(t=ep['ret'], saved=g['saved'], title=titles.get(ep['cid'], {}).get('title'), away=ep['gap'])
    for c in out.values():
        c['net'] = c['saved'] - c['cost']; c['net_usd'] = c['saved_usd'] - c['cost_usd']
    c24 = consumption(M, spans['d24'])
    use_pct = (c24['live'] - c24[BASELINE]) / c24[BASELINE] if c24[BASELINE] else None
    live = api_live(store)
    due = [c['next_check'] for c in live['chats'] if c['verdict'] == 'keep']
    pings = [d['t'] for d in M['events'] if d['kind'] == 'kept']
    plan = live.get('plan')
    week = next((w for a in (plan or []) for w in a['windows'] if w['name'] == 'This week'), None)
    return dict(now=now, use_pct=use_pct, plan_week=week, baseline_label=BASELINE_LABEL[BASELINE].split(' (')[0], on=M['disabled'] is None, disabled=(M['disabled'] or {}).get('reason'), mode=live['head']['mode'],
                kind='Claude Code' if (D / 'keepalive_proxy.py').exists() else 'opencodex', since=M['t0'],
                **{k: dict(v) for k, v in out.items()}, warm=len(live['chats']), warm_tokens=sum(c['prompt'] for c in live['chats']),
                next_keepalive=min(due) if due else None, last_keepalive=max(pings) if pings else None, last_save=last)


# ---------------------------------------------------------------- policy page (server-rendered)

BASELINE = 'cc_1h' if (D / 'keepalive_proxy.py').exists() else 'old_5m'   # what you had before installing
BASELINE_LABEL = {'old_5m': '5-minute cache (opencodex default)', 'cc_1h': '1-hour cache alone (Claude Code default)'}


def replay(M):
    """Every request since install, priced three ways: what actually happened (live), the same request under
    the 1-hour cache without keep-alives, and under the 5-minute cache. A cold return re-reads only the
    always-cached system prompt and tools (floor) and rewrites the rest. Keep-alive costs are added separately."""
    floor, t0 = M['floor'], M['t0']
    for v in M['streams'].values():
        prev = None
        for r in v:
            # skip each chat's first request after install: the one-time switch to the 1-hour cache
            if r['t'] >= t0 and not (prev is not None and prev['t'] < t0):
                m = rm(r['model'])
                u = max(0, r['P'] - r['cr'] - r['cc'])
                gap = r['t'] - prev['t'] if prev else None
                read_1h = min(r['cr'], floor) if gap is not None and gap > TTL + 30 else r['cr']
                read_5m = min(r['cr'], floor) if gap is not None and gap > 300 else r['cr']
                yield r['t'], r['model'], {'live': u + W60 * r['cc'] + m * r['cr'], 'cc_1h': u + W60 * (r['P'] - u - read_1h) + m * read_1h,
                                           'old_5m': u + W5 * (r['P'] - u - read_5m) + m * read_5m}, OUTW * r['out']
            prev = r


def baselines(M):
    """The same requests replayed under the 5-minute and 1-hour cache, next to what actually happened."""
    day = lambda t: datetime.datetime.fromtimestamp(t, TZ).strftime('%d/%m')
    tot, usd_tot, daily = collections.Counter(), collections.Counter(), collections.defaultdict(collections.Counter)
    for t, model, vals, _out in replay(M):
        for k, val in vals.items():
            tot[k] += val; usd_tot[k] += usd(val, model); daily[day(t)][k] += val
        tot['requests'] += 1
    for d in M['events']:
        if d['kind'] == 'kept':
            tot['live'] += d['cost']; usd_tot['live'] += usd(d['cost'], d['model']); daily[day(d['t'])]['live'] += d['cost']
    return tot, usd_tot, daily


def consumption(M, t_from):
    c = collections.Counter()
    for t, model, vals, out in replay(M):
        if t >= t_from:
            for k, val in vals.items():
                c[k] += val + out; c[k + '_usd'] += usd(val + out, model)
    for d in M['events']:
        if d['kind'] == 'kept' and d['t'] >= t_from:
            c['live'] += d['cost']; c['live_usd'] += usd(d['cost'], d['model'])
    return c


def pct(a, b): return f"{100 * (a - b) / b:+.1f}%" if b else '–'


def policy_html(store):
    M = store.model()
    pol, floor = M['pol'], M['floor']
    tot, u, daily = baselines(M)
    cards = ''
    for key, label, sub in (('old_5m', '5-minute cache', 'every write 5 min (opencodex default)'),
                            ('cc_1h', '1-hour cache only', 'every write 1 h, no keep-alive (Claude Code default)'),
                            ('live', '1 h + keep-alive (live)', 'what actually happened, keep-alives included')):
        cards += f"""<div class="card"><div class="lbl">{label}</div><div class="sub">{sub}</div><div class="big">{fmt_tok(tot[key])}</div>
<div class="sub">token-equivalents · ~{fmt_usd(u[key])} at API prices</div><div class="row">vs 5 min <b>{pct(tot[key], tot['old_5m'])}</b> · vs 1 h only <b>{pct(tot[key], tot['cc_1h'])}</b></div></div>"""
    days = sorted(daily, key=lambda d: (d[3:], d[:2]))
    mx = max([max(daily[d].values() or [1]) for d in days] or [1])
    bars = ''.join("<div class='day'><div class='bars'>" + ''.join(
        f"<div class='bar {k}' title='{k}: {fmt_tok(daily[d][k])}' style='height:{110 * daily[d][k] / mx:.0f}px'></div>" for k in ('old_5m', 'cc_1h', 'live'))
        + f"</div><div class='sub'>{d}</div></div>" for d in days)
    hz = pol.get('hazard')
    mode = pol.get('mode', 'rule')
    hr, rr, tr = pol.get('hazard_result', {}), pol.get('rule_result', {}), pol.get('trained_on', {})
    head = f"""<div class="cards">
<div class="card"><div class="lbl">Rule in use</div><div class="big">{'probability' if mode == 'hazard' else 'fixed limit'}</div>
<div class="sub">{'Keep a chat alive while the chance you return within the next hour beats the break-even chance (table below). Hard limit ' + dur(pol.get('max_idle_s', 0)) + ' idle.' if mode == 'hazard' else f"Keep chats over {pol.get('min_prompt_tokens', 0) // 1000}k tokens alive for up to {pol.get('max_idle_s', 0) // 3600} h idle."}
Daily cap {pol.get('daily_cap', '–')} keep-alives.</div></div>
<div class="card"><div class="lbl">Why this rule</div><div class="sub">Replayed on your last {tr.get('days', '–')} days ({tr.get('requests', 0):,} requests, {tr.get('conversations', 0)} chats):</div>
<div class="row">probability rule <b>{hr.get('saving_pct', '–')}%</b> saved · {hr.get('pings', '–')} keep-alives</div>
<div class="row">best fixed limit ({rr.get('max_idle_h', '–')} h, chats over {(rr.get('min_prompt_tokens') or 0) // 1000}k) <b>{rr.get('saving_pct', '–')}%</b> · {rr.get('pings', '–')} keep-alives</div>
<div class="sub">Trained {esc(str(pol.get('trained_at', '–')))}; retrained daily by the monitor.</div></div>
<div class="card"><div class="lbl">Break-even</div><div class="sub">A keep-alive re-reads the whole chat at {rm('claude-opus-5-5')}× input; coming back to a cold chat rewrites it at 2×. So a keep-alive pays when</div>
<div class="row"><b>P(return within the hour) &gt; keep-alive cost ÷ rewrite avoided</b></div>
<div class="sub">For a 300k-token Opus chat: {fmt_tok(0.05 * 300000)} ÷ {fmt_tok(1.95 * (0.97 * 300000 - floor))} ≈ {100 * 0.05 * 300000 / (1.95 * (0.97 * 300000 - floor)):.1f}%. The first ~{fmt_tok(floor)} (system prompt and tools) stay cached anyway, so small chats need a higher chance.</div></div>
</div>"""
    table = ''
    if hz:
        edges = hz['size_edges']
        labels = [f"{edges[i] // 1000}k–{edges[i + 1] // 1000}k" if i + 1 < len(edges) else f"{edges[i] // 1000}k+" for i in range(len(edges))]
        reps = [50000, 140000, 350000, 700000][:len(edges)]
        needs = [0.05 * x / (1.95 * max(1, 0.97 * x - floor)) for x in reps]
        rows = ''
        for k in range(len(hz['p'][0])):
            cells = ''
            for b in range(len(edges)):
                p = hz['p'][b][k]
                n = hz.get('at_risk', [[0] * 24] * 4)[b][k]
                ev = hz.get('returns', [[0] * 24] * 4)[b][k]
                cells += f"<td class='{'on' if p > needs[b] else 'off'}' title='{ev} returns out of {n} chats still idle at this point'>{100 * p:.1f}%<span class='n'> {n}</span></td>"
            rows += f"<tr><th>{k + 1} h</th>{cells}</tr>"
        need_row = ''.join(f"<td class='need'>{100 * x:.1f}%</td>" for x in needs)
        table = f"""<h2>Your return probabilities</h2>
<p class="sub">Each cell: of the chats still idle about this long (row) at this size (column), how many you came back to within the next hour. Small grey number: how many cases it is based on. Green: above break-even for a typical chat of that size, so it keeps the cache. Sizes are total context.</p>
<div class="card flush"><table class="hz"><tr><th>idle</th>{''.join(f'<th>{l}</th>' for l in labels)}</tr><tr><th>needed</th>{need_row}</tr>{rows}</table></div>"""
    return f"""<h2>The policy</h2>{head}{table}
<h2>Cost under each cache rule</h2><p class="sub">Since {when(M['t0'])}, {tot['requests']:,} requests. The first two replay the same requests under that cache rule; input-side cost in base-input-token equivalents (1 h write 2×, 5 min write 1.25×, read 0.05× on Opus).</p><div class="cards">{cards}</div>
<div class="legend sub" style="margin:4px 0 8px;display:block">Per day<span class="sw" style="background:#c7c7cc"></span>5 min<span class="sw" style="background:#8fb4f2"></span>1 h only<span class="sw" style="background:#2f6fdf"></span>live</div>
<div class="card daychart">{bars or '<div class="sub">No requests yet.</div>'}</div>"""


# ---------------------------------------------------------------- page shell

PAGE = r"""<!doctype html><html lang="en"><head><meta charset="utf-8"><title>Cache keep-alive</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
:root{--bg:#f7f7f5;--card:#fff;--ink:#1d1d1f;--sub:#6e6e73;--faint:#aeaeb2;--line:#ececf0;--green:#1f8a4c;--green-bg:#e8f5ec;--orange:#c2410c;--orange-bg:#fdf0e8;--blue:#2f6fdf;--blue-bg:#eaf1fd;--amber:#a15c00;--amber-bg:#fdf3e1;--gray-bg:#f1f1f4;
--ring:0 0 0 1px rgba(0,0,0,.06),0 1px 2px rgba(0,0,0,.04),0 2px 8px rgba(0,0,0,.03)}
*{box-sizing:border-box}
body{font:14px/1.45 -apple-system,BlinkMacSystemFont,system-ui,sans-serif;margin:0;color:var(--ink);background:var(--bg);-webkit-font-smoothing:antialiased}
.wrap{max-width:1240px;margin:0 auto;padding:22px 24px 60px}
header{display:flex;flex-wrap:wrap;align-items:center;gap:10px 18px;margin-bottom:6px}
h1{font-size:19px;font-weight:650;margin:0;letter-spacing:-.01em}
h2{font-size:15px;font-weight:620;margin:30px 0 4px;letter-spacing:-.005em;text-wrap:balance}
nav{display:flex;gap:2px;background:#ebebef;border-radius:9px;padding:3px}
nav a{color:var(--sub);text-decoration:none;font-size:13px;padding:5px 12px;border-radius:7px;font-weight:520}
nav a.on{background:#fff;color:var(--ink);box-shadow:0 1px 2px rgba(0,0,0,.08)}
.status{margin-left:auto;font-size:12px;color:var(--sub);display:flex;gap:8px;align-items:center;flex-wrap:wrap}
.dot{display:inline-block;width:7px;height:7px;border-radius:50%;background:var(--green);margin-right:5px;vertical-align:1px}
.dot.off{background:var(--orange)}
.sub{color:var(--sub);font-size:12px;line-height:1.5;text-wrap:pretty}
p.sub{margin:0 0 10px;max-width:900px}
.bar-row{display:flex;align-items:center;gap:12px;flex-wrap:wrap;margin:18px 0 12px}
.seg{display:inline-flex;gap:2px;background:#ebebef;border-radius:8px;padding:2px}
.seg button{font:500 12px -apple-system,system-ui;border:0;background:transparent;color:var(--sub);padding:5px 11px;border-radius:6px;cursor:pointer;transition:transform .1s cubic-bezier(.23,1,.32,1)}
.seg button:active{transform:scale(.97)}
.seg button.on{background:#fff;color:var(--ink);box-shadow:0 1px 2px rgba(0,0,0,.08)}
.kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(210px,1fr));gap:12px}
.card{background:var(--card);border-radius:14px;padding:14px 16px;box-shadow:var(--ring)}
.card.flush{padding:0;overflow:hidden}
.kpi .lbl,.card .lbl{font-weight:600;font-size:12.5px;color:#3a3a3c}
.kpi .big{font-size:30px;font-weight:650;letter-spacing:-.02em;margin:4px 0 2px;font-variant-numeric:tabular-nums}
.kpi.hl{box-shadow:0 0 0 1.5px var(--blue),0 2px 8px rgba(47,111,223,.08)}
.kpi a{color:inherit}
.big{font-size:26px;font-weight:620;margin:6px 0 2px;font-variant-numeric:tabular-nums}
.row{margin-top:6px;font-size:12px}
.pos{color:var(--green)} .neg{color:var(--orange)} .blue{color:var(--blue)}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(260px,1fr));gap:12px;margin:10px 0}
.chartcard{margin-top:12px;padding:14px 16px 8px}
.charthead{display:flex;align-items:center;gap:14px;flex-wrap:wrap;margin-bottom:6px}
.legend{display:flex;gap:14px;font-size:12px;color:var(--sub);flex-wrap:wrap}
.legend i{display:inline-block;width:10px;height:10px;border-radius:3px;margin-right:5px;vertical-align:-1px}
.legend .sw{display:inline-block;width:10px;height:10px;border-radius:2px;margin:0 4px 0 12px;vertical-align:middle}
.chart{position:relative}
.chart svg{display:block;width:100%;overflow:visible}
.chart text{font:11px -apple-system,system-ui;fill:#8e8e93;font-variant-numeric:tabular-nums}
.tip{position:absolute;pointer-events:none;background:rgba(29,29,31,.94);color:#fff;font-size:12px;padding:8px 10px;border-radius:9px;white-space:nowrap;opacity:0;transition:opacity .12s;line-height:1.55;z-index:5}
.tip b{font-variant-numeric:tabular-nums}
table{border-collapse:collapse;width:100%}
td,th{padding:8px 10px;border-bottom:1px solid var(--line);text-align:left;font-size:12.5px;vertical-align:middle}
th{color:var(--sub);font-weight:500;font-size:11.5px;white-space:nowrap;background:#fbfbfc;position:sticky;top:0;z-index:1}
tr:last-child td{border-bottom:0}
td.num,th.num{text-align:right;font-variant-numeric:tabular-nums;white-space:nowrap}
.mono{font-family:ui-monospace,SFMono-Regular,monospace;font-size:11px;color:var(--faint)}
.chat .t{font-weight:540;max-width:330px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.chat .sub{white-space:nowrap;max-width:330px;overflow:hidden;text-overflow:ellipsis}
.pill{display:inline-block;font-size:11.5px;font-weight:560;padding:2px 9px;border-radius:999px;white-space:nowrap}
.p-keep,.p-paid{background:var(--green-bg);color:var(--green)}
.p-expire,.p-none,.p-letgo,.p-small,.p-off{background:var(--gray-bg);color:#636366}
.p-untracked,.p-missed{background:var(--amber-bg);color:var(--amber)}
.p-wasted{background:var(--orange-bg);color:var(--orange)}
.p-use{background:var(--blue-bg);color:var(--blue)}
.ttl{position:relative;width:96px;height:6px;background:#efeff3;border-radius:3px;overflow:hidden;margin-bottom:4px}
.ttl i{position:absolute;left:0;top:0;bottom:0;background:var(--blue);border-radius:3px;transition:width 1s linear}
.ttl.low i{background:var(--orange)}
.pb{position:relative;width:110px;height:8px;background:#efeff3;border-radius:4px;margin-bottom:4px}
.pb i{position:absolute;left:0;top:0;bottom:0;background:var(--blue);border-radius:4px}
.pb b{position:absolute;top:-3px;bottom:-3px;width:2px;background:var(--orange);border-radius:1px}
.strip{display:flex;gap:1px}
.strip i{width:5px;height:14px;border-radius:1.5px;background:#e5e5ea}
.strip i.k{background:#a8d8b9}
.strip i.now{outline:1.5px solid var(--ink);outline-offset:0}
.tablecard{max-height:680px;overflow:auto}
.stats{display:flex;gap:26px;flex-wrap:wrap;margin:6px 0 12px}
.stats div{font-size:12px;color:var(--sub)}
.stats b{display:block;font-size:20px;color:var(--ink);font-weight:620;font-variant-numeric:tabular-nums;letter-spacing:-.01em}
.checks{display:flex;gap:3px;align-items:center}
.checks i{width:9px;height:9px;border-radius:50%;background:var(--blue);display:inline-block}
.checks i.stopped{background:transparent;box-shadow:inset 0 0 0 1.5px #aeaeb2}
.checks i.failed{background:var(--orange)}
.checks .ret{font-size:11px;margin-left:3px;font-weight:600}
tr.ep{cursor:pointer} tr.ep:hover td{background:#fafafc}
tr.det td{background:#fbfbfc;padding:10px 16px 14px}
.det table td,.det table th{background:transparent;position:static;padding:5px 8px;font-size:12px}
.story{font-size:12.5px;margin-bottom:8px;max-width:900px}
.grid2{display:grid;grid-template-columns:1.3fr 1fr;gap:12px;margin-top:12px}
@media(max-width:900px){.grid2{grid-template-columns:1fr}.status{margin-left:0}}
.reasons div{display:flex;justify-content:space-between;font-size:12.5px;padding:6px 0;border-bottom:1px solid var(--line)}
.reasons div:last-child{border-bottom:0}
.empty{padding:22px;text-align:center;color:var(--sub);font-size:13px}
details summary{cursor:pointer;color:var(--sub);font-size:12.5px;margin:14px 0 8px}
.hz td{text-align:right;min-width:92px;font-variant-numeric:tabular-nums} .hz td.on{background:#e7f6ec} .hz td.off{color:#8e8e93} .hz td.need{color:var(--orange);font-weight:600} .n{color:#aeaeb2;font-size:10px}
.hz th{position:static}
.daychart{display:flex;gap:12px;align-items:flex-end;overflow-x:auto} .day{text-align:center} .bars{display:flex;gap:2px;align-items:flex-end;height:114px}
.bar{width:12px;border-radius:2px 2px 0 0} .old_5m{background:#c7c7cc} .cc_1h{background:#8fb4f2} .live{background:#2f6fdf}
.loading{opacity:.55;transition:opacity .2s}
.plancard{margin-top:12px;padding:14px 16px 10px}
.plan-row{display:grid;grid-template-columns:120px 1fr 120px;gap:16px;align-items:center;padding:8px 0;border-top:1px solid var(--line)}
.plan-row:first-of-type{border-top:0}
.plan-name{font-size:12.5px;font-weight:560}
.plan-bar{position:relative;height:10px;background:#efeff3;border-radius:5px;overflow:hidden;margin-bottom:5px}
.plan-bar i{position:absolute;left:0;top:0;bottom:0;background:var(--blue);border-radius:5px 0 0 5px}
.plan-bar b{position:absolute;top:0;bottom:0;background:repeating-linear-gradient(-45deg,rgba(194,65,12,.55) 0 4px,rgba(194,65,12,.25) 4px 8px)}
.plan-bar em{position:absolute;right:0;top:0;bottom:0;width:3px;background:var(--orange)}
.plan-reset{text-align:right}
@media(max-width:1120px){.o1{display:none}}
@media(max-width:820px){.o2{display:none}.chat .t,.chat .sub{max-width:200px}}
@media (prefers-reduced-motion: reduce){*{transition:none!important}}
</style></head><body><div class="wrap">
<header><h1>Prompt-cache keep-alive</h1>
<nav><a href="/" data-p="overview">Savings &amp; live</a><a href="/decisions" data-p="decisions">Decisions</a><a href="/policy" data-p="policy">Policy</a></nav>
<div class="status" id="status"></div></header>
<main id="app">%%BODY%%</main></div>
<script>
var PAGE = '%%PAGE%%';
var HEAD = %%HEAD%%;
document.querySelectorAll('nav a').forEach(function (a) { if (a.dataset.p === PAGE) a.classList.add('on'); });
function esc(s) { return String(s == null ? '' : s).replace(/[&<>"']/g, function (c) { return {'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[c]; }); }
function tok(x) { var a = Math.abs(x); if (a >= 1e9) return (x / 1e9).toFixed(2) + 'B'; if (a >= 1e6) return (x / 1e6).toFixed(a >= 1e8 ? 0 : 1) + 'M'; if (a >= 1e3) return Math.round(x / 1e3) + 'k'; return Math.round(x) + ''; }
function stok(x) { return (x > 0 ? '+' : x < 0 ? '−' : '') + tok(Math.abs(x)); }
function usd(x) { var a = Math.abs(x); return (x < 0 ? '−' : '') + 'US$ ' + (a >= 10 ? Math.round(a).toLocaleString('en-US') : a.toFixed(2)); }
function spct(x) { return x == null ? '–' : (x < 0 ? '−' : '+') + Math.abs(100 * x).toFixed(1) + '%'; }
function pct(x, d) { return x == null ? '–' : (100 * x).toFixed(d == null ? 1 : d) + '%'; }
function pad(n) { return (n < 10 ? '0' : '') + n; }
function clock(t) { var d = new Date(t * 1000); return pad(d.getHours()) + ':' + pad(d.getMinutes()); }
function day(t) { var d = new Date(t * 1000); return pad(d.getDate()) + '/' + pad(d.getMonth() + 1); }
function when(t) { var d = new Date(t * 1000), n = new Date(); if (d.toDateString() === n.toDateString()) return 'today ' + clock(t); if (new Date(n - 864e5).toDateString() === d.toDateString()) return 'yesterday ' + clock(t); return day(t) + ' ' + clock(t); }
function dur(s) { s = Math.max(0, s); if (s < 60) return Math.round(s) + ' s'; if (s < 3600) return Math.round(s / 60) + ' min'; if (s < 86400) { var h = Math.floor(s / 3600), m = Math.round((s % 3600) / 60); if (m === 60) { h++; m = 0; } return h + ' h' + (m ? ' ' + pad(m) : ''); } return (s / 86400).toFixed(1) + ' d'; }
function shortModel(m) { return String(m || '').replace('claude-', '').replace(/-(\d)-(\d)$/, ' $1.$2'); }
function chatCell(r) { var t = r.title ? esc(r.title) : '<span class="mono">' + esc(r.cid.slice(0, 8)) + '</span>'; return '<div class="t" title="' + esc(r.title || r.cid) + '">' + t + '</div><div class="sub">' + esc([r.project, shortModel(r.model)].filter(Boolean).join(' · ')) + (r.title ? ' · <span class="mono">' + esc(r.cid.slice(0, 6)) + '</span>' : '') + (r.side ? ' · <span style="color:var(--amber)" title="This chat has moved on in another cache context (a fork, side request or compacted history); this one may never be reused.">side context</span>' : '') + '</div>'; }
function renderStatus(h) {
  var on = !h.disabled;
  var s = '<span><span class="dot' + (on ? '' : ' off') + '"></span>' + (on ? 'Keep-alives on' : 'Keep-alives off: ' + esc(h.disabled)) + '</span><span>· ' + esc(h.mode) + ' rule</span>';
  if (h.proc_start) s += '<span>· tracking since ' + when(h.proc_start) + '</span>';
  if (h.monitor) s += '<span title="' + esc(h.monitor_action || '') + '">· monitor ran ' + when(Date.parse(h.monitor) / 1000) + '</span>';
  document.getElementById('status').innerHTML = s;
}
if (HEAD) renderStatus(HEAD);
var RANGES = [['6h', '6 h'], ['24h', '24 h'], ['3d', '3 days'], ['7d', '7 days'], ['30d', '30 days'], ['all', 'All']];
function param(k, d) { return new URLSearchParams(location.search).get(k) || d; }
function setParam(k, v) { var u = new URLSearchParams(location.search); u.set(k, v); history.replaceState(null, '', '?' + u.toString()); }
function seg(el, items, cur, pick) {
  el.innerHTML = items.map(function (r) { return '<button data-k="' + r[0] + '"' + (r[0] === cur ? ' class="on"' : '') + '>' + r[1] + '</button>'; }).join('');
  el.querySelectorAll('button').forEach(function (b) { b.onclick = function () { el.querySelectorAll('button').forEach(function (x) { x.classList.toggle('on', x === b); }); pick(b.dataset.k); }; });
}
function getJSON(u) { return fetch(u).then(function (r) { if (!r.ok) return r.text().then(function (t) { throw new Error(t); }); return r.json(); }); }
function niceMax(v) { if (v <= 0) return 1; var e = Math.pow(10, Math.floor(Math.log10(v))), f = v / e; return (f <= 1 ? 1 : f <= 2 ? 2 : f <= 2.5 ? 2.5 : f <= 5 ? 5 : 10) * e; }

/* ---------- savings chart ---------- */
function drawChart(box, data, mode) {
  var s = data.series, W = Math.max(320, box.clientWidth), H = 250, L = 54, R = 14, T = 12, B = 26;
  var n = s.length, iw = W - L - R, ih = H - T - B;
  if (!n) { box.innerHTML = '<div class="empty">No data in this window.</div>'; return; }
  var cs = 0, cc = 0, cum = s.map(function (b) { cs += b.saved; cc += b.cost; return {s: cs, c: cc, n: cs - cc}; });
  var u5 = 0, u1 = 0, ul = 0, use = s.map(function (b) { u5 += b.c_old_5m; u1 += b.c_cc_1h; ul += b.c_live; return {a: u5, b: u1, l: ul}; });
  var lo, hi;
  if (mode === 'use') { lo = 0; hi = niceMax(Math.max(data.baseline === 'old_5m' ? u5 : 0, u1, ul, 1)); }
  else if (mode === 'cum') { hi = niceMax(Math.max(cs, cc, 1)); lo = Math.min(0, Math.min.apply(null, cum.map(function (c) { return c.n; }))); if (lo < 0) lo = -niceMax(-lo); }
  else { hi = niceMax(Math.max.apply(null, s.map(function (b) { return b.saved; }).concat([1]))); lo = -niceMax(Math.max.apply(null, s.map(function (b) { return b.cost; }).concat([1]))); }
  var y = function (v) { return T + ih * (hi - v) / (hi - lo); };
  var bw = iw / n, x = function (i) { return L + bw * (i + 0.5); };
  var g = '';
  var ticks = [0, hi, hi / 2, lo, lo / 2].filter(function (v, i, a) { return a.slice(0, i).every(function (u) { return Math.abs(y(u) - y(v)) > 16; }); });
  ticks.forEach(function (v) { g += '<line x1="' + L + '" x2="' + (W - R) + '" y1="' + y(v) + '" y2="' + y(v) + '" stroke="' + (v === 0 ? '#c7c7cc' : '#efeff3') + '"/><text x="' + (L - 8) + '" y="' + (y(v) + 4) + '" text-anchor="end">' + (v < 0 ? '−' : '') + tok(Math.abs(v)) + '</text>'; });
  var every = Math.max(1, Math.ceil(n / Math.max(2, Math.floor(iw / 78))));
  var long = data.bucket >= 86400 || (s[n - 1].t - s[0].t) > 1.5 * 86400;
  s.forEach(function (b, i) { if (i % every) return; var lab = data.bucket >= 86400 ? day(b.t) : (long && new Date(b.t * 1000).getHours() < data.bucket / 3600 ? day(b.t) : clock(b.t)); g += '<text x="' + x(i) + '" y="' + (H - 6) + '" text-anchor="middle">' + lab + '</text>'; });
  if (mode === 'use') {
    var up = function (k) { return use.map(function (c, i) { return x(i).toFixed(1) + ',' + y(c[k]).toFixed(1); }).join(' '); };
    var bk = data.baseline === 'old_5m' ? 'a' : 'b';
    g += '<polygon points="' + x(0) + ',' + y(0) + ' ' + up(bk) + ' ' + use.slice().reverse().map(function (c, j) { var i = n - 1 - j; return x(i).toFixed(1) + ',' + y(c.l).toFixed(1); }).join(' ') + '" fill="rgba(31,138,76,.10)"/>';
    if (data.baseline === 'old_5m') g += '<polyline points="' + up('a') + '" fill="none" stroke="#8e8e93" stroke-width="2" stroke-dasharray="5 4" stroke-linejoin="round"/>';
    g += '<polyline points="' + up('b') + '" fill="none" stroke="#8fb4f2" stroke-width="2" stroke-linejoin="round"/>';
    g += '<polyline points="' + up('l') + '" fill="none" stroke="#2f6fdf" stroke-width="2.5" stroke-linejoin="round"/>';
  } else if (mode === 'cum') {
    var pts = function (k) { return cum.map(function (c, i) { return x(i).toFixed(1) + ',' + y(c[k]).toFixed(1); }).join(' '); };
    g += '<polygon points="' + x(0) + ',' + y(0) + ' ' + pts('n') + ' ' + x(n - 1) + ',' + y(0) + '" fill="rgba(47,111,223,.08)"/>';
    g += '<polyline points="' + pts('s') + '" fill="none" stroke="#1f8a4c" stroke-width="2" stroke-linejoin="round"/>';
    g += '<polyline points="' + pts('c') + '" fill="none" stroke="#c2410c" stroke-width="2" stroke-linejoin="round"/>';
    g += '<polyline points="' + pts('n') + '" fill="none" stroke="#2f6fdf" stroke-width="2.5" stroke-linejoin="round"/>';
  } else {
    var w = Math.max(1.5, Math.min(26, bw * 0.62));
    s.forEach(function (b, i) {
      if (b.saved) g += '<rect x="' + (x(i) - w / 2) + '" y="' + y(b.saved) + '" width="' + w + '" height="' + (y(0) - y(b.saved)) + '" rx="2" fill="#1f8a4c"/>';
      if (b.cost) g += '<rect x="' + (x(i) - w / 2) + '" y="' + y(0) + '" width="' + w + '" height="' + (y(-b.cost) - y(0)) + '" rx="2" fill="#e08a5e"/>';
    });
  }
  g += '<line id="hov" x1="0" x2="0" y1="' + T + '" y2="' + (T + ih) + '" stroke="#1d1d1f" stroke-opacity=".25" style="display:none"/>';
  g += '<rect x="' + L + '" y="' + T + '" width="' + iw + '" height="' + ih + '" fill="transparent" id="hit"/>';
  box.innerHTML = '<svg viewBox="0 0 ' + W + ' ' + H + '" height="' + H + '">' + g + '</svg><div class="tip" id="tip"></div>';
  var tip = box.querySelector('#tip'), hov = box.querySelector('#hov');
  box.querySelector('#hit').onmousemove = function (ev) {
    var r = box.getBoundingClientRect(), px = (ev.clientX - r.left) * W / r.width, i = Math.max(0, Math.min(n - 1, Math.floor((px - L) / bw))), b = s[i], c = cum[i];
    hov.setAttribute('x1', x(i)); hov.setAttribute('x2', x(i)); hov.style.display = '';
    var end = b.t + data.bucket, lab = data.bucket >= 86400 ? day(b.t) : day(b.t) + ' ' + clock(b.t) + '–' + clock(end);
    if (mode === 'use') { var u = use[i], ub = data.baseline === 'old_5m' ? u.a : u.b; tip.innerHTML = '<div style="opacity:.7">' + lab + ' · so far</div>Used <b>' + tok(u.l) + '</b>' + (ub ? ' · <b style="color:#7ee2a1">' + spct((u.l - ub) / ub) + '</b>' : '') + '<br><span style="opacity:.75">' + (data.baseline === 'old_5m' ? '5-minute cache <b>' + tok(u.a) + '</b><br>' : '') + '1-hour cache only <b>' + tok(u.b) + '</b></span>'; } else
    tip.innerHTML = '<div style="opacity:.7">' + lab + '</div><span class="pos" style="color:#7ee2a1">Saved <b>' + tok(b.saved) + '</b></span> · ' + b.rescues + ' warm return' + (b.rescues === 1 ? '' : 's') + '<br><span style="color:#ffb38a">Cost <b>' + tok(b.cost) + '</b></span> · ' + b.pings + ' keep-alive' + (b.pings === 1 ? '' : 's') + (b.missed ? '<br><span style="color:#ffd27a">Missed <b>' + tok(b.missed) + '</b></span> · ' + b.missed_n + ' cold return' + (b.missed_n === 1 ? '' : 's') : '') + '<br>Net so far <b>' + stok(c.n) + '</b>';
    tip.style.opacity = 1;
    var tx = x(i) * r.width / W + 14; if (tx > r.width - 230) tx = x(i) * r.width / W - 230;
    tip.style.left = tx + 'px'; tip.style.top = '18px';
  };
  box.querySelector('#hit').onmouseleave = function () { tip.style.opacity = 0; hov.style.display = 'none'; };
}

/* ---------- overview ---------- */
function useCard(d) {
  var c = d.consumption || {}, base = c[d.baseline] || 0, ch = base ? (c.live - base) / base : null, vs1 = c.cc_1h ? (c.live - c.cc_1h) / c.cc_1h : null;
  return '<div class="card kpi hl"><div class="lbl">Overall consumption</div><div class="big ' + (ch != null && ch <= 0 ? 'pos' : 'neg') + '">' + spct(ch) + '</div>' +
    '<div class="sub">' + tok(c.live || 0) + ' used vs ' + tok(base) + ' with the ' + esc(d.baseline_label.split(' (')[0]) + ' you had before' +
    (d.baseline === 'old_5m' && vs1 != null ? ' · ' + spct(vs1) + ' vs the 1-hour cache alone' : '') + '</div></div>';
}
function weekday(t) { var d = new Date(t * 1000); return ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat'][d.getDay()] + ' ' + clock(t); }
function renderPlan(plan) {
  var el = document.getElementById('plan');
  if (!el) return;
  if (!plan) { el.innerHTML = ''; return; }
  el.innerHTML = plan.map(function (a) {
    var rows = a.windows.map(function (w) {
      var used = Math.min(1, w.used), est = w.est, ghost = est == null ? 0 : Math.max(0, Math.min(1, est) - used);
      var bar = '<div class="plan-bar"><i style="width:' + (100 * used).toFixed(1) + '%"></i>' + (ghost > 0 ? '<b style="left:' + (100 * used).toFixed(1) + '%;width:' + (100 * ghost).toFixed(1) + '%"></b>' : '') + (est != null && est > 1 ? '<em></em>' : '') + '</div>';
      var what;
      if (est == null) what = '';
      else if (est > 1 && w.hit_at) what = 'without keep-alive: <b class="neg">limit hit ' + weekday(w.hit_at) + '</b>, ' + dur(w.reset - w.hit_at) + ' before reset (≈' + Math.round(100 * est) + '%)';
      else what = 'without keep-alive: <b>≈' + Math.round(100 * est) + '%</b> <span class="pos">(' + Math.round(100 * (est - w.used)) + (Math.round(100 * (est - w.used)) === 1 ? ' point' : ' points') + ' more)</span>';
      return '<div class="plan-row"><div class="plan-name">' + esc(w.name) + '</div><div class="plan-main">' + bar + '<div class="sub"><b style="color:var(--ink)">' + Math.round(100 * w.used) + '% used</b> · ' + what + '</div></div><div class="sub plan-reset">resets ' + weekday(w.reset) + '<br>in ' + dur(w.reset - Date.now() / 1000) + '</div></div>';
    }).join('');
    return '<div class="card plancard"><div class="charthead" style="margin-bottom:2px"><b style="font-size:13px">Claude plan</b><span class="sub">account ' + esc(a.id) + ' · meter read ' + dur(Date.now() / 1000 - a.updated) + ' ago' + (a.estimated ? ' · estimate assumes this plan is only used through opencodex' : ' · several accounts: no estimate') + '</span></div>' + rows + '</div>';
  }).join('');
}
function overview() {
  var app = document.getElementById('app');
  app.innerHTML = '<div class="bar-row"><div class="seg" id="range"></div><span class="sub" id="span"></span></div><div id="kpis" class="kpis"></div>' +
    '<div id="plan"></div><div class="card chartcard"><div class="charthead"><b style="font-size:13px" id="ctitle"></b><div class="seg" id="mode"></div><div class="legend" id="legend"></div></div><div class="chart" id="chart"></div></div>' +
    '<h2>On cache now</h2><p class="sub">Chats whose 1-hour cache is still warm. About a minute before it expires the keep-alive checks the chat: if the chance you come back within the next hour beats break-even it re-reads the cache (cheap), otherwise it lets it expire. Updates every 30 s.</p>' +
    '<div class="stats" id="livestats"></div><div class="card flush tablecard" id="live"></div><div id="expired"></div>';
  var range = param('range', '24h'), mode = param('chart', 'use'), last = null;
  seg(document.getElementById('range'), RANGES, range, function (k) { range = k; setParam('range', k); load(); });
  seg(document.getElementById('mode'), [['use', 'Consumption'], ['cum', 'Savings'], ['bars', 'Savings per period']], mode, function (k) { mode = k; setParam('chart', k); paint(); });
  function paint() {
    if (!last) return;
    document.getElementById('ctitle').textContent = mode === 'use' ? 'Tokens used, cumulative' : 'Savings vs cost';
    document.getElementById('legend').innerHTML = mode === 'use' ? '<span><i style="background:#2f6fdf"></i>Actual (keep-alive costs included)</span><span><i style="background:#8fb4f2"></i>1-hour cache alone</span>' + (last.baseline === 'old_5m' ? '<span><i style="background:#8e8e93"></i>5-minute cache (before)</span>' : '') : mode === 'cum' ? '<span><i style="background:#1f8a4c"></i>Rewrites avoided</span><span><i style="background:#c2410c"></i>Keep-alive cost</span><span><i style="background:#2f6fdf"></i>Net</span>' : '<span><i style="background:#1f8a4c"></i>Rewrites avoided</span><span><i style="background:#e08a5e"></i>Keep-alive cost</span>';
    drawChart(document.getElementById('chart'), last, mode);
  }
  function load() {
    document.getElementById('kpis').classList.add('loading');
    getJSON('/api/overview?range=' + range).then(function (d) {
      last = d; renderStatus(d.head);
      var t = d.totals, saved = t.saved || 0, cost = t.cost || 0, net = saved - cost, netu = (t.saved_usd || 0) - (t.cost_usd || 0);
      document.getElementById('span').textContent = when(d.t_from) + ' → now · ' + (d.bucket >= 86400 ? 'daily' : dur(d.bucket)) + ' buckets';
      var k = document.getElementById('kpis'); k.classList.remove('loading');
      k.innerHTML =
        useCard(d) + '<div class="card kpi"><div class="lbl">Net saved</div><div class="big ' + (net >= 0 ? 'blue' : 'neg') + '">' + stok(net) + '</div><div class="sub">' + usd(netu) + ' at API prices · ' + (cost ? (saved / cost).toFixed(1) + '× return on cost' : 'no keep-alives yet') + (d.spend ? ' · ' + pct(net / d.spend) + ' of total use' : '') + '</div></div>' +
        '<div class="card kpi"><div class="lbl">Rewrites avoided</div><div class="big pos">' + tok(saved) + '</div><div class="sub">' + (t.rescues || 0) + ' time' + (t.rescues === 1 ? '' : 's') + ' you came back after an hour+ to a warm cache · ' + usd(t.saved_usd || 0) + '</div></div>' +
        '<div class="card kpi"><div class="lbl">Keep-alive cost</div><div class="big neg">' + tok(cost) + '</div><div class="sub">' + (t.pings || 0) + ' keep-alives' + (t.failed ? ', ' + t.failed + ' failed' : '') + ' · ' + usd(t.cost_usd || 0) + '</div></div>' +
        '<div class="card kpi"><div class="lbl">Left on the table</div><div class="big" style="color:var(--amber)">' + tok(t.missed || 0) + '</div><div class="sub"><a href="/decisions?range=' + range + '&f=missed">' + (t.missed_n || 0) + ' cold return' + (t.missed_n === 1 ? '' : 's') + '</a> where a keep-alive would have paid' + (d.reasons.length ? ' · mostly ' + esc(d.reasons[0][0]) : '') + '</div></div>';
      paint();
    }).catch(function (e) { document.getElementById('kpis').innerHTML = '<div class="card empty">Could not load: ' + esc(e.message).slice(0, 300) + '</div>'; });
  }
  var live = null;
  function loadLive() { getJSON('/api/live').then(function (d) { live = d; renderLive(); }).catch(function () {}); }
  function renderLive() {
    var d = live, now = Date.now() / 1000, rows = d.chats;
    renderPlan(d.plan);
    var keep = rows.filter(function (r) { return r.verdict === 'keep' && r.next_check - now < 3600; });
    var keepCost = keep.reduce(function (a, r) { return a + 0.05 * r.prompt; }, 0);
    document.getElementById('livestats').innerHTML =
      '<div><b>' + rows.length + '</b>chats warm</div><div><b>' + tok(rows.reduce(function (a, r) { return a + r.prompt; }, 0)) + '</b>context cached</div>' +
      '<div><b>' + keep.length + '</b>keep-alives due in the next hour · ~' + tok(keepCost) + '</div><div><b>' + tok(rows.reduce(function (a, r) { return a + r.at_stake; }, 0)) + '</b>rewrite cost if all went cold</div>' +
      (d.small ? '<div><b>' + d.small + '</b>smaller chats, not kept</div>' : '');
    if (!rows.length) { document.getElementById('live').innerHTML = '<div class="empty">No chat has a warm cache right now.</div>'; }
    else document.getElementById('live').innerHTML = '<table><tr><th>Chat</th><th class="num">Context</th><th class="num o2">Idle</th><th>Cache expires</th><th title="The chance, from your history, that you come back within the hour after the next check; the red mark is the chance needed for a keep-alive to pay">Chance you return</th><th class="o1" title="The next 24 idle hours: green where the policy would keep it alive">Next 24 h</th><th class="num o1">This pause</th><th>Next check</th></tr>' + rows.map(liveRow).join('') + '</table>';
    var ex = d.expired;
    document.getElementById('expired').innerHTML = ex.length ? '<details><summary>Expired in the last 6 hours (' + ex.length + ')</summary><div class="card flush"><table><tr><th>Chat</th><th class="num">Context</th><th>Expired</th><th class="num">Keep-alives</th><th>Why</th></tr>' + ex.map(function (r) {
      return '<tr><td class="chat">' + chatCell(r) + '</td><td class="num">' + tok(r.prompt) + '</td><td>' + when(r.expires) + '</td><td class="num">' + (r.pings ? r.pings + ' · ' + tok(r.cost) : '–') + '</td><td class="sub">' + esc(r.note || (r.verdict === 'expire' ? 'chance of return below break-even' : '')) + '</td></tr>';
    }).join('') + '</table></div></details>' : '';
    tick();
  }
  function liveRow(r) {
    var p = r.p, need = r.need, sc = Math.max(0.3, p || 0, need || 0) * 1.1;
    var pb = p == null ? '<span class="sub">fixed rule</span>' : '<div class="pb"><i style="width:' + (100 * Math.min(p, sc) / sc).toFixed(0) + '%"></i><b style="left:' + (100 * Math.min(need, sc) / sc).toFixed(0) + '%"></b></div><div class="sub"><b style="color:var(--ink)">' + pct(p) + '</b> at ' + r.idle_h_next + ' h idle · needs ' + pct(need) + '</div>';
    var strip = '';
    if (r.strip) { var cur = Math.max(1, Math.min(24, Math.round(r.idle_h_next))); strip = '<div class="strip">' + r.strip.map(function (k, j) { return '<i class="' + (k ? 'k' : '') + (j + 1 === cur ? ' now' : '') + '" title="' + (j + 1) + ' h idle: ' + (k ? 'keep' : 'let expire') + '"></i>'; }).join('') + '</div><div class="sub" style="white-space:nowrap">' + (r.verdict === 'keep' ? 'keeps ~' + r.keep_hours + ' h more' : '&nbsp;') + '</div>'; }
    var v = {keep: ['p-keep', 'Keeps it'], expire: ['p-expire', 'Lets it expire'], letgo: ['p-letgo', 'Let go'], small: ['p-small', 'Too small'], untracked: ['p-untracked', 'Not tracked'], off: ['p-off', 'Off']}[r.verdict];
    var check = (r.verdict === 'keep' || r.verdict === 'expire') ? '<span class="pill ' + v[0] + '">' + v[1] + '</span><div class="sub" style="white-space:nowrap">' + clock(r.next_check) + ' · in <span data-until="' + r.next_check + '"></span></div>' : '<span class="pill ' + v[0] + '" title="' + esc(r.note) + '">' + v[1] + '</span><div class="sub" style="max-width:200px;white-space:normal">' + esc(r.note) + '</div>';
    return '<tr><td class="chat">' + chatCell(r) + '</td><td class="num">' + tok(r.prompt) + '</td><td class="num o2">' + (r.in_use ? '<span class="pill p-use">in use</span>' : '<span data-since="' + r.start + '"></span>') + '</td>' +
      '<td><div class="ttl" data-ttl="' + r.expires + '"><i></i></div><div class="sub">in <span data-until="' + r.expires + '"></span></div></td><td>' + pb + '</td><td class="o1">' + strip + '</td>' +
      '<td class="num o1">' + (r.pings ? r.pings + ' kept · ' + tok(r.cost) : '<span class="sub">–</span>') + '</td><td>' + check + '</td></tr>';
  }
  function tick() {
    var now = Date.now() / 1000;
    document.querySelectorAll('[data-until]').forEach(function (e) { e.textContent = dur(+e.dataset.until - now); });
    document.querySelectorAll('[data-since]').forEach(function (e) { e.textContent = dur(now - +e.dataset.since); });
    document.querySelectorAll('[data-ttl]').forEach(function (e) { var left = +e.dataset.ttl - now, f = Math.max(0, Math.min(1, left / 3600)); e.firstChild.style.width = (100 * f).toFixed(1) + '%'; e.classList.toggle('low', left < 600); });
  }
  load(); loadLive();
  setInterval(loadLive, 30000); setInterval(tick, 1000); setInterval(load, 300000);
  var rt; window.addEventListener('resize', function () { clearTimeout(rt); rt = setTimeout(paint, 120); });
}

/* ---------- decisions ---------- */
var OUT = {paid: ['p-paid', 'Paid off'], wasted: ['p-wasted', 'Wasted'], missed: ['p-missed', 'Missed'], none: ['p-none', 'Not needed']};
function calibChart(box, cal) {
  if (!cal.length) { box.innerHTML = '<div class="empty">No graded checks yet.</div>'; return; }
  var W = Math.max(300, box.clientWidth), H = 240, L = 44, R = 12, T = 10, B = 30, iw = W - L - R, ih = H - T - B;
  var mx = Math.min(1, Math.max(0.1, Math.ceil(10 * Math.max.apply(null, cal.map(function (c) { return Math.max(c.said, c.happened); })) * 1.1) / 10));
  var stepv = mx <= 0.5 ? 0.1 : 0.2, nt = Math.round(mx / stepv);
  var x = function (v) { return L + iw * v / mx; }, y = function (v) { return T + ih * (1 - v / mx); };
  var g = '', nmax = Math.max.apply(null, cal.map(function (c) { return c.n; }));
  for (var i = 0; i <= nt; i++) { var v = stepv * i; g += '<line x1="' + L + '" x2="' + (W - R) + '" y1="' + y(v) + '" y2="' + y(v) + '" stroke="#efeff3"/><text x="' + (L - 6) + '" y="' + (y(v) + 4) + '" text-anchor="end">' + Math.round(100 * v) + '%</text><text x="' + x(v) + '" y="' + (H - 12) + '" text-anchor="middle">' + Math.round(100 * v) + '%</text>'; }
  g += '<line x1="' + x(0) + '" y1="' + y(0) + '" x2="' + x(mx) + '" y2="' + y(mx) + '" stroke="#c7c7cc" stroke-dasharray="4 4"/>';
  g += '<text x="' + (L + iw / 2) + '" y="' + (H) + '" text-anchor="middle">model said</text>';
  g += '<text x="' + (L + 6) + '" y="' + (T + 10) + '">you came back</text>';
  cal.forEach(function (c) { var r = 4 + 10 * Math.sqrt(c.n / nmax); g += '<circle cx="' + x(c.said) + '" cy="' + y(c.happened) + '" r="' + r + '" fill="rgba(47,111,223,.22)" stroke="#2f6fdf" stroke-width="1.5"><title>' + Math.round(100 * c.lo) + '–' + Math.round(100 * c.hi) + '%: model said ' + pct(c.said) + ', you came back after ' + pct(c.happened) + ' of ' + c.n + ' checks</title></circle>'; });
  box.innerHTML = '<svg viewBox="0 0 ' + W + ' ' + H + '" height="' + H + '">' + g + '</svg>';
}
function decisions() {
  var app = document.getElementById('app');
  app.innerHTML = '<div class="bar-row"><div class="seg" id="range"></div><span class="sub" id="span"></span></div><div class="kpis" id="kpis"></div>' +
    '<div class="grid2"><div class="card"><div class="lbl">Were the chances right?</div><p class="sub" id="calsub" style="margin-top:4px"></p><div class="chart" id="cal"></div></div><div class="card"><div class="lbl">Why returns were missed</div><p class="sub" style="margin-top:4px">Cold returns where keeping the chat alive until you came back would have cost less than the rewrite.</p><div class="reasons" id="reasons"></div></div></div>' +
    '<h2>Every pause</h2><p class="sub">One row per time a chat sat idle for over an hour (or is still idle). Dots are the hourly checks: filled = kept alive, hollow = let go, orange = keep-alive failed. Net is rewrite avoided minus keep-alive cost; Best is what a policy that knew when you would come back could have saved. Click a row for each check.</p>' +
    '<div class="bar-row" style="margin:6px 0 10px"><div class="seg" id="filt"></div></div><div class="card flush tablecard" id="eps"></div>';
  var range = param('range', '7d'), f = param('f', 'all'), data = null;
  seg(document.getElementById('range'), RANGES, range, function (k) { range = k; setParam('range', k); load(); });
  function load() {
    getJSON('/api/decisions?range=' + range).then(function (d) {
      data = d; renderStatus(d.head);
      var t = d.totals;
      document.getElementById('span').textContent = 'pauses that started ' + when(d.t_from) + ' → now';
      var cap = t.best > 0 ? (t.actual || 0) / t.best : null;
      document.getElementById('kpis').innerHTML =
        '<div class="card kpi hl"><div class="lbl">Captured</div><div class="big blue">' + pct(cap, 0) + '</div><div class="sub">of the best possible: ' + stok(t.actual || 0) + ' net vs ' + tok(t.best || 0) + ' with perfect hindsight</div></div>' +
        '<div class="card kpi"><div class="lbl">Paid off</div><div class="big pos">' + (t.paid || 0) + '</div><div class="sub">kept alive and you came back · net ' + stok(t.paid_tok || 0) + '</div></div>' +
        '<div class="card kpi"><div class="lbl">Wasted</div><div class="big neg">' + (t.wasted || 0) + '</div><div class="sub">kept alive, no warm return · ' + stok(t.wasted_tok || 0) + '</div></div>' +
        '<div class="card kpi"><div class="lbl">Missed</div><div class="big" style="color:var(--amber)">' + (t.missed || 0) + '</div><div class="sub">came back cold where keeping it would have paid · ' + tok(t.missed_tok || 0) + ' left</div></div>' +
        '<div class="card kpi"><div class="lbl">Not needed</div><div class="big" style="color:#636366">' + (t.none || 0) + '</div><div class="sub">let it expire and a keep-alive would not have paid</div></div>';
      document.getElementById('calsub').innerHTML = d.checks ? 'Each dot groups hourly checks by the chance the model gave (x) against how often you actually came back within that hour (y). On the dashed line the model is calibrated. Over ' + d.checks + ' checks it said <b>' + pct(d.said) + '</b> on average; you came back after <b>' + pct(d.happened) + '</b>.' : '';
      calibChart(document.getElementById('cal'), d.calibration);
      document.getElementById('reasons').innerHTML = d.reasons.length ? d.reasons.map(function (r) { return '<div><span>' + esc(r[0]) + '</span><b>' + r[1] + '</b></div>'; }).join('') : '<div class="sub">None in this window.</div>';
      var counts = {all: d.episodes.length}; d.episodes.forEach(function (e) { counts[e.outcome] = (counts[e.outcome] || 0) + 1; });
      seg(document.getElementById('filt'), [['all', 'All'], ['paid', 'Paid off'], ['wasted', 'Wasted'], ['missed', 'Missed'], ['none', 'Not needed']].map(function (o) { return [o[0], o[1] + ' <span class="sub">' + (counts[o[0]] || 0) + '</span>']; }), f, function (k) { f = k; setParam('f', k); table(); });
      table();
    }).catch(function (e) { document.getElementById('kpis').innerHTML = '<div class="card empty">Could not load: ' + esc(e.message).slice(0, 300) + '</div>'; });
  }
  function table() {
    var eps = data.episodes.filter(function (e) { return f === 'all' || e.outcome === f; });
    if (!eps.length) { document.getElementById('eps').innerHTML = '<div class="empty">No pauses here.</div>'; return; }
    document.getElementById('eps').innerHTML = '<table><tr><th>Went idle</th><th>Chat</th><th class="num">Context</th><th class="num">Away</th><th>Checks</th><th>Outcome</th><th class="num">Net</th><th class="num">Best</th></tr>' + eps.map(function (e, i) {
      var dots = e.checks.map(function (c) { return '<i class="' + (c.kind === 'kept' ? '' : c.kind) + '" title="' + clock(c.t) + ' · ' + dur(c.idle) + ' idle · ' + (c.p == null ? '' : 'P ' + pct(c.p) + ' vs ' + pct(c.need) + ' · ') + c.kind + '"></i>'; }).join('');
      var ret = e.ret == null ? '' : '<span class="ret" style="color:' + (e.warm ? 'var(--green)' : 'var(--orange)') + '" title="' + (e.warm ? 'came back to a warm cache' : 'came back to a cold cache') + '">' + (e.warm ? '↩ warm' : '↩ cold') + '</span>';
      var o = OUT[e.outcome];
      return '<tr class="ep" data-i="' + i + '"><td class="num" style="text-align:left">' + when(e.start) + '</td><td class="chat">' + chatCell(e) + '</td><td class="num">' + tok(e.prompt) + '</td><td class="num">' + (e.gap == null ? '<span class="sub">not back</span>' : dur(e.gap)) + '</td>' +
        '<td><div class="checks">' + (dots || '<span class="sub">none</span>') + ret + '</div></td><td><span class="pill ' + o[0] + '">' + o[1] + '</span>' + (e.why ? '<div class="sub" style="max-width:220px;white-space:normal" title="' + esc(e.why) + '">' + esc(e.why.split(':')[0]) + '</div>' : '') + '</td>' +
        '<td class="num ' + (e.net > 0 ? 'pos' : e.net < 0 ? 'neg' : '') + '">' + (e.net ? stok(e.net) : '–') + '</td><td class="num">' + (e.best ? tok(e.best) : '–') + '</td></tr>';
    }).join('') + '</table>' + (data.total_episodes > data.episodes.length ? '<div class="empty">Showing the newest ' + data.episodes.length + ' of ' + data.total_episodes + ' pauses.</div>' : '');
    document.querySelectorAll('tr.ep').forEach(function (tr) { tr.onclick = function () {
      var nx = tr.nextElementSibling; if (nx && nx.classList.contains('det')) { nx.remove(); return; }
      var e = eps[+tr.dataset.i], d = document.createElement('tr'); d.className = 'det';
      d.innerHTML = '<td colspan="8"><div class="story">' + esc(e.story) + '</div>' + (e.checks.length ? '<table><tr><th>Check</th><th class="num">Idle</th><th class="num">P(return)</th><th class="num">Needed</th><th>Did</th><th>Next hour</th><th class="num">Cost</th></tr>' + e.checks.map(function (c) {
        var did = c.kind === 'kept' ? 'kept alive' : c.kind === 'failed' ? 'keep-alive failed: ' + esc(c.error || '') : 'let go (' + esc(c.reason) + ')';
        var nh = c.returned ? '<span class="pos">came back after ' + dur(c.ret_in) + '</span>' : '<span class="sub">no return</span>';
        return '<tr><td>' + when(c.t) + '</td><td class="num">' + dur(c.idle) + '</td><td class="num">' + pct(c.p) + '</td><td class="num">' + pct(c.need) + '</td><td>' + did + '</td><td>' + nh + '</td><td class="num">' + (c.cost ? tok(c.cost) : '–') + '</td></tr>';
      }).join('') + '</table>' : '') + '</td>';
      tr.after(d);
    }; });
  }
  load();
}
if (PAGE === 'overview') overview();
if (PAGE === 'decisions') decisions();
</script></body></html>"""


def shell(page, body='', head=None):
    return (PAGE.replace('%%BODY%%', body).replace('%%PAGE%%', page)
            .replace('%%HEAD%%', json.dumps(head).replace('</', '<\\/') if head else 'null'))


STORE = Store()


class H(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        q = dict(urllib.parse.parse_qsl(u.query))
        rng = q.get('range') if q.get('range') in RANGES else None
        try:
            if u.path == '/api/overview':
                return self.send(json.dumps(api_overview(STORE, rng or '24h')), 'application/json')
            if u.path == '/api/badge':
                return self.send(json.dumps(api_badge(STORE)), 'application/json')
            if u.path == '/api/live':
                return self.send(json.dumps(api_live(STORE)), 'application/json')
            if u.path == '/api/decisions':
                return self.send(json.dumps(api_decisions(STORE, rng or '7d')), 'application/json')
            if u.path in ('/', '/index.html'):
                return self.send(shell('overview'))
            if u.path == '/decisions':
                return self.send(shell('decisions'))
            if u.path == '/policy':
                M = STORE.model()
                return self.send(shell('policy', policy_html(STORE), header(STORE, M)))
            return self.send('not found', 'text/plain', 404)
        except Exception:
            import traceback
            return self.send(f"<pre>{esc(traceback.format_exc())}</pre>", 'text/html', 500)

    def send(self, body, ctype='text/html', code=200):
        b = body.encode()
        self.send_response(code)
        self.send_header('Content-Type', ctype + '; charset=utf-8')
        self.send_header('Cache-Control', 'no-store')
        self.send_header('Content-Length', str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def log_message(self, *a):
        pass


if __name__ == '__main__':
    port = int(os.environ.get('KEEPALIVE_DASHBOARD_PORT', '8797'))
    print(f'dashboard on http://127.0.0.1:{port}/')
    threading.Thread(target=STORE.model, daemon=True).start()   # warm the cache before the first page load
    http.server.ThreadingHTTPServer(('127.0.0.1', port), H).serve_forever()

