# SuperBrain Phase 1 — Instrument Bridge

Implemented in this package:

- Public no-login `Ask SuperBrain` entry point.
- Public instrument dropdown comes only from distinct `instrument` values in `TV_TEST_CURRENT`.
- ODME linkage uses exact normalized `instrument` equality with `odme_snapshots`; no alias/mapping table is invented.
- If the exact instrument is active, has a saved expiry, and `Enable Scan` is on, `Ask SuperBrain` performs a fresh ODME scan automatically.
- If ODME is unavailable/disabled/fails, the scan remains TV-only.
- Existing authenticated add/remove instrument, expiry selection, scan enablement and ODME dashboard remain in place.
- Manual Scan All no longer sends email; results render in Streamlit.
- Email notifier removed from the project.
- Finished-expiry ODME snapshots auto-clean once per India date, including public app use.
- Authenticated history management supports instrument + period deletion for:
  - ODME snapshots
  - `TV_TEST_EVENTS` history
- `TV_TEST_CURRENT` is never deleted by history management.

This phase prepares the exact input packet. Persistent SuperBrain reasoning/trade memory is intentionally the next step.
