#!/usr/bin/env python3
"""Cache keep-alive dashboard: live cost of the learned policy vs the old 5-minute cache and
Claude Code's 1-hour policy, recomputed from opencodex logs on every page load.
Serves http://127.0.0.1:8797/ ."""
import json, os, collections, datetime, html, http.server, pathlib, time

HOME = pathlib.Path(os.environ.get('OPENCODEX_HOME', pathlib.Path.home() / '.opencodex')).expanduser()
D = pathlib.Path(os.environ['KEEPALIVE_DIR']).expanduser() if os.environ.get('KEEPALIVE_DIR') else HOME / 'cache-keepalive'
USAGE = pathlib.Path(os.environ['KEEPALIVE_USAGE']).expanduser() if os.environ.get('KEEPALIVE_USAGE') else (D / 'usage.jsonl' if (D / 'usage.jsonl').exists() else HOME / 'usage.jsonl')
KA = D / 'keepalive.jsonl'
READ = {'claude-opus-5-5': 0.05, 'claude-sonnet-5-5': 0.05, 'claude-fable-5-1': 0.025}
PRICE = {'claude-opus-5-5': 4.0}  # US$ per million base input tokens (API list price), others assumed equal
FLOOR = 31559
W5, W60 = 1.25, 2.0
TZ = datetime.datetime.now().astimezone().tzinfo

def _json(p):
    try:
        return json.loads(p.read_text())
    except Exception:
        return {}

def switched_at():
    try:
        return json.loads((D / 'state.json').read_text())['switched_at']
    except Exception:
        return time.time() - 86400

def usage_rows(t0):
    rows = []
    with open(USAGE) as f:
        for line in f:
            if '"anthropic' not in line[:400]:
                continue
            try:
                d = json.loads(line)
            except Exception:
                continue
            if d.get('status') != 200 or not d.get('usage') or not d.get('conversationId'):
                continue
            t = d['timestamp'] / 1000
            if t < t0 - 86400:
                continue
            u = d['usage']
            rows.append(dict(t=t, cid=d['conversationId'], model=d.get('model'), acct=d.get('provider'),
                             P=u['inputTokens'], cr=u['cacheReadInputTokens'], cc=u['cacheCreationInputTokens'], out=u['outputTokens']))
    return rows

def compute():
    t0 = switched_at()
    rows = usage_rows(t0)
    streams = collections.defaultdict(list)
    for r in rows:
        streams[(r['cid'], r['model'], r['acct'])].append(r)
    day = lambda t: datetime.datetime.fromtimestamp(t, TZ).strftime('%m-%d')
    tot = collections.Counter(); daily = collections.defaultdict(collections.Counter); usd = collections.Counter()
    rescued = 0; rescued_tokens = 0; long_returns = 0
    for v in streams.values():
        v.sort(key=lambda r: r['t'])
        prev = None
        for r in v:
            if r['t'] >= t0 and prev is not None and prev['t'] < t0:
                # first request after the switch: its 5-minute cache cannot be read by a 1-hour request,
                # so it rewrites once at 2x. One-time cost, kept out of the ongoing comparison.
                rm = READ.get(r['model'], 0.1)
                tot['switch_cost'] += max(0, r['P'] - r['cr'] - r['cc']) + W60 * r['cc'] + rm * r['cr'] - (max(0, r['P'] - r['cr'] - r['cc']) + rm * min(r['P'], prev['P']) + W5 * max(0, r['P'] - min(r['P'], prev['P'])))
                tot['switch_requests'] += 1
            elif r['t'] >= t0:
                rm = READ.get(r['model'], 0.1); price = PRICE.get(r['model'], 4.0)
                u = max(0, r['P'] - r['cr'] - r['cc'])
                gap = r['t'] - prev['t'] if prev else None
                floor = min(r['cr'], FLOOR)
                actual = u + W60 * r['cc'] + rm * r['cr']
                read_cc = floor if gap is not None and gap > 3630 else r['cr']
                read_5 = floor if gap is not None and gap > 300 else r['cr']
                cc1h = u + W60 * (r['P'] - u - read_cc) + rm * read_cc
                old5 = u + W5 * (r['P'] - u - read_5) + rm * read_5
                for k, val in (('learned', actual), ('cc_1h', cc1h), ('old_5m', old5)):
                    tot[k] += val; daily[day(r['t'])][k] += val; usd[k] += val * price / 1e6
                tot['requests'] += 1; tot['output'] += 5 * r['out']
                if gap is not None and gap > 3630:
                    long_returns += 1
                    if prev and r['cr'] >= 0.8 * min(prev['P'], r['P']):
                        rescued += 1; rescued_tokens += r['cr']
            prev = r
    pings = collections.Counter(); stops = collections.Counter(); ping_cost = 0.0; ping_usd = 0.0
    recent = []
    if KA.exists():
        for line in open(KA):
            try:
                e = json.loads(line)
            except Exception:
                continue
            if e['ts'] / 1000 < t0:
                continue
            if e['event'] == 'ping':
                ok = e.get('status') == 200 and 'error' not in e
                pings['ok' if ok else 'failed'] += 1
                if ok:
                    rm = READ.get(e.get('model'), 0.1)
                    c = rm * e.get('read', 0) + W60 * e.get('write', 0) + e.get('input', 0) + 5 * 2
                    ping_cost += c; ping_usd += c * PRICE.get(e.get('model'), 4.0) / 1e6
                    daily[day(e['ts'] / 1000)]['learned'] += c
                    pings['hit_read'] += e.get('read', 0) >= 0.8 * e.get('prompt', 1)
            else:
                stops[e['event'] + ':' + e.get('reason', '')] += 1
            recent.append(e)
    tot['learned'] += ping_cost; usd['learned'] += ping_usd
    return dict(t0=t0, tot=tot, usd=usd, daily=daily, pings=pings, stops=stops, ping_cost=ping_cost,
                rescued=rescued, rescued_tokens=rescued_tokens, long_returns=long_returns, recent=recent[-12:],
                policy=_json(D / 'policy.json'), status=_json(D / 'status.json'), disabled=_json(D / 'disabled') if (D / 'disabled').exists() else None)

def pct(a, b):
    return f"{100 * (a - b) / b:+.1f}%" if b else '–'

def page():
    c = compute(); tot = c['tot']; usd = c['usd']
    hours = max(0.01, (time.time() - c['t0']) / 3600)
    started = datetime.datetime.fromtimestamp(c['t0'], TZ).strftime('%d/%m %H:%M')
    cards = ''
    for key, label, sub in (('old_5m', '5-minute cache', 'every write 5 min (opencodex default)'), ('cc_1h', '1-hour cache only', 'every write 1 h, no keep-alive (Claude Code default)'), ('learned', '1 h + keep-alive (live)', 'what actually happened')):
        cards += f"""<div class="card"><div class="lbl">{label}</div><div class="sub">{sub}</div>
<div class="big">{tot[key] / 1e6:,.1f}M</div><div class="sub">token-equivalents · ~US$ {usd[key]:,.0f} at API prices</div>
<div class="row">vs 5 min <b>{pct(tot[key], tot['old_5m'])}</b> · vs 1 h only <b>{pct(tot[key], tot['cc_1h'])}</b></div></div>"""
    days = sorted(c['daily'])
    mx = max([max(c['daily'][d].values() or [1]) for d in days] or [1])
    bars = ''
    for d in days:
        x = c['daily'][d]
        bars += f"<div class='day'><div class='bars'>" + ''.join(
            f"<div class='bar {k}' title='{k}: {x[k]/1e6:.1f}M' style='height:{120 * x[k] / mx:.0f}px'></div>" for k in ('old_5m', 'cc_1h', 'learned')) + f"</div><div class='sub'>{d}</div></div>"
    stops = ', '.join(f"{k} {v}" for k, v in c['stops'].most_common()) or 'none yet'
    pol = c['policy'] or {}; st = c['status'] or {}
    rec = ''.join(f"<tr><td>{datetime.datetime.fromtimestamp(e['ts']/1000, TZ).strftime('%d/%m %H:%M')}</td><td>{html.escape(e['event'])}</td><td>{html.escape(str(e.get('reason','')))}</td><td>{e.get('prompt',0)/1e3:.0f}k</td><td>{e.get('idle_s',0)/3600:.1f} h</td><td>{e.get('p') if e.get('p') is not None else '–'}</td><td>{e.get('read','')}</td></tr>" for e in reversed(c['recent']))
    return f"""<!doctype html><html><head><meta charset="utf-8"><title>Cache keep-alive</title><meta http-equiv="refresh" content="300">
<style>body{{font:14px -apple-system,system-ui,sans-serif;margin:24px;color:#1d1d1f;background:#fafafa}} h1{{font-size:18px;font-weight:500;margin:0 0 4px}}
.sub{{color:#6e6e73;font-size:12px}} .cards{{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:12px;margin:16px 0}}
.card{{background:#fff;border:1px solid #e5e5ea;border-radius:10px;padding:14px}} .lbl{{font-weight:500}} .big{{font-size:26px;font-weight:500;margin:8px 0 2px}} .row{{margin-top:8px;font-size:12px}}
.chart{{display:flex;gap:10px;align-items:flex-end;background:#fff;border:1px solid #e5e5ea;border-radius:10px;padding:14px;overflow-x:auto}} .day{{text-align:center}} .bars{{display:flex;gap:2px;align-items:flex-end;height:124px}}
.bar{{width:12px;border-radius:2px 2px 0 0}} .old_5m{{background:#c7c7cc}} .cc_1h{{background:#8fb4f2}} .learned{{background:#2f6fdf}}
table{{border-collapse:collapse;width:100%;background:#fff;border:1px solid #e5e5ea;border-radius:10px;margin-top:8px}} td,th{{padding:6px 10px;border-bottom:1px solid #f0f0f0;text-align:left;font-size:12px}}
.legend span{{display:inline-block;width:10px;height:10px;border-radius:2px;margin:0 4px 0 12px;vertical-align:middle}}</style></head><body>
<h1>Anthropic prompt cache: what the keep-alive saves</h1>
<div class="sub">Live since {started} ({hours:.1f} h, {tot['requests']:,} requests). Input-side cost in base-input-token equivalents (1 h write 2×, 5 min write 1.25×, read 0.05× on Opus). The first two columns replay the same requests under those cache rules.</div>
<div class="cards">{cards}</div>
<div class="legend sub">Per day<span style="background:#c7c7cc"></span>5 min<span style="background:#8fb4f2"></span>1 h only<span style="background:#2f6fdf"></span>learned (live)</div>
<div class="chart">{bars or '<div class="sub">No requests since the switch yet.</div>'}</div>
<div class="cards">
<div class="card"><div class="lbl">Keep-alives</div><div class="big">{c['pings']['ok']}</div><div class="sub">sent ({c['pings']['failed']} failed), {c['pings']['hit_read']} read the full cache · cost {c['ping_cost']/1e6:.2f}M</div></div>
<div class="card"><div class="lbl">Returns after more than 1 h idle</div><div class="big">{c['rescued']} / {c['long_returns']}</div><div class="sub">found their cache warm ({c['rescued_tokens']/1e6:.1f}M tokens read instead of rewritten)</div></div>
<div class="card"><div class="lbl">Policy (learned from your usage)</div><div class="big">{pol.get('max_idle_s',0)//3600} h</div><div class="sub">keep alive up to this long, chats over {pol.get('min_prompt_tokens',0)//1000}k tokens, cap {pol.get('daily_cap','–')}/day · tuner expects {pol.get('expected_saving_pct','–')}% vs 1 h only · trained {html.escape(str(pol.get('trained_at','–')))}</div></div>
<div class="card"><div class="lbl">Monitor</div><div class="big">{'OFF' if c['disabled'] is not None else 'on'}</div><div class="sub">{html.escape((c['disabled'] or {}).get('reason') or ('last check ' + str(st.get('checked_at','–')) + ': realized net ' + str((st.get('realized_72h') or {}).get('net_pct','–')) + '% of input over 72 h'))}</div></div>
</div>
<div class="sub">One-time switch cost (first request of each chat after the restart rewrote its context at the 1 h rate): {tot['switch_cost']/1e6:.1f}M over {tot['switch_requests']} chats, not included above.</div>
<div class="sub">Stopped keep-alives: {html.escape(stops)}</div>
<table><tr><th>When</th><th>Event</th><th>Why</th><th>Context</th><th>Idle</th><th>P(return within 1 h)</th><th>Read</th></tr>{rec or '<tr><td colspan=7 class=sub>No keep-alive decisions yet; the first ones come about an hour after a chat goes quiet.</td></tr>'}</table>
</body></html>"""

class H(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        try:
            body = page().encode()
            self.send_response(200); self.send_header('Content-Type', 'text/html; charset=utf-8')
        except Exception as e:
            body = f"<pre>{html.escape(repr(e))}</pre>".encode(); self.send_response(500)
        self.end_headers(); self.wfile.write(body)
    def log_message(self, *a):
        pass

if __name__ == '__main__':
    port = int(os.environ.get('KEEPALIVE_DASHBOARD_PORT', '8797'))
    print(f'dashboard on http://127.0.0.1:{port}/')
    http.server.ThreadingHTTPServer(('127.0.0.1', port), H).serve_forever()
