const SPREADSHEET_ID = '14kiqCPXDI_K33HZV8b1-wYvoStqNx9JvXGHibNY77SA';
const EVENTS_SHEET = 'TV_TEST_EVENTS';
const CURRENT_SHEET = 'TV_TEST_CURRENT';

// v14 EDGE EXPANDED FACT MAP + LOW-LATENCY / NO EVENT SHEET
// IMPORTANT:
// - EDGE packets use a dedicated narrow write path instead of rewriting all 297 columns.
// - Normal event-history writes remain disabled on the hot path.
// - TV_TEST_EVENTS is no longer required. Normal history logging stays disabled.
// - Failures are written only to Apps Script execution logs via console.error().
// - TV_TEST_CURRENT remains the sole live TradingView truth used by SuperBrain.
const ENABLE_EVENT_LOG = false;
const ENABLE_ERROR_DIAGNOSTICS = true;
const CURRENT_KEY_COLS = 5; // A:E = updated_at, instrument, tv_ticker, source, tf
const NEW_ROW_LOCK_MS = 350;
const SHEET_WRITE_RETRY_MS = 120;

const EVENT_HEADERS = [
  'received_at','instrument','tv_ticker','source','tf','bar_time','close',
  'schema_version','event_type','payload_json','alert_id','status','note'
];

const CURRENT_HEADERS = ["updated_at", "instrument", "tv_ticker", "source", "tf", "bar_time", "close", "schema_version", "macro", "defender_tf", "defender_side", "defender_low", "defender_high", "defender_state", "challenger_tf", "challenger_side", "challenger_low", "challenger_high", "challenger_state", "exec_tf", "exec_of", "fp_side", "fp_low", "fp_high", "fp_strength", "fp_status", "fp_hold", "fp_interacting", "aurora_state", "aurora_prev", "aurora_change", "structure_trend", "fork_valid", "fork_slope", "fork_slope_flip", "fork_position", "fork_reclaimed_2sd", "liq_buy_level", "liq_buy_count", "liq_sell_level", "liq_sell_count", "liq_last_event", "liq_event_level", "liq_event_count", "liq_event_reclaimed", "raw_json", "struct_high", "struct_low", "last_clean_price", "last_clean_time", "new_clean_events", "mintick", "h1_price", "h1_time", "h1_type", "h2_price", "h2_time", "h2_type", "h3_price", "h3_time", "h3_type", "l1_price", "l1_time", "l1_type", "l2_price", "l2_time", "l2_type", "l3_price", "l3_time", "l3_type", "fork_candidate_valid", "fork_candidate_kind", "fork_candidate_slope", "fork_previous_candidate_slope", "fork_active_kind", "fork_a_price", "fork_a_time", "fork_b_price", "fork_b_time", "fork_c_price", "fork_c_time", "fork_median", "fork_upper_1sd", "fork_upper_2sd", "fork_lower_1sd", "fork_lower_2sd", "fork_reclaim_side", "eta_up_level", "eta_up_regime", "eta_up_locked_time", "eta_up_model_time", "eta_up_model_bars", "eta_up_model_distance", "eta_up_model_atr", "eta_up_speed_price_per_bar", "eta_up_speed_atr_per_bar", "eta_up_locked_remaining_bars", "eta_down_level", "eta_down_regime", "eta_down_locked_time", "eta_down_model_time", "eta_down_model_bars", "eta_down_model_distance", "eta_down_model_atr", "eta_down_speed_price_per_bar", "eta_down_speed_atr_per_bar", "eta_down_locked_remaining_bars", "eta_current_atr", "liq_buy_valid", "liq_buy_low", "liq_buy_high", "liq_buy_distance", "liq_sell_valid", "liq_sell_low", "liq_sell_high", "liq_sell_distance", "liq_pending_count", "liq_history_count", "liq_pending1_side", "liq_pending1_full_candidate", "liq_pending1_level", "liq_pending1_swept_count", "liq_pending1_remaining_count", "liq_pending1_cross_time", "liq_pending1_cross_close", "liq_pending2_side", "liq_pending2_full_candidate", "liq_pending2_level", "liq_pending2_swept_count", "liq_pending2_remaining_count", "liq_pending2_cross_time", "liq_pending2_cross_close", "liq_pending3_side", "liq_pending3_full_candidate", "liq_pending3_level", "liq_pending3_swept_count", "liq_pending3_remaining_count", "liq_pending3_cross_time", "liq_pending3_cross_close", "liq_event1_type", "liq_event1_side", "liq_event1_level", "liq_event1_swept_count", "liq_event1_remaining_count", "liq_event1_cross_time", "liq_event1_cross_close", "liq_event1_cross_high", "liq_event1_cross_low", "liq_event1_confirm_time", "liq_event1_confirm_close", "liq_event2_type", "liq_event2_side", "liq_event2_level", "liq_event2_swept_count", "liq_event2_remaining_count", "liq_event2_cross_time", "liq_event2_cross_close", "liq_event2_cross_high", "liq_event2_cross_low", "liq_event2_confirm_time", "liq_event2_confirm_close", "liq_event3_type", "liq_event3_side", "liq_event3_level", "liq_event3_swept_count", "liq_event3_remaining_count", "liq_event3_cross_time", "liq_event3_cross_close", "liq_event3_cross_high", "liq_event3_cross_low", "liq_event3_confirm_time", "liq_event3_confirm_close", "liq_event4_type", "liq_event4_side", "liq_event4_level", "liq_event4_swept_count", "liq_event4_remaining_count", "liq_event4_cross_time", "liq_event4_cross_close", "liq_event4_cross_high", "liq_event4_cross_low", "liq_event4_confirm_time", "liq_event4_confirm_close", "liq_event5_type", "liq_event5_side", "liq_event5_level", "liq_event5_swept_count", "liq_event5_remaining_count", "liq_event5_cross_time", "liq_event5_cross_close", "liq_event5_cross_high", "liq_event5_cross_low", "liq_event5_confirm_time", "liq_event5_confirm_close", "liq_buy_box_low", "liq_buy_box_high", "liq_sell_box_low", "liq_sell_box_high", "liq_buy2_valid", "liq_buy2_low", "liq_buy2_high", "liq_buy2_level", "liq_buy2_count", "liq_buy2_distance", "liq_buy3_valid", "liq_buy3_low", "liq_buy3_high", "liq_buy3_level", "liq_buy3_count", "liq_buy3_distance", "liq_sell2_valid", "liq_sell2_low", "liq_sell2_high", "liq_sell2_level", "liq_sell2_count", "liq_sell2_distance", "liq_sell3_valid", "liq_sell3_low", "liq_sell3_high", "liq_sell3_level", "liq_sell3_count", "liq_sell3_distance", "liq_event1_full_cross", "liq_event2_full_cross", "liq_event3_full_cross", "liq_event4_full_cross", "liq_event5_full_cross", "macro_tf", "macro_score", "battlefield_paired", "exec_of_score", "exec_move_quality", "fp_zone_id", "fp_formation_time", "fp_context_state", "fp_session_delta", "fp_waiting_confirmation", "fp_breach_pending", "fp_last_interaction_time", "fp_bars_since_interaction", "fp_event_type", "fp_event_score", "fp_event_time", "fp_bars_since_event", "fp_distance_atr", "fp_d_valid", "fp_d_zone_id", "fp_d_formation_time", "fp_d_low", "fp_d_high", "fp_d_strength_score", "fp_d_strength", "fp_d_hold", "fp_d_status", "fp_d_context_state", "fp_d_interacting", "fp_d_session_delta", "fp_d_waiting_confirmation", "fp_d_breach_pending", "fp_d_last_interaction_time", "fp_d_bars_since_interaction", "fp_d_event_type", "fp_d_event_score", "fp_d_event_time", "fp_d_bars_since_event", "fp_d_distance_atr", "fp_s_valid", "fp_s_zone_id", "fp_s_formation_time", "fp_s_low", "fp_s_high", "fp_s_strength_score", "fp_s_strength", "fp_s_hold", "fp_s_status", "fp_s_context_state", "fp_s_interacting", "fp_s_session_delta", "fp_s_waiting_confirmation", "fp_s_breach_pending", "fp_s_last_interaction_time", "fp_s_bars_since_interaction", "fp_s_event_type", "fp_s_event_score", "fp_s_event_time", "fp_s_bars_since_event", "fp_s_distance_atr", "fp_valid", "fp_strength_score", "edge_tracking_state", "edge_alert_tf", "edge_required_exec_tf", "edge_previous_exec_tf", "edge_needs_alert_change", "edge_handoff_time", "edge_tracking_message", "edge_tracking_reason"];

function doGet(e) {
  return jsonResponse_({ ok: true, service: 'SUPERBRAIN_TV_TEST', version: 'V14_NO_EVENT_SHEET', now: new Date().toISOString() });
}

function doPost(e) {
  const expectedKey = PropertiesService.getScriptProperties().getProperty('TV_WEBHOOK_KEY') || '';
  const suppliedKey = String((e && e.parameter && e.parameter.key) || '');
  if (!expectedKey || suppliedKey !== expectedKey) {
    // Do not log unauthorized payloads into the workbook.
    throw new Error('unauthorized');
  }

  const raw = (e && e.postData && e.postData.contents) ? e.postData.contents : '';
  if (!raw) {
    diagnosticErrorLog_(raw, null, 'EMPTY_BODY', new Error('empty_body'));
    return jsonResponse_({ ok: false, version: 'V14_NO_EVENT_SHEET', stage: 'EMPTY_BODY', error: 'empty_body' });
  }

  let p = null;
  try {
    p = JSON.parse(raw);
  } catch (err) {
    diagnosticErrorLog_(raw, null, 'JSON_PARSE_ERROR', err);
    return jsonResponse_({
      ok: false,
      version: 'V14_NO_EVENT_SHEET',
      stage: 'JSON_PARSE_ERROR',
      error: errorMessage_(err)
    });
  }

  const instrument = clean_(p.instrument || p.symbol || p.ticker || '');
  const source = clean_(p.source || '');
  const tf = clean_(p.tf || p.interval || '');
  if (!instrument || !source || !tf) {
    const err = new Error('instrument_source_tf_required');
    diagnosticErrorLog_(raw, p, 'REQUIRED_FIELDS_ERROR', err);
    return jsonResponse_({
      ok: false,
      version: 'V14_NO_EVENT_SHEET',
      stage: 'REQUIRED_FIELDS_ERROR',
      error: err.message,
      instrument: instrument,
      source: source,
      tf: tf
    });
  }

  const now = new Date();
  let ss = null;
  try {
    ss = SpreadsheetApp.openById(SPREADSHEET_ID);
    const current = ss.getSheetByName(CURRENT_SHEET);
    if (!current) throw new Error('current_sheet_missing');

    const safe = Object.assign({}, p);
    delete safe.key;
    // EDGE expansion can be large; its authoritative path writes a compact expansion JSON
    // directly into the existing raw_json cell. Avoid a redundant stringify of the whole packet.
    const rawJson = source === 'EDGE' && !ENABLE_EVENT_LOG ? '' : JSON.stringify(safe);

    // EDGE gets a dedicated narrow-write path. Other sources keep the proven v10 path.
    const rowNumber = source === 'EDGE'
      ? upsertEdgeCurrentFast_(current, now, safe)
      : upsertCurrent_(current, now, safe, rawJson);


    return jsonResponse_({
      ok: true,
      version: 'V14_NO_EVENT_SHEET',
      instrument: instrument,
      source: source,
      tf: tf,
      event_type: clean_(p.event_type || 'STATE'),
      current_row: rowNumber,
      received_at: now.toISOString()
    });
  } catch (err) {
    // Normal event logging stays OFF. Only failed packets get one diagnostic row.
    diagnosticErrorLog_(raw, p, 'PROCESSING_ERROR', err, ss);
    return jsonResponse_({
      ok: false,
      version: 'V14_NO_EVENT_SHEET',
      stage: 'PROCESSING_ERROR',
      instrument: instrument,
      source: source,
      tf: tf,
      error: errorMessage_(err)
    });
  }
}

function diagnosticErrorLog_(raw, parsed, stage, err, existingSs) {
  if (!ENABLE_ERROR_DIAGNOSTICS) return;
  try {
    const p = parsed || {};
    const guessed = parsed ? {} : recoverDiagnosticIdentityFromRaw_(raw);
    const diagnostic = {
      stage: clean_(stage || 'ERROR'),
      error: errorMessage_(err),
      instrument: clean_(p.instrument || p.symbol || p.ticker || guessed.instrument || ''),
      tv_ticker: clean_(p.tv_ticker || p.tickerid || p.exchange_ticker || guessed.tv_ticker || ''),
      source: clean_(p.source || guessed.source || 'UNKNOWN'),
      tf: clean_(p.tf || p.interval || guessed.tf || 'UNKNOWN'),
      bar_time: value_(p.bar_time !== undefined ? p.bar_time : guessed.bar_time),
      close: numberOrBlank_(p.close !== undefined ? p.close : guessed.close),
      schema_version: clean_(p.schema_version || guessed.schema_version || 'SB1'),
      event_type: clean_(p.event_type || guessed.event_type || stage || 'ERROR'),
      raw_preview: truncateCell_(String(raw || ''), 12000),
      stack: truncateCell_(err && err.stack ? String(err.stack) : '', 12000),
      logged_at: new Date().toISOString()
    };
    console.error('SUPERBRAIN_WEBHOOK_ERROR ' + JSON.stringify(diagnostic));
  } catch (diagnosticErr) {
    console.error('SUPERBRAIN_WEBHOOK_ERROR_LOGGER_FAILED ' + String(diagnosticErr));
  }
}

function recoverDiagnosticIdentityFromRaw_(raw) {
  const s = String(raw || '');
  return {
    instrument: regexJsonString_(s, 'instrument') || regexJsonString_(s, 'symbol') || regexJsonString_(s, 'ticker'),
    tv_ticker: regexJsonString_(s, 'tv_ticker') || regexJsonString_(s, 'tickerid'),
    source: regexJsonString_(s, 'source'),
    tf: regexJsonString_(s, 'tf') || regexJsonString_(s, 'interval'),
    schema_version: regexJsonString_(s, 'schema_version'),
    event_type: regexJsonString_(s, 'event_type'),
    bar_time: regexJsonNumber_(s, 'bar_time'),
    close: regexJsonNumber_(s, 'close')
  };
}

function regexJsonString_(raw, key) {
  const re = new RegExp('"' + key + '"\\s*:\\s*"([^"\\\\]*(?:\\\\.[^"\\\\]*)*)"');
  const m = raw.match(re);
  if (!m) return '';
  try { return JSON.parse('"' + m[1] + '"'); } catch (e) { return m[1]; }
}

function regexJsonNumber_(raw, key) {
  const re = new RegExp('"' + key + '"\\s*:\\s*(-?(?:\\d+\\.?\\d*|\\.\\d+)(?:[eE][+-]?\\d+)?)');
  const m = raw.match(re);
  return m ? Number(m[1]) : '';
}

function errorMessage_(err) {
  if (!err) return 'unknown_error';
  if (err.message) return String(err.message);
  return String(err);
}

function truncateCell_(s, maxLen) {
  const text = String(s || '');
  const limit = Math.max(100, Number(maxLen) || 45000);
  if (text.length <= limit) return text;
  return text.slice(0, limit) + '...[TRUNCATED ' + (text.length - limit) + ' chars]';
}


function upsertEdgeCurrentFast_(sheet, now, p) {
  const instrument = clean_(p.instrument || p.symbol || p.ticker || '');
  const source = 'EDGE';
  const tf = clean_(p.tf || p.interval || '');
  const eventType = clean_(p.event_type || 'STATE').toUpperCase();
  const isControl = eventType === 'TF_STATUS' ||
                    eventType === 'TF_MISMATCH' ||
                    eventType === 'EXEC_TF_CHANGED' ||
                    eventType === 'REQUIRED_TF_CHANGED';

  let targetRow = findCurrentKeyRow_(sheet, instrument, source, tf);
  if (!targetRow) {
    targetRow = allocateAndSeedCurrentKeyRow_(sheet, now, p, instrument, source, tf);
  }

  const base = {
    updated_at: now,
    instrument: instrument,
    tv_ticker: clean_(p.tv_ticker || p.tickerid || p.exchange_ticker || ''),
    source: source,
    tf: tf,
    bar_time: value_(p.bar_time),
    close: numberOrBlank_(p.close),
    schema_version: clean_(p.schema_version || 'SB1')
  };

  if (isControl) {
    // Read only the 8 tracking cells (KD:KK) for transition semantics.
    // No 297-column prior-row read.
    const prevTracking = readEdgeTrackingBlock_(sheet, targetRow);

    const alertTf = clean_(p.edge_alert_tf || tf);
    const requiredTf = clean_(p.edge_required_exec_tf || p.exec_tf);
    const prevTrackingState = clean_(prevTracking.edge_tracking_state).toUpperCase();
    const prevRequiredTf = clean_(prevTracking.edge_required_exec_tf);
    const prevWasActive = prevTrackingState === 'ACTIVE';
    const requiredChanged = !!prevRequiredTf && !!requiredTf && prevRequiredTf !== requiredTf;
    const nowBarTime = value_(p.bar_time);

    const tracking = {
      edge_tracking_state: 'ALERT_CHANGE_REQUIRED',
      edge_alert_tf: alertTf,
      edge_required_exec_tf: requiredTf,
      edge_previous_exec_tf: '',
      edge_needs_alert_change: true,
      edge_handoff_time: '',
      edge_tracking_message: '',
      edge_tracking_reason: ''
    };

    if (prevWasActive) {
      tracking.edge_tracking_reason = 'ACTIVE_ALERT_LOST_AUTHORITY';
      tracking.edge_previous_exec_tf = prevRequiredTf || alertTf;
      tracking.edge_handoff_time = nowBarTime;
      tracking.edge_tracking_message =
        'EDGE execution TF changed from ' + tfLabel_(tracking.edge_previous_exec_tf) +
        ' to ' + tfLabel_(requiredTf) + '. The ' + tfLabel_(alertTf) +
        ' terminal alert is no longer authoritative. Create or enable the ' +
        tfLabel_(requiredTf) + ' EDGE terminal alert to resume full tracking.';
    } else if (requiredChanged) {
      tracking.edge_tracking_reason = 'REQUIRED_TF_CHANGED_WHILE_INACTIVE';
      tracking.edge_previous_exec_tf = prevRequiredTf;
      tracking.edge_handoff_time = nowBarTime;
      tracking.edge_tracking_message =
        'EDGE required execution TF changed from ' + tfLabel_(prevRequiredTf) +
        ' to ' + tfLabel_(requiredTf) + ' while this alert remains on ' +
        tfLabel_(alertTf) + '. Create or enable the ' + tfLabel_(requiredTf) +
        ' EDGE terminal alert to resume full tracking.';
    } else {
      tracking.edge_tracking_reason = 'TF_MISMATCH';
      tracking.edge_previous_exec_tf = prevRequiredTf || '';
      tracking.edge_handoff_time = prevTracking.edge_handoff_time || nowBarTime;
      tracking.edge_tracking_message =
        'EDGE terminal alert is on ' + tfLabel_(alertTf) +
        '; required execution TF is ' + tfLabel_(requiredTf) +
        '. Create or enable the ' + tfLabel_(requiredTf) +
        ' EDGE terminal alert to resume full tracking.';
    }

    // A:H transport identity/time.
    writeContiguousFieldsRetry_(sheet, targetRow, 1, [
      base.updated_at, base.instrument, base.tv_ticker, base.source, base.tf,
      base.bar_time, base.close, base.schema_version
    ]);

    // T = exec_tf. Preserve all other last-authoritative EDGE facts in this TF row.
    writeCellRetry_(sheet, targetRow, 20, normalizeSheetValue_(requiredTf));

    // KD:KK = eight EDGE tracking fields.
    writeContiguousFieldsRetry_(sheet, targetRow, 290, [
      tracking.edge_tracking_state,
      tracking.edge_alert_tf,
      tracking.edge_required_exec_tf,
      tracking.edge_previous_exec_tf,
      tracking.edge_needs_alert_change,
      tracking.edge_handoff_time,
      tracking.edge_tracking_message,
      tracking.edge_tracking_reason
    ]);
    return targetRow;
  }

  // Authoritative STATE packet.
  // Preserve the same TWO write calls as v12 to protect webhook latency.
  // Write A:AT in one block so the existing raw_json cell can carry only the
  // compact Superbrain expansion (router map + bounded hurdle book), not the
  // full alert payload. No new Sheet columns are required.
  const coreEndIdx0 = CURRENT_HEADERS.indexOf('raw_json');
  const coreHeaders = CURRENT_HEADERS.slice(0, coreEndIdx0 + 1); // A:AT
  const coreObj = Object.assign({}, p, base, { raw_json: edgeExpansionJson_(p) });
  const coreValues = coreHeaders.map(h => normalizeSheetValue_(coreObj[h]));
  writeContiguousFieldsRetry_(sheet, targetRow, 1, coreValues);

  // HT:KK = existing typed EDGE facts + tracking fields. This remains one
  // contiguous write exactly as before; no extra write is introduced.
  const extStartIdx0 = CURRENT_HEADERS.indexOf('macro_tf');
  const extHeaders = CURRENT_HEADERS.slice(extStartIdx0);
  const extValues = extHeaders.map(h => normalizeSheetValue_(p[h]));
  writeContiguousFieldsRetry_(sheet, targetRow, extStartIdx0 + 1, extValues);

  return targetRow;
}

function edgeExpansionJson_(p) {
  // Keep only the extra facts not already represented by typed EDGE columns.
  // Nested router/hurdle structures stay compact in one existing cell, so the
  // hot path keeps two Sheet writes and avoids hundreds of new columns.
  const expansion = {
    schema_version: 'EDGE_EXP1',
    edge_feed_version: clean_(p.edge_feed_version || ''),
    hierarchy_reference_price: p.hierarchy_reference_price === undefined ? null : p.hierarchy_reference_price,
    battlefield_divider: p.battlefield_divider === undefined ? null : p.battlefield_divider,
    battlefield_map_lower: p.battlefield_map_lower === undefined ? null : p.battlefield_map_lower,
    battlefield_map_upper: p.battlefield_map_upper === undefined ? null : p.battlefield_map_upper,
    battlefield_range_position_pct: p.battlefield_range_position_pct === undefined ? null : p.battlefield_range_position_pct,
    battlefield_demand_tf: clean_(p.battlefield_demand_tf),
    battlefield_demand_low: p.battlefield_demand_low === undefined ? null : p.battlefield_demand_low,
    battlefield_demand_high: p.battlefield_demand_high === undefined ? null : p.battlefield_demand_high,
    battlefield_demand_time: p.battlefield_demand_time === undefined ? null : p.battlefield_demand_time,
    battlefield_supply_tf: clean_(p.battlefield_supply_tf),
    battlefield_supply_low: p.battlefield_supply_low === undefined ? null : p.battlefield_supply_low,
    battlefield_supply_high: p.battlefield_supply_high === undefined ? null : p.battlefield_supply_high,
    battlefield_supply_time: p.battlefield_supply_time === undefined ? null : p.battlefield_supply_time,
    edge_router_map: Array.isArray(p.edge_router_map) ? p.edge_router_map : [],
    fp_hurdle_book: p.fp_hurdle_book && typeof p.fp_hurdle_book === 'object' ? p.fp_hurdle_book : {}
  };
  return JSON.stringify(expansion);
}

function findCurrentKeyRow_(sheet, instrument, source, tf) {
  const lastRow = sheet.getLastRow();
  if (lastRow < 2) return 0;
  const keys = sheet.getRange(2, 1, lastRow - 1, CURRENT_KEY_COLS).getValues();
  for (let i = keys.length - 1; i >= 0; i--) {
    if (clean_(keys[i][1]) === instrument &&
        clean_(keys[i][3]) === source &&
        clean_(keys[i][4]) === tf) {
      return i + 2;
    }
  }
  return 0;
}

function allocateAndSeedCurrentKeyRow_(sheet, now, p, instrument, source, tf) {
  const lock = LockService.getScriptLock();
  let locked = false;
  try {
    locked = lock.tryLock(NEW_ROW_LOCK_MS);
    const existing = findCurrentKeyRow_(sheet, instrument, source, tf);
    if (existing) return existing;

    const row = Math.max(2, sheet.getLastRow() + 1);

    // Reserve the row key before releasing the allocation lock so simultaneous
    // first packets for 3m/15m/60m cannot select the same physical row.
    writeContiguousFieldsRetry_(sheet, row, 1, [
      now,
      instrument,
      clean_(p.tv_ticker || p.tickerid || p.exchange_ticker || ''),
      source,
      tf
    ]);
    return row;
  } finally {
    if (locked) lock.releaseLock();
  }
}

function readEdgeTrackingBlock_(sheet, row) {
  const vals = sheet.getRange(row, 290, 1, 8).getValues()[0]; // KD:KK
  return {
    edge_tracking_state: vals[0],
    edge_alert_tf: vals[1],
    edge_required_exec_tf: vals[2],
    edge_previous_exec_tf: vals[3],
    edge_needs_alert_change: vals[4],
    edge_handoff_time: vals[5],
    edge_tracking_message: vals[6],
    edge_tracking_reason: vals[7]
  };
}

function normalizeSheetValue_(v) {
  if (v === undefined || v === null || v === '') return '';
  if (typeof v === 'boolean') return v;
  if (typeof v === 'number') return Number.isFinite(v) ? v : '';
  return String(v);
}

function writeCellRetry_(sheet, row, col, value) {
  let lastErr = null;
  for (let attempt = 0; attempt < 2; attempt++) {
    try {
      sheet.getRange(row, col).setValue(value);
      return;
    } catch (err) {
      lastErr = err;
      if (attempt === 0) Utilities.sleep(SHEET_WRITE_RETRY_MS);
    }
  }
  throw lastErr || new Error('sheet_write_failed');
}

function writeContiguousFieldsRetry_(sheet, row, startCol, values) {
  let lastErr = null;
  for (let attempt = 0; attempt < 2; attempt++) {
    try {
      sheet.getRange(row, startCol, 1, values.length).setValues([values]);
      return;
    } catch (err) {
      lastErr = err;
      if (attempt === 0) Utilities.sleep(SHEET_WRITE_RETRY_MS);
    }
  }
  throw lastErr || new Error('sheet_write_failed');
}


function appendEvent_(sheet, now, p, rawJson) {
  const row = {
    received_at: now,
    instrument: clean_(p.instrument || p.symbol || p.ticker || ''),
    tv_ticker: clean_(p.tv_ticker || p.tickerid || p.exchange_ticker || ''),
    source: clean_(p.source || ''),
    tf: clean_(p.tf || p.interval || ''),
    bar_time: value_(p.bar_time),
    close: numberOrBlank_(p.close),
    schema_version: clean_(p.schema_version || 'SB1'),
    event_type: clean_(p.event_type || 'STATE'),
    payload_json: rawJson,
    alert_id: clean_(p.alert_id || ''),
    status: 'OK',
    note: clean_(p.note || '')
  };
  sheet.appendRow(EVENT_HEADERS.map(h => row[h] === undefined ? '' : row[h]));
}

function upsertCurrent_(sheet, now, p, rawJson) {
  const instrument = clean_(p.instrument || p.symbol || p.ticker || '');
  const source = clean_(p.source || '');
  const tf = clean_(p.tf || p.interval || '');

  let lastRow = sheet.getLastRow();
  let targetRow = 0;
  let previousRowObj = {};

  // Fast lookup: read only A:E, never the full 297-column table just to locate a row.
  if (lastRow >= 2) {
    const keys = sheet.getRange(2, 1, lastRow - 1, CURRENT_KEY_COLS).getValues();
    for (let i = keys.length - 1; i >= 0; i--) {
      const rInstrument = clean_(keys[i][1]);
      const rSource = clean_(keys[i][3]);
      const rTf = clean_(keys[i][4]);
      if (rInstrument === instrument && rSource === source && rTf === tf) {
        targetRow = i + 2;
        break;
      }
    }
  }

  // Only STRUCTURE and EDGE need prior-state context. Read exactly one row when needed.
  if (targetRow && (source === 'STRUCTURE' || source === 'EDGE')) {
    const width = Math.min(CURRENT_HEADERS.length, sheet.getMaxColumns());
    previousRowObj = rowToObject_(sheet.getRange(targetRow, 1, 1, width).getValues()[0]);
  }

  const rowObj = {
    updated_at: now,
    instrument: instrument,
    tv_ticker: clean_(p.tv_ticker || p.tickerid || p.exchange_ticker || ''),
    source: source,
    tf: tf,
    bar_time: value_(p.bar_time),
    close: numberOrBlank_(p.close),
    schema_version: clean_(p.schema_version || 'SB1'),

    macro: clean_(p.macro),
    defender_tf: clean_(p.defender_tf),
    defender_side: clean_(p.defender_side),
    defender_low: numberOrBlank_(p.defender_low),
    defender_high: numberOrBlank_(p.defender_high),
    defender_state: clean_(p.defender_state),
    challenger_tf: clean_(p.challenger_tf),
    challenger_side: clean_(p.challenger_side),
    challenger_low: numberOrBlank_(p.challenger_low),
    challenger_high: numberOrBlank_(p.challenger_high),
    challenger_state: clean_(p.challenger_state),
    exec_tf: clean_(p.exec_tf),
    exec_of: clean_(p.exec_of),

    // EDGE terminal/control-plane tracking status. These are factual transport fields,
    // not trading signals. ACTIVE means the alert is on the required execution TF.
    edge_tracking_state: clean_(p.edge_tracking_state),
    edge_tracking_reason: clean_(p.edge_tracking_reason),
    edge_alert_tf: clean_(p.edge_alert_tf),
    edge_required_exec_tf: clean_(p.edge_required_exec_tf),
    edge_previous_exec_tf: clean_(p.edge_previous_exec_tf),
    edge_needs_alert_change: boolOrBlank_(p.edge_needs_alert_change),
    edge_handoff_time: value_(p.edge_handoff_time),
    edge_tracking_message: clean_(p.edge_tracking_message),

    fp_side: clean_(p.fp_side),
    fp_low: numberOrBlank_(p.fp_low),
    fp_high: numberOrBlank_(p.fp_high),
    fp_strength: clean_(p.fp_strength),
    fp_status: clean_(p.fp_status),
    fp_hold: numberOrBlank_(p.fp_hold),
    fp_interacting: boolOrBlank_(p.fp_interacting),
    fp_valid: boolOrBlank_(p.fp_valid),
    fp_strength_score: numberOrBlank_(p.fp_strength_score),

    // EDGE strategic / execution facts. Additive only; legacy EDGE columns remain intact.
    macro_tf: clean_(p.macro_tf),
    macro_score: numberOrBlank_(p.macro_score),
    battlefield_paired: boolOrBlank_(p.battlefield_paired),
    exec_of_score: numberOrBlank_(p.exec_of_score),
    exec_move_quality: clean_(p.exec_move_quality),

    // Most recently interacted footprint. fp_valid reports whether original EDGE hard-qualification still holds.
    fp_zone_id: numberOrBlank_(p.fp_zone_id),
    fp_formation_time: value_(p.fp_formation_time),
    fp_context_state: clean_(p.fp_context_state),
    fp_session_delta: numberOrBlank_(p.fp_session_delta),
    fp_waiting_confirmation: boolOrBlank_(p.fp_waiting_confirmation),
    fp_breach_pending: boolOrBlank_(p.fp_breach_pending),
    fp_last_interaction_time: value_(p.fp_last_interaction_time),
    fp_bars_since_interaction: numberOrBlank_(p.fp_bars_since_interaction),
    fp_event_type: clean_(p.fp_event_type),
    fp_event_score: numberOrBlank_(p.fp_event_score),
    fp_event_time: value_(p.fp_event_time),
    fp_bars_since_event: numberOrBlank_(p.fp_bars_since_event),
    fp_distance_atr: numberOrBlank_(p.fp_distance_atr),

    // Current selected qualifying demand footprint.
    fp_d_valid: boolOrBlank_(p.fp_d_valid),
    fp_d_zone_id: numberOrBlank_(p.fp_d_zone_id),
    fp_d_formation_time: value_(p.fp_d_formation_time),
    fp_d_low: numberOrBlank_(p.fp_d_low),
    fp_d_high: numberOrBlank_(p.fp_d_high),
    fp_d_strength_score: numberOrBlank_(p.fp_d_strength_score),
    fp_d_strength: clean_(p.fp_d_strength),
    fp_d_hold: numberOrBlank_(p.fp_d_hold),
    fp_d_status: clean_(p.fp_d_status),
    fp_d_context_state: clean_(p.fp_d_context_state),
    fp_d_interacting: boolOrBlank_(p.fp_d_interacting),
    fp_d_session_delta: numberOrBlank_(p.fp_d_session_delta),
    fp_d_waiting_confirmation: boolOrBlank_(p.fp_d_waiting_confirmation),
    fp_d_breach_pending: boolOrBlank_(p.fp_d_breach_pending),
    fp_d_last_interaction_time: value_(p.fp_d_last_interaction_time),
    fp_d_bars_since_interaction: numberOrBlank_(p.fp_d_bars_since_interaction),
    fp_d_event_type: clean_(p.fp_d_event_type),
    fp_d_event_score: numberOrBlank_(p.fp_d_event_score),
    fp_d_event_time: value_(p.fp_d_event_time),
    fp_d_bars_since_event: numberOrBlank_(p.fp_d_bars_since_event),
    fp_d_distance_atr: numberOrBlank_(p.fp_d_distance_atr),

    // Current selected qualifying supply footprint.
    fp_s_valid: boolOrBlank_(p.fp_s_valid),
    fp_s_zone_id: numberOrBlank_(p.fp_s_zone_id),
    fp_s_formation_time: value_(p.fp_s_formation_time),
    fp_s_low: numberOrBlank_(p.fp_s_low),
    fp_s_high: numberOrBlank_(p.fp_s_high),
    fp_s_strength_score: numberOrBlank_(p.fp_s_strength_score),
    fp_s_strength: clean_(p.fp_s_strength),
    fp_s_hold: numberOrBlank_(p.fp_s_hold),
    fp_s_status: clean_(p.fp_s_status),
    fp_s_context_state: clean_(p.fp_s_context_state),
    fp_s_interacting: boolOrBlank_(p.fp_s_interacting),
    fp_s_session_delta: numberOrBlank_(p.fp_s_session_delta),
    fp_s_waiting_confirmation: boolOrBlank_(p.fp_s_waiting_confirmation),
    fp_s_breach_pending: boolOrBlank_(p.fp_s_breach_pending),
    fp_s_last_interaction_time: value_(p.fp_s_last_interaction_time),
    fp_s_bars_since_interaction: numberOrBlank_(p.fp_s_bars_since_interaction),
    fp_s_event_type: clean_(p.fp_s_event_type),
    fp_s_event_score: numberOrBlank_(p.fp_s_event_score),
    fp_s_event_time: value_(p.fp_s_event_time),
    fp_s_bars_since_event: numberOrBlank_(p.fp_s_bars_since_event),
    fp_s_distance_atr: numberOrBlank_(p.fp_s_distance_atr),

    aurora_state: clean_(p.aurora_state),
    aurora_prev: clean_(p.aurora_prev),
    aurora_change: clean_(p.aurora_change),

    eta_up_level: numberOrBlank_(p.eta_up_level),
    eta_up_regime: clean_(p.eta_up_regime),
    eta_up_locked_time: value_(p.eta_up_locked_time),
    eta_up_model_time: value_(p.eta_up_model_time),
    eta_up_model_bars: numberOrBlank_(p.eta_up_model_bars),
    eta_up_model_distance: numberOrBlank_(p.eta_up_model_distance),
    eta_up_model_atr: numberOrBlank_(p.eta_up_model_atr),
    eta_up_speed_price_per_bar: numberOrBlank_(p.eta_up_speed_price_per_bar),
    eta_up_speed_atr_per_bar: numberOrBlank_(p.eta_up_speed_atr_per_bar),
    eta_up_locked_remaining_bars: numberOrBlank_(p.eta_up_locked_remaining_bars),

    eta_down_level: numberOrBlank_(p.eta_down_level),
    eta_down_regime: clean_(p.eta_down_regime),
    eta_down_locked_time: value_(p.eta_down_locked_time),
    eta_down_model_time: value_(p.eta_down_model_time),
    eta_down_model_bars: numberOrBlank_(p.eta_down_model_bars),
    eta_down_model_distance: numberOrBlank_(p.eta_down_model_distance),
    eta_down_model_atr: numberOrBlank_(p.eta_down_model_atr),
    eta_down_speed_price_per_bar: numberOrBlank_(p.eta_down_speed_price_per_bar),
    eta_down_speed_atr_per_bar: numberOrBlank_(p.eta_down_speed_atr_per_bar),
    eta_down_locked_remaining_bars: numberOrBlank_(p.eta_down_locked_remaining_bars),
    eta_current_atr: numberOrBlank_(p.eta_current_atr),

    structure_trend: clean_(p.structure_trend),

    liq_buy_level: numberOrBlank_(p.liq_buy_level),
    liq_buy_count: numberOrBlank_(p.liq_buy_count),
    liq_sell_level: numberOrBlank_(p.liq_sell_level),
    liq_sell_count: numberOrBlank_(p.liq_sell_count),
    liq_last_event: clean_(p.liq_last_event || p.liq_event1_type),
    liq_event_level: numberOrBlank_(p.liq_event_level !== undefined ? p.liq_event_level : p.liq_event1_level),
    liq_event_count: numberOrBlank_(p.liq_event_count !== undefined ? p.liq_event_count : p.liq_event1_swept_count),
    liq_event_reclaimed: p.liq_event_reclaimed !== undefined ? boolOrBlank_(p.liq_event_reclaimed) : (clean_(p.liq_event1_type).indexOf('SWEEP') >= 0 ? true : clean_(p.liq_event1_type).indexOf('RUN') >= 0 ? false : ''),

    liq_buy_valid: boolOrBlank_(p.liq_buy_valid),
    liq_buy_low: numberOrBlank_(p.liq_buy_low),
    liq_buy_high: numberOrBlank_(p.liq_buy_high),
    liq_buy_distance: numberOrBlank_(p.liq_buy_distance),
    liq_sell_valid: boolOrBlank_(p.liq_sell_valid),
    liq_sell_low: numberOrBlank_(p.liq_sell_low),
    liq_sell_high: numberOrBlank_(p.liq_sell_high),
    liq_sell_distance: numberOrBlank_(p.liq_sell_distance),
    liq_pending_count: numberOrBlank_(p.liq_pending_count),
    liq_history_count: numberOrBlank_(p.liq_history_count),

    // Exact TradingView rendered-box boundaries for pool 1.
    liq_buy_box_low: numberOrBlank_(p.liq_buy_box_low),
    liq_buy_box_high: numberOrBlank_(p.liq_buy_box_high),
    liq_sell_box_low: numberOrBlank_(p.liq_sell_box_low),
    liq_sell_box_high: numberOrBlank_(p.liq_sell_box_high),

    // Additional active pools beyond the visible nearest pool.
    liq_buy2_valid: boolOrBlank_(p.liq_buy2_valid),
    liq_buy2_low: numberOrBlank_(p.liq_buy2_low),
    liq_buy2_high: numberOrBlank_(p.liq_buy2_high),
    liq_buy2_level: numberOrBlank_(p.liq_buy2_level),
    liq_buy2_count: numberOrBlank_(p.liq_buy2_count),
    liq_buy2_distance: numberOrBlank_(p.liq_buy2_distance),

    liq_buy3_valid: boolOrBlank_(p.liq_buy3_valid),
    liq_buy3_low: numberOrBlank_(p.liq_buy3_low),
    liq_buy3_high: numberOrBlank_(p.liq_buy3_high),
    liq_buy3_level: numberOrBlank_(p.liq_buy3_level),
    liq_buy3_count: numberOrBlank_(p.liq_buy3_count),
    liq_buy3_distance: numberOrBlank_(p.liq_buy3_distance),

    liq_sell2_valid: boolOrBlank_(p.liq_sell2_valid),
    liq_sell2_low: numberOrBlank_(p.liq_sell2_low),
    liq_sell2_high: numberOrBlank_(p.liq_sell2_high),
    liq_sell2_level: numberOrBlank_(p.liq_sell2_level),
    liq_sell2_count: numberOrBlank_(p.liq_sell2_count),
    liq_sell2_distance: numberOrBlank_(p.liq_sell2_distance),

    liq_sell3_valid: boolOrBlank_(p.liq_sell3_valid),
    liq_sell3_low: numberOrBlank_(p.liq_sell3_low),
    liq_sell3_high: numberOrBlank_(p.liq_sell3_high),
    liq_sell3_level: numberOrBlank_(p.liq_sell3_level),
    liq_sell3_count: numberOrBlank_(p.liq_sell3_count),
    liq_sell3_distance: numberOrBlank_(p.liq_sell3_distance),

    raw_json: rawJson,

    struct_high: numberOrBlank_(p.struct_high),
    struct_low: numberOrBlank_(p.struct_low),
    last_clean_price: numberOrBlank_(p.last_clean_price),
    last_clean_time: value_(p.last_clean_time),
    new_clean_events: clean_(p.new_clean_events),
    mintick: numberOrBlank_(p.mintick),

    h1_price: numberOrBlank_(p.h1_price),
    h1_time: value_(p.h1_time),
    h1_type: clean_(p.h1_type),
    h2_price: numberOrBlank_(p.h2_price),
    h2_time: value_(p.h2_time),
    h2_type: clean_(p.h2_type),
    h3_price: numberOrBlank_(p.h3_price),
    h3_time: value_(p.h3_time),
    h3_type: clean_(p.h3_type),

    l1_price: numberOrBlank_(p.l1_price),
    l1_time: value_(p.l1_time),
    l1_type: clean_(p.l1_type),
    l2_price: numberOrBlank_(p.l2_price),
    l2_time: value_(p.l2_time),
    l2_type: clean_(p.l2_type),
    l3_price: numberOrBlank_(p.l3_price),
    l3_time: value_(p.l3_time),
    l3_type: clean_(p.l3_type)
  };

  if (source === 'LIQUIDITY') {
    for (let i = 1; i <= 3; i++) {
      rowObj['liq_pending' + i + '_side'] = clean_(p['liq_pending' + i + '_side']);
      rowObj['liq_pending' + i + '_full_candidate'] = boolOrBlank_(p['liq_pending' + i + '_full_candidate']);
      rowObj['liq_pending' + i + '_level'] = numberOrBlank_(p['liq_pending' + i + '_level']);
      rowObj['liq_pending' + i + '_swept_count'] = numberOrBlank_(p['liq_pending' + i + '_swept_count']);
      rowObj['liq_pending' + i + '_remaining_count'] = numberOrBlank_(p['liq_pending' + i + '_remaining_count']);
      rowObj['liq_pending' + i + '_cross_time'] = value_(p['liq_pending' + i + '_cross_time']);
      rowObj['liq_pending' + i + '_cross_close'] = numberOrBlank_(p['liq_pending' + i + '_cross_close']);
    }
    let histCount = 0;
    for (let i = 1; i <= 5; i++) {
      const prefix = 'liq_event' + i + '_';
      rowObj[prefix + 'type'] = clean_(p[prefix + 'type']);
      rowObj[prefix + 'side'] = clean_(p[prefix + 'side']);
      rowObj[prefix + 'full_cross'] = boolOrBlank_(p[prefix + 'full_cross']);
      rowObj[prefix + 'level'] = numberOrBlank_(p[prefix + 'level']);
      rowObj[prefix + 'swept_count'] = numberOrBlank_(p[prefix + 'swept_count']);
      rowObj[prefix + 'remaining_count'] = numberOrBlank_(p[prefix + 'remaining_count']);
      rowObj[prefix + 'cross_time'] = value_(p[prefix + 'cross_time']);
      rowObj[prefix + 'cross_close'] = numberOrBlank_(p[prefix + 'cross_close']);
      rowObj[prefix + 'cross_high'] = numberOrBlank_(p[prefix + 'cross_high']);
      rowObj[prefix + 'cross_low'] = numberOrBlank_(p[prefix + 'cross_low']);
      rowObj[prefix + 'confirm_time'] = value_(p[prefix + 'confirm_time']);
      rowObj[prefix + 'confirm_close'] = numberOrBlank_(p[prefix + 'confirm_close']);
      if (rowObj[prefix + 'type']) histCount++;
    }
    rowObj.liq_history_count = histCount;
  }

  if (source === 'STRUCTURE') {
    const pf = calculatePitchforkState_(p, previousRowObj);

    rowObj.fork_candidate_valid = pf.candidate.valid;
    rowObj.fork_candidate_kind = pf.candidate.kind || '';
    rowObj.fork_candidate_slope = pf.candidate.slope || '';
    rowObj.fork_previous_candidate_slope = pf.previousCandidate.slope || '';

    rowObj.fork_valid = pf.active.valid;
    rowObj.fork_active_kind = pf.active.kind || '';
    rowObj.fork_slope = pf.active.slope || '';
    rowObj.fork_slope_flip = pf.slopeFlip || 'NONE';
    rowObj.fork_position = pf.active.position || '';
    rowObj.fork_reclaimed_2sd = pf.reclaimed2sd;
    rowObj.fork_reclaim_side = pf.reclaimSide || '';

    rowObj.fork_a_price = blankIfNaN_(pf.active.aPrice);
    rowObj.fork_a_time = blankIfNaN_(pf.active.aTime);
    rowObj.fork_b_price = blankIfNaN_(pf.active.bPrice);
    rowObj.fork_b_time = blankIfNaN_(pf.active.bTime);
    rowObj.fork_c_price = blankIfNaN_(pf.active.cPrice);
    rowObj.fork_c_time = blankIfNaN_(pf.active.cTime);

    rowObj.fork_median = blankIfNaN_(pf.active.median);
    rowObj.fork_upper_1sd = blankIfNaN_(pf.active.upper1);
    rowObj.fork_upper_2sd = blankIfNaN_(pf.active.upper2);
    rowObj.fork_lower_1sd = blankIfNaN_(pf.active.lower1);
    rowObj.fork_lower_2sd = blankIfNaN_(pf.active.lower2);
  } else {
    rowObj.fork_valid = boolOrBlank_(p.fork_valid);
    rowObj.fork_slope = clean_(p.fork_slope);
    rowObj.fork_slope_flip = clean_(p.fork_slope_flip);
    rowObj.fork_position = clean_(p.fork_position);
    rowObj.fork_reclaimed_2sd = boolOrBlank_(p.fork_reclaimed_2sd);
  }

  // EDGE wrong-TF packets are deliberately stateless in Pine and arrive as TF_STATUS
  // on every confirmed bar. Infer the semantic transition here using the previous
  // row for the same instrument/source/chart-TF. This avoids Pine-side one-shot
  // watcher memory and guarantees a fresh control packet after alert creation.
  const edgeEventType = clean_(p.event_type).toUpperCase();
  const edgeTfStatusPacket = source === 'EDGE' && edgeEventType === 'TF_STATUS';

  if (edgeTfStatusPacket) {
    const alertTf = clean_(p.edge_alert_tf || tf);
    const requiredTf = clean_(p.edge_required_exec_tf || p.exec_tf);
    const prevTrackingState = clean_(previousRowObj.edge_tracking_state).toUpperCase();
    const prevRequiredTf = clean_(previousRowObj.edge_required_exec_tf || previousRowObj.exec_tf);
    const prevWasActive = prevTrackingState === 'ACTIVE';
    const requiredChanged = !!prevRequiredTf && !!requiredTf && prevRequiredTf !== requiredTf;
    const nowBarTime = value_(p.bar_time);

    rowObj.edge_tracking_state = 'ALERT_CHANGE_REQUIRED';
    rowObj.edge_alert_tf = alertTf;
    rowObj.edge_required_exec_tf = requiredTf;
    rowObj.exec_tf = requiredTf;
    rowObj.edge_needs_alert_change = true;

    if (prevWasActive) {
      rowObj.edge_tracking_reason = 'ACTIVE_ALERT_LOST_AUTHORITY';
      rowObj.edge_previous_exec_tf = prevRequiredTf || alertTf;
      rowObj.edge_handoff_time = nowBarTime;
      rowObj.edge_tracking_message = 'EDGE execution TF changed from ' + tfLabel_(rowObj.edge_previous_exec_tf) + ' to ' + tfLabel_(requiredTf) + '. The ' + tfLabel_(alertTf) + ' terminal alert is no longer authoritative. Create or enable the ' + tfLabel_(requiredTf) + ' EDGE terminal alert to resume full tracking.';
    } else if (requiredChanged) {
      rowObj.edge_tracking_reason = 'REQUIRED_TF_CHANGED_WHILE_INACTIVE';
      rowObj.edge_previous_exec_tf = prevRequiredTf;
      rowObj.edge_handoff_time = nowBarTime;
      rowObj.edge_tracking_message = 'EDGE required execution TF changed from ' + tfLabel_(prevRequiredTf) + ' to ' + tfLabel_(requiredTf) + ' while this alert remains on ' + tfLabel_(alertTf) + '. Create or enable the ' + tfLabel_(requiredTf) + ' EDGE terminal alert to resume full tracking.';
    } else {
      rowObj.edge_tracking_reason = 'TF_MISMATCH';
      rowObj.edge_previous_exec_tf = prevRequiredTf || '';
      rowObj.edge_handoff_time = previousRowObj.edge_handoff_time || nowBarTime;
      rowObj.edge_tracking_message = 'EDGE terminal alert is on ' + tfLabel_(alertTf) + '; required execution TF is ' + tfLabel_(requiredTf) + '. Create or enable the ' + tfLabel_(requiredTf) + ' EDGE terminal alert to resume full tracking.';
    }
  }

  // Preserve the last authoritative execution/footprint facts in a wrong-TF row.
  // Tracking fields and transport metadata continue to update on every TF_STATUS packet.
  const edgeControlPacket = source === 'EDGE' && (
    edgeEventType === 'TF_STATUS' ||
    edgeEventType === 'TF_MISMATCH' ||
    edgeEventType === 'EXEC_TF_CHANGED' ||
    edgeEventType === 'REQUIRED_TF_CHANGED'
  );
  if (edgeControlPacket && targetRow && previousRowObj) {
    const alwaysFresh = new Set([
      'updated_at','instrument','tv_ticker','source','tf','bar_time','close','schema_version','raw_json',
      'exec_tf','edge_tracking_state','edge_tracking_reason','edge_alert_tf','edge_required_exec_tf','edge_previous_exec_tf',
      'edge_needs_alert_change','edge_handoff_time','edge_tracking_message'
    ]);
    CURRENT_HEADERS.forEach(h => {
      if (alwaysFresh.has(h)) return;
      if (p[h] === undefined && previousRowObj[h] !== undefined) rowObj[h] = previousRowObj[h];
    });
  }

  const values = CURRENT_HEADERS.map(h => rowObj[h] === undefined ? '' : rowObj[h]);

  if (!targetRow) {
    // New instrument/source/TF rows are rare. Lock only this allocation step, never the whole webhook.
    const lock = LockService.getScriptLock();
    let locked = false;
    try {
      locked = lock.tryLock(NEW_ROW_LOCK_MS);
      if (locked) {
        // Another request may have created this key while we waited; re-check only A:E.
        lastRow = sheet.getLastRow();
        if (lastRow >= 2) {
          const keys2 = sheet.getRange(2, 1, lastRow - 1, CURRENT_KEY_COLS).getValues();
          for (let i = keys2.length - 1; i >= 0; i--) {
            if (clean_(keys2[i][1]) === instrument && clean_(keys2[i][3]) === source && clean_(keys2[i][4]) === tf) {
              targetRow = i + 2;
              break;
            }
          }
        }
        if (!targetRow) targetRow = Math.max(2, lastRow + 1);
      } else {
        // Do not burn TradingView's timeout waiting for a global lock.
        // A rare allocation collision is preferable to dropping the webhook entirely.
        targetRow = Math.max(2, sheet.getLastRow() + 1);
      }
      sheet.getRange(targetRow, 1, 1, values.length).setValues([values]);
    } finally {
      if (locked) lock.releaseLock();
    }
  } else {
    sheet.getRange(targetRow, 1, 1, values.length).setValues([values]);
  }

  return targetRow;
}

function calculatePitchforkState_(p, prev) {
  const trend = clean_(p.structure_trend).toUpperCase();
  const nowTime = toNum_(p.bar_time);
  const nowPrice = toNum_(p.close);
  const mintick = Math.abs(toNum_(p.mintick)) || 0;
  const flatTicks = Math.max(0, toNum_(p.pf_flat_ticks) || 2);
  const minWidthTicks = Math.max(0, toNum_(p.pf_min_width_ticks) || 5);

  const highs = compactPoints_([
    point_(p.h1_price, p.h1_time, p.h1_type),
    point_(p.h2_price, p.h2_time, p.h2_type),
    point_(p.h3_price, p.h3_time, p.h3_type)
  ]);
  const lows = compactPoints_([
    point_(p.l1_price, p.l1_time, p.l1_type),
    point_(p.l2_price, p.l2_time, p.l2_type),
    point_(p.l3_price, p.l3_time, p.l3_type)
  ]);

  const pairSet = trend === 'UP' ? highs : trend === 'DOWN' ? lows : [];
  const crossSet = trend === 'UP' ? lows : trend === 'DOWN' ? highs : [];

  const candidate = buildCandidateFromPair_(pairSet, crossSet, pairSet.length - 2, pairSet.length - 1, trend, nowTime, nowPrice, mintick, flatTicks, minWidthTicks);
  const previousCandidate = buildCandidateFromPair_(pairSet, crossSet, pairSet.length - 3, pairSet.length - 2, trend, nowTime, nowPrice, mintick, flatTicks, minWidthTicks);

  const prevActive = activeFromPreviousRow_(prev, nowTime, nowPrice);
  let active = prevActive.valid ? prevActive : invalidFork_();

  // Cold-start correction: with only the last 3 same-side pivots we can rebuild
  // the last two candidate forks. Start from the EARLIER valid candidate, then
  // apply the normal slope/side replacement rule to the latest candidate. This
  // avoids incorrectly bootstrapping the newest same-slope candidate as active.
  if (!active.valid && previousCandidate.valid) {
    active = previousCandidate;
  }

  let activeChanged = false;
  if (candidate.valid) {
    if (!active.valid || active.kind !== candidate.kind || active.slope !== candidate.slope) {
      active = candidate;
      activeChanged = true;
    } else {
      active = evaluateStoredForkAt_(active, nowTime, nowPrice);
    }
  } else if (active.valid) {
    active = evaluateStoredForkAt_(active, nowTime, nowPrice);
  }

  let slopeFlip = 'NONE';
  if (activeChanged && prevActive.valid && prevActive.slope !== active.slope) {
    slopeFlip = active.slope === 'UP' ? 'UP' : active.slope === 'DOWN' ? 'DOWN' : 'FLAT';
  }

  const prevPosition = clean_(prev.fork_position);
  let reclaimed2sd = false;
  let reclaimSide = '';

  const sameFork = prevActive.valid && active.valid &&
    nearlyEqual_(prevActive.aTime, active.aTime) &&
    nearlyEqual_(prevActive.bTime, active.bTime) &&
    nearlyEqual_(prevActive.cTime, active.cTime);

  if (sameFork) {
    if (prevPosition === 'BELOW_LOWER_2SD' && active.position !== 'BELOW_LOWER_2SD') {
      reclaimed2sd = true;
      reclaimSide = 'LOWER';
    }
    if (prevPosition === 'ABOVE_UPPER_2SD' && active.position !== 'ABOVE_UPPER_2SD') {
      reclaimed2sd = true;
      reclaimSide = 'UPPER';
    }
  }

  return { candidate, previousCandidate, active, slopeFlip, reclaimed2sd, reclaimSide };
}

function buildCandidateFromPair_(same, cross, aIdx, bIdx, trend, nowTime, nowPrice, mintick, flatTicks, minWidthTicks) {
  if (aIdx < 0 || bIdx < 0 || aIdx >= same.length || bIdx >= same.length) return invalidFork_();

  const A = same[aIdx];
  const B = same[bIdx];
  if (!(A.time < B.time)) return invalidFork_();

  const between = cross.filter(x => x.time > A.time && x.time < B.time).sort((x, y) => y.time - x.time);
  if (!between.length) return invalidFork_();
  const C = between[0];

  const mTime = Math.round((B.time + C.time) / 2);
  const mPrice = (B.price + C.price) / 2;
  const xVec = mTime - A.time;
  const yVec = mPrice - A.price;

  if (!Number.isFinite(xVec) || xVec === 0) return invalidFork_();

  const width = Math.abs(B.price - C.price);
  const enoughWidth = mintick > 0 ? width >= mintick * minWidthTicks : width > 0;
  if (!enoughWidth) return invalidFork_();

  let slope = 'FLAT';
  const flatBand = mintick * flatTicks;
  if (Math.abs(yVec) > flatBand) slope = yVec > 0 ? 'UP' : 'DOWN';

  const fork = {
    valid: true,
    kind: trend === 'UP' ? 'BULL' : 'BEAR',
    slope: slope,
    aPrice: A.price, aTime: A.time,
    bPrice: B.price, bTime: B.time,
    cPrice: C.price, cTime: C.time,
    mPrice: mPrice, mTime: mTime
  };
  return evaluateStoredForkAt_(fork, nowTime, nowPrice);
}

function evaluateStoredForkAt_(fork, nowTime, nowPrice) {
  if (!fork || !fork.valid) return invalidFork_();
  const aT = toNum_(fork.aTime), aP = toNum_(fork.aPrice);
  const bT = toNum_(fork.bTime), bP = toNum_(fork.bPrice);
  const cT = toNum_(fork.cTime), cP = toNum_(fork.cPrice);
  const mT = Number.isFinite(toNum_(fork.mTime)) ? toNum_(fork.mTime) : Math.round((bT + cT) / 2);
  const mP = Number.isFinite(toNum_(fork.mPrice)) ? toNum_(fork.mPrice) : (bP + cP) / 2;

  const xVec = mT - aT;
  const yVec = mP - aP;
  if (!Number.isFinite(xVec) || xVec === 0) return invalidFork_();

  const slopePerMs = yVec / xVec;
  const lineAt_ = (x0, y0) => y0 + slopePerMs * (nowTime - x0);

  const median = lineAt_(aT, aP);
  const railB = lineAt_(bT, bP);
  const railC = lineAt_(cT, cP);

  const plus2T = 2 * bT - mT;
  const plus2P = 2 * bP - mP;
  const minus2T = 2 * cT - mT;
  const minus2P = 2 * cP - mP;
  const railB2 = lineAt_(plus2T, plus2P);
  const railC2 = lineAt_(minus2T, minus2P);

  const upper1 = Math.max(railB, railC);
  const lower1 = Math.min(railB, railC);
  const upper2 = Math.max(railB2, railC2);
  const lower2 = Math.min(railB2, railC2);

  const out = Object.assign({}, fork, {
    mTime: mT, mPrice: mP,
    median, upper1, upper2, lower1, lower2,
    position: classifyForkPosition_(nowPrice, median, upper1, upper2, lower1, lower2)
  });
  return out;
}

function classifyForkPosition_(price, median, upper1, upper2, lower1, lower2) {
  if (![price, median, upper1, upper2, lower1, lower2].every(Number.isFinite)) return '';
  if (price > upper2) return 'ABOVE_UPPER_2SD';
  if (price >= upper1) return 'UPPER_1SD_TO_2SD';
  if (price >= median) return 'MEDIAN_TO_UPPER_1SD';
  if (price >= lower1) return 'LOWER_1SD_TO_MEDIAN';
  if (price >= lower2) return 'LOWER_2SD_TO_1SD';
  return 'BELOW_LOWER_2SD';
}

function activeFromPreviousRow_(prev, nowTime, nowPrice) {
  const valid = prev && (prev.fork_valid === true || String(prev.fork_valid).toUpperCase() === 'TRUE');
  if (!valid) return invalidFork_();

  const fork = {
    valid: true,
    kind: clean_(prev.fork_active_kind),
    slope: clean_(prev.fork_slope),
    aPrice: toNum_(prev.fork_a_price),
    aTime: toNum_(prev.fork_a_time),
    bPrice: toNum_(prev.fork_b_price),
    bTime: toNum_(prev.fork_b_time),
    cPrice: toNum_(prev.fork_c_price),
    cTime: toNum_(prev.fork_c_time)
  };

  if (![fork.aPrice,fork.aTime,fork.bPrice,fork.bTime,fork.cPrice,fork.cTime].every(Number.isFinite)) return invalidFork_();
  return evaluateStoredForkAt_(fork, nowTime, nowPrice);
}

function invalidFork_() {
  return {
    valid:false, kind:'', slope:'', position:'',
    aPrice:NaN,aTime:NaN,bPrice:NaN,bTime:NaN,cPrice:NaN,cTime:NaN,
    mPrice:NaN,mTime:NaN,median:NaN,upper1:NaN,upper2:NaN,lower1:NaN,lower2:NaN
  };
}

function point_(price, time, type) {
  return { price: toNum_(price), time: toNum_(time), type: clean_(type) };
}

function compactPoints_(arr) {
  return arr.filter(x => Number.isFinite(x.price) && Number.isFinite(x.time)).sort((a,b) => a.time - b.time);
}

function rowToObject_(row) {
  const o = {};
  CURRENT_HEADERS.forEach((h, i) => o[h] = i < row.length ? row[i] : '');
  return o;
}

function ensureCurrentHeaders_(sheet) {
  if (sheet.getMaxColumns() < CURRENT_HEADERS.length) {
    sheet.insertColumnsAfter(sheet.getMaxColumns(), CURRENT_HEADERS.length - sheet.getMaxColumns());
  }
  const existing = sheet.getRange(1, 1, 1, CURRENT_HEADERS.length).getValues()[0];
  let mismatch = false;
  for (let i = 0; i < CURRENT_HEADERS.length; i++) {
    if (String(existing[i] || '') !== CURRENT_HEADERS[i]) {
      mismatch = true;
      break;
    }
  }
  if (mismatch) sheet.getRange(1, 1, 1, CURRENT_HEADERS.length).setValues([CURRENT_HEADERS]);
}

function cleanupLegacyEventSheetV14() {
  const ss = SpreadsheetApp.openById(SPREADSHEET_ID);
  const legacy = ss.getSheetByName(EVENTS_SHEET);
  if (!legacy) return { ok: true, deleted: false, message: 'TV_TEST_EVENTS already absent' };
  ss.deleteSheet(legacy);
  return { ok: true, deleted: true, message: 'TV_TEST_EVENTS deleted; v14 does not use it' };
}

function setupFastWebhookV10() {
  const ss = SpreadsheetApp.openById(SPREADSHEET_ID);
  const current = ss.getSheetByName(CURRENT_SHEET);
  if (!current) throw new Error('TV_TEST_CURRENT missing');
  ensureCurrentHeaders_(current);
  return { ok: true, version: 'V14_NO_EVENT_SHEET', headers: CURRENT_HEADERS.length, event_log_enabled: false, diagnostics: 'Apps Script execution log' };
}

function testWriteBTCUSD() {
  const ss = SpreadsheetApp.openById(SPREADSHEET_ID);
  const current = ss.getSheetByName(CURRENT_SHEET);
  ensureCurrentHeaders_(current);
  const now = new Date();
  const p = {
    instrument: 'BTCUSD',
    tv_ticker: 'COINBASE:BTCUSD',
    source: 'AURORA',
    tf: '60',
    bar_time: now.getTime(),
    close: 100000,
    schema_version: 'SB1',
    event_type: 'STATE_TEST',
    aurora_state: 'GREEN',
    aurora_prev: 'WHITE',
    aurora_change: 'FRESH_TURN'
  };
  const rawJson = JSON.stringify(p);
  upsertCurrent_(current, now, p, rawJson);
  SpreadsheetApp.flush();
}

function testWriteStructureBTCUSD() {
  const ss = SpreadsheetApp.openById(SPREADSHEET_ID);
  const current = ss.getSheetByName(CURRENT_SHEET);
  ensureCurrentHeaders_(current);
  const now = new Date();
  const t = now.getTime();
  const p = {
    instrument:'BTCUSD', tv_ticker:'CRYPTO:BTCUSD', source:'STRUCTURE', tf:'1',
    bar_time:t, close:81250, schema_version:'SB1', event_type:'STATE',
    structure_trend:'UP', mintick:0.01, pf_flat_ticks:2, pf_min_width_ticks:5,
    h1_price:80800, h1_time:t-18*60000, h1_type:'H',
    h2_price:81100, h2_time:t-10*60000, h2_type:'HH',
    h3_price:81400, h3_time:t-2*60000, h3_type:'HH',
    l1_price:80550, l1_time:t-14*60000, l1_type:'L',
    l2_price:80900, l2_time:t-6*60000, l2_type:'HL',
    l3_price:null, l3_time:null, l3_type:''
  };
  const rawJson = JSON.stringify(p);
  upsertCurrent_(current, now, p, rawJson);
  SpreadsheetApp.flush();
}

function testWriteLiquidityBTCUSD() {
  const ss = SpreadsheetApp.openById(SPREADSHEET_ID);
  const current = ss.getSheetByName(CURRENT_SHEET);
  ensureCurrentHeaders_(current);
  const now = new Date();
  const t = now.getTime();
  const p = {
    instrument:'BTCUSD', tv_ticker:'CRYPTO:BTCUSD', source:'LIQUIDITY', tf:'1',
    bar_time:t, close:81350, schema_version:'SB1', event_type:'STATE',
    liq_buy_valid:true, liq_buy_low:81420, liq_buy_high:81435, liq_buy_level:81427.5, liq_buy_count:3, liq_buy_distance:70,
    liq_sell_valid:true, liq_sell_low:81220, liq_sell_high:81235, liq_sell_level:81227.5, liq_sell_count:2, liq_sell_distance:115,
    liq_pending_count:1, liq_pending1_side:'BUY', liq_pending1_full_candidate:false, liq_pending1_level:81390, liq_pending1_swept_count:1, liq_pending1_remaining_count:2, liq_pending1_cross_time:t-60000, liq_pending1_cross_close:81380,
    liq_event1_type:'SELL_FULL_SWEEP', liq_event1_side:'SELL', liq_event1_level:81260, liq_event1_swept_count:2, liq_event1_remaining_count:0, liq_event1_cross_time:t-4*60000, liq_event1_cross_close:81255, liq_event1_cross_high:81280, liq_event1_cross_low:81240, liq_event1_confirm_time:t-3*60000, liq_event1_confirm_close:81270,
    liq_event2_type:'BUY_RUN', liq_event2_side:'BUY', liq_event2_level:81480, liq_event2_swept_count:3, liq_event2_remaining_count:0, liq_event2_cross_time:t-10*60000, liq_event2_cross_close:81490, liq_event2_cross_high:81510, liq_event2_cross_low:81430, liq_event2_confirm_time:t-9*60000, liq_event2_confirm_close:81500
  };
  const rawJson = JSON.stringify(p);
  upsertCurrent_(current, now, p, rawJson);
  SpreadsheetApp.flush();
}

function testWriteEDGEBTCUSD() {
  const ss = SpreadsheetApp.openById(SPREADSHEET_ID);
  const current = ss.getSheetByName(CURRENT_SHEET);
  ensureCurrentHeaders_(current);
  const now = new Date();
  const t = now.getTime();
  const p = {
    instrument:'BTCUSD', tv_ticker:'COINBASE:BTCUSD', source:'EDGE', tf:'15',
    bar_time:t, close:81250, schema_version:'SB1', event_type:'STATE',
    macro:'BULLISH', macro_tf:'D', macro_score:31.5, battlefield_paired:true,
    defender_tf:'60', defender_side:'DEMAND', defender_low:80800, defender_high:80920, defender_state:'ACTIVE',
    challenger_tf:'15', challenger_side:'SUPPLY', challenger_low:81600, challenger_high:81740, challenger_state:'ACTIVE',
    exec_tf:'15', exec_of:'BULLISH', exec_of_score:28.4, exec_move_quality:'Conviction',
    edge_tracking_state:'ACTIVE', edge_tracking_reason:'ACTIVE_EXEC_TF', edge_alert_tf:'15', edge_required_exec_tf:'15', edge_previous_exec_tf:'15',
    edge_needs_alert_change:false, edge_handoff_time:null, edge_tracking_message:'',
    fp_valid:true, fp_side:'DEMAND', fp_low:81020, fp_high:81110, fp_strength:'Strong', fp_strength_score:78, fp_status:'TOUCHED_REDUCED', fp_hold:76, fp_interacting:false,
    fp_zone_id:42, fp_formation_time:t-45*60000, fp_context_state:'TOUCHED_REDUCED', fp_session_delta:0,
    fp_waiting_confirmation:false, fp_breach_pending:false, fp_last_interaction_time:t-2*15*60000, fp_bars_since_interaction:2,
    fp_event_type:'DEFENCE_CONFIRMED', fp_event_score:10, fp_event_time:t-15*60000, fp_bars_since_event:1, fp_distance_atr:0.55,
    fp_d_valid:true, fp_d_zone_id:42, fp_d_formation_time:t-45*60000, fp_d_low:81020, fp_d_high:81110, fp_d_strength_score:78,
    fp_d_strength:'Strong', fp_d_hold:76, fp_d_status:'TOUCHED_REDUCED', fp_d_context_state:'TOUCHED_REDUCED', fp_d_interacting:false,
    fp_d_session_delta:0, fp_d_waiting_confirmation:false, fp_d_breach_pending:false, fp_d_last_interaction_time:t-2*15*60000,
    fp_d_bars_since_interaction:2, fp_d_event_type:'DEFENCE_CONFIRMED', fp_d_event_score:10, fp_d_event_time:t-15*60000, fp_d_bars_since_event:1, fp_d_distance_atr:0.55,
    fp_s_valid:true, fp_s_zone_id:55, fp_s_formation_time:t-90*60000, fp_s_low:81480, fp_s_high:81570, fp_s_strength_score:81,
    fp_s_strength:'Strong', fp_s_hold:72, fp_s_status:'ACTIVE_FRESH', fp_s_context_state:'ACTIVE_FRESH', fp_s_interacting:false,
    fp_s_session_delta:0, fp_s_waiting_confirmation:false, fp_s_breach_pending:false, fp_s_last_interaction_time:null,
    fp_s_bars_since_interaction:null, fp_s_event_type:'', fp_s_event_score:0, fp_s_event_time:null, fp_s_bars_since_event:null, fp_s_distance_atr:1.4
  };
  const rawJson = JSON.stringify(p);
  upsertCurrent_(current, now, p, rawJson);
  SpreadsheetApp.flush();
}

function testWriteEdgeExecTfHandoffBTCUSD() {
  const ss = SpreadsheetApp.openById(SPREADSHEET_ID);
  const current = ss.getSheetByName(CURRENT_SHEET);
  ensureCurrentHeaders_(current);
  const now = new Date();
  const t = now.getTime();
  const p = {
    instrument:'BTCUSD', tv_ticker:'COINBASE:BTCUSD', source:'EDGE', tf:'15',
    bar_time:t, close:81280, schema_version:'SB1', event_type:'EXEC_TF_CHANGED',
    exec_tf:'60',
    edge_tracking_state:'ALERT_CHANGE_REQUIRED',
    edge_tracking_reason:'ACTIVE_ALERT_LOST_AUTHORITY',
    edge_alert_tf:'15',
    edge_required_exec_tf:'60',
    edge_previous_exec_tf:'15',
    edge_needs_alert_change:true,
    edge_handoff_time:t,
    edge_tracking_message:'EDGE execution TF changed from 15m to 1H. Update TradingView EDGE terminal alert to 1H to resume full tracking.'
  };
  const rawJson = JSON.stringify(p);
  upsertCurrent_(current, now, p, rawJson);
  SpreadsheetApp.flush();
}

function testWriteEdgeColdStartMismatchBTCUSD() {
  const ss = SpreadsheetApp.openById(SPREADSHEET_ID);
  const current = ss.getSheetByName(CURRENT_SHEET);
  ensureCurrentHeaders_(current);
  const now = new Date();
  const t = now.getTime();
  const p = {
    instrument:'BTCUSD', tv_ticker:'COINBASE:BTCUSD', source:'EDGE', tf:'3',
    bar_time:t, close:81280, schema_version:'SB1', event_type:'TF_MISMATCH',
    exec_tf:'15',
    edge_tracking_state:'ALERT_CHANGE_REQUIRED',
    edge_tracking_reason:'COLD_START_TF_MISMATCH',
    edge_alert_tf:'3',
    edge_required_exec_tf:'15',
    edge_previous_exec_tf:'',
    edge_needs_alert_change:true,
    edge_handoff_time:t,
    edge_tracking_message:'EDGE terminal alert is on 3m; required execution TF is 15m. Create or enable the 15m EDGE terminal alert to resume full tracking.'
  };
  const rawJson = JSON.stringify(p);
  upsertCurrent_(current, now, p, rawJson);
  SpreadsheetApp.flush();
}

function tfLabel_(tf) {
  const s = clean_(tf);
  if (!s) return '';
  if (s === '60') return '1H';
  if (s === '120') return '2H';
  if (s === '180') return '3H';
  if (s === '240') return '4H';
  if (s === 'D') return '1D';
  if (s === 'W') return '1W';
  if (/^\d+$/.test(s)) return s + 'm';
  return s;
}

function jsonResponse_(obj) {
  return ContentService.createTextOutput(JSON.stringify(obj))
    .setMimeType(ContentService.MimeType.JSON);
}

function clean_(v) {
  return v === undefined || v === null ? '' : String(v).trim();
}

function value_(v) {
  if (v === undefined || v === null || v === '') return '';
  return v;
}

function numberOrBlank_(v) {
  if (v === undefined || v === null || v === '') return '';
  const n = Number(v);
  return Number.isFinite(n) ? n : '';
}

function boolOrBlank_(v) {
  if (v === undefined || v === null || v === '') return '';
  if (v === true || v === 'true' || v === 1 || v === '1') return true;
  if (v === false || v === 'false' || v === 0 || v === '0') return false;
  return '';
}

function toNum_(v) {
  if (v === undefined || v === null || v === '') return NaN;
  const n = Number(v);
  return Number.isFinite(n) ? n : NaN;
}

function blankIfNaN_(v) {
  return Number.isFinite(v) ? v : '';
}

function nearlyEqual_(a, b) {
  const x = Number(a), y = Number(b);
  if (!Number.isFinite(x) || !Number.isFinite(y)) return false;
  return Math.abs(x - y) < 0.5;
}
