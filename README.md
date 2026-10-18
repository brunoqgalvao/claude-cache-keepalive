# claude-cache-keepalive

Free tokens, sometimes. Anthropic's prompt cache expires after 5 minutes (or 1 hour) without use. Step away from a long Claude chat and the next turn rewrites the whole context, often hundreds of thousands of tokens.

This keeps idle chats cached. Shortly before a chat's 1-hour cache expires, it re-sends the chat's last request and cuts it off at the first event. That reads the cache at the read price (0.05x input on Opus 5.5) instead of rewriting it at 2x, and restarts the hour. It learns how long to keep doing this from your own usage, watches whether it pays, and switches itself off if it stops paying.

Two setups:

- **opencodex** (Codex with Claude models): a small patch to opencodex. Also switches opencodex from its 5-minute default to the 1-hour cache, which on its own saves tokens.
- **Claude Code**: a local pass-through proxy. Claude Code's main chat already uses the 1-hour cache on a subscription; this keeps it warm past the hour.

## What it saved me

One user, about 80k Anthropic requests through opencodex, Opus 5.5, Claude subscription. Costs in Anthropic's price ratios (5-min write 1.25x, 1-hour write 2x, read 0.05x).

| | input cost vs 1-hour cache alone |
|---|---|
| opencodex default (5-minute cache) | about +5% to +10% |
| 1-hour cache only (Claude Code's default) | baseline |
| 1-hour cache + keep-alive, first 30 hours live | **-10.6%** |
| 1-hour cache + keep-alive, backtest on 2 weeks | -19% to -23% |

Live, all 369 keep-alives that got a response read the full cached context and wrote nothing, and chats stayed warm after 4 hours idle. After paying for every keep-alive, net savings were 11.4% of input over the period. On a subscription this is quota, not dollars; Anthropic doesn't publish how cache tokens count against limits.

Details, methods and the things that did not work: [analysis/RESULTS.md](analysis/RESULTS.md).

## Setup: opencodex

Requires opencodex installed with npm and Claude accounts logged in through OAuth.

```sh
git clone https://github.com/brunoqgalvao/claude-cache-keepalive
cd claude-cache-keepalive
python3 opencodex/install.py
ocx restart
```

If chats are running, use `python3 opencodex/install.py --restart-when-quiet` instead of `ocx restart`: a one-shot background job waits until no chat has been active for 10 minutes, asks opencodex for its graceful restart, then removes itself.

The installer:

1. sets `cacheRetention` to `"long"` (1-hour cache) in `~/.opencodex/config.json`
2. copies `cache-keepalive.ts` into opencodex and adds four small hooks to `src/adapters/anthropic.ts` (original saved as `anthropic.ts.orig`)
3. tunes a policy on your last 14 days of `~/.opencodex/usage.jsonl`
4. on macOS, starts a dashboard at http://127.0.0.1:8797 and a daily monitor

Switching to the 1-hour cache makes each open chat rewrite its cache once (5-minute and 1-hour caches are separate). An opencodex update replaces the patched file: run `python3 opencodex/install.py` again afterwards. The patch runs on opencodex 2.73 and applies cleanly to 2.87.

## Setup: Claude Code

```sh
python3 claude-code/install.py
```

Then restart open Claude Code sessions. The installer starts the proxy on 127.0.0.1:8796 (macOS launchd keeps it running) and adds two entries to the `env` block of `~/.claude/settings.json`:

- `ANTHROPIC_BASE_URL=http://127.0.0.1:8796`: Claude Code's requests go through the proxy, which relays them to api.anthropic.com unchanged and streams responses back.
- `CLAUDE_CODE_PROMPT_CACHE_TTL=1h`: 1-hour cache for the main chat. Claude Code does this by default on a subscription, but in my tests it wrote 5-minute caches behind a custom base URL. Keep-alives only apply to 1-hour caches. Subagents keep their 5-minute default.

The installer seeds the usage log from your existing Claude Code transcripts (`~/.claude/projects`, last 14 days), so the policy is trained on your history from the first minute. If that history says keep-alives would not pay (short sessions, few long pauses), they start switched off: the proxy still relays everything with the 1-hour cache, and keep-alives switch on by themselves once a daily retune says they pay. With fewer than 200 requests to learn from, it keeps chats over 40k tokens alive for up to 4 hours.

The dashboard is at http://127.0.0.1:8798. If the proxy is not running, Claude Code cannot reach Anthropic, so uninstall it before removing the files.

## How it learns and when it turns off

`tools/tune.py` replays your recent conversations and fits two policies, then keeps whichever saves more on your data:

- **Probability rule.** For each hour a chat has been idle (1 to 24) and each chat size (20k, 80k, 200k, 500k+ tokens), it counts how often you came back within the next hour. Sparse cells are shrunk toward the overall rate. A keep-alive is sent when that chance beats break-even: keep-alive cost ÷ rewrite avoided, which is about 2.8% for a big Opus chat and 6% for a 20–80k one.
- **Fixed limit.** "Keep alive up to N hours, only chats over X tokens", the best N and X on your data.

On my opencodex usage the probability rule wins (12.3% of input saved vs 12.0% for the best fixed limit): return chances are 6–37% in the first three idle hours and mostly 1–5% after that, with spikes like the next morning that a fixed limit can't follow. It reruns every day.

`tools/monitor.py` runs daily: it retunes, then measures what keep-alives actually did over the last 72 hours (rewrites avoided on returns after more than 1 hour idle, minus the cost of every keep-alive). If they lost tokens it writes a `disabled` file and keep-alives stop until you run `install.py --enable`. If only the tuner says they would not pay, they stop and come back on by themselves when a later retune says they pay.

I also tried a logistic model with signals from Codex's own session logs. It gained under one point over the simpler rules: a keep-alive costs about 2.5% of a rewrite on a big chat, so it pays whenever there's more than a few percent chance you come back, and the hour-by-size table already captures most of that.

## Dashboard

- **What the keep-alives did:** rewrites avoided − keep-alive cost = net saved, in tokens and US$ at API prices, plus the last 24 hours and the returns that still found a cold cache.
- **Cost under each cache rule:** the same requests replayed under the 5-minute cache and the 1-hour cache without keep-alives, next to what actually happened, per day.
- **The policy:** the rule in use, why the tuner picked it, and the break-even math.
- **Your return probabilities:** the full idle-hours × chat-size table with sample counts; green cells are where it keeps chats alive.
- **Every decision:** each hourly check with the chance you return, the chance needed, and what it did.

## What it sends and stores

Keep-alives re-send your own last request with your own credentials, from your machine to Anthropic, with `max_tokens` 16 and the connection dropped at the first event. Logs hold token counts, timings and a hash of each conversation's opening, never content. Each keep-alive uses quota (about 0.05x of the chat's context on Opus). A daily cap limits them.

## Uninstall

```sh
python3 opencodex/install.py --uninstall     # add --keep-1h to keep the 1-hour cache setting
ocx restart
python3 claude-code/install.py --uninstall
```

## Caveats

- One user's traffic. Your pauses decide whether this pays; that's why it tunes on your log and monitors itself.
- A chat is identified by its model, system prompt and first two messages. If the start of a conversation changes (for example an edited instructions file), the old cache stops matching; the keep-alive notices (the re-read comes back small) and stops for that chat.
- The opencodex patch edits installed source. It is small and marked with `// claude-cache-keepalive`, but read it before running it.

MIT license.
