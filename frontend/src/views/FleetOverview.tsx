/**
 * M-CT-FLEET-DASHBOARD-2 — the operational command dashboard.
 *
 * Every statement on this page traces to ONE node authority. The dashboard is a
 * courier: it routes, labels and ages verdicts, and derives none of them. That
 * is the L_2106 lesson — a parallel interpretation will eventually disagree with
 * the rails that actually execute, and the dashboard is what gets believed.
 *
 *   NODE LIVENESS      heartbeat block only              ~20s cadence
 *   RUNTIME            runtime snapshot only             phase-aware
 *   STRATEGY CYCLE     complete snapshot only            ~25min recomputes
 *   EXECUTION READY    runtime.execution_readiness only  the real rails
 *   CT DELIVERY        node delivery/outbox block only   transport, not trading
 *   RECONCILIATION     tri-state; `clean: null` = UNKNOWN
 *
 * Structured for N nodes from the start: every section maps over the fleet and
 * fleet health names the offending node rather than collapsing to one colour.
 */
import { useQuery, useQueryClient, useQueries } from '@tanstack/react-query';
import { useState } from 'react';
import { RefreshCw, AlertTriangle } from 'lucide-react';
import { api, QK } from '@/lib/api';
import { Panel, EmptyState } from '@/components/structures/Panel';
import { useOperationalFleet } from '@/hooks/useRepository';
import {
  livenessSeverity, LIVENESS_LABEL, runtimeSeverity, cycleSeverity, readinessView,
  deliverySeverity, pendingSlots, reconciliationView, attentionItems, fleetHealth,
  humanAge, clean, safeAccountLabel, ageOf, SEV_TONE, splitLedger,
  deliveryView, lifecycleOf, sortNewestFirst,
  type Sev, type FleetNodeView,
} from '@/lib/fleetModel';

const TONE: Record<string, string> = {
  green: 'var(--positive)', blue: 'var(--accent-secondary, #6aa9ff)',
  amber: 'var(--warning)', red: 'var(--negative)', neutral: 'var(--text-muted)',
};
const colorOf = (s: Sev) => TONE[SEV_TONE[s]] ?? TONE.neutral;

function Pill({ sev, children, testid }: { sev: Sev; children: React.ReactNode; testid?: string }) {
  const c = colorOf(sev);
  return (
    <span data-testid={testid} data-severity={sev}
          className="mono text-2xs uppercase tracking-wider px-1.5 py-0.5 rounded-sm"
          style={{ color: c, border: `1px solid ${c}`,
                   background: `color-mix(in srgb, ${c} 12%, transparent)` }}>
      {children}
    </span>
  );
}

function Line({ label, sev, state, detail, testid }: {
  label: string; sev: Sev; state: string; detail?: string | null; testid: string;
}) {
  return (
    <div className="flex items-baseline gap-3" data-testid={testid}>
      <span className="text-[10px] font-ui uppercase tracking-wider text-text-muted w-[118px] shrink-0">{label}</span>
      <Pill sev={sev} testid={`${testid}-pill`}>{state}</Pill>
      {detail && <span className="mono text-2xs text-text-muted">{detail}</span>}
    </div>
  );
}

function KV({ k, v, tone }: { k: string; v: React.ReactNode; tone?: string }) {
  return (
    <div className="flex justify-between gap-4 text-2xs py-0.5">
      <span className="text-text-muted">{k}</span>
      <span className="mono text-text-2" style={tone ? { color: tone } : undefined}>{v ?? '—'}</span>
    </div>
  );
}

export function FleetOverview() {
  const qc = useQueryClient();
  const { nodes: fleetNodes } = useOperationalFleet();
  const ids = fleetNodes.map((n) => n.nodeId).filter(Boolean) as string[];
  const [refreshing, setRefreshing] = useState(false);
  const [refreshError, setRefreshError] = useState<string | null>(null);
  const [refreshedAt, setRefreshedAt] = useState<Date | null>(null);

  // One coordinated poll for the whole dashboard. 10s: the node beats every 20s,
  // so this sees every beat without doubling the request rate.
  const strategyQueries = useQueries({
    queries: ids.map((id) => ({
      queryKey: QK.liveStrategy(id),
      queryFn: () => api.liveStrategy(id),
      refetchInterval: 10_000,
      retry: false,
    })),
  });
  // The governed control surface decides which controls may render AT ALL.
  const controls = useQuery({
    queryKey: ['operator-commands'],
    queryFn: () => api.operatorCommands(),
    refetchInterval: 30_000, retry: false,
  });
  const ledger = useQuery({
    queryKey: ['fleet-ledger-trades'],
    queryFn: () => api.ledgerTrades({}),
    refetchInterval: 10_000, retry: false,
  });

  const views: FleetNodeView[] = strategyQueries
    .map((q) => q.data as FleetNodeView | undefined)
    .filter(Boolean) as FleetNodeView[];

  /** Manual refresh REFETCHES. It never rewrites a source timestamp — a
   *  successful GET of an 11-minute-old snapshot still reads 11 minutes. */
  async function refresh() {
    setRefreshing(true); setRefreshError(null);
    try {
      await qc.refetchQueries({ type: 'active' });
      setRefreshedAt(new Date());
    } catch (e: any) {
      // Last-known data stays on screen; only the banner changes.
      setRefreshError(String(e?.message ?? e ?? 'refresh failed'));
    } finally {
      setRefreshing(false);
    }
  }

  const health = fleetHealth(views);
  const attention = views.flatMap((v) =>
    attentionItems(v).map((a) => ({ ...a, node: v.instanceId })));
  const liveCount = views.filter((v) => livenessSeverity(v) === 'ok').length;
  const openPositions = views.reduce(
    (n, v) => n + (Array.isArray(v.node?.positions) ? v.node!.positions.length : 0), 0);

  return (
    <div className="p-6 h-full min-h-0 overflow-auto">
      <div className="flex items-start justify-between mb-4">
        <div>
          <h1 className="text-2xl font-semibold text-text tracking-tight">Control Tower</h1>
          <p className="text-xs text-text-muted mt-1">Fleet command dashboard</p>
        </div>
        <div className="flex flex-col items-end gap-1">
          <button data-testid="fleet-refresh" onClick={refresh} disabled={refreshing}
                  className="flex items-center gap-1.5 mono text-2xs uppercase tracking-wider px-2 py-1
                             rounded-sm border border-[hsl(var(--border-mid))] hover:border-[hsl(var(--accent-secondary))]
                             disabled:opacity-50">
            <RefreshCw size={11} className={refreshing ? 'animate-spin' : undefined} />
            {refreshing ? 'Refreshing…' : 'Refresh'}
          </button>
          {refreshError
            ? <span data-testid="refresh-error" className="mono text-2xs" style={{ color: TONE.red }}>
                refresh failed — showing last known state
              </span>
            : refreshedAt && <span className="mono text-2xs text-text-muted">
                refreshed {refreshedAt.toISOString().slice(11, 19)}Z · source ages unchanged
              </span>}
        </div>
      </div>

      {/* ── fleet strip ─────────────────────────────────────────────────── */}
      <Panel provenance="live" title="Fleet">
        <div className="flex flex-wrap gap-x-10 gap-y-2" data-testid="fleet-strip">
          <div><div className="text-[10px] uppercase tracking-wider text-text-muted">Nodes</div>
            <div className="mono text-sm text-text">{liveCount} / {views.length} live</div></div>
          <div><div className="text-[10px] uppercase tracking-wider text-text-muted">Open positions</div>
            <div className="mono text-sm text-text">{openPositions}</div></div>
          <div><div className="text-[10px] uppercase tracking-wider text-text-muted">Fleet health</div>
            <div className="mt-0.5"><Pill sev={health.sev} testid="fleet-health">{health.detail}</Pill></div></div>
          <div><div className="text-[10px] uppercase tracking-wider text-text-muted">Attention</div>
            <div className="mt-0.5"><Pill sev={attention.length ? 'warn' : 'ok'} testid="attention-count">
              {attention.length === 0 ? 'NONE' : String(attention.length)}</Pill></div></div>
        </div>
      </Panel>

      <div className="grid grid-cols-12 gap-4 mt-4">
        <div className="col-span-12 lg:col-span-9 space-y-4">
          {views.length === 0 && (
            <Panel provenance="live" title="Nodes">
              <EmptyState title="No node is reporting"
                          description="No execution node has published telemetry to this tower." />
            </Panel>
          )}

          {views.map((v) => {
            const rd = readinessView(v);
            const rc = reconciliationView(v);
            const nd = v.node ?? {};
            const arming = nd.arming ?? {};
            const risk = nd.risk ?? {};
            const acct = nd.account ?? {};
            const health = acct.health ?? {};
            const cyc = nd.cycle ?? {};
            const news = v.lastComplete?.news ?? {};
            const decisions = v.lastComplete?.decisions ?? {};
            const records: any[] = Array.isArray(decisions.decisions) ? decisions.decisions : [];
            const positions: any[] = Array.isArray(nd.positions) ? nd.positions : [];
            const runtimeAge = ageOf(nd.snapshotReceivedAt);
            const deliveryAge = ageOf(nd.deliveryAt);
            const pend = pendingSlots(v);

            return (
              <div key={v.instanceId} className="space-y-4" data-testid={`node-${v.instanceId}`}>
                {/* three independent statuses */}
                <Panel provenance="live" title={`Node · ${v.instanceId}`}>
                  <div className="space-y-1.5">
                    <Line testid="node-liveness" label="Node" sev={livenessSeverity(v)}
                          state={LIVENESS_LABEL[v.heartbeat?.status ?? 'unknown'] ?? 'UNKNOWN'}
                          detail={v.heartbeat?.available
                            ? `heartbeat ${humanAge(v.heartbeat.ageSeconds)} ago · uptime ${humanAge(v.heartbeat.uptimeSeconds)}`
                            : 'no heartbeat observed'} />
                    <Line testid="node-runtime" label="Runtime" sev={runtimeSeverity(v)}
                          state={runtimeSeverity(v) === 'ok' ? 'CURRENT' : 'AGING'}
                          detail={`snapshot ${humanAge(runtimeAge)} ago`} />
                    <Line testid="node-cycle" label="Strategy cycle" sev={cycleSeverity(v)}
                          state={String(v.current?.cycleStatus ?? 'unknown').toUpperCase()}
                          detail={v.lastComplete?.available
                            ? `last complete ${humanAge(v.lastComplete.ageSeconds)} ago · boundary ${clean(cyc.last_boundary) ?? '—'}`
                            : 'no complete cycle observed'} />
                    {(() => { const dv = deliveryView(v); return (
                      <Line testid="node-delivery" label="CT delivery" sev={dv.severity} state={dv.label}
                            detail={dv.aged
                              // Age is part of the verdict: a report this old cannot
                              // assert the transport is degraded NOW, only what it was.
                              ? `last report ${humanAge(dv.ageSeconds)} ago · last reported: ${dv.reported}`
                              : `${dv.reported} · reported ${humanAge(dv.ageSeconds)} ago · transport only`} />
                    ); })()}
                  </div>
                </Panel>

                {/* EXECUTION READINESS — the node's own rails */}
                <Panel provenance="live" title="Execution readiness">
                  <div className="flex items-center gap-3 mb-2" data-testid="readiness-header">
                    <Pill sev={rd.severity} testid="readiness-pill">{rd.label}</Pill>
                    {rd.severity === 'ok' && (
                      <span className="mono text-2xs text-text-muted">
                        except candidate-specific checks
                      </span>
                    )}
                    {rd.evaluatedAt && (
                      <span className="mono text-2xs text-text-muted">
                        · evaluated {humanAge(ageOf(rd.evaluatedAt))} ago by the node
                      </span>
                    )}
                  </div>
                  {!rd.available ? (
                    <EmptyState title="Readiness unknown"
                                description="The node has not published an execution-readiness verdict. This is unknown — not ready." />
                  ) : (
                    <>
                      {rd.reasons.length > 0 && (
                        <div data-testid="readiness-reasons" className="mono text-2xs mb-2"
                             style={{ color: TONE.red }}>
                          {rd.reasons.join(' · ')}
                        </div>
                      )}
                      <div className="grid grid-cols-1 md:grid-cols-2 gap-x-8">
                        {rd.checks.map((c) => (
                          <KV key={c.name} k={c.name.replace(/_/g, ' ')}
                              v={<>{c.ok ? 'PASS' : 'FAIL'}{c.detail && <span className="text-text-muted"> · {c.detail}</span>}</>}
                              tone={c.ok ? TONE.green : TONE.red} />
                        ))}
                      </div>
                      {rd.candidateNotEvaluated.length > 0 && (
                        <div className="mt-2 pt-2 border-t border-[hsl(var(--border-mid)/0.4)]"
                             data-testid="candidate-checks">
                          <div className="text-[10px] uppercase tracking-wider text-text-muted mb-1">
                            Candidate-specific — evaluated on a real OPEN
                          </div>
                          {rd.candidateNotEvaluated.map((c) => (
                            <KV key={c.name} k={c.name.replace(/_/g, ' ')} v={c.why} />
                          ))}
                        </div>
                      )}
                    </>
                  )}
                </Panel>

                {/* ACCOUNT + PROTECTION */}
                <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
                  <Panel provenance="live" title="Account">
                    <div data-testid="account-card">
                      <KV k="Server" v={clean(acct.identity?.server)} />
                      <KV k="Account" v={safeAccountLabel(acct)} />
                      <KV k="Balance" v={health.balance != null ? `${health.balance} ${clean(acct.identity?.currency) ?? ''}` : null} />
                      <KV k="Equity" v={health.equity ?? null} />
                      <KV k="Free margin" v={health.free_margin ?? null} />
                      <KV k="Authorization" v={clean(arming.authorization_type) ?? clean(arming.status)} />
                      <KV k="Daily OPENs" v={`${arming.opens_today ?? 0} / ${arming.daily_open_cap ?? '—'}`} />
                      <KV k="Fingerprint match" v={arming.fingerprint_matches === true ? 'yes' : arming.fingerprint_matches === false ? 'NO' : 'unknown'}
                          tone={arming.fingerprint_matches === false ? TONE.red : undefined} />
                      <KV k="AutoTrading" v={health.trade_expert === true ? 'ON' : health.trade_expert === false ? 'OFF' : 'unknown'} />
                    </div>
                  </Panel>

                  <Panel provenance="live" title="Protection">
                    <div data-testid="protection-card">
                      <KV k="News gate" v={news.new_open_allowed === false ? 'BLOCKING' : news.health ? 'CLEAR' : 'unknown'}
                          tone={news.new_open_allowed === false ? TONE.amber : undefined} />
                      <KV k="Blackout" v={news.blackout_active ? 'ACTIVE' : news.health ? 'inactive' : 'unknown'}
                          tone={news.blackout_active ? TONE.amber : undefined} />
                      <KV k="Flatten open trades" v={news.flatten_active_trades === true ? 'ON' : news.health ? 'OFF' : 'unknown'}
                          tone={news.flatten_active_trades === true ? TONE.amber : TONE.green} />
                      <KV k="Next HIGH event" v={news.next_relevant_event
                        ? `${news.next_relevant_event.currency} ${news.next_relevant_event.event} · ${news.next_relevant_event.time}` : null} />
                      <KV k="Kill switch" v={nd.killSwitchActive ? 'ACTIVE' : 'CLEAR'}
                          tone={nd.killSwitchActive ? TONE.red : TONE.green} />
                      <KV k="Reconciliation" v={rc.label} tone={colorOf(rc.severity)} />
                      <KV k="Daily loss" v={`${risk.daily_realized_r ?? 0} / ${risk.daily_loss_limit_r ?? '—'}R`} />
                      <KV k="Positions" v={`${risk.open_mirror_count ?? positions.length} / ${risk.max_open_positions ?? '—'}`} />
                      {v.lastComplete?.available && !v.lastComplete.isCurrent && (
                        <div className="mono text-2xs text-text-muted mt-1">
                          news from last complete cycle · {humanAge(v.lastComplete.ageSeconds)} ago
                        </div>
                      )}
                    </div>
                  </Panel>
                </div>

                {/* OPERATOR CONTROLS — only what a governed backend actually backs */}
                <Panel provenance="live" title="Operator controls">
                  <div data-testid="operator-controls">
                    {(() => {
                      const enabled = (controls.data as any)?.enabled === true;
                      const available = ((controls.data as any)?.commands ?? []) as any[];
                      if (!enabled || available.length === 0) {
                        // NO FAKE BUTTONS. The command transport is NullTransport by
                        // default and `/api/commands/{name}` is the fixture-surface
                        // vocabulary, explicitly not wired to arming or order
                        // machinery. Rendering KILL / REVOKE here would offer an
                        // operator a control that silently does nothing — worse than
                        // offering none at all, because it would be trusted in an
                        // emergency.
                        return (
                          <>
                            <div className="mono text-2xs" style={{ color: TONE.amber }}>
                              No governed operator controls are available
                            </div>
                            <div className="mono text-2xs text-text-muted mt-1">
                              The operator command channel reports
                              {' '}<span className="text-text-2">enabled: false</span> with an empty registry,
                              and there are no active authorization grants to revoke. KILL, CLEAR KILL and
                              REVOKE are therefore not rendered — a control that cannot act must not look
                              like one that can.
                            </div>
                            <div className="mono text-2xs text-text-muted mt-1">
                              Kill state is still reported above (Protection → Kill switch) from node telemetry.
                            </div>
                          </>
                        );
                      }
                      return (
                        <div className="flex flex-wrap gap-2" data-testid="operator-control-buttons">
                          {available.map((c: any) => (
                            <button key={c.name ?? c.id} data-testid={`control-${c.name ?? c.id}`}
                                    className="mono text-2xs uppercase tracking-wider px-2 py-1 rounded-sm
                                               border border-[hsl(var(--border-mid))]"
                                    onClick={() => {
                                      // Destructive controls use the existing typed
                                      // confirmation pattern; nothing fires without it.
                                      const word = String(c.name ?? c.id).toUpperCase();
                                      const typed = window.prompt(
                                        `${word}\n\nThis is a governed, destructive control.\n` +
                                        `Type ${word} to confirm.`);
                                      if (typed !== word) return;
                                      api.runOperatorCommand?.(c.name ?? c.id)
                                        .then(() => qc.refetchQueries({ type: 'active' }))
                                        .catch((e: any) => setRefreshError(String(e?.message ?? e)));
                                    }}>
                              {c.name ?? c.id}
                            </button>
                          ))}
                        </div>
                      );
                    })()}
                  </div>
                </Panel>

                {/* ACTIVE MARKETS */}
                <Panel provenance="live" title="Active markets">
                  <div data-testid="markets-card" className="grid grid-cols-1 md:grid-cols-2 gap-x-8">
                    <KV k="Instrument" v={`${clean(nd.engine?.symbol) ?? '—'} · ${clean(nd.engine?.timeframe) ?? '—'}`} />
                    <KV k="Cycle" v={String(v.current?.cycleStatus ?? 'unknown').toUpperCase()} />
                    <KV k="Last boundary" v={clean(cyc.last_boundary)} />
                    <KV k="Last bar" v={clean(cyc.last_bar_time)} />
                    <KV k="Bid / Ask" v={nd.market?.available ? `${nd.market.bid} / ${nd.market.ask}` : 'not sampled this cycle'} />
                    <KV k="Open positions" v={positions.length} />
                  </div>
                </Panel>

                {/* OPEN POSITIONS — the node's own array */}
                <Panel provenance="live" title={`Open positions (${positions.length})`}>
                  {positions.length === 0 ? (
                    <div data-testid="no-positions" className="mono text-2xs text-text-muted">
                      No open positions
                      {nd.reconciliation?.available === true && (
                        <> · node reports expected {nd.reconciliation.expected_position_count ?? '—'} / observed {nd.reconciliation.observed_position_count ?? '—'}</>
                      )}
                    </div>
                  ) : (
                    <table className="w-full text-2xs mono" data-testid="positions-table">
                      <thead><tr className="text-text-muted text-left">
                        {['Pair', 'Side', 'Entry', 'Stop', 'TP', 'Ticket'].map((h) =>
                          <th key={h} className="font-normal py-1 pr-3">{h}</th>)}
                      </tr></thead>
                      <tbody>{positions.map((p, i) => (
                        <tr key={i} className="border-t border-[hsl(var(--border-mid)/0.4)]">
                          <td className="py-1 pr-3">{clean(p.symbol) ?? '—'}</td>
                          <td className="py-1 pr-3">{clean(p.side) ?? '—'}</td>
                          <td className="py-1 pr-3">{clean(p.entry) ?? '—'}</td>
                          <td className="py-1 pr-3">{clean(p.sl) ?? '—'}</td>
                          <td className="py-1 pr-3">{clean(p.tp) ?? '—'}</td>
                          <td className="py-1 pr-3">{clean(p.ticket) ?? '—'}</td>
                        </tr>))}
                      </tbody>
                    </table>
                  )}
                </Panel>

                {/* RECENT EXECUTION ACTIVITY — why a trade was or wasn't taken */}
                <Panel provenance="live" title="Recent execution activity">
                  <div data-testid="execution-activity">
                    {records.length === 0 ? (
                      <div className="mono text-2xs text-text-muted">
                        No decision records — the node publishes these only at cycle end.
                      </div>
                    ) : (
                      <>
                        <div className="mono text-2xs text-text-muted mb-2">
                          from last complete cycle · {humanAge(v.lastComplete?.ageSeconds)} ago ·
                          showing {Math.min(12, records.length)} of {decisions.total_candidates ?? records.length}
                        </div>
                        <table className="w-full text-2xs mono">
                          <thead><tr className="text-text-muted text-left">
                            {['Time (London)', 'Trade', 'Dir', 'Lifecycle', 'Reason'].map((h) =>
                              <th key={h} className="font-normal py-1 pr-3">{h}</th>)}
                          </tr></thead>
                          <tbody>{sortNewestFirst(records).slice(0, 12).map((r, i) => {
                            const lc = lifecycleOf(r);
                            return (
                              <tr key={clean(r.trade_id) ?? i} className="border-t border-[hsl(var(--border-mid)/0.4)]"
                                  data-testid="activity-row">
                                <td className="py-1 pr-3 whitespace-nowrap">
                                  {clean(r.session_local)?.slice(0, 16) ?? clean(r.utc)?.slice(0, 16) ?? '—'}
                                  <span className="text-text-muted"> {clean(r.session_tz_abbrev) ?? ''}</span>
                                </td>
                                <td className="py-1 pr-3">{clean(r.trade_id) ?? '—'}</td>
                                <td className="py-1 pr-3">{clean(r.direction) === 'bullish' ? 'long' : clean(r.direction) === 'bearish' ? 'short' : '—'}</td>
                                <td className="py-1 pr-3">
                                  <span className="mono text-2xs px-1 rounded-sm"
                                        data-testid="activity-lifecycle" data-terminal={lc.terminal}
                                        style={{ color: colorOf(lc.sev),
                                                 background: `color-mix(in srgb, ${colorOf(lc.sev)} 12%, transparent)` }}>
                                    {lc.label}
                                  </span></td>
                                <td className="py-1 pr-3">
                                  {clean(r.refusal_reason) ?? clean(r.cancel_reason) ?? clean(r.outcome) ?? '—'}</td>
                              </tr>);
                          })}</tbody>
                        </table>
                      </>
                    )}
                  </div>
                </Panel>
              </div>
            );
          })}

          {/* BROKER TRADES — kept visually distinct from strategy/replay results */}
          <Panel provenance="live" title="Broker trades">
            <div data-testid="broker-trades">
              {(() => {
                const { broker, simulated, unverified } = splitLedger((ledger.data as any)?.trades ?? []);
                return (
                  <>
                    {broker.length === 0 ? (
                      <div className="mono text-2xs text-text-muted" data-testid="no-broker-trades">
                        No broker-executed trades recorded.
                      </div>
                    ) : (
                      <div className="mono text-2xs" data-testid="broker-trade-count">
                        {broker.length} broker-executed
                      </div>
                    )}
                    {unverified.length > 0 && (
                      // Neither provably broker-executed nor provably simulated.
                      // It gets its own bucket rather than a guess in either direction.
                      <div className="mono text-2xs text-text-muted mt-2 pt-2
                                      border-t border-[hsl(var(--border-mid)/0.4)]"
                           data-testid="unverified-trade-count">
                        {unverified.length} record{unverified.length === 1 ? '' : 's'} of
                        {' '}unverified provenance — not counted as broker-executed
                      </div>
                    )}
                    {simulated.length > 0 && (
                      // Shown, but NEVER under the broker heading and never counted
                      // as broker-executed — the local mock adapter writes into the
                      // same durable ledger.
                      <div className="mono text-2xs text-text-muted mt-2 pt-2
                                      border-t border-[hsl(var(--border-mid)/0.4)]"
                           data-testid="simulated-trade-count">
                        {simulated.length} simulated / mock-adapter record
                        {simulated.length === 1 ? '' : 's'} in the ledger — not broker-executed
                      </div>
                    )}
                  </>
                );
              })()}
            </div>
          </Panel>
        </div>

        {/* ── attention rail ────────────────────────────────────────────── */}
        <div className="col-span-12 lg:col-span-3">
          <Panel provenance="live" title="Attention">
            <div data-testid="attention-rail">
              {attention.length === 0 ? (
                <div className="mono text-2xs text-text-muted" data-testid="attention-none">
                  No operator action required
                </div>
              ) : (
                <div className="space-y-3">
                  {attention.map((a, i) => (
                    <div key={i} data-testid="attention-item" data-severity={a.sev}>
                      <div className="flex items-center gap-1.5">
                        <AlertTriangle size={11} style={{ color: colorOf(a.sev) }} />
                        <span className="mono text-2xs uppercase tracking-wider"
                              style={{ color: colorOf(a.sev) }}>{a.title}</span>
                      </div>
                      <div className="mono text-2xs text-text-muted ml-4">{a.detail}</div>
                      <div className="mono text-2xs text-text-muted ml-4 opacity-60">{a.node}</div>
                    </div>
                  ))}
                </div>
              )}
            </div>
          </Panel>
        </div>
      </div>
    </div>
  );
}
