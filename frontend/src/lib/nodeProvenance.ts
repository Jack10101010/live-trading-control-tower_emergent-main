/**
 * M-NODE-READ-1 — the admission gate for NODE OPERATIONAL truth.
 *
 * THE DEFECT THIS EXISTS TO FIX
 *   `NodeOperationalView` carries provenance `node-telemetry`. Every ordinary
 *   surface filtered nodes through `authoritativeOnly` — the BROKER gate, which
 *   admits `live_mt5` and `node_mt5` and nothing else. `node-telemetry` is
 *   correctly not in that set, so every node view was silently dropped.
 *
 *   With a VPS node publishing validated telemetry every cycle, Fleet Overview
 *   said "No authoritative operational source", the ScopeNavigator showed "—"
 *   nodes, and the operational dashboard reported none. Real data, arriving,
 *   validated, stored — and shown nowhere.
 *
 *   M-PROVENANCE-FINAL established that understating real data is a defect of
 *   the same family as overstating invented data. This was the largest
 *   remaining instance.
 *
 * WHY A SEPARATE GATE RATHER THAN A WIDER ONE
 *   Adding `node-telemetry` to `AUTHORITATIVE_PROVENANCE` would have been one
 *   line, and it would have been wrong. That set answers "may this record be
 *   shown as BROKER truth?" — and it guards balances, equity, positions, orders
 *   and analytics admission. A node publishing successfully says nothing about
 *   whether its MT5 terminal is readable; widening the broker gate would have
 *   let a heartbeat imply an account.
 *
 *   The two authorities are independent in BOTH directions:
 *     node health          grants nothing about account availability
 *     account availability grants nothing about node health
 *
 *   The function names are deliberately long and unlike `isAuthoritative`, so
 *   the two can never be swapped by autocomplete in a hurry.
 */

/** Values the backend's `node_provenance` seam can stamp. Keep in sync. */
export const PROV_NODE_TELEMETRY = 'node-telemetry';
export const PROV_FIXTURE_NODE = 'fixture-node';
export const PROV_NODE_ABSENT = 'absent';

/** The ONLY provenance admissible as node operational truth. */
export const AUTHORITATIVE_NODE_PROVENANCE = new Set<string>([PROV_NODE_TELEMETRY]);

/**
 * Node lifecycle, decided by the backend from the node's OWN reported
 * conditions. The frontend renders these; it never derives them.
 */
export type NodeLifecycle = 'absent' | 'current' | 'stale' | 'degraded';

export interface NodeRecord {
  provenance?: string;
  lifecycleState?: string;
  degradedReasons?: string[];
  freshness?: { stale?: boolean; available?: boolean } | null;
}

/** True only for a record admissible as NODE truth. Fails closed. */
export function isAuthoritativeNodeObservation(
  record: NodeRecord | null | undefined
): boolean {
  if (!record || typeof record.provenance !== 'string') return false;
  return AUTHORITATIVE_NODE_PROVENANCE.has(record.provenance);
}

/** Keep admissible node records; drop everything else, leaving no placeholder. */
export function authoritativeNodesOnly<T extends NodeRecord>(
  records: readonly T[] | null | undefined
): T[] {
  if (!Array.isArray(records)) return [];
  return records.filter(isAuthoritativeNodeObservation);
}

/**
 * The eight states a node surface must be able to tell apart.
 *
 * `absent`, `stale`, `degraded` and "operational but broker unknown" are FOUR
 * DIFFERENT FACTS and must never collapse into one empty state — that collapse
 * is what made a reporting node indistinguishable from no node at all.
 *
 * `stopped` is deliberately NOT in this union. `ct.node-telemetry.v1` carries no
 * stopped signal: a node that halts simply stops publishing, which is
 * indistinguishable from one that cannot reach this Control Tower. Inventing
 * `stopped` from silence would assert a halt on the evidence of a network
 * problem. See `docs/governance/NODE-AUTHORITY.md`.
 */
export type NodeObservationStatus = 'absent' | 'current' | 'stale' | 'degraded';

/**
 * Classify the admitted node set.
 *
 * `sourceAnswered` distinguishes "the endpoint failed" from "the endpoint said
 * no node has reported". Both render as absence, but only one of them is a fact
 * about the fleet, and the copy differs accordingly.
 */
export function classifyNodeObservation<T extends NodeRecord>(
  admitted: readonly T[],
  { sourceAnswered }: { sourceAnswered: boolean }
): NodeObservationStatus {
  if (!sourceAnswered || admitted.length === 0) return 'absent';
  // Worst-first across the fleet: one degraded node must never be hidden behind
  // a healthy one, and one stale node must not be hidden behind a current one.
  if (admitted.some((n) => n.lifecycleState === 'degraded')) return 'degraded';
  if (admitted.every((n) => n.lifecycleState === 'stale')) return 'stale';
  if (admitted.some((n) => n.lifecycleState === 'stale')) return 'stale';
  return 'current';
}

/**
 * Panel tone for a node card, under the M-PROVENANCE-FINAL policy.
 *
 * Every admitted node is `live`: it is genuine data with a real source. A stale
 * node is real data that is old, and a degraded node is real data about real
 * trouble — neither is fixture data, so neither may take the red border that
 * means "this is not operational data". The staleness and the failure are shown
 * as warnings ON a live card, which is where an operator will actually read
 * them.
 */
export function nodeCardProvenance(record: NodeRecord): 'live' | 'placeholder' {
  return isAuthoritativeNodeObservation(record) ? 'live' : 'placeholder';
}

/** Reason text when no node has been observed at all. */
export const NODE_ABSENT_DETAIL =
  'No execution node has published telemetry to this Control Tower. That is not ' +
  'evidence that a node has stopped — a node keeps trading and protecting the ' +
  'account while unable to reach this tower — so no node state is claimed here.';
