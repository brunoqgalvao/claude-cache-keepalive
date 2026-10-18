#!/usr/bin/env python3
"""Install the prompt-cache keep-alive for Claude Code.

  python3 install.py              install (or update)
  python3 install.py --enable     clear a "disabled" flag set by the monitor
  python3 install.py --uninstall  point Claude Code back at Anthropic and remove the extras

What install does:
  1. copies the proxy and tools to ~/.claude-cache-keepalive
  2. starts the proxy on 127.0.0.1:8796 (macOS: a launchd job that restarts it if it stops)
  3. adds to ~/.claude/settings.json "env" (backup kept):
       ANTHROPIC_BASE_URL=http://127.0.0.1:8796   send Claude Code's requests through the proxy
       CLAUDE_CODE_PROMPT_CACHE_TTL=1h            1-hour cache for the main conversation (keep-alives
                                                  only apply to 1-hour caches)
  4. dashboard on http://127.0.0.1:8798 and a daily monitor that re-tunes or switches keep-alives off
New Claude Code sessions pick up the settings; restart open ones.
"""
import argparse, json, os, pathlib, platform, shutil, subprocess, sys, time, urllib.request

HERE = pathlib.Path(__file__).resolve().parent
D = pathlib.Path(os.environ.get('KEEPALIVE_DIR', pathlib.Path.home() / '.claude-cache-keepalive')).expanduser()
SETTINGS = pathlib.Path.home() / '.claude' / 'settings.json'
PORT = 8796
DASH_PORT = 8798
LABELS = {'proxy': 'com.claude-cache-keepalive.cc-proxy', 'dashboard': 'com.claude-cache-keepalive.cc-dashboard', 'monitor': 'com.claude-cache-keepalive.cc-monitor'}
ENV = {'ANTHROPIC_BASE_URL': f'http://127.0.0.1:{PORT}', 'CLAUDE_CODE_PROMPT_CACHE_TTL': '1h'}

def launchd(name, args, extra, env):
    plist = pathlib.Path.home() / 'Library' / 'LaunchAgents' / f'{LABELS[name]}.plist'
    plist.parent.mkdir(parents=True, exist_ok=True)
    prog = ''.join(f'<string>{a}</string>' for a in args)
    envx = ''.join(f'<key>{k}</key><string>{v}</string>' for k, v in env.items())
    plist.write_text('<?xml version="1.0" encoding="UTF-8"?>\n'
        '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n'
        f'<plist version="1.0"><dict><key>Label</key><string>{LABELS[name]}</string>'
        f'<key>ProgramArguments</key><array>{prog}</array>{extra}'
        f'<key>EnvironmentVariables</key><dict>{envx}</dict>'
        f'<key>StandardErrorPath</key><string>{D}/{name}.err</string><key>StandardOutPath</key><string>{D}/{name}.out</string>'
        '</dict></plist>\n')
    uid = os.getuid()
    subprocess.run(['launchctl', 'bootout', f'gui/{uid}/{LABELS[name]}'], capture_output=True)
    for _ in range(5):  # bootstrap right after bootout can fail while the old job is still tearing down
        time.sleep(0.5)
        subprocess.run(['launchctl', 'bootstrap', f'gui/{uid}', str(plist)], capture_output=True)
        if subprocess.run(['launchctl', 'print', f'gui/{uid}/{LABELS[name]}'], capture_output=True).returncode == 0:
            break
    else:
        print(f'warning: could not start {LABELS[name]}; try: launchctl bootstrap gui/{uid} {plist}')

def proxy_up():
    try:
        urllib.request.urlopen(f'http://127.0.0.1:{PORT}/v1/models', timeout=5)
        return True
    except urllib.error.HTTPError:
        return True  # reached Anthropic through the proxy (401 without a key is fine)
    except Exception:
        return False

def edit_settings(add):
    SETTINGS.parent.mkdir(parents=True, exist_ok=True)
    d = json.loads(SETTINGS.read_text()) if SETTINGS.exists() else {}
    env = d.setdefault('env', {})
    if add:
        if env.get('ANTHROPIC_BASE_URL') != ENV['ANTHROPIC_BASE_URL']:  # keep the original on reinstall
            (D / 'settings.backup.json').write_text(json.dumps(d, indent=2))
            (D / 'previous_env.json').write_text(json.dumps({k: env.get(k) for k in ENV}))
        env.update(ENV)
    else:
        try:
            prev = json.loads((D / 'previous_env.json').read_text())
        except Exception:
            prev = {}
        for k in ENV:
            if prev.get(k) is None:
                env.pop(k, None)
            else:
                env[k] = prev[k]
        if not env:
            d.pop('env', None)
    SETTINGS.write_text(json.dumps(d, indent=2) + '\n')

def seed_from_transcripts(days=14):
    """Learn from Claude Code's own transcripts (token counts and timestamps only) so the policy fits you on day one."""
    import datetime, glob, hashlib
    out = D / 'usage.jsonl'
    if out.exists() and out.stat().st_size > 100_000:
        return 0
    since = time.time() - days * 86400
    rows = []
    for f in glob.glob(str(pathlib.Path.home() / '.claude' / 'projects' / '*' / '*.jsonl')):
        try:
            if os.path.getmtime(f) < since:
                continue
            for line in open(f, errors='ignore'):
                if '"usage"' not in line or '"isSidechain":true' in line.replace(' ', ''):
                    continue
                try:
                    d = json.loads(line)
                except Exception:
                    continue
                m = d.get('message') or {}
                u = m.get('usage') or {}
                if 'cache_read_input_tokens' not in u or d.get('isSidechain'):
                    continue
                t = datetime.datetime.fromisoformat(d['timestamp'].replace('Z', '+00:00')).timestamp()
                if t < since:
                    continue
                cr, cc, inp = (int(u.get(k) or 0) for k in ('cache_read_input_tokens', 'cache_creation_input_tokens', 'input_tokens'))
                rows.append({'provider': 'anthropic-claude-code-history', 'timestamp': int(t * 1000), 'status': 200, 'model': m.get('model'),
                             'conversationId': hashlib.sha256(str(d.get('sessionId')).encode()).hexdigest()[:32],
                             'usage': {'inputTokens': inp + cr + cc, 'cacheReadInputTokens': cr, 'cacheCreationInputTokens': cc, 'outputTokens': int(u.get('output_tokens') or 0)}})
        except Exception:
            continue
    rows.sort(key=lambda r: r['timestamp'])
    existing = out.read_text() if out.exists() else ''
    out.write_text(''.join(json.dumps(r) + '\n' for r in rows) + existing)
    return len(rows)

def install(a):
    D.mkdir(parents=True, exist_ok=True)
    os.chmod(D, 0o700)
    shutil.copy(HERE / 'keepalive_proxy.py', D / 'keepalive_proxy.py')
    for f in ('tune.py', 'monitor.py', 'dashboard.py'):
        shutil.copy(HERE.parent / 'tools' / f, D / f)
    shutil.copy(HERE / 'install.py', D / 'install.py')
    if not (D / 'state.json').exists():
        (D / 'state.json').write_text(json.dumps({'switched_at': time.time()}))
    py = sys.executable
    env = {'KEEPALIVE_DIR': str(D)}
    n = seed_from_transcripts()
    if n:
        print(f'learned from {n:,} requests in your Claude Code transcripts (last 14 days):')
        subprocess.run([py, str(D / 'tune.py'), '--days', '14'], env={**os.environ, **env})
    if platform.system() == 'Darwin' and not a.no_launchd:
        launchd('proxy', [py, str(D / 'keepalive_proxy.py')], '<key>RunAtLoad</key><true/><key>KeepAlive</key><true/>', env)
        launchd('dashboard', [py, str(D / 'dashboard.py')], '<key>RunAtLoad</key><true/><key>KeepAlive</key><true/>', {**env, 'KEEPALIVE_DASHBOARD_PORT': str(DASH_PORT)})
        launchd('monitor', [py, str(D / 'monitor.py')], '<key>StartCalendarInterval</key><dict><key>Hour</key><integer>6</integer><key>Minute</key><integer>20</integer></dict>', env)
        for _ in range(20):
            if proxy_up():
                break
            time.sleep(0.5)
        if not proxy_up():
            sys.exit(f'proxy did not start; see {D}/proxy.err. Claude Code settings were not changed.')
    else:
        print(f'start the proxy and keep it running: KEEPALIVE_DIR={D} {py} {D}/keepalive_proxy.py')
        print(f'daily monitor, e.g. crontab: 20 6 * * * KEEPALIVE_DIR={D} {py} {D}/monitor.py')
        if not proxy_up():
            print('then run this installer again to point Claude Code at it (settings unchanged for now)')
            return
    edit_settings(True)
    print(f'Claude Code now goes through http://127.0.0.1:{PORT} with a 1-hour cache')
    print(f'dashboard: http://127.0.0.1:{DASH_PORT}/   logs: {D}')
    if not (D / 'policy.json').exists():
        print('policy: keep alive up to 4 h for chats over 40k tokens until there is enough usage to tune (the monitor tunes daily)')
    print('restart open Claude Code sessions to pick it up')

def uninstall(a):
    edit_settings(False)
    print('Claude Code points at Anthropic again (restart open sessions)')
    if platform.system() == 'Darwin':
        for label in LABELS.values():
            subprocess.run(['launchctl', 'bootout', f'gui/{os.getuid()}/{label}'], capture_output=True)
            plist = pathlib.Path.home() / 'Library' / 'LaunchAgents' / f'{label}.plist'
            if plist.exists():
                plist.rename(plist.with_suffix('.plist.removed'))
    print('logs stay in', D)

def main():
    ap = argparse.ArgumentParser(description='Prompt-cache keep-alive for Claude Code')
    ap.add_argument('--uninstall', action='store_true')
    ap.add_argument('--enable', action='store_true', help='clear the "disabled" flag set by the monitor')
    ap.add_argument('--no-launchd', action='store_true', help='do not install macOS background jobs (run the proxy yourself)')
    a = ap.parse_args()
    if a.enable:
        f = D / 'disabled'
        if f.exists():
            f.rename(D / f'disabled.cleared-{int(time.time())}')
        print('keep-alives enabled')
    elif a.uninstall:
        uninstall(a)
    else:
        install(a)

if __name__ == '__main__':
    main()
