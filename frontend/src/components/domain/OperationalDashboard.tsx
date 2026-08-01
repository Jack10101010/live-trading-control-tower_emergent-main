/**
 * LIVE-4A — the operational dashboard.
 *
 * ONE polling source (`api.operationsSummary`) feeds every card below. No card
 * fetches, aggregates, joins or re-derives operational truth — each receives an
 * already-projected immutable model and renders it. Formatting lives in the
 * shared helpers at the top of this file, so no transformation is duplicated.
 *
 * Badges are explicit and never inferred from a missing value:
 *   LIVE / MOCK              — provenance of the underlying read
 *   STALE                    — read succeeded but is older than its threshold
 *   UNAVAILABLE              — the source could not be read at all
 *   RECONCILIATION REQUIRED  — the projection says this entity needs evidence
 */
import { useQuery } from '@tanstack/react-query';
import { isAuthoritative, authoritativeOnly } from '@/lib/operationalProvenance';
import {
  api,
  type AccountOperationalView,
  type NodeOperationalView,
  type OperationalSummaryView,
  type OrderOperationalView,
  type PositionOperationalView,
  type ProjectionFreshness,
} from '@/lib/api';

/* ── shared formatting + badges (the only transformation code in the UI) ──── */

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

const BADGE = 'text-2xs mono px-1.5 py-0.5 rounded-sm border';
const C = {
  live: { borderColor: 'var(--caution, var(--warning))', color: 'var(--caution, var(--warning))' },
  mock: { borderColor: 'var(--text-muted)', color: 'var(--text-muted)' },
  bad: { borderColor: 'var(--negative)', color: 'var(--negative)' },
  ok: { borderColor: 'var(--positive)', color: 'var(--positive)' },
  muted: { borderColor: 'var(--border-subtle)', color: 'var(--text-muted)' },
} as const;

function ProvenanceBadge({ provenance }: { provenance: string }) {
  // M-FLEET-2: provenance is decided in one module (see operationalProvenance).
  const live = isAuthoritative({ provenance });
  const absent = provenance === 'absent';
  return (
    <span className={BADGE} style={absent ? C.bad : live ? C.live : C.mock}
          data-testid="provenance-badge">
      {absent ? 'UNAVAILABLE' : live ? 'LIVE' : provenance === 'node-telemetry' ? 'NODE' : 'MOCK'}
    </span>
  );
}

function FreshnessBadge({ freshness }: { freshness: ProjectionFreshness | null }) {
  if (!freshness) return null;
  if (!freshness.available) {
    return <span className={BADGE} style={C.bad} data-testid="freshness-badge">UNAVAILABLE</span>;
  }
  if (freshness.stale) {
    return <span className={BADGE} style={C.bad} data-testid="freshness-badge">STALE</span>;
  }
  return (
    <span className={BADGE} style={C.ok} data-testid="freshness-badge">
      FRESH {age(freshness.ageSeconds)}
    </span>
  );
}

function ReconBadge({ required }: { required: boolean | undefined }) {
  if (!required) return null;
  return (
    <span className={BADGE} style={C.bad} data-testid="reconciliation-badge">
      RECONCILIATION REQUIRED
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

function Card({ title, badges, children, testId }: {
  title: string; badges?: React.ReactNode; children: React.ReactNode; testId: string;
}) {
  return (
    <section className="rounded-md border border-[color:var(--border)] bg-[color:var(--panel)] p-3"
             data-testid={testId} aria-label={title}>
      <header className="flex flex-wrap items-baseline justify-between gap-2 mb-1.5">
        <h3 className="text-xs uppercase tracking-wider text-text-muted">{title}</h3>
        <span className="flex flex-wrap gap-1.5">{badges}</span>
      </header>
      {children}
    </section>
  );
}

/* ── cards: each renders ONE projected model, no derivation ──────────────── */

function NodeCard({ node }: { node: NodeOperationalView }) {
  return (
    <Card title={`Node · ${node.nodeId}`} testId="node-card"
          badges={<>
            <ProvenanceBadge provenance={node.provenance} />
            <FreshnessBadge freshness={node.freshness} />
          </>}>
      <div className="space-y-1">
        <Row label="Deployment" value={txt(node.deployment)} />
        <Row label="Adapter" value={txt(node.adapter)} />
        <Row label="Broker" value={txt(node.broker)} />
        <Row label="Account" value={txt(node.accountFingerprintMasked)} />
        <Row label="Connection" value={txt(node.connectionState)} />
        <Row label="Heartbeat" value={age(node.heartbeatAgeSeconds)} />
        <Row label="Health" value={txt(node.health)} />
        <Row label="Execution mode" value={txt(node.executionMode)} />
        <Row label="Authorization"
             value={node.authorizationSummary ? 'active' : 'none active'} />
        <Row label="Reconciliation" value={txt(node.reconciliationState)} />
        <Row label="Open positions / orders"
             value={`${node.openPositionCount ?? '—'} / ${node.openOrderCount ?? '—'}`} />
        <Row label="Active scenarios" value={String(node.activeScenarioCount ?? '—')} />
        <Row label="Last activity" value={txt(node.lastActivity)} />
        {node.warnings.length > 0 && (
          <ul className="mt-1 pt-1 border-t space-y-0.5" data-testid="node-warnings"
              style={{ borderColor: 'var(--border-subtle)' }}>
            {node.warnings.map((w) => (
              <li key={w} className="text-2xs" style={{ color: 'var(--negative)' }}>⚠ {w}</li>
            ))}
          </ul>
        )}
      </div>
    </Card>
  );
}

function AccountCard({ account }: { account: AccountOperationalView }) {
  const ccy = account.currency ? ` ${account.currency}` : '';
  return (
    <Card title="Account" testId="account-card"
          badges={<>
            <ProvenanceBadge provenance={account.provenance} />
            <FreshnessBadge freshness={account.freshness} />
          </>}>
      {account.freshness && !account.freshness.available ? (
        <div className="text-2xs text-text-muted" data-testid="account-unavailable">
          Account read unavailable — {txt(account.freshness.detail)}. No values are shown
          because none were read.
        </div>
      ) : (
        <div className="space-y-1">
          <Row label="Fingerprint" value={txt(account.accountFingerprint)} />
          <Row label="Broker" value={txt(account.broker)} />
          <Row label="Server" value={txt(account.server)} />
          <Row label="Balance" value={num(account.balance, 2, ccy)} />
          <Row label="Equity" value={num(account.equity, 2, ccy)} />
          <Row label="Margin" value={num(account.margin, 2, ccy)} />
          <Row label="Margin level" value={num(account.marginLevel, 2, '%')} />
          <Row label="Leverage" value={account.leverage ? `1:${account.leverage}` : '—'} />
          <Row label="Unrealized P&L" value={num(account.unrealizedPnL, 2, ccy)} />
          <Row label="Realized today"
               value={account.realizedPnLToday === null ? 'not derivable' : num(account.realizedPnLToday, 2, ccy)} />
          <Row label="Open risk"
               value={account.openRisk === null ? 'not derivable' : num(account.openRisk, 2)} />
          <Row label="Connection" value={txt(account.connectionState)} />
        </div>
      )}
    </Card>
  );
}

function OrdersTable({ orders }: { orders: OrderOperationalView[] }) {
  return (
    <Card title={`Orders (${orders.length})`} testId="orders-card"
          badges={<span className={BADGE} style={C.muted}>DERIVED</span>}>
      {orders.length === 0 ? (
        <div className="text-2xs text-text-muted" data-testid="orders-empty">
          No active orders in the projection.
        </div>
      ) : (
        <div className="overflow-x-auto">
          <table className="w-full text-2xs" data-testid="orders-table">
            <thead className="text-text-muted">
              <tr>
                {['Intent', 'Ticket', 'Instrument', 'Side', 'Qty', 'State', 'Broker', 'Scenario', ''].map((h) => (
                  <th key={h} className="text-left font-normal pr-2 pb-1">{h}</th>
                ))}
              </tr>
            </thead>
            <tbody className="mono text-text-2">
              {orders.map((o) => (
                <tr key={o.intentId ?? o.brokerTicket ?? Math.random()} data-testid="order-row">
                  <td className="pr-2">{txt(o.intentId?.slice(0, 14))}</td>
                  <td className="pr-2">{txt(o.brokerTicket)}</td>
                  <td className="pr-2">{txt(o.instrument)}</td>
                  <td className="pr-2">{txt(o.side)}</td>
                  <td className="pr-2">{num(o.quantity, 2)}</td>
                  <td className="pr-2">{txt(o.currentState)}</td>
                  <td className="pr-2">{txt(o.brokerStatus)}</td>
                  <td className="pr-2">{txt(o.scenarioId)}</td>
                  <td><ReconBadge required={o.reconciliation?.required} /></td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </Card>
  );
}

function PositionsTable({ positions }: { positions: PositionOperationalView[] }) {
  return (
    <Card title={`Positions (${positions.length})`} testId="positions-card"
          badges={<span className={BADGE} style={C.muted}>DERIVED</span>}>
      {positions.length === 0 ? (
        <div className="text-2xs text-text-muted" data-testid="positions-empty">
          No open positions in the projection.
        </div>
      ) : (
        <div className="overflow-x-auto">
          <table className="w-full text-2xs" data-testid="positions-table">
            <thead className="text-text-muted">
              <tr>
                {['Reference', 'Instrument', 'Side', 'Qty', 'Entry', 'P&L', 'SL', 'TP', 'Protection', ''].map((h) => (
                  <th key={h} className="text-left font-normal pr-2 pb-1">{h}</th>
                ))}
              </tr>
            </thead>
            <tbody className="mono text-text-2">
              {positions.map((p) => (
                <tr key={p.brokerPositionReference ?? Math.random()} data-testid="position-row">
                  <td className="pr-2">{txt(p.brokerPositionReference?.slice(0, 16))}</td>
                  <td className="pr-2">{txt(p.instrument)}</td>
                  <td className="pr-2">{txt(p.side)}</td>
                  <td className="pr-2">{num(p.quantity, 2)}</td>
                  <td className="pr-2">{num(p.entryPrice, 5)}</td>
                  <td className="pr-2">{num(p.unrealizedPnL, 2)}</td>
                  <td className="pr-2">{num(p.stopLoss, 5)}</td>
                  <td className="pr-2">{num(p.takeProfit, 5)}</td>
                  <td className="pr-2">{txt(p.protectionState)}</td>
                  <td>
                    <ReconBadge required={p.reconciliation?.required} />
                    {p.reconciliation?.locked && (
                      <span className={BADGE} style={C.muted} data-testid="position-locked">LOCKED</span>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </Card>
  );
}

function OperationsCard({ summary }: { summary: OperationalSummaryView }) {
  return (
    <Card title="Operations in flight" testId="operations-card"
          badges={<span className={BADGE} style={C.muted}>DERIVED</span>}>
      {summary.activeOperations.length === 0 ? (
        <div className="text-2xs text-text-muted" data-testid="operations-empty">
          No entity locks held — nothing in flight.
        </div>
      ) : (
        <div className="space-y-1" data-testid="operations-list">
          {summary.activeOperations.map((o) => (
            <Row key={o.entityRef ?? ''} label={txt(o.operation)}
                 value={`${txt(o.entityRef?.slice(0, 16))} · ${txt(o.acquiredAt)}`} />
          ))}
        </div>
      )}
    </Card>
  );
}

function ReconciliationCard({ summary }: { summary: OperationalSummaryView }) {
  const issues = summary.reconciliationIssues;
  return (
    <Card title={`Reconciliation (${issues.length})`} testId="reconciliation-card"
          badges={issues.some((i) => i.critical)
            ? <span className={BADGE} style={C.bad}>CRITICAL</span>
            : <span className={BADGE} style={C.ok}>CLEAN</span>}>
      {issues.length === 0 ? (
        <div className="text-2xs text-text-muted" data-testid="reconciliation-empty">
          No unresolved discrepancies in the latest run.
        </div>
      ) : (
        <ul className="space-y-1" data-testid="reconciliation-list">
          {issues.map((i, idx) => (
            <li key={`${i.class}-${i.entityId}-${idx}`} className="text-2xs"
                style={{ color: i.critical ? 'var(--negative)' : 'var(--text-muted)' }}>
              <span className="mono">{txt(i.class)}</span> · {txt(i.entityId)} — {txt(i.detail)}
            </li>
          ))}
        </ul>
      )}
    </Card>
  );
}

function SystemHealthCard({ summary }: { summary: OperationalSummaryView }) {
  return (
    <Card title="System health" testId="system-health-card"
          badges={<FreshnessBadge freshness={summary.freshness} />}>
      <div className="space-y-1">
        <Row label="Projection at" value={txt(summary.projectionTimestamp)} />
        <Row label="Source at" value={txt(summary.freshness?.sourceAt ?? null)} />
        <Row label="Projection age" value={age(summary.freshness?.ageSeconds ?? null)} />
        <Row label="Schema" value={txt(summary.schemaVersion)} />
        {Object.entries(summary.counts).map(([k, v]) => (
          <Row key={k} label={k} value={String(v)} />
        ))}
      </div>
    </Card>
  );
}

function WarningsCard({ summary }: { summary: OperationalSummaryView }) {
  return (
    <Card title={`Warnings (${summary.warnings.length})`} testId="warnings-card"
          badges={summary.warnings.length > 0
            ? <span className={BADGE} style={C.bad}>ATTENTION</span>
            : <span className={BADGE} style={C.ok}>NONE</span>}>
      {summary.warnings.length === 0 ? (
        <div className="text-2xs text-text-muted" data-testid="warnings-empty">
          No warnings raised by the projection.
        </div>
      ) : (
        <ul className="space-y-0.5" data-testid="warnings-list">
          {summary.warnings.map((w) => (
            <li key={w} className="text-2xs" style={{ color: 'var(--negative)' }}>⚠ {w}</li>
          ))}
        </ul>
      )}
    </Card>
  );
}

/* ── the dashboard: one query, models passed downward ────────────────────── */

export function OperationalDashboard() {
  const { data, isLoading, isError } = useQuery({
    queryKey: ['operations-summary'],
    queryFn: () => api.operationsSummary(),
    refetchInterval: 10_000,
    staleTime: 5_000,
    retry: false,
  });

  if (isError) {
    return (
      <section className="rounded-md border border-[color:var(--border)] bg-[color:var(--panel)] p-3"
               data-testid="operational-dashboard-error">
        <span className={BADGE} style={C.bad}>UNAVAILABLE</span>
        <p className="text-2xs text-text-muted mt-2">
          The operational projection could not be read. No operational state is shown —
          nothing is inferred locally.
        </p>
      </section>
    );
  }
  if (isLoading || !data) {
    return (
      <div className="text-2xs text-text-muted" data-testid="operational-dashboard-loading">
        Building operational projection…
      </div>
    );
  }

  return (
    <div className="space-y-4" data-testid="operational-dashboard">
      {/* M-HONESTY-FINAL: this rendered account BALANCE and EQUITY straight from
          the projection with only a MOCK badge — under the mock adapter that is
          the $100,000 fixture figure on an ordinary route. Badging is
          M-FLEET-1-era treatment; every other surface applies the provenance
          gate. It does now: only authoritative records render, and a rejected
          set reports absence rather than showing labelled fabrications. */}
      {authoritativeOnly(data.accounts as never[]).length === 0
        && authoritativeOnly(data.nodes as never[]).length === 0 ? (
        <section className="rounded-md border border-[color:var(--border)] bg-[color:var(--panel)] p-3"
                 data-testid="operational-dashboard-unauthoritative">
          <span className={BADGE} style={C.bad}>NO AUTHORITATIVE SOURCE</span>
          <p className="text-2xs text-text-muted mt-2">
            The operational projection answered, but no record carries authoritative
            provenance. The active broker adapter is not a live MT5 connection, so no
            node, account, order or position can be shown as operational truth.
          </p>
        </section>
      ) : (
      <>
      <div className="grid grid-cols-12 gap-4">
        {authoritativeOnly(data.nodes as never[]).map((n: typeof data.nodes[number]) => (
          <div key={n.nodeId} className="col-span-12 lg:col-span-6">
            <NodeCard node={n} />
          </div>
        ))}
        {authoritativeOnly(data.accounts as never[]).map((a: typeof data.accounts[number], i: number) => (
          <div key={a.accountFingerprint ?? i} className="col-span-12 lg:col-span-6">
            <AccountCard account={a} />
          </div>
        ))}
      </div>
      <div className="grid grid-cols-12 gap-4">
        <div className="col-span-12"><OrdersTable orders={authoritativeOnly(data.activeOrders as never[]) as typeof data.activeOrders} /></div>
        <div className="col-span-12"><PositionsTable positions={authoritativeOnly(data.openPositions as never[]) as typeof data.openPositions} /></div>
      </div>
      <div className="grid grid-cols-12 gap-4">
        <div className="col-span-12 lg:col-span-4"><OperationsCard summary={data} /></div>
        <div className="col-span-12 lg:col-span-4"><ReconciliationCard summary={data} /></div>
        <div className="col-span-12 lg:col-span-4"><WarningsCard summary={data} /></div>
        <div className="col-span-12 lg:col-span-4"><SystemHealthCard summary={data} /></div>
      </div>
      </>
      )}
    </div>
  );
}
