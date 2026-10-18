#!/usr/bin/env python3
"""Keep-alive dashboard: what the cache keep-alive saved and cost, the policy it learned, and every
decision it made. Recomputed from local logs on each page load. http://127.0.0.1:8797/"""
import collections, datetime, html, http.server, json, os, pathlib, time

HOME = pathlib.Path(os.environ.get('OPENCODEX_HOME', pathlib.Path.home() / '.opencodex')).expanduser()
D = pathlib.Path(os.environ['KEEPALIVE_DIR']).expanduser() if os.environ.get('KEEPALIVE_DIR') else HOME / 'cache-keepalive'
USAGE = pathlib.Path(os.environ['KEEPALIVE_USAGE']).expanduser() if os.environ.get('KEEPALIVE_USAGE') else (D / 'usage.jsonl' if (D / 'usage.jsonl').exists() else HOME / 'usage.jsonl')
KA = D / 'keepalive.jsonl'
READ = {'claude-opus-5-5': 0.05, 'claude-sonnet-5-5': 0.05, 'claude-fable-5-1': 0.025}
PRICE = {'claude-opus-5-5': 4.0}            # US$ per million base input tokens (API list); others assumed equal
W5, W60, TTL = 1.25, 2.0, 3600
TZ = datetime.datetime.now().astimezone().tzinfo
esc = html.escape

def _json(p):
    try:
        return json.loads(p.read_text())
    except Exception:
        return {}

def rm(model): return READ.get(model, 0.1)
def usd(tokens, model=None): return tokens * PRICE.get(model, 4.0) / 1e6
def fmt_tok(x): return f"{x / 1e6:,.1f}M" if abs(x) >= 1e6 else f"{x / 1e3:,.0f}k"
def fmt_usd(x): return f"US$ {x:,.0f}" if abs(x) >= 10 else f"US$ {x:,.2f}"
def when(ts): return datetime.datetime.fromtimestamp(ts, TZ).strftime('%d/%m %H:%M')

def load_usage(t0):
    streams = collections.defaultdict(list)
    if not USAGE.exists():
        return streams
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
            if t < t0 - 86400:
                continue
            streams[(d['conversationId'], d.get('model'), d.get('provider'))].append(
                dict(t=t, model=d.get('model'), P=u['inputTokens'], cr=u['cacheReadInputTokens'], cc=u['cacheCreationInputTokens'], out=u['outputTokens']))
    for v in streams.values():
        v.sort(key=lambda r: r['t'])
    return streams

def compute():
    st = _json(D / 'state.json'); t0 = st.get('switched_at', time.time() - 86400)
    pol = _json(D / 'policy.json'); floor = pol.get('shared_floor_tokens', 30000)
    streams = load_usage(t0)
    day = lambda t: datetime.datetime.fromtimestamp(t, TZ).strftime('%d/%m')
    tot = collections.Counter(); usd_tot = collections.Counter(); daily = collections.defaultdict(collections.Counter)
    eff = {'saved': 0.0, 'saved_usd': 0.0, 'rescues': 0, 'rescued_tokens': 0, 'cold': 0, 'cold_tokens': 0.0, 'cold_usd': 0.0}
    eff24 = collections.Counter(); now = time.time()
    switch = {'cost': 0.0, 'chats': 0}
    for v in streams.values():
        prev = None
        for r in v:
            if r['t'] >= t0 and prev is not None and prev['t'] < t0:
                switch['chats'] += 1
            elif r['t'] >= t0:
                m = rm(r['model']); u = max(0, r['P'] - r['cr'] - r['cc'])
                gap = r['t'] - prev['t'] if prev else None
                read_1h = min(r['cr'], floor) if gap is not None and gap > TTL + 30 else r['cr']
                read_5m = min(r['cr'], floor) if gap is not None and gap > 300 else r['cr']
                vals = {'live': u + W60 * r['cc'] + m * r['cr'],
                        'cc_1h': u + W60 * (r['P'] - u - read_1h) + m * read_1h,
                        'old_5m': u + W5 * (r['P'] - u - read_5m) + m * read_5m}
                for k, val in vals.items():
                    tot[k] += val; usd_tot[k] += usd(val, r['model']); daily[day(r['t'])][k] += val
                tot['requests'] += 1
                if gap is not None and gap > TTL + 30 and prev:
                    if r['cr'] >= 0.8 * min(prev['P'], r['P']):
                        s = (W60 - m) * max(0, r['cr'] - floor)
                        eff['saved'] += s; eff['saved_usd'] += usd(s, r['model']); eff['rescues'] += 1; eff['rescued_tokens'] += r['cr']
                        if now - r['t'] < 86400: eff24['saved'] += s; eff24['rescues'] += 1
                    else:
                        c = W60 * max(0, r['cc'])
                        eff['cold'] += 1; eff['cold_tokens'] += r['cc']; eff['cold_usd'] += usd(c, r['model'])
            prev = r
    events = []
    pings = collections.Counter(); spent = 0.0; spent_usd = 0.0; stops = collections.Counter()
    if KA.exists():
        for line in open(KA):
            try:
                e = json.loads(line)
            except Exception:
                continue
            if e.get('ts', 0) / 1000 < t0:
                continue
            m = rm(e.get('model'))
            if e.get('p_needed') is None and e.get('prompt'):
                reuse = max(0.0, 0.97 * (e['prompt'] + e.get('out', 0)) - floor)
                cost = m * (e['prompt'] + e.get('out', 0)) + 85
                e['p_needed'] = round(cost / ((W60 - m) * reuse), 4) if reuse > 0 else 1.0
            if e.get('p_return') is None and e.get('p') is not None:
                e['p_return'] = e['p']
            if e['event'] == 'ping':
                ok = e.get('status') == 200 and 'error' not in e
                pings['ok' if ok else 'failed'] += 1
                if ok:
                    c = m * e.get('read', 0) + W60 * e.get('write', 0) + e.get('input', 0) + 10
                    spent += c; spent_usd += usd(c, e.get('model')); daily[day(e['ts'] / 1000)]['live'] += c
                    pings['full'] += e.get('read', 0) >= 0.95 * e.get('prompt', 1)
                    if now - e['ts'] / 1000 < 86400: eff24['spent'] += c; eff24['pings'] += 1
            else:
                stops[e.get('reason', '?')] += 1
            events.append(e)
    tot['live'] += spent; usd_tot['live'] += spent_usd
    return dict(t0=t0, pol=pol, status=_json(D / 'status.json'), disabled=_json(D / 'disabled') if (D / 'disabled').exists() else None,
                tot=tot, usd=usd_tot, daily=daily, eff=eff, eff24=eff24, spent=spent, spent_usd=spent_usd, pings=pings, stops=stops,
                events=events, switch=switch, floor=floor)

def pct(a, b): return f"{100 * (a - b) / b:+.1f}%" if b else '–'

def model_section(c):
    pol = c['pol']; hz = pol.get('hazard'); floor = c['floor']
    if not pol:
        return '<p class="sub">No policy yet: it is trained once there are enough requests (the monitor runs daily).</p>'
    mode = pol.get('mode', 'rule')
    hr, rr = pol.get('hazard_result', {}), pol.get('rule_result', {})
    tr = pol.get('trained_on', {})
    head = f"""<div class="cards">
<div class="card"><div class="lbl">Rule in use</div><div class="big">{'probability' if mode == 'hazard' else 'fixed limit'}</div>
<div class="sub">{'Keep a chat alive while the chance you return within the next hour beats the break-even chance (table below). Hard limit 24 h idle.' if mode == 'hazard' else f"Keep chats over {pol.get('min_prompt_tokens', 0) // 1000}k tokens alive for up to {pol.get('max_idle_s', 0) // 3600} h idle."}
Daily cap {pol.get('daily_cap', '–')} keep-alives.</div></div>
<div class="card"><div class="lbl">Why this rule</div><div class="sub">Replayed on your last {tr.get('days', '–')} days ({tr.get('requests', 0):,} requests, {tr.get('conversations', 0)} chats):</div>
<div class="row">probability rule <b>{hr.get('saving_pct', '–')}%</b> saved · {hr.get('pings', '–')} keep-alives</div>
<div class="row">best fixed limit ({rr.get('max_idle_h', '–')} h, chats over {(rr.get('min_prompt_tokens') or 0) // 1000}k) <b>{rr.get('saving_pct', '–')}%</b> · {rr.get('pings', '–')} keep-alives</div>
<div class="sub">Trained {esc(str(pol.get('trained_at', '–')))}; retrained daily by the monitor.</div></div>
<div class="card"><div class="lbl">Break-even</div><div class="sub">A keep-alive re-reads the whole chat at {rm('claude-opus-5-5')}× input; coming back to a cold chat rewrites it at 2×. So a keep-alive pays when</div>
<div class="row"><b>P(return within the hour) &gt; keep-alive cost ÷ rewrite avoided</b></div>
<div class="sub">For a 300k-token Opus chat: {fmt_tok(0.05 * 300000)} ÷ {fmt_tok(1.95 * (0.97 * 300000 - floor))} ≈ {100 * 0.05 * 300000 / (1.95 * (0.97 * 300000 - floor)):.1f}%. The first ~{fmt_tok(floor)} (system prompt and tools) stay cached anyway, so small chats need a higher chance.</div></div>
</div>"""
    if not hz:
        return head
    edges = hz['size_edges']; labels = [f"{edges[i] // 1000}k–{edges[i + 1] // 1000}k" if i + 1 < len(edges) else f"{edges[i] // 1000}k+" for i in range(len(edges))]
    reps = [50000, 140000, 350000, 700000][:len(edges)]
    needs = [0.05 * x / (1.95 * max(1, 0.97 * x - floor)) for x in reps]
    rows = ''
    for k in range(len(hz['p'][0])):
        cells = ''
        for b in range(len(edges)):
            p = hz['p'][b][k]; n = hz.get('at_risk', [[0] * 24] * 4)[b][k]; ev = hz.get('returns', [[0] * 24] * 4)[b][k]
            on = p > needs[b]
            cells += f"<td class='{'on' if on else 'off'}' title='{ev} returns out of {n} chats still idle at this point'>{100 * p:.1f}%<span class='n'> {n}</span></td>"
        rows += f"<tr><th>{k + 1} h</th>{cells}</tr>"
    need_row = ''.join(f"<td class='need'>{100 * x:.1f}%</td>" for x in needs)
    return head + f"""<h2>Your return probabilities</h2>
<div class="sub">Each cell: of the chats still idle about this long (row) at this size (column), how many you came back to within the next hour. Small grey number: how many cases it is based on. Green: above break-even for a typical chat of that size, so it keeps the cache. Sizes are total context.</div>
<table class="hz"><tr><th>idle</th>{''.join(f'<th>{l}</th>' for l in labels)}</tr><tr><th>needed</th>{need_row}</tr>{rows}</table>"""

def page():
    c = compute(); tot = c['tot']; u = c['usd']; eff = c['eff']; e24 = c['eff24']
    hours = max(0.01, (time.time() - c['t0']) / 3600)
    net = eff['saved'] - c['spent']; net_usd = eff['saved_usd'] - c['spent_usd']
    status = 'OFF — ' + esc((c['disabled'] or {}).get('reason', 'disabled')) if c['disabled'] is not None else 'on'
    cards = ''
    for key, label, sub in (('old_5m', '5-minute cache', 'every write 5 min (opencodex default)'),
                            ('cc_1h', '1-hour cache only', 'every write 1 h, no keep-alive (Claude Code default)'),
                            ('live', '1 h + keep-alive (live)', 'what actually happened, keep-alives included')):
        cards += f"""<div class="card"><div class="lbl">{label}</div><div class="sub">{sub}</div><div class="big">{fmt_tok(tot[key])}</div>
<div class="sub">token-equivalents · ~{fmt_usd(u[key])} at API prices</div><div class="row">vs 5 min <b>{pct(tot[key], tot['old_5m'])}</b> · vs 1 h only <b>{pct(tot[key], tot['cc_1h'])}</b></div></div>"""
    effect = f"""<div class="eq">
<div class="card"><div class="lbl">Rewrites avoided</div><div class="big pos">{fmt_tok(eff['saved'])}</div><div class="sub">{eff['rescues']} times you came back after more than 1 h idle and the cache was still warm ({fmt_tok(eff['rescued_tokens'])} read instead of rewritten) · ~{fmt_usd(eff['saved_usd'])}</div></div>
<div class="op">−</div>
<div class="card"><div class="lbl">Keep-alive cost</div><div class="big neg">{fmt_tok(c['spent'])}</div><div class="sub">{c['pings']['ok']} keep-alives ({c['pings']['full']} read the full chat, {c['pings']['failed']} failed) · ~{fmt_usd(c['spent_usd'])}</div></div>
<div class="op">=</div>
<div class="card hl"><div class="lbl">Net saved by keep-alives</div><div class="big">{fmt_tok(net)}</div><div class="sub">~{fmt_usd(net_usd)} · {100 * net / tot['live']:.1f}% of input · returned {eff['saved'] / c['spent']:.1f}× its cost</div></div>
</div>
<div class="sub">Last 24 h: {e24['rescues']} rescues worth {fmt_tok(e24['saved'])} for {e24['pings']} keep-alives costing {fmt_tok(e24['spent'])}. Still cold after more than 1 h idle: {eff['cold']} returns rewrote {fmt_tok(eff['cold_tokens'])} (~{fmt_usd(eff['cold_usd'])}) — chats not kept alive, or whose opening changed. One-time switch to the 1-hour cache: {c['switch']['chats']} chats rewrote once, not counted.</div>""" if c['spent'] else '<div class="sub">No keep-alives yet; the first ones come about an hour after a chat goes quiet.</div>'
    days = sorted(c['daily'], key=lambda d: (d[3:], d[:2]))
    mx = max([max(c['daily'][d].values() or [1]) for d in days] or [1])
    bars = ''.join("<div class='day'><div class='bars'>" + ''.join(
        f"<div class='bar {k}' title='{k}: {fmt_tok(c['daily'][d][k])}' style='height:{110 * c['daily'][d][k] / mx:.0f}px'></div>" for k in ('old_5m', 'cc_1h', 'live'))
        + f"</div><div class='sub'>{d}</div></div>" for d in days)
    rows = ''
    for e in reversed(c['events'][-600:]):
        pr, need = e.get('p_return'), e.get('p_needed')
        bar = ''
        if pr is not None and need is not None:
            scale = max(0.3, pr, need) * 1.1
            bar = f"<div class='pb'><div class='pf' style='width:{100 * min(pr, scale) / scale:.0f}%'></div><div class='pn' style='left:{100 * min(need, scale) / scale:.0f}%'></div></div>"
        kind = 'ping' if e['event'] == 'ping' else 'stop'
        read = f"{100 * e.get('read', 0) / max(1, e.get('prompt', 1)):.0f}%" if e['event'] == 'ping' and 'read' in e else ''
        why = {'worth_it': 'kept: P above break-even', 'rule': 'kept: within fixed limit', 'not_worth_it': 'stopped: P below break-even', 'max_idle': 'stopped: idle limit',
               'daily_cap': 'stopped: daily cap', 'cache_changed': 'stopped: chat opening changed', 'too_small': 'stopped: chat too small', 'disabled': 'stopped: disabled',
               'no_valid_token': 'stopped: login expired'}.get(e.get('reason'), e.get('reason') or ('kept' if kind == 'ping' else ''))
        if e['event'] == 'ping' and (e.get('status') != 200 or 'error' in e):
            why = 'failed: ' + esc(str(e.get('error', e.get('status'))))[:60]
        rows += (f"<tr class='{kind}'><td>{when(e['ts'] / 1000)}</td><td class='mono'>{esc(e.get('conv', '')[:6])}</td><td>{e.get('prompt', 0) / 1e3:,.0f}k</td>"
                 f"<td>{e.get('idle_s', 0) / 3600:.1f} h</td><td>{'' if pr is None else f'{100 * pr:.1f}%'}</td><td>{'' if need is None else f'{100 * need:.1f}%'}</td>"
                 f"<td>{bar}</td><td>{why}</td><td>{read}</td></tr>")
    return f"""<!doctype html><html><head><meta charset="utf-8"><title>Cache keep-alive</title><meta http-equiv="refresh" content="300">
<style>
body{{font:14px -apple-system,system-ui,sans-serif;margin:24px auto;max-width:1180px;padding:0 20px;color:#1d1d1f;background:#fafafa}}
h1{{font-size:20px;font-weight:600;margin:0 0 4px}} h2{{font-size:15px;font-weight:600;margin:28px 0 6px}}
.sub{{color:#6e6e73;font-size:12px;line-height:1.45}} .cards{{display:grid;grid-template-columns:repeat(auto-fit,minmax(260px,1fr));gap:12px;margin:12px 0}}
.card{{background:#fff;border:1px solid #e5e5ea;border-radius:12px;padding:14px}} .lbl{{font-weight:600}} .big{{font-size:26px;font-weight:600;margin:6px 0 2px}} .row{{margin-top:6px;font-size:12px}}
.pos{{color:#1f8a4c}} .neg{{color:#c2410c}} .hl{{border-color:#2f6fdf;box-shadow:0 0 0 1px #2f6fdf inset}}
.eq{{display:grid;grid-template-columns:1fr auto 1fr auto 1fr;gap:10px;align-items:center;margin:10px 0}} .op{{font-size:26px;color:#8e8e93}}
@media(max-width:820px){{.eq{{grid-template-columns:1fr}} .op{{display:none}}}}
.chart{{display:flex;gap:12px;align-items:flex-end;background:#fff;border:1px solid #e5e5ea;border-radius:12px;padding:14px;overflow-x:auto}} .day{{text-align:center}} .bars{{display:flex;gap:2px;align-items:flex-end;height:114px}}
.bar{{width:12px;border-radius:2px 2px 0 0}} .old_5m{{background:#c7c7cc}} .cc_1h{{background:#8fb4f2}} .live{{background:#2f6fdf}}
.legend span{{display:inline-block;width:10px;height:10px;border-radius:2px;margin:0 4px 0 12px;vertical-align:middle}}
table{{border-collapse:collapse;width:100%;background:#fff;border:1px solid #e5e5ea;border-radius:12px;overflow:hidden}} td,th{{padding:6px 9px;border-bottom:1px solid #f0f0f0;text-align:left;font-size:12px;white-space:nowrap}}
th{{color:#6e6e73;font-weight:500}} .mono{{font-family:ui-monospace,monospace}}
table.hz{{width:auto}} .hz td{{text-align:right;min-width:92px}} .hz td.on{{background:#e7f6ec}} .hz td.off{{background:#fbfbfb;color:#8e8e93}} .hz td.need{{color:#c2410c;font-weight:600}} .n{{color:#aeaeb2;font-size:10px}}
.pb{{position:relative;width:140px;height:8px;background:#f0f0f3;border-radius:4px}} .pf{{position:absolute;left:0;top:0;bottom:0;background:#2f6fdf;border-radius:4px}} .pn{{position:absolute;top:-3px;bottom:-3px;width:2px;background:#c2410c}}
.filters button{{font:12px -apple-system,system-ui;border:1px solid #d1d1d6;background:#fff;border-radius:999px;padding:4px 12px;margin-right:6px;cursor:pointer}} .filters button.on{{background:#1d1d1f;color:#fff;border-color:#1d1d1f}}
.tablewrap{{max-height:560px;overflow:auto;border-radius:12px}}
</style></head><body>
<h1>Prompt-cache keep-alive</h1>
<div class="sub">Live since {when(c['t0'])} ({hours:.0f} h, {tot['requests']:,} requests) · keep-alives {status} · input-side cost in base-input-token equivalents (1 h write 2×, 5 min write 1.25×, read 0.05× on Opus); US$ at Opus API prices, on a subscription it is quota.</div>
<h2>What the keep-alives did</h2>{effect}
<h2>Cost under each cache rule</h2><div class="sub">The first two replay the same requests under that cache rule.</div><div class="cards">{cards}</div>
<div class="legend sub">Per day<span style="background:#c7c7cc"></span>5 min<span style="background:#8fb4f2"></span>1 h only<span style="background:#2f6fdf"></span>live</div>
<div class="chart">{bars or '<div class="sub">No requests yet.</div>'}</div>
<h2>The policy</h2>{model_section(c)}
<h2>Every decision</h2>
<div class="sub">One row per check, about an hour after a chat's last use and every hour after that. Blue bar: chance you return within the next hour; red mark: chance needed for the keep-alive to pay. Read: share of the chat the keep-alive found cached.</div>
<div class="filters" style="margin:8px 0"><button class="on" data-f="all">All</button><button data-f="ping">Kept alive</button><button data-f="stop">Stopped</button></div>
<div class="tablewrap"><table id="ev"><tr><th>When</th><th>Chat</th><th>Context</th><th>Idle</th><th>P(return, next hour)</th><th>P needed</th><th></th><th>Decision</th><th>Read</th></tr>{rows or '<tr><td colspan=9 class=sub>No decisions yet.</td></tr>'}</table></div>
<script>
document.querySelectorAll('.filters button').forEach(b => b.onclick = () => {{
  document.querySelectorAll('.filters button').forEach(x => x.classList.toggle('on', x === b));
  document.querySelectorAll('#ev tr.ping, #ev tr.stop').forEach(r => r.style.display = (b.dataset.f === 'all' || r.classList.contains(b.dataset.f)) ? '' : 'none');
}});
</script>
</body></html>"""

class H(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        try:
            body = page().encode(); self.send_response(200); self.send_header('Content-Type', 'text/html; charset=utf-8')
        except Exception as e:
            import traceback
            body = f"<pre>{esc(traceback.format_exc())}</pre>".encode(); self.send_response(500)
        self.end_headers(); self.wfile.write(body)
    def log_message(self, *a):
        pass

if __name__ == '__main__':
    port = int(os.environ.get('KEEPALIVE_DASHBOARD_PORT', '8797'))
    print(f'dashboard on http://127.0.0.1:{port}/')
    http.server.ThreadingHTTPServer(('127.0.0.1', port), H).serve_forever()
