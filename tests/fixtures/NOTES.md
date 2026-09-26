# Fixture notes — candle timestamp semantics

Recorded via `python scripts/record_fixtures.py` against live public endpoints on
2026-08-30 (~04:26 UTC / 13:26 KST). All 22 targets returned real, successful
responses on the first try — no *HTTP* failures needed retrying. 🚨 Two of
those 22 targets (`okx/candles_swap_1d`, `bitget/candles_linear_1d`) did need
a *parameter* fix after the fact: both were originally recorded with the bare
daily-granularity string, which is Hong-Kong-time-aligned rather than UTC —
see the per-venue okx/bitget entries and the trap note below the summary for
the full story; both were corrected and re-recorded on 2026-08-30 (Task 13).
One line per exchange below: timestamp field, unit, OPEN vs CLOSE, and
timezone/day-boundary.

- **upbit** (`/v1/candles/days`, `/v1/candles/minutes/1`) — `candle_date_time_utc`
  (+ mirrored `candle_date_time_kst`) is the candle **OPEN** time, ISO-8601 string
  with no offset suffix; the separate `timestamp` field (epoch **ms**) is the last
  trade tick inside the candle, not a boundary. 🚨 Daily candles reset at **00:00
  UTC = 09:00 KST**, i.e. UTC midnight — NOT KST midnight.
  Array order: newest-first (descending).

- **bithumb** (`/v1/candles/days`) — same field shape as Upbit
  (`candle_date_time_utc`/`_kst` = **OPEN**, ISO string, no offset; `timestamp` =
  last tick, epoch ms). 🚨 Unlike Upbit, daily candles reset at **00:00 KST
  (= 15:00 UTC the previous day)** — despite identical field names, Upbit and
  Bithumb use different daily boundaries. Array order: newest-first (descending).

- **korbit** (`/v2/candles`) — only one time field, `timestamp` (epoch **ms**,
  UTC). Confirmed **OPEN** (and confirmed KST-midnight day boundary, matching
  Bithumb) by cross-checking identical epoch values against Bithumb's
  `candle_date_time_utc` for the same three days (1787842800000 /
  1787929200000 / 1788015600000 → 2026-08-27/28/29T15:00:00 UTC each = 00:00
  KST next day). Array order: oldest-first (ascending).

- **binance** (`/fapi/v1/klines`, linear/USDT-M futures) — kline array index 0 is
  **open** time (epoch **ms**, UTC); index 6 is close time (= next open − 1ms,
  verified in the fixture). Array order: oldest-first (ascending).

- **okx** (`/api/v5/market/candles`, SWAP, `bar=1Dutc`) — array index 0
  (`ts`) is the **opening**/start time of the candle, epoch **ms**, UTC
  midnight (`ts % 86_400_000 == 0`). Array order: newest-first (descending).
  🚨 **Re-recorded 2026-08-30 (Task 13 correction)**: the fixture originally
  shipped in this task was recorded with the bare `bar=1D`, which OKX aligns
  to **Hong Kong time (UTC+8)**, not UTC — every timestamp landed on
  `ts % 86_400_000 == 57_600_000` instead of `0`. Live-diffing `bar=1D` vs
  `bar=1Dutc` on the same instrument showed timestamps exactly `28_800_000`ms
  (8h) apart. `scripts/record_fixtures.py` and the fixture were corrected to
  use `bar=1Dutc`, matching what `src/pycex/exchanges/okx.py::_TIMEFRAME_MAP`
  actually sends (fixed in the same Task-13 pass after a live smoke failure).

- **bitget** (`/api/v2/mix/market/candles`, USDT-FUTURES,
  `granularity=1Dutc`) — array index 0 (`ts`) is the candle **start/open**
  time, epoch **ms**, UTC midnight (`ts % 86_400_000 == 0`). Array order:
  oldest-first (ascending). 🚨 **Re-recorded 2026-08-30 (Task 13
  correction)**: same Hong Kong-time trap as OKX — the fixture originally
  shipped here was recorded with the bare `granularity=1D`, landing every
  timestamp on `ts % 86_400_000 == 57_600_000` (UTC+8) instead of `0`.
  Corrected to `granularity=1Dutc`, matching what
  `src/pycex/exchanges/bitget.py::_MIX_TIMEFRAME_MAP` already sends for
  `"1d"` (the mix adapter code was already correct — only the fixture
  recording script and the recorded fixture itself were wrong).

## Summary for later parsers
All six exchanges' raw candle timestamps are candle **OPEN**, not close — this
matches the project convention (`Candle.timestamp` = bar-open UTC epoch ms), so
no shift is needed when mapping raw → unified `Candle`. The one real trap is
**Upbit vs. Bithumb/Korbit daily-bar day boundary**: Upbit resets at 00:00 UTC
(09:00 KST) while Bithumb and Korbit reset at 00:00 KST (15:00 UTC previous
day). This does not change the timestamp *value* semantics (still OPEN, still
UTC ms) but means "the daily candle for a given KST calendar date" picks a
different underlying bar on Upbit than on Bithumb/Korbit — parser tests should
not assume the three Korean exchanges' daily bars are calendar-aligned.

🚨 **Second trap, found live in Task 13 (not caught by this original Task-0
recording pass)**: OKX and Bitget's *bare* daily/weekly granularity strings
(`bar=1D` on OKX, `granularity=1D`/`1day` on Bitget) do **not** align to UTC
midnight — they align to **Hong Kong time (UTC+8)**. Both exchanges expose a
separate `...utc`-suffixed granularity (`1Dutc`/`1Wutc`) that does align to
UTC, which this library's UTC-epoch-ms `Candle.timestamp` contract requires.
The original fixtures recorded here used the bare form and were silently
wrong (shape-correct, boundary-wrong) until the Task-13 live smoke caught the
8-hour offset; both `scripts/record_fixtures.py` and the fixtures themselves
were corrected to request the `utc`-suffixed granularity — see the per-venue
entries above. Any future exchange whose daily-bar API takes a bare vs.
`utc`-suffixed variant should be recorded with the `utc` form by default and
have its boundary explicitly checked (`ts % 86_400_000`), not assumed from
the array shape alone.

- **korbit `tick_size_policy_xrp_krw.json`** (`GET /v2/tickSizePolicy?symbol=xrp_krw`) — NOT a live
  recording: the response example of the official page
  docs.digitalx.miraeasset.com/llms/en/rest_api/quotation.md (read 2026-09-26; no exchange call was
  made). `priceGte` is inclusive; the item with the largest `priceGte` <= order price gives `tickSize`.
