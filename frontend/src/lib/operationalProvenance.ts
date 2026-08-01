/**
 * M-FLEET-2 — the single provenance gate for ordinary operator surfaces.
 *
 * THE DEFECT THIS EXISTS TO PREVENT
 *   `/api/operations/*` looks authoritative and is not, by itself. The
 *   projection stamps every record with the provenance of the adapter that
 *   produced it, and under the development-default MOCK adapter that is
 *   `mock-fixture` — carrying the SAME invented balance (100000 / 100412) and
 *   the same fixture account fingerprint that `/api/fleet` serves.
 *
 *   So "switch the UI from /api/fleet to /api/operations/accounts" would not
 *   remove the fabricated figures. It would launder them: identical numbers,
 *   now stripped of the FIXTURE badge and wrapped in a `freshness: ok`
 *   envelope that makes them look MORE trustworthy. Endpoint identity is not
 *   provenance.
 *
 * THE RULE
 *   An ordinary operator surface renders a record only if that record's own
 *   provenance is authoritative. Today exactly one value qualifies: `live_mt5`.
 *   Everything else — mock-fixture, absent, or anything unrecognised — is
 *   DROPPED. Unknown fails closed: a provenance value this file has never heard
 *   of is treated as untrusted, not waved through.
 *
 * WHY ONE MODULE
 *   Thirteen components consumed fleet data. Thirteen ad-hoc string comparisons
 *   would be thirteen chances to get it wrong, and one missed check is one
 *   invented balance on an operator's screen. The comparison happens here, once.
 */

/** Values the backend's `operational_projection` can stamp. Keep in sync with
 *  `PROV_LIVE_MT5` / `PROV_MOCK_FIXTURE` / `PROV_ABSENT` in that module. */
export const PROV_LIVE_MT5 = 'live_mt5';
export const PROV_MOCK_FIXTURE = 'mock-fixture';
export const PROV_ABSENT = 'absent';

/** The ONLY provenance an ordinary operator surface may render. */
export const AUTHORITATIVE_PROVENANCE = new Set<string>([PROV_LIVE_MT5]);

/**
 * Four distinct outcomes that must never collapse into one another:
 *   available   — authoritative records exist and are being rendered
 *   empty       — the authoritative source answered, and has nothing (a FACT)
 *   unavailable — no authoritative source answered (NOT a fact about the fleet)
 *   stale       — authoritative records exist but are older than their budget
 */
export type OperationalStatus = 'available' | 'empty' | 'unavailable' | 'stale';

export interface Freshness {
  available?: boolean;
  stale?: boolean;
  ageSeconds?: number | null;
  status?: string;
  sourceAt?: string | null;
}

export interface ProvenancedRecord {
  provenance?: string;
  freshness?: Freshness | null;
}

/** True only for a record this application is willing to call operational. */
export function isAuthoritative(record: ProvenancedRecord | null | undefined): boolean {
  if (!record || typeof record.provenance !== 'string') return false;   // fail closed
  return AUTHORITATIVE_PROVENANCE.has(record.provenance);
}

/**
 * Keep authoritative records; drop everything else.
 *
 * Note what this deliberately does NOT do: it never *merges* authoritative and
 * non-authoritative records, and it never rewrites a dropped record into a
 * placeholder with zeroed fields. A dropped record leaves no trace, because a
 * trace shaped like a record is how invented data gets back onto a screen.
 */
export function authoritativeOnly<T extends ProvenancedRecord>(
  records: readonly T[] | null | undefined
): T[] {
  if (!Array.isArray(records)) return [];
  return records.filter(isAuthoritative);
}

/**
 * Classify a filtered result.
 *
 * `sourceAnswered` is the crux. When the endpoint failed, or every record was
 * rejected as non-authoritative, the honest answer is `unavailable` — this
 * application has no operational view. Reporting `empty` there would assert
 * "there are no accounts", which is a claim about the world we cannot support.
 */
export function classify<T extends ProvenancedRecord>(
  authoritative: readonly T[],
  { sourceAnswered, rejectedCount = 0 }: { sourceAnswered: boolean; rejectedCount?: number }
): OperationalStatus {
  if (!sourceAnswered) return 'unavailable';
  if (authoritative.length === 0) {
    // The source answered but nothing survived the gate: we have no operational
    // view, and saying "none exist" would be a fabrication of a different kind.
    return rejectedCount > 0 ? 'unavailable' : 'empty';
  }
  return authoritative.every((r) => r.freshness?.stale === true) ? 'stale' : 'available';
}

/**
 * A numeric field that may legitimately be absent.
 *
 * The projection already uses `null` to mean "not derivable from the evidence"
 * (see `realizedPnLToday`, `openRisk`). That distinction must survive all the
 * way to the pixel: `0` is a measurement, `null` is the absence of one, and a
 * renderer that coerces the second into the first invents a fact.
 */
export function numericOrNull(value: unknown): number | null {
  return typeof value === 'number' && Number.isFinite(value) ? value : null;
}

/** Human-readable reason for an unavailable state — names the missing source. */
export const UNAVAILABLE_DETAIL =
  'No authoritative operational source is reporting. The active broker adapter is ' +
  'not a live MT5 connection, so no deployment, broker or account records can be ' +
  'presented as operational truth.';

/**
 * M-TRADES-1 — why the durable trade ledger is NOT admitted here.
 *
 * `/api/ledger/*` reports `provenance: "durable-store"`. That names where the
 * record is KEPT, not where it came from: the same store holds trades ingested
 * from the mock adapter. Storage durability is not evidence of origin, so a
 * `durable-store` record cannot be shown as operational truth without inferring
 * authority from the endpoint — the exact mistake M-FLEET-2 exists to prevent.
 *
 * MISSING CONTRACT: the ledger must state the ORIGIN of each entry (which
 * adapter produced the fill) alongside its storage class. Until it does, ledger
 * history is reported as unavailable rather than rendered. This is a deliberate
 * gap, not an oversight.
 */
export const PROV_DURABLE_STORE = 'durable-store';

/** Reason text for trade surfaces with no admissible source. */
export const TRADES_UNAVAILABLE_DETAIL =
  'No authoritative operational source is reporting positions or orders. The active ' +
  'broker adapter is not a live MT5 connection, so no trade, order or execution ' +
  'record can be presented as operational truth.';

/**
 * Guard for derived performance figures.
 *
 * `computeMetrics([])` returns a mathematically valid report — 0 trades, 0% win
 * rate, $0 expectancy — that reads as an OBSERVED flat performance. When the
 * input list is empty because the source was unavailable or every record was
 * rejected, that report is a fabrication. Analytics must ask this first.
 */
export function analyticsInputAdmissible(status: OperationalStatus): boolean {
  return status === 'available' || status === 'stale';
}

/**
 * M-PKG-1 — there is no package registry.
 *
 * Every package, version, hash, promotion date and policy matrix on the
 * operator surfaces came from the development fixture. No registry module
 * exists in the backend; it was never built. The honest answer is unavailable,
 * and it must stay unavailable: deriving a "version" from configuration would
 * assert that a specific strategy build is deployed and governing decisions,
 * which nothing in this system can currently support.
 */
export const PACKAGES_UNAVAILABLE_DETAIL =
  'No strategy-package registry exists. Package identity, version, hash, promotion ' +
  'history and policy matrices have no authoritative source, so no package state ' +
  'can be reported. This is a missing capability, not a missing connection.';

/**
 * M-REC-1 — the durable recommendation store is a genuine authority.
 *
 * Unlike `/api/ledger/*` (rejected in M-TRADES-1 because `durable-store` names
 * storage, not origin), the trade-recommendation store has a verified
 * ingestion path: records enter only through operator action, and
 * `recommendation_store.py` contains no fixture read of any kind. Durable and
 * fixture recommendations have therefore never been able to mix, which is why
 * this domain can be migrated rather than merely emptied.
 *
 * Note the asymmetry deliberately: storage class alone still proves nothing.
 * What admits this store is the audited absence of any fixture path INTO it.
 */
export const RECOMMENDATIONS_UNAVAILABLE_DETAIL =
  'The durable recommendation store is not reporting. No recommendation, decision ' +
  'or evidence can be shown.';
