/**
 * LIVE-4B — the Scenario section.
 *
 * Scenarios are the parent object of the trading lineage. This panel is
 * strictly READ-ONLY: it lists projected scenarios, supports filtering by
 * status / instrument / session / node, and shows each scenario's linkage
 * counts and freshness.
 *
 * There is NO editing, NO mutation and NO strategy control — the panel renders
 * exactly what the projection reports and nothing else. An unreadable scenario
 * store is shown as UNAVAILABLE rather than as "no scenarios".
 */
import { useMemo, useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { api, type ProjectionFreshness, type ScenarioOperationalView } from '@/lib/api';

const BADGE = 'text-2xs mono px-1.5 py-0.5 rounded-sm border';
const C = {
  active: { borderColor: 'var(--positive)', color: 'var(--positive)' },
  terminal: { borderColor: 'var(--text-muted)', color: 'var(--text-muted)' },
  bad: { borderColor: 'var(--negative)', color: 'var(--negative)' },
  muted: { borderColor: 'var(--border-subtle)', color: 'var(--text-muted)' },
} as const;

/** Terminal statuses, mirrored from the domain (display only). */
const TERMINAL = new Set(['COMPLETED', 'INVALIDATED', 'EXPIRED', 'CANCELLED', 'REJECTED']);

function txt(v: string | null | undefined): string {
  return v === null || v === undefined || v === '' ? '—' : v;
}
function age(seconds: number | null | undefined): string {
  if (seconds === null || seconds === undefined) return '—';
  if (seconds < 60) return `${Math.round(seconds)}s`;
  if (seconds < 3600) return `${Math.round(seconds / 60)}m`;
  return `${(seconds / 3600).toFixed(1)}h`;
}

function StatusBadge({ status }: { status: string | null }) {
  const terminal = status ? TERMINAL.has(status) : false;
  return (
    <span className={BADGE} style={terminal ? C.terminal : C.active}
          data-testid="scenario-status-badge">
      {txt(status)}
    </span>
  );
}

function FreshnessBadge({ freshness }: { freshness: ProjectionFreshness | null }) {
  if (!freshness) return null;
  if (!freshness.available) {
    return <span className={BADGE} style={C.bad} data-testid="scenario-freshness-badge">UNAVAILABLE</span>;
  }
  if (freshness.stale) {
    return <span className={BADGE} style={C.bad} data-testid="scenario-freshness-badge">STALE</span>;
  }
  return <span className={BADGE} style={C.muted} data-testid="scenario-freshness-badge">FRESH</span>;
}

export function ScenarioPanel() {
  const [status, setStatus] = useState('');
  const [instrument, setInstrument] = useState('');
  const [session, setSession] = useState('');
  const [nodeId, setNodeId] = useState('');

  const { data, isLoading, isError } = useQuery({
    queryKey: ['scenarios'],
    queryFn: () => api.scenarios(),
    refetchInterval: 15_000,
    staleTime: 5_000,
    retry: false,
  });

  const scenarios = data?.scenarios ?? [];

  // Filtering is a pure view concern over projected models — it derives no
  // operational truth of its own.
  const filtered = useMemo(
    () => scenarios.filter((s: ScenarioOperationalView) =>
      (!status || s.status === status) &&
      (!instrument || s.instrument === instrument) &&
      (!session || s.session === session) &&
      (!nodeId || s.nodeId === nodeId)),
    [scenarios, status, instrument, session, nodeId]);

  const options = useMemo(() => ({
    status: Array.from(new Set(scenarios.map((s) => s.status).filter(Boolean))).sort() as string[],
    instrument: Array.from(new Set(scenarios.map((s) => s.instrument).filter(Boolean))).sort() as string[],
    session: Array.from(new Set(scenarios.map((s) => s.session).filter(Boolean))).sort() as string[],
    nodeId: Array.from(new Set(scenarios.map((s) => s.nodeId).filter(Boolean))).sort() as string[],
  }), [scenarios]);

  const select = 'text-2xs mono px-1.5 py-1 rounded-sm border bg-transparent';

  return (
    <section className="rounded-md border border-[color:var(--border)] bg-[color:var(--panel)] p-3"
             aria-label="Scenarios" data-testid="scenario-panel">
      <header className="flex flex-wrap items-baseline justify-between gap-2 mb-1.5">
        <h3 className="text-xs uppercase tracking-wider text-text-muted">
          Scenarios{data?.summary ? ` (${data.summary.active} active / ${data.summary.total})` : ''}
        </h3>
        <span className={BADGE} style={C.muted} data-testid="scenario-scope">
          READ ONLY · PARENT OF EVERY TRADE
        </span>
      </header>

      {isError && (
        <div className="text-2xs" style={{ color: 'var(--negative)' }} data-testid="scenario-error">
          UNAVAILABLE — the scenario store could not be read. No scenarios are shown;
          this is not a claim that none exist.
        </div>
      )}
      {isLoading && (
        <div className="text-2xs text-text-muted" data-testid="scenario-loading">Loading scenarios…</div>
      )}

      {data && (
        <>
          <div className="flex flex-wrap gap-2 mb-2" data-testid="scenario-filters">
            {([
              ['status', status, setStatus, options.status, 'All statuses'],
              ['instrument', instrument, setInstrument, options.instrument, 'All instruments'],
              ['session', session, setSession, options.session, 'All sessions'],
              ['node', nodeId, setNodeId, options.nodeId, 'All nodes'],
            ] as const).map(([name, value, setter, opts, label]) => (
              <select key={name} className={select} style={C.muted} value={value}
                      onChange={(e) => (setter as (v: string) => void)(e.target.value)}
                      data-testid={`scenario-filter-${name}`} aria-label={label}>
                <option value="">{label}</option>
                {opts.map((o) => <option key={o} value={o}>{o}</option>)}
              </select>
            ))}
          </div>

          {filtered.length === 0 ? (
            <div className="text-2xs text-text-muted" data-testid="scenario-empty">
              {scenarios.length === 0
                ? 'No scenarios recorded. Scenarios are created explicitly — none are generated automatically.'
                : 'No scenarios match the current filters.'}
            </div>
          ) : (
            <div className="overflow-x-auto">
              <table className="w-full text-2xs" data-testid="scenario-table">
                <thead className="text-text-muted">
                  <tr>
                    {['Scenario', 'Status', 'Instrument', 'Direction', 'Session', 'Structure',
                      'Entry model', 'Node', 'Created', 'Age', 'Rec.', 'Intents', 'Orders',
                      'Positions', ''].map((h) => (
                      <th key={h} className="text-left font-normal pr-2 pb-1">{h}</th>
                    ))}
                  </tr>
                </thead>
                <tbody className="mono text-text-2">
                  {filtered.map((s) => (
                    <tr key={s.scenarioId} data-testid="scenario-row">
                      <td className="pr-2">{s.scenarioId.slice(0, 12)}</td>
                      <td className="pr-2"><StatusBadge status={s.status} /></td>
                      <td className="pr-2">{txt(s.instrument)}</td>
                      <td className="pr-2">{txt(s.direction)}</td>
                      <td className="pr-2">{txt(s.session)}</td>
                      <td className="pr-2">{txt(s.structure)}</td>
                      <td className="pr-2">{txt(s.entryModel)}</td>
                      <td className="pr-2">{txt(s.nodeId)}</td>
                      <td className="pr-2">{txt(s.createdAt)}</td>
                      <td className="pr-2">{age(s.ageSeconds)}</td>
                      <td className="pr-2">{txt(s.linkedRecommendation)}</td>
                      <td className="pr-2">{s.linkedIntentCount}</td>
                      <td className="pr-2">{s.linkedOrderCount}</td>
                      <td className="pr-2">{s.linkedPositionCount}</td>
                      <td><FreshnessBadge freshness={s.freshness} /></td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </>
      )}

      <p className="text-2xs text-text-muted mt-2">
        A Scenario is the parent of every recommendation, intent, order, position and
        (in future) ledger entry. This view is read-only: scenarios are never created,
        edited or advanced from the Control Tower UI, and no strategy logic exists.
      </p>
    </section>
  );
}
