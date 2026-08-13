/**
 * M-CT-RUNTIME-COMPLETE-UX-1 — the two-status header, plus the strategy cards
 * that consume the CORRECT authority.
 *
 *   CURRENT RUNTIME     what the node is doing now (includes transitions)
 *   LAST COMPLETE       the strategy context from the last cycle-end payload
 *   CT DELIVERY         whether telemetry is still arriving (transport only)
 *
 * News and decisions come from LAST COMPLETE and always say so. During a
 * recompute — ~25 minutes on the live node — the newest runtime payload carries
 * no strategy blocks at all, so blanking these cards would destroy the operator's
 * only view of what the strategy last decided, and presenting them unlabelled
 * would imply they describe now. Neither is acceptable, hence the explicit
 * provenance line on both cards.
 */
import { useQuery } from '@tanstack/react-query';
import { api, QK } from '@/lib/api';
import { Panel, EmptyState } from '@/components/structures/Panel';
import {
  runtimeSeverity, completeSeverity, deliverySeverity, strategyProvenance,
  humanAge, cleanField, isResolved, SEVERITY_TONE,
  type RuntimeSeverity, type StrategyView,
} from '@/lib/runtimeComplete';

const TONE_STYLE: Record<string, { color: string; bg: string }> = {
  green: { color: 'var(--positive)', bg: 'color-mix(in srgb, var(--positive) 12%, transparent)' },
  blue: { color: 'var(--accent-secondary, #6aa9ff)', bg: 'color-mix(in srgb, var(--accent-secondary, #6aa9ff) 12%, transparent)' },
  amber: { color: 'var(--warning)', bg: 'color-mix(in srgb, var(--warning) 12%, transparent)' },
  red: { color: 'var(--negative)', bg: 'color-mix(in srgb, var(--negative) 12%, transparent)' },
  neutral: { color: 'var(--text-muted)', bg: 'transparent' },
};

const LABEL: Record<RuntimeSeverity, string> = {
  healthy: 'OK', recomputing: 'RECOMPUTING', delayed: 'DELAYED',
  degraded: 'DEGRADED', stale: 'STALE', unknown: 'UNKNOWN',
};

function StatusPill({ severity, testid }: { severity: RuntimeSeverity; testid: string }) {
  const st = TONE_STYLE[SEVERITY_TONE[severity]] ?? TONE_STYLE.neutral;
  return (
    <span data-testid={testid} data-severity={severity}
          className="mono text-2xs uppercase tracking-wider px-1.5 py-0.5 rounded-sm"
          style={{ color: st.color, background: st.bg, border: `1px solid ${st.color}` }}>
      {LABEL[severity]}
    </span>
  );
}

function StatusBlock({ title, severity, lines, testid }: {
  title: string; severity: RuntimeSeverity; lines: (string | null)[]; testid: string;
}) {
  return (
    <div className="flex-1 min-w-[190px]" data-testid={testid}>
      <div className="text-[10px] font-ui uppercase tracking-wider text-text-muted mb-1">{title}</div>
      <div className="flex items-center gap-2">
        <StatusPill severity={severity} testid={`${testid}-pill`} />
        {lines[0] && <span className="mono text-2xs text-text-2">{lines[0]}</span>}
      </div>
      {lines.slice(1).filter(Boolean).map((l, i) => (
        <div key={i} className="mono text-2xs text-text-muted mt-0.5">{l}</div>
      ))}
    </div>
  );
}

/** Provenance line every strategy card carries. Never omitted. */
function ProvenanceLine({ view }: { view: StrategyView }) {
  const p = strategyProvenance(view);
  const age = humanAge(p.ageSeconds);
  return (
    <div className="flex items-center gap-2 mb-2" data-testid="strategy-provenance">
      <span className="mono text-2xs uppercase tracking-wider"
            style={{ color: p.isCurrent ? 'var(--positive)' : 'var(--text-muted)' }}>
        {p.label}
      </span>
      {age && <span className="mono text-2xs text-text-muted">· completed {age} ago</span>}
      {p.refreshing && (
        <span data-testid="refreshing-indicator" className="mono text-2xs uppercase tracking-wider px-1 rounded-sm"
              style={{ color: TONE_STYLE.blue.color, background: TONE_STYLE.blue.bg }}>
          refreshing with current cycle
        </span>
      )}
    </div>
  );
}

function Row({ label, value, tone }: { label: string; value: React.ReactNode; tone?: string }) {
  return (
    <div className="flex justify-between gap-4 text-2xs">
      <span className="text-text-muted">{label}</span>
      <span className="mono text-text-2" style={tone ? { color: tone } : undefined}>{value ?? '—'}</span>
    </div>
  );
}

export function RuntimeCompletePanel({ instanceId }: { instanceId: string }) {
  const { data, isError } = useQuery({
    queryKey: QK.liveStrategy(instanceId),
    queryFn: () => api.liveStrategy(instanceId),
    refetchInterval: 10_000,
    retry: false,
  });

  if (isError || !data) {
    return (
      <Panel provenance="live" title="Runtime & Strategy Cycle">
        <EmptyState title="Strategy cycle state unavailable"
                    description="The tower could not read its own runtime/complete projection. This is a tower fault, not a node verdict." />
      </Panel>
    );
  }

  // Runtime staleness stays owned by the phase-aware freshness authority; this
  // panel consumes that verdict and never re-derives it.
  const runtimeStale = false;
  const rSev = runtimeSeverity(data, runtimeStale);
  const cSev = completeSeverity(data);
  const dSev = deliverySeverity(data);

  const cur = data.current ?? {};
  const lc = data.lastComplete ?? {};
  const news = (lc.news ?? {}) as Record<string, any>;
  const decisions = (lc.decisions ?? {}) as Record<string, any>;
  const records: Record<string, any>[] = Array.isArray(decisions.decisions) ? decisions.decisions : [];

  return (
    <div className="space-y-4">
      {/* ── the two-status header ─────────────────────────────────────────── */}
      <Panel provenance="live" title="Runtime & Strategy Cycle">
        <div className="flex flex-wrap gap-6" data-testid="runtime-complete-strip">
          <StatusBlock
            title="Current runtime" severity={rSev} testid="current-runtime"
            lines={[
              humanAge(data.delivery?.ageSeconds) ? `updated ${humanAge(data.delivery?.ageSeconds)} ago` : null,
              cleanField(cur.cycleNote),
            ]}
          />
          <StatusBlock
            title="Last complete cycle" severity={cSev} testid="last-complete"
            lines={[
              humanAge(lc.ageSeconds) ? `completed ${humanAge(lc.ageSeconds)} ago` : 'never observed',
              cleanField(lc.boundary) ? `boundary ${cleanField(lc.boundary)}` : null,
              lc.available ? `${decisions.count ?? 0} decisions · news ${news.health ?? 'unknown'}` : null,
            ]}
          />
          <StatusBlock
            title="CT delivery" severity={dSev} testid="ct-delivery"
            lines={[
              humanAge(data.delivery?.ageSeconds) ? `last delivery ${humanAge(data.delivery?.ageSeconds)} ago` : null,
              'transport only — not execution health',
            ]}
          />
        </div>
      </Panel>

      {/* ── NEWS, from the last complete cycle ────────────────────────────── */}
      <Panel provenance="live" title="News (strategy cycle)">
        <div data-testid="news-card" />
        <ProvenanceLine view={data} />
        {!lc.available ? (
          <EmptyState title="No complete cycle observed"
                      description="No cycle-end payload has been received since this tower started. This is unknown, not healthy." />
        ) : (
          <div className="grid grid-cols-1 md:grid-cols-2 gap-x-8 gap-y-1">
            <Row label="Feed health" value={news.health ?? 'unknown'}
                 tone={news.healthy ? 'var(--positive)' : 'var(--warning)'} />
            <Row label="Source" value={news.source} />
            <Row label="Refreshed" value={humanAge(news.last_refresh_age_s) ? `${humanAge(news.last_refresh_age_s)} ago` : null} />
            <Row label="Coverage until" value={cleanField(news.coverage_until)} />
            <Row label="Events (relevant / total)" value={`${news.relevant_event_count ?? '—'} / ${news.event_count ?? '—'}`} />
            <Row label="Watching" value={`${(news.watched_currencies ?? []).join(', ')} · ${(news.watched_impacts ?? []).join(', ')}`} />
            <Row label="New-fill blackout"
                 value={`${news.blackout_minutes_before ?? '—'}m before / ${news.blackout_minutes_after ?? '—'}m after`} />
            <Row label="Blackout active" value={news.blackout_active ? 'YES' : 'no'}
                 tone={news.blackout_active ? 'var(--warning)' : undefined} />
            {/* The T-8 lesson: this is a SEPARATE rule from the ±3m blackout. */}
            <Row label="Flatten open trades" value={news.flatten_active_trades ? 'ON' : 'OFF'}
                 tone={news.flatten_active_trades ? 'var(--warning)' : 'var(--positive)'} />
            <Row label="New opens allowed" value={news.new_open_allowed ? 'yes' : 'NO'}
                 tone={news.new_open_allowed ? undefined : 'var(--warning)'} />
            {cleanField(news.refusal_reason) && (
              <Row label="Refusal reason" value={cleanField(news.refusal_reason)} tone="var(--warning)" />
            )}
            <Row label="Next relevant event"
                 value={news.next_relevant_event
                   ? `${news.next_relevant_event.currency} ${news.next_relevant_event.impact} · ${news.next_relevant_event.event} · ${news.next_relevant_event.time}`
                   : 'none scheduled'} />
          </div>
        )}
      </Panel>

      {/* ── DECISIONS, from the last complete cycle ───────────────────────── */}
      <Panel provenance="live" title="Strategy decisions (last complete cycle)">
        <div data-testid="decisions-card" />
        <ProvenanceLine view={data} />
        {!lc.available ? (
          <EmptyState title="No complete cycle observed"
                      description="Decision records arrive only with a cycle-end payload." />
        ) : (
          <>
            <div className="mono text-2xs text-text-muted mb-2" data-testid="decisions-count">
              showing {decisions.count ?? records.length} of {decisions.total_candidates ?? '—'} candidates
              {decisions.truncated ? ' · truncated by the node' : ''}
            </div>
            <div className="overflow-x-auto">
              <table className="w-full text-2xs mono">
                <thead>
                  <tr className="text-text-muted text-left">
                    {['Time (London)', 'OB / trade', 'Dir', 'Cohort', 'Market state', 'Action', 'RR', 'Entry', 'Stop', 'TP', 'Reason']
                      .map((h) => <th key={h} className="font-normal py-1 pr-3 whitespace-nowrap">{h}</th>)}
                  </tr>
                </thead>
                <tbody>
                  {records.slice(0, 25).map((r, i) => {
                    const resolved = isResolved(r);
                    const action = cleanField(r.action) ?? '—';
                    const refused = action === 'REFUSED' || cleanField(r.eligible) === 'False' || r.eligible === false;
                    return (
                      <tr key={cleanField(r.trade_id) ?? i} className="border-t border-[hsl(var(--border-mid)/0.4)]"
                          data-testid="decision-row" data-resolved={resolved}>
                        <td className="py-1 pr-3 whitespace-nowrap">
                          {cleanField(r.session_local)?.slice(0, 16) ?? cleanField(r.utc)?.slice(0, 16) ?? '—'}
                          <span className="text-text-muted"> {cleanField(r.session_tz_abbrev) ?? ''}</span>
                        </td>
                        <td className="py-1 pr-3">{cleanField(r.trade_id) ?? cleanField(r.ob_id) ?? '—'}</td>
                        <td className="py-1 pr-3">{cleanField(r.direction) === 'bullish' ? 'long' : cleanField(r.direction) === 'bearish' ? 'short' : '—'}</td>
                        <td className="py-1 pr-3 whitespace-nowrap">
                          {cleanField(r.cohort_key)?.replace('EURUSD|', '') ?? '—'}
                          {/* Session/cohort/state/RR resolve at FILL time. Until then
                              they are detection-time provisional and must say so. */}
                          {!resolved && <span className="text-text-muted"> (provisional)</span>}
                        </td>
                        <td className="py-1 pr-3">{cleanField(r.market_state) ?? <span className="text-text-muted">unresolved</span>}</td>
                        <td className="py-1 pr-3" style={refused ? { color: 'var(--warning)' } : undefined}>{action}</td>
                        <td className="py-1 pr-3">{resolved ? (cleanField(r.target_rr) ?? '—') : <span className="text-text-muted">{cleanField(r.target_rr) ?? '—'}*</span>}</td>
                        <td className="py-1 pr-3">{cleanField(r.entry) ?? '—'}</td>
                        <td className="py-1 pr-3">{cleanField(r.stop) ?? '—'}</td>
                        <td className="py-1 pr-3">{cleanField(r.tp) ?? '—'}</td>
                        <td className="py-1 pr-3">
                          {cleanField(r.refusal_reason) ?? cleanField(r.cancel_reason)
                            ?? cleanField(r.portfolio_decision_reason) ?? cleanField(r.outcome) ?? '—'}
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
            <div className="mono text-2xs text-text-muted mt-2">
              * RR and cohort are provisional until the order fills — session, market state and
              final target resolve at fill/trigger time, not at detection.
            </div>
          </>
        )}
      </Panel>
    </div>
  );
}
