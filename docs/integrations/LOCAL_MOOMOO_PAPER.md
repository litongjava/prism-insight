# Local US paper runtime: moomoo REST and Lark

This runtime connects the existing PRISM US execution service to the moomoo REST
paper API. OpenD is not used when `MOOMOO_TRANSPORT=rest`. Live accounts are
rejected. Each buy is capped at USD 5,000; existing entry, exit, portfolio and
stop decisions are retained. ETFs remain subject to the existing US screener's
eligibility rules; this change does not introduce a new ETF strategy.

## Change review

1. Problem: the standalone paper manager did not serve the US tracker and
   notifications required Telegram. The authorized scope is automatic US paper
   execution and Lark group delivery, not live trading or strategy promotion.
2. Path: existing screener -> report and BUY/SELL decision -> ExecutionService
   stable intent -> REST adapter -> durable paper manager -> exact broker order
   reconciliation. Broker snapshots, not strategy holdings, establish fills.
3. Compatibility: reuse sizing, regular-session checks, portfolio limits and
   stop logic; market sells only for the existing exit path, no market buys.
   Missing intent, missing broker evidence, stale quotes and ambiguous submissions
   fail closed. No economic prompt or screening threshold changes.
4. Evidence: offline boundary tests cover idempotent submissions, unknown states,
   market exits, broker evidence normalization, notification failure and calendar
   scheduling. No real or simulated order is submitted solely as a test.
5. Deployment: private configuration is external; use the feature PR and exact
   head CI before activating cron. A successful process exit is not a confirmed
   trade. Registration, first execution and confirmed fill remain separate facts.
6. Rollback: remove only the two PRISM cron entries to stop new batches; preserve
   protection/reconciliation for any open holdings until explicitly wound down.
   Never delete the paper ledger to retry an uncertain order.

## Private configuration

The launcher reads `~/.config/prism-insight/{openai,lark,runtime}.env` (0600).
Never commit their values. Required runtime names include:

```
PRISM_US_BROKER=moomoo
MOOMOO_TRANSPORT=rest
MOOMOO_TRADING_MODE=demo
MOOMOO_ALLOW_LIVE=false
MOOMOO_ACCOUNT_ID=<paper account>
MOOMOO_BUY_AMOUNT_USD=5000
PRISM_NOTIFICATION_PROVIDER=lark
LARK_APP_ID=<app>
LARK_APP_SECRET=<private secret>
LARK_CHAT_ID=<prism-insight group>
STOCK_TRACKING_DB=<runtime>/stock_tracking_db.sqlite
HARDSTOP_ENABLED=true
HARDSTOP_LIVE=true
```

`HARDSTOP_LIVE` activates the existing worker's execution path; the broker is
still restricted to simulation by both launcher and REST adapter. It does not
enable a real-money account. Credentials are inherited by child processes, not
passed on command lines. All runtime commands must use the validated Python.

Lark notifications use the official `lark-oapi` Python SDK (Lark domain), with
SDK HTTP/authentication off the event loop. Only the configured group is allowed.
Text and file messages accept UUIDs; ambiguous sends are recorded UNKNOWN and
not retried automatically. The CLI is useful for setup but is not the runtime
message transport. App scopes: `im:message:send_as_bot`, `im:resource` (files),
`im:chat:readonly` (setup lookup). Add the published bot to the group.

## Scheduling

After validation, preserve existing crontab entries and add invocations of
`tools/local_paper_automation.py batch` each minute and `monitor` every five
minutes, via the validated Python. This entrypoint loads the private launcher
configuration itself. A NYSE calendar handles US holidays, DST and early closes.
Morning starts 15 minutes after the open; afternoon 90 minutes before the close.
A 15-minute dispatch window avoids late catch-up entry orders. SQLite claims
prevent replay even if a batch dies before recording its result.

The monitor reconciles paper orders/positions and sends changes through Lark.
During the session it runs the existing hardstop worker if the tracking DB exists;
after the close it runs the existing fill reconciliation. An UNKNOWN delivery or
STARTED run with a dead process requires review rather than blind replay.

Read-only/manual diagnostics:

```
<python> tools/local_paper_automation.py reconcile
<python> tools/local_paper_automation.py status
```

State and private logs live under `~/.local/state/prism-moomoo/automation`.
The broker ledger lives in `~/.local/state/prism-moomoo/paper-ledger.sqlite3`.
A broker submission acknowledgment is never described as a fill.

## Local research data

OpenAI web search supplies search results. `cores/llm/dsb_scrape_mcp_server.py`
exposes the Firecrawl scrape tool schema through a separate ephemeral dsb browser,
with public HTTPS checks, serialized browser access and actual page source text.
It does not reuse the authenticated Lark browser. MCP configuration is local and
ignored by Git. PDF rendering uses the project's existing Playwright renderer.


## Yahoo metadata rate-limit recovery (2026-11-17)

A read-only full-universe smoke retrieved 4,930 current price rows but all 366
liquid candidates failed Yahoo profile retrieval (`YFRateLimitError`). This is a
data transport failure, not evidence that no stocks qualify. The local runtime
can set `PRISM_US_ELIGIBILITY_SOURCE=nasdaq` and an explicit
`US_ELIGIBILITY_CACHE_PATH`; before each analysis batch, Nasdaq directory and
screener downloads refresh that existing cache atomically. No trigger, market-cap
floor, turnover floor, score or BUY/SELL prompt is changed.

The cache admits only directory-confirmed common/ADR shares with matching exchange,
USD quoted price, positive market cap and nonempty sector/industry. Unverified
security names, duplicate symbols and incomplete records are never synthesized.
The existing eligibility consumer still applies all its filters. Capture time is
recorded; an absent source publication time remains null. Failed refresh prevents
that batch from starting; exit monitoring remains a separate job.
