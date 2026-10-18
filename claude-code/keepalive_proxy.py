#!/usr/bin/env python3
"""Local pass-through proxy for Claude Code that keeps idle prompt caches warm past 1 hour.
https://github.com/brunoqgalvao/claude-cache-keepalive

Claude Code points at it with ANTHROPIC_BASE_URL=http://127.0.0.1:8796. Every request is relayed
to api.anthropic.com unchanged and streamed back as it arrives. For conversations that use the
1-hour cache (Claude Code's main chat on a subscription), shortly before the cache would expire it
re-sends the conversation's last request with max_tokens=16 and drops the connection at
message_start: the cached prefix is read at the read price and its 1-hour clock restarts.

It logs request usage (no content) to usage.jsonl so tune.py and monitor.py can learn your policy,
and every keep-alive to keepalive.jsonl. Standard library only.
"""
import gzip, hashlib, http.client, http.server, json, os, pathlib, ssl, threading, time

D = pathlib.Path(os.environ.get('KEEPALIVE_DIR', pathlib.Path.home() / '.claude-cache-keepalive')).expanduser()
UPSTREAM = os.environ.get('KEEPALIVE_UPSTREAM', 'api.anthropic.com')
PORT = int(os.environ.get('KEEPALIVE_PROXY_PORT', '8796'))
DEFAULT_POLICY = {'mode': 'rule', 'max_idle_s': 4 * 3600, 'min_prompt_tokens': 40000, 'daily_cap': 400, 'shared_floor_tokens': 30000, 'hazard': None}
READ = {'claude-opus-5-5': 0.05, 'claude-sonnet-5-5': 0.05, 'claude-fable-5-1': 0.025}
LEAD_S = int(os.environ.get('KEEPALIVE_LEAD_S', '60'))
TTL_S = int(os.environ.get('KEEPALIVE_TTL_S', '3600'))  # override only for testing
HOP = {'host', 'content-length', 'connection', 'accept-encoding', 'transfer-encoding', 'keep-alive', 'proxy-connection', 'upgrade', 'te'}
CTX = ssl.create_default_context()
lock = threading.Lock()
convs = {}
latest_auth = {}
today = {'day': '', 'pings': 0}

def jlog(name, rec):
    try:
        D.mkdir(parents=True, exist_ok=True)
        with open(D / name, 'a') as f:
            f.write(json.dumps(rec) + '\n')
    except Exception:
        pass

def policy():
    try:
        p = json.loads((D / 'policy.json').read_text())
        return {k: p.get(k, v) for k, v in DEFAULT_POLICY.items()}
    except Exception:
        return dict(DEFAULT_POLICY)

def decide(c, p, idle):
    """Chance you come back within the next hour (from your history) vs the chance needed for a re-read to pay."""
    r = READ.get(c['model'], 0.1)
    reuse = max(0.0, 0.97 * (c['prompt'] + c.get('out', 0)) - p['shared_floor_tokens'])
    cost = r * (c['prompt'] + c.get('out', 0)) + 2 * 30 + 5 * 5
    need = cost / ((2 - r) * reuse) if reuse > 0 else 1.0
    hz = p.get('hazard')
    if p.get('mode') != 'hazard' or not hz:
        ok = idle + TTL_S <= p['max_idle_s'] + 60 and c['prompt'] >= p['min_prompt_tokens']
        return ok, ('rule' if ok else 'max_idle'), None, round(need, 4)
    b = -1
    for i, e in enumerate(hz['size_edges']):
        if c['prompt'] >= e:
            b = i
    if b < 0:
        return False, 'too_small', None, round(need, 4)
    row = hz['p'][b]
    k = max(1, min(len(row), round(idle / hz['step_s'])))
    pr = row[k - 1]
    ok = pr > need and idle + TTL_S <= p['max_idle_s'] + 60
    return ok, ('worth_it' if ok else ('max_idle' if pr > need else 'not_worth_it')), round(pr, 4), round(need, 4)

def disabled():
    return os.environ.get('CLAUDE_CACHE_KEEPALIVE') == '0' or (D / 'disabled').exists()

def conv_key(body):
    msgs = body.get('messages') or []
    if not msgs:
        return None
    return hashlib.sha256(json.dumps([body.get('model'), body.get('system'), msgs[:2]], sort_keys=True).encode()).hexdigest()[:32]

def uses_1h(body):
    return '"ttl": "1h"' in json.dumps(body) or '"ttl":"1h"' in json.dumps(body, separators=(',', ':'))

class SSEWatch:
    """Reads usage and stop_reason out of the event stream as it passes through."""
    def __init__(self):
        self.buf = b''
        self.usage = {}
        self.stop = None
    def feed(self, chunk):
        self.buf += chunk
        while b'\n' in self.buf:
            line, self.buf = self.buf.split(b'\n', 1)
            line = line.strip()
            if not line.startswith(b'data:'):
                continue
            try:
                ev = json.loads(line[5:])
            except Exception:
                continue
            if ev.get('type') == 'message_start':
                self.usage.update((ev.get('message') or {}).get('usage') or {})
            elif ev.get('type') == 'message_delta':
                self.usage.update({k: v for k, v in (ev.get('usage') or {}).items() if v is not None})
                self.stop = (ev.get('delta') or {}).get('stop_reason') or self.stop

class Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'

    def log_message(self, *a):
        pass

    def _relay(self):
        n = int(self.headers.get('content-length') or 0)
        raw = self.rfile.read(n) if n else b''
        fwd = {k: v for k, v in self.headers.items() if k.lower() not in HOP}
        fwd['Accept-Encoding'] = 'identity'
        body = None
        if self.command == 'POST' and self.path.split('?')[0].endswith('/v1/messages'):
            try:
                body = json.loads(raw)
            except Exception:
                body = None
        auth = {k: v for k, v in fwd.items() if k.lower() in ('authorization', 'x-api-key')}
        if auth:
            with lock:
                latest_auth.clear(); latest_auth.update(auth)
        key = conv_key(body) if isinstance(body, dict) else None
        if key:
            with lock:
                c = convs.get(key)
                if c and c.get('timer'):
                    c['timer'].cancel()
        sent_at = time.time()
        up = http.client.HTTPSConnection(UPSTREAM, context=CTX, timeout=600)
        try:
            up.request(self.command, self.path, body=raw if raw else None, headers=fwd)
            res = up.getresponse()
        except Exception as e:
            msg = json.dumps({'type': 'error', 'error': {'type': 'proxy_error', 'message': str(e)[:200]}}).encode()
            self.send_response(502); self.send_header('Content-Type', 'application/json'); self.send_header('Content-Length', str(len(msg))); self.end_headers(); self.wfile.write(msg)
            return
        self.send_response(res.status, res.reason)
        for k, v in res.getheaders():
            if k.lower() not in HOP and k.lower() != 'content-encoding':
                self.send_header(k, v)
        self.send_header('Transfer-Encoding', 'chunked')
        self.end_headers()
        watch = SSEWatch() if (key and res.status == 200) else None
        try:
            while True:
                chunk = res.read1(65536)
                if not chunk:
                    break
                if watch:
                    watch.feed(chunk)
                self.wfile.write(b'%x\r\n%s\r\n' % (len(chunk), chunk))
                self.wfile.flush()
            self.wfile.write(b'0\r\n\r\n'); self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            watch = None
        finally:
            up.close()
        if watch and watch.usage:
            done(key, body, fwd, sent_at, watch, self.path)

    do_POST = do_GET = do_PUT = do_DELETE = do_PATCH = _relay

def done(key, body, headers, sent_at, watch, path):
    u = watch.usage
    cr, cc, inp, out = (int(u.get(k) or 0) for k in ('cache_read_input_tokens', 'cache_creation_input_tokens', 'input_tokens', 'output_tokens'))
    prompt = inp + cr + cc
    jlog('usage.jsonl', {'provider': 'anthropic-claude-code', 'timestamp': int(time.time() * 1000), 'status': 200, 'model': body.get('model'),
                         'conversationId': key, 'usage': {'inputTokens': prompt, 'cacheReadInputTokens': cr, 'cacheCreationInputTokens': cc, 'outputTokens': out}})
    ttl = 3600 if uses_1h(body) else 300
    if ttl != 3600 or prompt < policy()['min_prompt_tokens'] or disabled():
        with lock:
            convs.pop(key, None)
        return
    c = {'key': key, 'path': path, 'out': out, 'body': gzip.compress(json.dumps(body).encode()), 'headers': headers, 'model': body.get('model'),
         'prompt': prompt, 'sent': sent_at, 'done': time.time(), 'pings': 0}
    with lock:
        convs[key] = c
    schedule(c)

def schedule(c):
    delay = max(1.0, c['sent'] + TTL_S - LEAD_S - time.time())
    t = threading.Timer(delay, fire, args=(c['key'],))
    t.daemon = True
    c['timer'] = t
    t.start()

def fire(key):
    with lock:
        c = convs.get(key)
    if not c:
        return
    p = policy()
    idle = time.time() - c['done']
    ok, why, pr, need = decide(c, p, idle)
    base = {'ts': int(time.time() * 1000), 'v': 2, 'src': 'claude-code', 'mode': p.get('mode'), 'conv': key, 'model': c['model'], 'prompt': c['prompt'],
            'idle_s': int(idle), 'pings_before': c['pings'], 'p_return': pr, 'p_needed': need}
    def stop(reason):
        jlog('keepalive.jsonl', {'event': 'stop', 'reason': reason, **base})
        with lock:
            convs.pop(key, None)
    if disabled():
        return stop('disabled')
    if not ok:
        return stop(why)
    day = time.strftime('%Y-%m-%d')
    if today['day'] != day:
        today.update(day=day, pings=0)
    if today['pings'] >= p['daily_cap']:
        return stop('daily_cap')
    started = time.time()
    r = ping(c)
    today['pings'] += 1
    jlog('keepalive.jsonl', {'event': 'ping', 'reason': why, 'ms': int((time.time() - started) * 1000), **base, **r})
    if r.get('status') != 200 or 'read' not in r:
        with lock:
            convs.pop(key, None)
        return
    if r['read'] < 0.5 * c['prompt']:
        return stop('cache_changed')
    c['pings'] += 1
    c['sent'] = started
    with lock:
        if convs.get(key) is c:
            schedule(c)

def ping(c):
    body = json.loads(gzip.decompress(c['body']))
    body['stream'] = True
    th = body.get('thinking') or {}
    body['max_tokens'] = th['budget_tokens'] + 64 if th.get('type') == 'enabled' and isinstance(th.get('budget_tokens'), int) else 16
    headers = dict(c['headers'])
    with lock:
        if latest_auth:
            for k in [k for k in headers if k.lower() in ('authorization', 'x-api-key')]:
                headers.pop(k)
            headers.update(latest_auth)
    headers['Accept'] = 'text/event-stream'
    up = http.client.HTTPSConnection(UPSTREAM, context=CTX, timeout=90)
    try:
        up.request('POST', c.get('path') or '/v1/messages', body=json.dumps(body).encode(), headers=headers)
        res = up.getresponse()
        if res.status != 200:
            return {'status': res.status, 'error': res.read(300).decode('utf8', 'replace')}
        w = SSEWatch()
        seen = 0
        while not w.usage and seen < 200000:
            chunk = res.read1(8192)
            if not chunk:
                break
            seen += len(chunk)
            w.feed(chunk)
        if not w.usage:
            return {'status': 200, 'error': 'no_message_start'}
        return {'status': 200, 'read': int(w.usage.get('cache_read_input_tokens') or 0), 'write': int(w.usage.get('cache_creation_input_tokens') or 0), 'input': int(w.usage.get('input_tokens') or 0)}
    except Exception as e:
        return {'status': 0, 'error': str(e)[:200]}
    finally:
        up.close()

if __name__ == '__main__':
    D.mkdir(parents=True, exist_ok=True)
    print(f'claude-cache-keepalive proxy on http://127.0.0.1:{PORT} -> https://{UPSTREAM}', flush=True)
    http.server.ThreadingHTTPServer(('127.0.0.1', PORT), Handler).serve_forever()
