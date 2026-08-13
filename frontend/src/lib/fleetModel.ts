/**
 * M-CT-FLEET-DASHBOARD-2 — the Fleet dashboard's selectors.
 *
 * EVERY VERDICT HERE IS THE NODE'S. This module routes and labels; it does not
 * decide. Re-deriving readiness, liveness or reconciliation Mac-side is the
 * L_2106 failure — a dashboard that computes its own answer will eventually
 * disagree with the rails that actually execute, and the dashboard will be
 * believed. So: `executionReadiness` comes from `runtime.execution_readiness`,
 * liveness from the heartbeat and nothing else, delivery from the node's outbox
 * block, and `clean: null` stays UNKNOWN.
 *
 * FIVE INDEPENDENT CLOCKS, never substituted for one another:
 *
 *   heartbeat      is the process alive          ~20s cadence
 *   runtime        how old is the node's view    phase-aware
 *   complete       last finished strategy cycle  ~25min recomputes
 *   readiness      when the rails were evaluated rides the runtime snapshot
 *   delivery       transport health              ALSO rides the runtime snapshot
 *
 * That last one matters and is easy to get wrong: the node's `delivery` block is
 * embedded in the runtime snapshot, so its `at` equals that snapshot's
 * `published_at`. During a recompute it is minutes old. Rendering it as current
 * would report transport health the node last measured 25 minutes ago.
 */

import { SIMULATED_MARKERS, BROKER_ORIGINS, MOCK_ACCOUNT_PREFIX } from '@/lib/operationalProvenance';

export type Sev = 'ok' | 'info' | 'warn' | 'bad' | 'unknown';

/** Red is reserved for "an operator must act". */
export const SEV_TONE: Record<Sev, 'green' | 'blue' | 'amber' | 'red' | 'neutral'> = {
  ok: 'green',
  info: 'blue',      // deliberate work in progress
  warn: 'amber',
  bad: 'red',
  unknown: 'amber',  // absence is not health — but it is not a fault either
};

export interface FleetNodeView {
  instanceId: string;
  heartbeat?: { available?: boolean; status?: string; ageSeconds?: number | null;
                phase?: string | null; boundary?: string | null; uptimeSeconds?: number | null };
  current?: { cycleStatus?: string | null; cycleNote?: string | null; strategyBlocksPresent?: boolean };
  lastComplete?: { available?: boolean; ageSeconds?: number | null; boundary?: string | null;
                   isCurrent?: boolean; news?: any; decisions?: any };
  delivery?: { healthy?: boolean; ageSeconds?: number | null };
  node?: Record<string, any>;
}

/* ── A · NODE LIVENESS — heartbeat only ─────────────────────────────────── */

export function livenessSeverity(v: FleetNodeView): Sev {
  const s = v?.heartbeat?.status;
  if (!v?.heartbeat?.available || !s) return 'unknown';
  if (s === 'live') return 'ok';
  if (s === 'degraded') return 'warn';
  return 'bad';                       // offline — the node is genuinely gone
}
export const LIVENESS_LABEL: Record<string, string> = {
  live: 'LIVE', degraded: 'DEGRADED', offline: 'OFFLINE', unknown: 'UNKNOWN',
};

/* ── B · RUNTIME — the runtime snapshot only ────────────────────────────── */

export function runtimeSeverity(v: FleetNodeView, staleAfterS = 900): Sev {
  const age = v?.node?.snapshotReceivedAt ? ageOf(v.node.snapshotReceivedAt) : null;
  if (age == null) return 'unknown';
  if (age <= staleAfterS) return 'ok';
  return 'warn';   // NOT red: a live heartbeat means the node is fine, just quiet
}

/* ── C · STRATEGY CYCLE — the complete snapshot only ────────────────────── */

export const CYCLE_DELAYED_AFTER_S = 1800;

export function cycleSeverity(v: FleetNodeView): Sev {
  const lc = v?.lastComplete;
  const status = v?.current?.cycleStatus;
  if (status === 'frozen' || status === 'error') return 'bad';
  if (!lc?.available) return 'unknown';
  if (lc.isCurrent) return 'ok';
  const age = lc.ageSeconds;
  if (typeof age === 'number' && age > CYCLE_DELAYED_AFTER_S) return 'warn';
  return 'info';                      // recomputing — deliberate work, not a fault
}

/* ── D · EXECUTION READINESS — the node's rails, verbatim ───────────────── */

export interface ReadinessView {
  severity: Sev;
  label: string;
  /** Present only when the node says blocked. Its words, not ours. */
  reasons: string[];
  checks: Array<{ name: string; ok: boolean; detail: string }>;
  candidateNotEvaluated: Array<{ name: string; why: string }>;
  evaluatedAt: string | null;
  available: boolean;
}

export function readinessView(v: FleetNodeView): ReadinessView {
  const r = v?.node?.executionReadiness;
  if (!r || typeof r !== 'object') {
    // Never synthesise green from absence.
    return { severity: 'unknown', label: 'READINESS UNKNOWN', reasons: [], checks: [],
             candidateNotEvaluated: [], evaluatedAt: null, available: false };
  }
  const checks = Object.entries(r.checks ?? {}).map(([name, c]: [string, any]) => ({
    name, ok: Boolean(c?.ok), detail: String(c?.detail ?? ''),
  }));
  const candidateNotEvaluated = Object.entries(r.candidate_checks_not_evaluated ?? {})
    .map(([name, why]) => ({ name, why: String(why) }));
  const blocked = r.status === 'blocked' || r.eligible === false;
  return {
    // "READY" is deliberately qualified: the node cannot evaluate duplicate_intent
    // or stale_open without a real candidate, so an unconditional claim that any
    // hypothetical trade would pass would be false.
    severity: blocked ? 'bad' : r.status === 'ready' ? 'ok' : 'unknown',
    label: blocked ? 'OPEN BLOCKED'
         : r.status === 'ready' ? 'READY' : String(r.status ?? 'unknown').toUpperCase(),
    reasons: Array.isArray(r.reasons) ? r.reasons.map(String) : [],
    checks, candidateNotEvaluated,
    evaluatedAt: r.evaluated_at ?? null,
    available: true,
  };
}

/* ── E · CT DELIVERY — node outbox authority only ───────────────────────── */

/**
 * How current the delivery AUTHORITY must be before its verdict may be asserted.
 *
 * The delivery block rides the runtime snapshot, so during a recompute it can be
 * 20+ minutes old. A 19-minute-old report saying "heartbeat pending" describes a
 * moment that has already passed — asserting CT DELIVERY DEGRADED from it raises
 * a current alarm from historical evidence. Two runtime publish intervals is the
 * point past which the report can no longer speak for now.
 */
export const DELIVERY_AUTHORITY_FRESH_S = 180;

export interface DeliveryView {
  severity: Sev;
  /** What to show as the state. */
  label: string;
  /** True when the authority is too old to speak for the present. */
  aged: boolean;
  /** The node's last reported facts — historical when `aged`. */
  reported: string | null;
  ageSeconds: number | null;
}

/**
 * CT DELIVERY, with the authority's own age folded into its epistemic status.
 *
 * AGE IS PART OF THE VERDICT. A stale report cannot assert a current fault, and
 * it cannot assert current health either — it is simply no longer evidence about
 * now. Its facts are preserved as history rather than discarded.
 *
 * Deliberately NOT inferred from heartbeat success: a beat proves the node
 * process is alive, which says nothing about whether its outbox is draining.
 * Substituting one for the other is the same class of error as the freshness
 * split-authority incident.
 */
export function deliveryView(v: FleetNodeView): DeliveryView {
  const d = v?.node?.nodeDelivery;
  const age = ageOf(v?.node?.deliveryAt);
  const pend = pendingSlots(v);
  const reported = d
    ? `${String(d.ct_delivery ?? 'unknown')}${pend.length ? ` · pending: ${pend.join(', ')}` : ''}`
    : null;
  if (!d) {
    return { severity: 'unknown', label: 'UNKNOWN', aged: false, reported: null, ageSeconds: age };
  }
  if (age != null && age > DELIVERY_AUTHORITY_FRESH_S) {
    // Too old to speak for now — in EITHER direction.
    return { severity: 'unknown', label: 'STATUS AGED', aged: true, reported, ageSeconds: age };
  }
  const s = d.ct_delivery;
  const severity: Sev = s === 'healthy' ? 'ok' : s === 'degraded' ? 'warn'
                      : s === 'offline' ? 'bad' : 'unknown';
  return { severity, label: String(s ?? 'unknown').toUpperCase(), aged: false, reported, ageSeconds: age };
}

/** Back-compat thin wrapper. Prefer `deliveryView`. */
export function deliverySeverity(v: FleetNodeView): Sev {
  return deliveryView(v).severity;
}

/** Which slots the node last reported as still pending. Its words. */
export function pendingSlots(v: FleetNodeView): string[] {
  const d = v?.node?.nodeDelivery ?? {};
  return (['runtime', 'complete', 'heartbeat'] as const)
    .filter((k) => d[`pending_${k}`] === true);
}

/* ── F · RECONCILIATION — tri-state, `null` stays UNKNOWN ───────────────── */

export function reconciliationView(v: FleetNodeView): { severity: Sev; label: string } {
  const rc = v?.node?.reconciliation;
  if (!rc || rc.available !== true) return { severity: 'unknown', label: 'UNKNOWN' };
  if (rc.frozen === true) return { severity: 'bad', label: 'FROZEN' };
  if (rc.recovery_required === true) return { severity: 'bad', label: 'RECOVERY REQUIRED' };
  // `clean: null` means the node has evidence but cannot conclude. It is NOT a
  // pass. Collapsing it to PASS is how a reconciliation fault gets a green badge.
  if (rc.clean === null || rc.clean === undefined) return { severity: 'unknown', label: 'UNKNOWN' };
  return rc.clean ? { severity: 'ok', label: 'CLEAN' } : { severity: 'bad', label: 'NOT CLEAN' };
}

/* ── Attention rail — only things needing action ────────────────────────── */

export interface Attention { sev: Sev; title: string; detail: string }

export function attentionItems(v: FleetNodeView): Attention[] {
  const out: Attention[] = [];
  const rd = readinessView(v);
  if (rd.severity === 'bad') {
    out.push({ sev: 'bad', title: 'Execution blocked',
               detail: rd.reasons.join(', ') || 'node reports OPEN blocked' });
  }
  const live = livenessSeverity(v);
  if (live === 'bad') {
    out.push({ sev: 'bad', title: 'Node offline',
               detail: `no heartbeat for ${humanAge(v.heartbeat?.ageSeconds) ?? 'an unknown time'}` });
  } else if (live === 'warn') {
    out.push({ sev: 'warn', title: 'Node heartbeat degraded',
               detail: `last beat ${humanAge(v.heartbeat?.ageSeconds) ?? '?'} ago` });
  }
  const rc = reconciliationView(v);
  if (rc.severity === 'bad') {
    out.push({ sev: 'bad', title: 'Reconciliation', detail: rc.label });
  }
  const dv = deliveryView(v);
  if (dv.aged) {
    // NOT a transport-failure assertion. The tower cannot currently tell.
    out.push({ sev: 'warn', title: 'CT delivery status aged',
               detail: `last report ${humanAge(dv.ageSeconds)} ago (${dv.reported}) — current transport state unknown` });
  } else if (dv.severity === 'warn' || dv.severity === 'bad') {
    out.push({ sev: dv.severity, title: 'CT delivery degraded',
               detail: `${dv.reported} — node trading unaffected` });
  }
  if (cycleSeverity(v) === 'warn') {
    out.push({ sev: 'warn', title: 'Strategy cycle delayed',
               detail: `last complete ${humanAge(v.lastComplete?.ageSeconds) ?? '?'} ago` });
  }
  if (v?.node?.killSwitchActive === true) {
    out.push({ sev: 'bad', title: 'Kill switch ACTIVE', detail: 'new opens are refused' });
  }
  return out;
}

/* ── helpers ────────────────────────────────────────────────────────────── */

export function ageOf(iso: string | null | undefined, now: Date = new Date()): number | null {
  if (!iso) return null;
  const t = Date.parse(String(iso).replace(' ', 'T'));
  if (!Number.isFinite(t)) return null;
  return Math.abs((now.getTime() - t) / 1000);
}

export function humanAge(seconds: number | null | undefined): string | null {
  if (typeof seconds !== 'number' || !Number.isFinite(seconds)) return null;
  const s = Math.max(0, Math.round(seconds));
  if (s < 90) return `${s}s`;
  const m = Math.round(s / 60);
  if (m < 90) return `${m}m`;
  const h = Math.floor(m / 60);
  return `${h}h ${m % 60}m`;
}

/** `ct.node-decisions.v1` ships absence as the STRING "nan". */
export function clean(value: unknown): string | null {
  if (value == null) return null;
  const s = String(value).trim();
  if (!s || s.toLowerCase() === 'nan' || s.toLowerCase() === 'none') return null;
  return s;
}

/** Fleet health = worst node, and it must NAME the node rather than collapse. */
export function fleetHealth(nodes: FleetNodeView[]): { sev: Sev; detail: string } {
  if (!nodes.length) return { sev: 'unknown', detail: 'no nodes reporting' };
  const rank: Sev[] = ['bad', 'warn', 'unknown', 'info', 'ok'];
  let worst: { sev: Sev; id: string } = { sev: 'ok', id: nodes[0].instanceId };
  for (const n of nodes) {
    for (const s of [livenessSeverity(n), readinessView(n).severity, reconciliationView(n).severity]) {
      if (rank.indexOf(s) < rank.indexOf(worst.sev)) worst = { sev: s, id: n.instanceId };
    }
  }
  return { sev: worst.sev,
           detail: worst.sev === 'ok' ? `${nodes.length} node${nodes.length === 1 ? '' : 's'} healthy`
                                      : `${worst.id}` };
}

/** Never render an account login. Fingerprints are safe; numbers are not. */
export function safeAccountLabel(account: any): string {
  const fp = account?.identity?.fingerprint;
  return typeof fp === 'string' && fp ? `${fp.slice(0, 12)}…${fp.slice(-4)}` : 'unidentified';
}

/**
 * Is this ledger record a trade the BROKER actually executed?
 *
 * The local mock adapter writes into the same durable ledger, and its records
 * are indistinguishable at a glance: same shape, same store, `provenance:
 * "durable-store"` at the top level. The truth is one level down — the local
 * fixture carries `executionOrigin: "mock"`, `lineage.adapter: "mock"`,
 * lineage.broker carries the mock-adapter marker, and the account fingerprint
 * uses the mock prefix. The markers themselves live in operationalProvenance.
 *
 * Counting those as broker trades is exactly the confusion the brief forbids:
 * a simulated trade must never look broker-executed. Fails CLOSED — anything
 * that cannot be shown to be genuine is not counted.
 */
export function isBrokerExecuted(trade: any): boolean {
  if (!trade || typeof trade !== 'object') return false;
  if (isSimulated(trade)) return false;
  const origin = String(trade.executionOrigin ?? '').toLowerCase();
  return (BROKER_ORIGINS as readonly string[]).includes(origin);
}

/** Is this record POSITIVELY identifiable as simulated/mock/fixture/replay? */
export function isSimulated(trade: any): boolean {
  if (!trade || typeof trade !== 'object') return false;
  const markers = (SIMULATED_MARKERS as readonly string[]).map((m) => m.toLowerCase());
  const origin = String(trade.executionOrigin ?? '').toLowerCase();
  if (markers.includes(origin)) return true;
  const lineage = trade.detail?.trade?.lineage ?? trade.lineage ?? {};
  for (const val of [lineage.adapter, lineage.broker, trade.detail?.trade?.provenance]) {
    const x = String(val ?? '').toLowerCase();
    if (markers.some((m) => x.includes(m))) return true;
  }
  return String(lineage.accountFingerprint ?? '').toLowerCase().startsWith(MOCK_ACCOUNT_PREFIX);
}

/**
 * Three buckets, because two would force a guess.
 *
 * A record that is neither provably broker-executed nor provably simulated is
 * UNVERIFIED — it does not belong in `broker` (that would overstate) and it does
 * not belong in `simulated` (that would understate). Unknown provenance gets its
 * own bucket and is never counted as broker-executed.
 */
export function splitLedger(trades: any[]): { broker: any[]; simulated: any[]; unverified: any[] } {
  const broker: any[] = [], simulated: any[] = [], unverified: any[] = [];
  for (const t of trades ?? []) {
    if (isBrokerExecuted(t)) broker.push(t);
    else if (isSimulated(t)) simulated.push(t);
    else unverified.push(t);
  }
  return { broker, simulated, unverified };
}

/**
 * Lifecycle presentation for a decision record.
 *
 * `action: "PENDING"` alongside `never_triggered` or `invalidated_before_edge_entry`
 * is a TERMINAL historical outcome, not a resting order. Rendering it as "PENDING"
 * implies something is live at the broker right now, which is false. The reason
 * field is the authority on whether the lifecycle ended.
 */
const TERMINAL_REASONS = new Set([
  'never_triggered', 'invalidated_before_edge_entry', 'invalidated_before_fill',
  'state_target_block', 'reverse_touch_cancel', 'news_touch_cancel',
]);

export function lifecycleOf(rec: any): { label: string; sev: Sev; terminal: boolean } {
  const action = clean(rec?.action) ?? '';
  const reason = clean(rec?.refusal_reason) ?? clean(rec?.cancel_reason) ?? '';
  const outcome = clean(rec?.outcome) ?? '';
  if (action === 'REFUSED' || rec?.eligible === false) {
    return { label: 'REFUSED', sev: 'warn', terminal: true };
  }
  if (outcome === 'WIN' || outcome === 'LOSS' || clean(rec?.fill_time)) {
    return { label: 'FILLED', sev: 'ok', terminal: true };
  }
  if (TERMINAL_REASONS.has(reason) || TERMINAL_REASONS.has(outcome.toLowerCase())) {
    // Terminal — deliberately NOT "PENDING".
    return { label: 'CLOSED · NO FILL', sev: 'info', terminal: true };
  }
  if (action === 'PENDING') return { label: 'PENDING', sev: 'info', terminal: false };
  return { label: action || outcome || 'UNKNOWN', sev: 'unknown', terminal: false };
}

/** Newest first, by the authoritative fill/detection instant. */
export function sortNewestFirst(records: any[]): any[] {
  return [...(records ?? [])].sort((a, b) => {
    const ta = Date.parse(String(clean(a?.utc) ?? clean(a?.detection_time) ?? '').replace(' ', 'T'));
    const tb = Date.parse(String(clean(b?.utc) ?? clean(b?.detection_time) ?? '').replace(' ', 'T'));
    if (!Number.isFinite(ta) && !Number.isFinite(tb)) return 0;
    if (!Number.isFinite(ta)) return 1;
    if (!Number.isFinite(tb)) return -1;
    return tb - ta;
  });
}
