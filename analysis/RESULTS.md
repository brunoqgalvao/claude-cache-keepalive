# Results and method

All numbers come from one user's traffic: Codex through opencodex 2.73 with Claude OAuth accounts, mostly Opus 5.5, Sep 23 to Oct 17 2026, plus Claude Code transcripts on the same accounts. Costs are in base-input-token equivalents using Anthropic's price ratios: uncached input 1x, 5-minute cache write 1.25x, 1-hour write 2x, cache read 0.05x on Opus/Sonnet 5.5 (0.025x Fable 5.1), output 5x.

## How the cache behaves

- **The 5-minute expiry is sharp.** Requests 290-300 s after the previous one read the cache 91-96% of the time; at 320-360 s, 0-9%.
- **Claude Code**: its main chat writes 1-hour caches (99.6% of mid-turn write tokens), subagents write 5-minute caches (100%). After 15-60 minute breaks its main chat still found the cache 79% of the time; after more than an hour, 3%.
- **5-minute and 1-hour caches are separate.** I wrote 48k tokens at 5 minutes, then sent a 1-hour request 15 s later: it read 0 and rewrote all 48k at 2x. A 5-minute control read 36.5k. Eight minutes later the 1-hour session read all 48k; the 5-minute control had expired. Script: [ttl_separation_test.py](ttl_separation_test.py) (needs the claude CLI; uses Haiku).
- **A re-read restarts the clock.** Live, keep-alives sent 3.9 hours after a chat went quiet (its fourth in a row) still read 100% of the cache.

## Backtest (train before Oct 7, test Oct 7-14, vs every write at 1 hour)

| Policy | Input cost vs 1-hour cache alone |
|---|---|
| Every write 5 minutes (opencodex default) | +10.1% |
| 5 minutes mid-turn, 1 hour on the turn-ending request | +128% |
| 1 hour + keep alive up to 4 h | -17.1% |
| 1 hour + learned keep-alive (logistic, proxy-visible signals) | -21.0% to -22.8% |
| 1 hour + keep alive whenever it would pay, perfect hindsight | -21.5% to -23.4% |
| Any mix of lifetimes and keep-alives, perfect hindsight | -32.7% |

Switching TTL mid-chat costs a full 2x rewrite, so "5 minutes mid-turn, 1 hour at the end" more than doubles input cost. The extra ~11 points in the last row need knowing in advance whether a long pause is coming; the best simple rule I tried for that saved 0.9%.

Adding signals from Codex's own session logs (what came right before the request, whether an automation started the chat, whether the response ended the turn) did not help: test AUC fell from 0.62 to 0.55 and savings moved by under a point. Display on/off and activity in other chats moved savings by 0.2 points.

## Live (opencodex, 1-hour cache + keep-alive, from Oct 16 15:55)

After 30 hours and 6,821 requests: input cost -10.6% vs replaying the same requests with a 1-hour cache alone, -14.5% vs the 5-minute default. 369 keep-alives sent; every one that got a response read the full context and wrote nothing. Net of their cost, savings were 11.4% of input. Switching from 5 minutes to 1 hour cost a one-time rewrite of each open chat (16.7M token-equivalents across 21 chats on my setup), not included above.

## Caveats

One user. Subscription quota accounting for cache tokens is not public, so the savings are in API-price terms. Returns that found the cache cold despite keep-alives were mostly chats whose opening had changed.
