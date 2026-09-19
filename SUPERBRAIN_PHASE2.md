# SuperBrain Phase 2 — Persistent Memory Foundation

This phase adds persistence only. It does **not** create trading decisions yet.

## What changed

- `Ask SuperBrain` still uses instruments directly from `TV_TEST_CURRENT` and exact instrument matching to ODME.
- Every public scan now persists one current `STATE` row per instrument in a new compact `superbrain_memory` tab.
- The `STATE` row stores:
  - current scan id and prior scan id
  - exact current TradingView rows
  - latest ODME snapshot, when available
  - mode (`TV only` or `TV + ODME`)
  - an input fingerprint
  - the immediately previous market-state JSON for next-scan comparison
- SuperBrain-owned theoretical trades use `TRADE` rows in the same `superbrain_memory` tab.
- No Angel/broker positions are read for trade memory.
- Open trade rows can later be updated as HOLD / ADD / REDUCE / EXIT / TARGET / INVALIDATED / EXPIRED.
- Authenticated History Data Management can delete ODME history and/or SuperBrain memory/trades by instrument and date period.
- `TV_TEST_CURRENT` remains protected live truth and is never deleted from the app.

## TV_TEST_EVENTS cleanup

The production app no longer needs `TV_TEST_EVENTS`.

Deploy `Superbrain_TV_Webhook_Code_v14_NO_EVENT_SHEET.gs` first. v14 keeps the same low-latency `TV_TEST_CURRENT` writes and sends webhook errors only to the Apps Script execution log.

After v14 is deployed and its browser version shows `V14_NO_EVENT_SHEET`, run the Apps Script function:

`cleanupLegacyEventSheetV14()`

once. It deletes the old `TV_TEST_EVENTS` tab. Do not delete the tab before v14 is live because v13 still uses it for diagnostic errors.

## No trading logic yet

`action` and `thesis` in the STATE record intentionally remain blank in Phase 2. The next phase can build the actual market-view / decision engine using the current state, previous state and any open SuperBrain trades.
