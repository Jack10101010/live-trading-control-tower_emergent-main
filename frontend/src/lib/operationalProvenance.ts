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
