#!/usr/bin/env python3
"""Install the prompt-cache keep-alive into opencodex.

  python3 install.py              install (or update) and tune on your usage
  python3 install.py --restart-when-quiet
                                  same, then restart opencodex by itself once no chat is running
  python3 install.py --enable     clear a "disabled" flag set by the monitor
  python3 install.py --uninstall  restore opencodex's original adapter and remove the extras

What install does:
  1. sets cacheRetention to "long" (1-hour cache) in ~/.opencodex/config.json (backup kept)
  2. copies cache-keepalive.ts into opencodex/src/lib and adds four small hooks to
     src/adapters/anthropic.ts (original saved as anthropic.ts.orig)
  3. copies tools/tune.py, monitor.py and dashboard.py to ~/.opencodex/cache-keepalive and tunes the
     policy on your own usage log
  4. on macOS, adds launchd jobs: the dashboard (http://127.0.0.1:8797) and a daily monitor
Then run: ocx restart
"""
import argparse, json, os, pathlib, platform, re, shutil, subprocess, sys, time

HERE = pathlib.Path(__file__).resolve().parent
HOME = pathlib.Path(os.environ.get('OPENCODEX_HOME', pathlib.Path.home() / '.opencodex')).expanduser()
D = HOME / 'cache-keepalive'
LABELS = {'menubar': 'com.claude-cache-keepalive.menubar', 'dashboard': 'com.claude-cache-keepalive.dashboard', 'monitor': 'com.claude-cache-keepalive.monitor',
          'quiet-restart': 'com.claude-cache-keepalive.quiet-restart'}
IMPORT = 'import { keepaliveCapture, keepaliveDone, type KeepaliveCapture } from "../lib/cache-keepalive";'
TAG = ' // claude-cache-keepalive'

def die(msg):
    sys.exit(f'error: {msg}')

def find_src(explicit):
    if explicit:
        p = pathlib.Path(explicit).expanduser()
    else:
        root = subprocess.run(['npm', 'root', '-g'], capture_output=True, text=True).stdout.strip()
        p = pathlib.Path(root) / '@bitkyc08' / 'opencodex' / 'src'
    if not (p / 'adapters' / 'anthropic.ts').exists():
        die(f'opencodex source not found at {p}; pass --opencodex-src /path/to/opencodex/src')
    return p

def patch_adapter(src):
    """Add the hooks, anchored on stable code instead of line numbers so it survives most updates."""
    p = src / 'adapters' / 'anthropic.ts'
    s = p.read_text()
    if 'keepaliveCapture' in s:
        # earlier versions keyed chats without the Codex thread id; add it to an existing hook
        if 'keepaliveCapture({ retention:' in s and '_clientThreadId' not in s.split('keepaliveCapture({', 1)[1][:200]:
            p.write_text(s.replace('keepaliveCapture({ retention:', 'keepaliveCapture({ thread: parsed._clientThreadId, retention:', 1))
            return 'already patched; added the thread id to the keep-alive key'
        return 'already patched'
    orig = s
    for need in ('usesNativeAnthropicEndpoint(', 'isOAuth', 'pendingStopReason', 'normalizeTtlOrdering(body);'):
        if need not in orig:
            die(f'opencodex changed ({need} not found); please open an issue at github.com/brunoqgalvao/claude-cache-keepalive')
    imports = list(re.finditer(r'^import [^;]+;[ \t]*$', s, re.M))
    if not imports:
        die('no imports found in anthropic.ts')
    k = imports[-1].end()
    s = s[:k] + '\n' + IMPORT + s[k:]
    m = re.search(r'^([ \t]*)const toolNames = buildToolNameTransforms\(provider\);\n', s, re.M)
    if not m:
        die('anchor "const toolNames = buildToolNameTransforms(provider);" not found')
    s = s[:m.end()] + m.group(1) + 'let keepalive: KeepaliveCapture | undefined;' + TAG + '\n' + s[m.end():]
    m = re.search(r'^([ \t]*)normalizeTtlOrdering\(body\);\n', s, re.M)
    ind = m.group(1)
    hook = (ind + 'const keepaliveBody = JSON.stringify(body);' + TAG + '\n'
            + ind + 'keepalive = isOAuth && usesNativeAnthropicEndpoint(provider)\n'
            + ind + '  ? keepaliveCapture({ thread: parsed._clientThreadId, retention: cacheRetention, model: parsed.modelId, url, headers, body, serialized: keepaliveBody, token: provider.apiKey })\n'
            + ind + '  : undefined;\n')
    s = s[:m.end()] + hook + s[m.end():]
    n = s.find('body: JSON.stringify(body),', m.end())
    if n < 0 or n - m.end() > 1500:
        die('could not find the request body right after normalizeTtlOrdering(body)')
    s = s[:n] + 'body: keepaliveBody,' + s[n + len('body: JSON.stringify(body),'):]
    pat = re.compile(r'^([ \t]*)((?:yield|const \w+: AdapterEvent =) \{\n[ \t]*type: "done",\n[ \t]*usage: usageFromAnthropic\(pendingUsage\),)', re.M)
    s, count = pat.subn(lambda mm: mm.group(1) + 'keepaliveDone(keepalive, pendingUsage, pendingStopReason);' + TAG + '\n' + mm.group(1) + mm.group(2), s)
    if count == 0:
        die('could not find the streamed completion event (type: "done" with pendingUsage)')
    shutil.copy(p, p.with_name('anthropic.ts.orig'))
    p.write_text(s)
    return f'patched ({count} completion hook{"s" if count > 1 else ""})'

def set_retention(value='long'):
    cfg = HOME / 'config.json'
    if not cfg.exists():
        die(f'{cfg} not found; run opencodex once first')
    raw = cfg.read_text()
    d = json.loads(raw)
    if d.get('cacheRetention') == value:
        return f'cacheRetention already "{value}"'
    D.mkdir(parents=True, exist_ok=True)
    (D / 'config.backup.json').write_text(raw)
    os.chmod(D / 'config.backup.json', 0o600)
    prev = d.get('cacheRetention')
    d['cacheRetention'] = value
    tmp = cfg.with_suffix('.tmp-keepalive')
    tmp.write_text(json.dumps(d, indent=2, ensure_ascii=False) + '\n')
    os.chmod(tmp, os.stat(cfg).st_mode & 0o777)
    os.replace(tmp, cfg)
    (D / 'previous_retention.json').write_text(json.dumps({'cacheRetention': prev}))
    return f'cacheRetention: {prev or "unset (5 min)"} -> "{value}"'

def launchd(name, args, extra):
    plist = pathlib.Path.home() / 'Library' / 'LaunchAgents' / f'{LABELS[name]}.plist'
    plist.parent.mkdir(parents=True, exist_ok=True)
    prog = ''.join(f'<string>{a}</string>' for a in args)
    plist.write_text('<?xml version="1.0" encoding="UTF-8"?>\n'
        '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n'
        f'<plist version="1.0"><dict><key>Label</key><string>{LABELS[name]}</string>'
        f'<key>ProgramArguments</key><array>{prog}</array>{extra}'
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

def menubar(start):
    """Menu bar item: shows that keep-alives are on and what they saved today, and opens the dashboard.
    Built from tools/menubar.swift; needs swiftc (Xcode command line tools)."""
    swiftc = shutil.which('swiftc')
    if not swiftc:
        return 'menu bar item: skipped (no swiftc; xcode-select --install, then rerun)'
    shutil.copy(HERE.parent / 'tools' / 'menubar.swift', D / 'menubar.swift')
    r = subprocess.run([swiftc, '-O', str(D / 'menubar.swift'), '-o', str(D / 'CacheKeepaliveBar')], capture_output=True, text=True)
    if r.returncode:
        return 'menu bar item: build failed, skipped (' + (r.stderr.strip().splitlines() or ['?'])[-1][:120] + ')'
    start([str(D / 'CacheKeepaliveBar')], '<key>RunAtLoad</key><true/><key>KeepAlive</key><dict><key>SuccessfulExit</key><false/></dict>')
    return 'menu bar item: on (bolt with today\'s net savings; click it for details and the dashboard)'

def install(a):
    src = find_src(a.opencodex_src)
    print(set_retention('long'))
    D.mkdir(parents=True, exist_ok=True)
    os.chmod(D, 0o700)
    shutil.copy(HERE / 'cache-keepalive.ts', src / 'lib' / 'cache-keepalive.ts')
    print('adapter:', patch_adapter(src))
    for f in ('tune.py', 'monitor.py', 'dashboard.py', 'quiet_restart.py'):
        shutil.copy(HERE.parent / 'tools' / f, D / f)
    for f in ('install.py', 'cache-keepalive.ts'):
        shutil.copy(HERE / f, D / f)
    state = D / 'state.json'
    if not state.exists():
        state.write_text(json.dumps({'switched_at': time.time()}))
    print('tuning on your usage...')
    subprocess.run([sys.executable, str(D / 'tune.py'), '--days', '14'])
    py = sys.executable
    if platform.system() == 'Darwin' and not a.no_launchd:
        launchd('dashboard', [py, str(D / 'dashboard.py')], '<key>RunAtLoad</key><true/><key>KeepAlive</key><true/>')
        launchd('monitor', [py, str(D / 'monitor.py')],
                '<key>StartCalendarInterval</key><dict><key>Hour</key><integer>6</integer><key>Minute</key><integer>15</integer></dict>')
        print('dashboard: http://127.0.0.1:8797/  (monitor runs daily at 06:15)')
        if not a.no_menubar:
            print(menubar(lambda args, extra: launchd('menubar', args, extra)))
    else:
        print(f'add a daily job for the monitor, e.g. crontab: 15 6 * * * {py} {D}/monitor.py')
        print(f'dashboard: {py} {D}/dashboard.py, then open http://127.0.0.1:8797/')
    if a.restart_when_quiet and platform.system() == 'Darwin' and not a.no_launchd:
        launchd('quiet-restart', [py, str(D / 'quiet_restart.py')], '<key>RunAtLoad</key><true/>')
        print('\nopencodex will restart by itself once no chat has been active for 10 minutes')
        print(f'(one-shot job; progress in {D}/quiet_restart.json)')
    elif a.restart_when_quiet:
        print(f'\nto restart once nothing is running:  nohup {py} {D}/quiet_restart.py &')
    else:
        print('\nnow restart opencodex so it loads the patch:  ocx restart')
        print('  (or rerun with --restart-when-quiet to let it restart once no chat is running)')
    print('after an opencodex update, run this installer again (updates replace the patched file)')

def uninstall(a):
    src = find_src(a.opencodex_src)
    p = src / 'adapters' / 'anthropic.ts'
    orig = p.with_name('anthropic.ts.orig')
    if orig.exists() and 'keepaliveCapture' in p.read_text():
        shutil.copy(orig, p)
        print('restored original anthropic.ts')
    lib = src / 'lib' / 'cache-keepalive.ts'
    if lib.exists():
        lib.rename(lib.with_suffix('.ts.removed'))
    if platform.system() == 'Darwin':
        for label in LABELS.values():
            subprocess.run(['launchctl', 'bootout', f'gui/{os.getuid()}/{label}'], capture_output=True)
            plist = pathlib.Path.home() / 'Library' / 'LaunchAgents' / f'{label}.plist'
            if plist.exists():
                plist.rename(plist.with_suffix('.plist.removed'))
    if a.keep_1h:
        print('kept cacheRetention "long"')
    else:
        try:
            prev = json.loads((D / 'previous_retention.json').read_text()).get('cacheRetention')
            cfg = HOME / 'config.json'
            d = json.loads(cfg.read_text())
            if prev is None:
                d.pop('cacheRetention', None)
            else:
                d['cacheRetention'] = prev
            cfg.write_text(json.dumps(d, indent=2, ensure_ascii=False) + '\n')
            print(f'cacheRetention restored to {prev or "default (5 min)"}')
        except Exception:
            print('left cacheRetention as is')
    print('logs and settings stay in', D)
    print('now run: ocx restart')

def main():
    ap = argparse.ArgumentParser(description='Prompt-cache keep-alive for opencodex')
    ap.add_argument('--uninstall', action='store_true')
    ap.add_argument('--enable', action='store_true', help='clear the "disabled" flag set by the monitor')
    ap.add_argument('--keep-1h', action='store_true', help='with --uninstall: keep the 1-hour cache setting')
    ap.add_argument('--no-launchd', action='store_true', help='skip the macOS background jobs')
    ap.add_argument('--no-menubar', action='store_true', help='skip the menu bar item')
    ap.add_argument('--restart-when-quiet', action='store_true',
                    help='restart opencodex automatically once no chat has been active for 10 minutes')
    ap.add_argument('--opencodex-src', help='path to opencodex/src if it is not under "npm root -g"')
    a = ap.parse_args()
    if a.enable:
        f = D / 'disabled'
        if f.exists():
            f.rename(D / f'disabled.cleared-{int(time.time())}')
        print('keep-alives enabled (no restart needed)')
    elif a.uninstall:
        uninstall(a)
    else:
        install(a)

if __name__ == '__main__':
    main()
