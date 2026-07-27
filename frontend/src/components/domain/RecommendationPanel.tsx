/**
 * LIVE-4D — the read-only Recommendation section.
 *
 * A Recommendation is a PROPOSAL to act on a Scenario. This panel renders the
 * proposal, its decision history and its lineage exactly as the backend
 * recorded them:
 *
 *   - NO lifecycle reconstruction here: status, outcome and past-due all come
 *     from the projection;
 *   - NO authorization logic and NO financial calculation of any kind;
 *   - NO broker controls and NO decision buttons — there is no authenticated
 *     decision surface in this slice, so the UI offers no way to mutate;
 *   - unavailable proposal terms render as "—", never as 0.
 */
import { useMemo, useState, type CSSProperties } from 'react';
import { useQuery } from '@tanstack/react-query';
import {
  api,
  type RecommendationOperationalView,
} from '@/lib/api';
import {
  DECISION_NOTICE,
  RecommendationDetail,
} from '@/components/domain/RecommendationDetail';

const BADGE = 'text-2xs mono px-1.5 py-0.5 rounded-sm border';
const C = {
  ok: { borderColor: 'var(--positive)', color: 'var(--positive)' },
  bad: { borderColor: 'var(--negative)', color: 'var(--negative)' },
  warn: { borderColor: 'var(--caution, var(--warning))', color: 'var(--caution, var(--warning))' },
  muted: { borderColor: 'var(--border-subtle)', color: 'var(--text-muted)' },
} as const;

/* ── shared formatting (the only transformation in this panel) ────────────── */

function price(v: number | null | undefined): string {
  return v === null || v === undefined ? '—' : v.toFixed(5);
}
function num(v: number | null | undefined, digits = 2, suffix = ''): string {
  return v === null || v === undefined ? '—' : `${v.toFixed(digits)}${suffix}`;
}
function txt(v: string | null | undefined): string {
  return v === null || v === undefined || v === '' ? '—' : v;
}
function age(seconds: number | null | undefined): string {
  if (seconds === null || seconds === undefined) return '—';
  if (seconds < 60) return `${Math.round(seconds)}s`;
  if (seconds < 3600) return `${Math.round(seconds / 60)}m`;
  return `${(seconds / 3600).toFixed(1)}h`;
}

/** Status → badge. Driven entirely by the projected status. */
const STATUS_STYLE: Record<string, CSSProperties> = {
  PROPOSED: C.muted, PENDING_DECISION: C.warn, ACCEPTED: C.ok,
  REJECTED: C.bad, EXPIRED: C.muted, WITHDRAWN: C.muted, SUPERSEDED: C.muted,
  INTENT_CREATED: C.ok, PARTIALLY_EXECUTED: C.warn, EXECUTED: C.ok,
  FAILED: C.bad, CANCELLED: C.muted, DRAFT: C.muted,
};

function StatusBadges({ item }: { item: RecommendationOperationalView }) {
  const badges: Array<[string, CSSProperties]> = [];
  if (item.status) {
    badges.push([item.status.replace(/_/g, ' '), STATUS_STYLE[item.status] ?? C.muted]);
  }
  if (item.warnings.some((w) => w.startsWith('scenario_unavailable'))) {
    badges.push(['SCENARIO UNAVAILABLE', C.bad]);
  }
  if (item.linkedIntentCount === 0) badges.push(['INTENT UNLINKED', C.muted]);
  if (item.warnings.some((w) => w.includes('conflict') || w.includes('differs'))) {
    badges.push(['CONFLICTED', C.bad]);
  }
  if (item.pastDue) badges.push(['PAST DUE', C.warn]);
  return (
    <span className="flex flex-wrap gap-1" data-testid="recommendation-badges">
      {badges.map(([label, style]) => (
        <span key={label} className={BADGE} style={style}>{label}</span>
      ))}
    </span>
  );
}

function Row({ label, value }: { label: string; value: string }) {
  return (
    <div className="flex items-baseline justify-between gap-3 text-2xs">
      <span className="text-text-muted">{label}</span>
      <span className="mono text-text-2">{value}</span>
    </div>
  );
}

export function RecommendationPanel({ operatorId }: { operatorId?: string } = {}) {
  const [statusFilter, setStatusFilter] = useState('');
  const [instrumentFilter, setInstrumentFilter] = useState('');
  const [directionFilter, setDirectionFilter] = useState('');
  const [sourceFilter, setSourceFilter] = useState('');
  const [linkFilter, setLinkFilter] = useState('');
  const [selected, setSelected] = useState<string | null>(null);

  const { data, isLoading, isError } = useQuery({
    queryKey: ['trade-recommendations'],
    queryFn: () => api.tradeRecommendations({ limit: 50 }),
    refetchInterval: 20_000,
    staleTime: 5_000,
    retry: false,
  });
  const items = data?.recommendations ?? [];
  const filtered = useMemo(
    () => items.filter((r) =>
      (!statusFilter || r.status === statusFilter) &&
      (!instrumentFilter || r.instrument === instrumentFilter) &&
      (!directionFilter || r.direction === directionFilter) &&
      (!sourceFilter || r.source === sourceFilter) &&
      (!linkFilter ||
        (linkFilter === 'linked' ? r.linkedIntentCount > 0 : r.linkedIntentCount === 0))),
    [items, statusFilter, instrumentFilter, directionFilter, sourceFilter, linkFilter]);

  const options = useMemo(() => ({
    status: Array.from(new Set(items.map((r) => r.status).filter(Boolean))).sort() as string[],
    instrument: Array.from(new Set(items.map((r) => r.instrument).filter(Boolean))).sort() as string[],
    direction: Array.from(new Set(items.map((r) => r.direction).filter(Boolean))).sort() as string[],
    source: Array.from(new Set(items.map((r) => r.source).filter(Boolean))).sort() as string[],
  }), [items]);

  const summary = data?.summary;
  const select = 'text-2xs mono px-1.5 py-1 rounded-sm border bg-transparent';

  return (
    <section className="rounded-md border border-[color:var(--border)] bg-[color:var(--panel)] p-3"
             aria-label="Recommendations" data-testid="recommendation-panel">
      <header className="flex flex-wrap items-baseline justify-between gap-2 mb-1.5">
        <h3 className="text-xs uppercase tracking-wider text-text-muted">
          Recommendations{summary ? ` (${summary.activeCount} active)` : ''}
        </h3>
        <span className={BADGE} style={C.muted} data-testid="recommendation-scope">
          READ ONLY · PROPOSAL ONLY · ACCEPTANCE DOES NOT EXECUTE
        </span>
      </header>

      {isError && (
        <div className="text-2xs" style={{ color: 'var(--negative)' }}
             data-testid="recommendation-error">
          UNAVAILABLE — the recommendation store could not be read. Nothing is shown;
          this is not a claim that no proposals exist.
        </div>
      )}
      {isLoading && (
        <div className="text-2xs text-text-muted" data-testid="recommendation-loading">
          Loading recommendations…
        </div>
      )}

      {data && summary && (
        <>
          <div className="grid grid-cols-2 md:grid-cols-4 gap-x-4 gap-y-1 mb-2"
               data-testid="recommendation-summary">
            <Row label="Active" value={String(summary.activeCount)} />
            <Row label="Pending decision" value={String(summary.pendingDecisionCount)} />
            <Row label="Accepted" value={String(summary.acceptedCount)} />
            <Row label="Rejected" value={String(summary.rejectedCount)} />
            <Row label="Expired" value={String(summary.expiredCount)} />
            <Row label="Withdrawn" value={String(summary.withdrawnCount)} />
            <Row label="Superseded" value={String(summary.supersededCount)} />
            <Row label="Intent linked" value={String(summary.executionLinkedCount)} />
          </div>

          <div className="flex flex-wrap gap-2 mb-2" data-testid="recommendation-filters">
            {([
              ['status', statusFilter, setStatusFilter, options.status, 'All statuses'],
              ['instrument', instrumentFilter, setInstrumentFilter, options.instrument, 'All instruments'],
              ['direction', directionFilter, setDirectionFilter, options.direction, 'All directions'],
              ['source', sourceFilter, setSourceFilter, options.source, 'All sources'],
            ] as const).map(([name, value, setter, opts, label]) => (
              <select key={name} className={select} style={C.muted} value={value}
                      onChange={(e) => (setter as (v: string) => void)(e.target.value)}
                      data-testid={`recommendation-filter-${name}`} aria-label={label}>
                <option value="">{label}</option>
                {opts.map((o) => <option key={o} value={o}>{o}</option>)}
              </select>
            ))}
            <select className={select} style={C.muted} value={linkFilter}
                    onChange={(e) => setLinkFilter(e.target.value)}
                    data-testid="recommendation-filter-link" aria-label="All linkage">
              <option value="">All linkage</option>
              <option value="linked">Intent linked</option>
              <option value="unlinked">Intent unlinked</option>
            </select>
          </div>

          {filtered.length === 0 ? (
            <div className="text-2xs text-text-muted" data-testid="recommendation-empty">
              {items.length === 0
                ? 'No recommendations recorded. Proposals are created explicitly — none are generated automatically.'
                : 'No recommendations match the current filters.'}
            </div>
          ) : (
            <div className="overflow-x-auto">
              <table className="w-full text-2xs" data-testid="recommendation-table">
                <thead className="text-text-muted">
                  <tr>
                    {['Created', 'Recommendation', 'Scenario', 'Instrument', 'Dir',
                      'Source', 'Entry', 'Stop', 'Target', 'Qty', 'Risk', 'R',
                      'Decision', 'Intents', 'Age', 'Status'].map((h) => (
                      <th key={h} className="text-left font-normal pr-2 pb-1">{h}</th>
                    ))}
                  </tr>
                </thead>
                <tbody className="mono text-text-2">
                  {filtered.map((r) => (
                    <tr key={r.recommendationId} data-testid="recommendation-row"
                        onClick={() => setSelected(
                          selected === r.recommendationId ? null : r.recommendationId)}
                        style={{ cursor: 'pointer' }}>
                      <td className="pr-2">{txt(r.createdAt)}</td>
                      <td className="pr-2">{r.recommendationId.slice(0, 12)}</td>
                      <td className="pr-2">{r.scenarioId.slice(0, 12)}</td>
                      <td className="pr-2">{txt(r.instrument)}</td>
                      <td className="pr-2">{txt(r.direction)}</td>
                      <td className="pr-2">{txt(r.source)}</td>
                      <td className="pr-2">{price(r.proposedEntry)}</td>
                      <td className="pr-2">{price(r.stopLoss)}</td>
                      <td className="pr-2">{price(r.takeProfit)}</td>
                      <td className="pr-2">{num(r.quantity)}</td>
                      <td className="pr-2">{num(r.riskAmount)}</td>
                      <td className="pr-2">{r.plannedR === null ? '—' : `${r.plannedR}R`}</td>
                      <td className="pr-2">{txt(r.latestDecision?.decisionType)}</td>
                      <td className="pr-2">{r.linkedIntentCount}</td>
                      <td className="pr-2">{age(r.ageSeconds)}</td>
                      <td className="pr-2"><StatusBadges item={r} /></td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}

          {selected && filtered.some((r) => r.recommendationId === selected) && (
            /* LIVE-4E: the detail view owns the decision surface. The LIST
               above stays free of controls — a decision is only reachable
               after an operator opens and reviews the proposal. */
            <RecommendationDetail
              item={filtered.find((r) => r.recommendationId === selected)!}
              actorId={operatorId} />
          )}
        </>
      )}

      <p className="text-2xs text-text-muted mt-2">
        A Recommendation is a proposal to act on a Scenario — not an order, not an
        execution intent, and not proof a trade occurred. {DECISION_NOTICE} Execution
        still requires authorization, execution mode and the safety gates.
      </p>
    </section>
  );
}
