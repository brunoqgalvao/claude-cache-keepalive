#!/usr/bin/env python3
"""Restart opencodex at a quiet moment so it loads an updated patch without cutting off running chats.

Waits until opencodex reports no turns in progress for about a minute and no request has finished
in the last 10 minutes, then asks opencodex for its graceful drain-and-restart (it waits up to 60 s
for anything still running). Gives up after 24 h. Writes the outcome to quiet_restart.json.
"""
import json, os, pathlib, subprocess, time, urllib.request

HOME = pathlib.Path(os.environ.get('OPENCODEX_HOME', pathlib.Path.home() / '.opencodex')).expanduser()
D = HOME / 'cache-keepalive'
BASE = 'http://127.0.0.1:' + os.environ.get('OCX_PORT', '10100')
QUIET_S = int(os.environ.get('QUIET_RESTART_IDLE_S', '600'))
LABEL = 'com.claude-cache-keepalive.quiet-restart'

def call(path, method='GET', headers=None):
    tok = (HOME / 'admin-api-token').read_text().strip()
    req = urllib.request.Request(BASE + path, method=method, headers={'Authorization': f'Bearer {tok}', **(headers or {})})
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read() or b'{}')

def last_request_age():
    try:
        return time.time() - (HOME / 'usage.jsonl').stat().st_mtime
    except Exception:
        return 1e9

def write(result):
    (D / 'quiet_restart.json').write_text(json.dumps({'at': time.strftime('%Y-%m-%dT%H:%M:%S'), **result}, indent=1))

def remove_job():
    """One-shot: retire the launchd job so it does not restart opencodex again at the next login."""
    plist = pathlib.Path.home() / 'Library/LaunchAgents' / f'{LABEL}.plist'
    if plist.exists():
        plist.rename(plist.with_suffix('.plist.done'))
    subprocess.run(['launchctl', 'bootout', f'gui/{os.getuid()}/{LABEL}'], capture_output=True)

def run():
    D.mkdir(parents=True, exist_ok=True)
    deadline = time.time() + 24 * 3600
    calm = 0
    write({'state': 'waiting for a quiet moment'})
    while time.time() < deadline:
        try:
            m = call('/api/system/memory')
            if m.get('activeTurnCount', 1) == 0 and not m.get('isDraining') and last_request_age() > QUIET_S:
                calm += 1
            else:
                calm = 0
            if calm >= 3:
                old = m.get('pid')
                res = call('/api/system/restart', 'POST', {'x-ocx-expected-pid': str(old)})
                for _ in range(60):
                    time.sleep(5)
                    try:
                        new = call('/api/system/memory').get('pid')
                        if new and new != old:
                            write({'state': 'restarted', 'old_pid': old, 'new_pid': new, 'drain': res})
                            return
                    except Exception:
                        pass
                write({'state': 'restart requested but no new process seen within 5 min', 'old_pid': old, 'drain': res})
                return
        except Exception as e:
            calm = 0
            write({'state': 'waiting', 'last_error': str(e)[:200]})
        time.sleep(20)
    write({'state': 'gave up after 24 h without a quiet moment'})

def main():
    try:
        run()
    finally:
        remove_job()

if __name__ == '__main__':
    main()
