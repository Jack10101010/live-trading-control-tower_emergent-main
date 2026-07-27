/**
 * LIVE-4C — the read-only Trade Ledger.
 *
 * The ledger records what ACTUALLY happened to closed trades. This panel is a
 * pure renderer of already-recorded ledger facts:
 *
 *   - NO financial reconstruction happens here. Gross, costs, net and realized
 *     R are printed exactly as the backend recorded them;
 *   - an unavailable number stays unavailable ("—"), never rendered as 0;
 *   - there are no edit, delete or PnL-adjustment controls of any kind;
 *   - completeness and conflict states are surfaced as explicit badges.
 */
import { useMemo, useState, type CSSProperties } from 'react';
import { useQuery } from '@tanstack/react-query';
import {
  api,
  type ClosedTradeOperationalView,
  type LedgerEventView,
  type ProjectionFreshness,
} from '@/lib/api';

const BADGE = 'text-2xs mono px-1.5 py-0.5 rounded-sm border';
const C = {
  ok: { borderColor: 'var(--positive)', color: 'var(--positive)' },
  bad: { borderColor: 'var(--negative)', color: 'var(--negative)' },
  warn: { borderColor: 'var(--caution, var(--warning))', color: 'var(--caution, var(--warning))' },
  muted: { borderColor: 'var(--border-subtle)', color: 'var(--text-muted)' },
} as const;

/* ── shared formatting: the ONLY place ledger values are formatted ────────── */

function money(v: number | null | undefined, ccy: string | null | undefined): string {
  if (v === null || v === undefined) return '—';        // unavailable ≠ zero
  return `${v.toFixed(2)}${ccy ? ` ${ccy}` : ''}`;
}
function price(v: number | null | undefined): string {
  return v === null || v === undefined ? '—' : v.toFixed(5);
}
function qty(v: number | null | undefined): string {
  return v === null || v === undefined ? '—' : v.toFixed(2);
}
function rMultiple(v: number | null | undefined): string {
  return v === null || v === undefined ? '—' : `${v.toFixed(2)}R`;
}
function txt(v: string | null | undefined): string {
  return v === null || v === undefined || v === '' ? '—' : v;
}
function duration(seconds: number | null | undefined): string {
  if (seconds === null || seconds === undefined) return '—';
  if (seconds < 60) return `${Math.round(seconds)}s`;
  if (seconds < 3600) return `${Math.round(seconds / 60)}m`;
  return `${(seconds / 3600).toFixed(1)}h`;
}

function StatusBadges({ trade }: { trade: ClosedTradeOperationalView }) {
  const badges: Array<[string, CSSProperties]> = [];
  if (trade.status === 'FINALIZED') badges.push(['FINALIZED', C.ok]);
  if (trade.status === 'AMENDED') badges.push(['AMENDED', C.warn]);
  if (trade.status === 'INCOMPLETE') badges.push(['INCOMPLETE', C.warn]);
  if (trade.status === 'CONFLICTED') badges.push(['CONFLICTED', C.bad]);
  if (trade.costCompleteness && trade.costCompleteness !== 'complete') {
    badges.push(['COSTS PENDING', C.warn]);
  }
  if (trade.riskCompleteness && trade.riskCompleteness !== 'complete') {
    badges.push(['RISK UNAVAILABLE', C.muted]);
  }
  if (!trade.scenarioId) badges.push(['SCENARIO UNLINKED', C.muted]);
  if (trade.warnings.some((w) => w.includes('reconciliation'))) {
    badges.push(['RECONCILIATION REQUIRED', C.bad]);
  }
  return (
    <span className="flex flex-wrap gap-1" data-testid="ledger-status-badges">
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

function List({ label, values }: { label: string; values: unknown }) {
  const items = Array.isArray(values) ? values.map(String) : [];
  return (
    <Row label={label} value={items.length ? items.join(', ') : '—'} />
  );
}

/* ── detail view: lineage, deals, accounting, management, events ─────────── */

function TradeDetail({ trade, events }: {
  trade: ClosedTradeOperationalView; events: LedgerEventView[];
}) {
  const detail = (trade.detail ?? {}) as Record<string, any>;
  const inner = (detail.trade ?? {}) as Record<string, any>;
  const lineage = (inner.lineage ?? {}) as Record<string, any>;
  const ccy = trade.accountCurrency;
  return (
    <div className="space-y-2 pt-2 border-t" style={{ borderColor: 'var(--border-subtle)' }}
         data-testid="ledger-trade-detail">
      <div className="grid grid-cols-1 md:grid-cols-2 gap-x-6 gap-y-1">
        <div className="space-y-1" data-testid="ledger-lineage">
          <div className="text-2xs uppercase tracking-wider text-text-muted">Lineage</div>
          <Row label="Trade" value={txt(trade.tradeId)} />
          <Row label="Scenario" value={txt(trade.scenarioId)} />
          <Row label="Recommendation" value={txt(lineage.recommendationId)} />
          <List label="Intents" values={lineage.intentIds} />
          <List label="Operations" values={lineage.internalOperationIds} />
          <List label="Broker orders" values={lineage.brokerOrderIds} />
          <List label="Broker positions" values={lineage.brokerPositionIds} />
          <List label="Broker deals" values={lineage.brokerDealIds} />
          <Row label="Node" value={txt(lineage.nodeId)} />
          <Row label="Account" value={txt(lineage.accountFingerprint)} />
          <Row label="Origin" value={txt(trade.origin)} />
        </div>
        <div className="space-y-1">
          <div className="text-2xs uppercase tracking-wider text-text-muted">Timing</div>
          <Row label="Opened" value={txt(trade.openedAt)} />
          <Row label="Closed" value={txt(trade.closedAt)} />
          <Row label="Duration" value={duration(trade.durationSeconds)} />
          <div className="text-2xs uppercase tracking-wider text-text-muted pt-1">
            Financial
          </div>
          <Row label="Gross realized" value={money(trade.grossRealizedPnL, ccy)} />
          <Row label="Commission" value={money(inner.commission, ccy)} />
          <Row label="Fees" value={money(inner.fees, ccy)} />
          <Row label="Swap" value={money(inner.swap, ccy)} />
          <Row label="Total costs" value={money(trade.totalCosts, ccy)} />
          <Row label="Net realized" value={money(trade.netRealizedPnL, ccy)} />
          <Row label="Cost completeness" value={txt(trade.costCompleteness)} />
          <div className="text-2xs uppercase tracking-wider text-text-muted pt-1">Risk</div>
          <Row label="Initial stop" value={price(inner.initialStopPrice)} />
          <Row label="Initial risk" value={money(inner.initialRiskAmount, ccy)} />
          <Row label="Realized R" value={rMultiple(trade.realizedR)} />
          <Row label="R basis" value={txt(inner.realizedRBasis)} />
          <Row label="Risk completeness" value={txt(trade.riskCompleteness)} />
          <div className="text-2xs uppercase tracking-wider text-text-muted pt-1">
            Execution quality
          </div>
          <Row label="Requested entry" value={price(inner.requestedEntryPrice)} />
          <Row label="Average fill" value={price(inner.averageFillPrice)} />
          <Row label="Entry slippage" value={price(inner.entrySlippage)} />
          <Row label="Quality completeness"
               value={txt(inner.executionQualityCompleteness)} />
        </div>
      </div>

      {(detail.managementHistory ?? []).length > 0 && (
        <div data-testid="ledger-management-history">
          <div className="text-2xs uppercase tracking-wider text-text-muted">
            Management history
          </div>
          <ul className="space-y-0.5">
            {(detail.managementHistory as any[]).map((m, i) => (
              <li key={`${m.intentId}-${i}`} className="text-2xs mono text-text-2">
                {txt(m.at)} · {txt(m.command)} → {txt(m.state)} ({txt(m.reason)})
              </li>
            ))}
          </ul>
        </div>
      )}

      {(detail.reconciliationFindings ?? []).length > 0 && (
        <div data-testid="ledger-reconciliation">
          <div className="text-2xs uppercase tracking-wider text-text-muted">
            Reconciliation evidence
          </div>
          <ul className="space-y-0.5">
            {(detail.reconciliationFindings as any[]).map((f, i) => (
              <li key={i} className="text-2xs" style={{ color: 'var(--negative)' }}>
                {txt(f.class)} · {txt(f.entityId)} — {txt(f.detail)}
              </li>
            ))}
          </ul>
        </div>
      )}

      {trade.warnings.length > 0 && (
        <div data-testid="ledger-warnings">
          <div className="text-2xs uppercase tracking-wider text-text-muted">
            Completeness warnings
          </div>
          <ul className="space-y-0.5">
            {trade.warnings.map((w) => (
              <li key={w} className="text-2xs" style={{ color: 'var(--caution, var(--warning))' }}>
                ⚠ {w}
              </li>
            ))}
          </ul>
        </div>
      )}

      <div data-testid="ledger-event-history">
        <div className="text-2xs uppercase tracking-wider text-text-muted">
          Ledger events (v{trade.version})
        </div>
        <ul className="space-y-0.5">
          {events.map((e) => (
            <li key={e.eventId} className="text-2xs mono text-text-2">
              #{e.sequence} {e.eventType} · {e.occurredAt}
            </li>
          ))}
        </ul>
      </div>
    </div>
  );
}

/* ── the ledger panel ────────────────────────────────────────────────────── */

export function TradeLedgerPanel() {
  const [statusFilter, setStatusFilter] = useState('');
  const [instrumentFilter, setInstrumentFilter] = useState('');
  const [selected, setSelected] = useState<string | null>(null);

  const { data, isLoading, isError } = useQuery({
    queryKey: ['ledger-trades'],
    queryFn: () => api.ledgerTrades({ limit: 50 }),
    refetchInterval: 30_000,
    staleTime: 10_000,
    retry: false,
  });
  const { data: historyData } = useQuery({
    queryKey: ['ledger-history', selected],
    queryFn: () => api.ledgerTradeHistory(selected as string),
    enabled: Boolean(selected),
    retry: false,
  });

  const trades = data?.trades ?? [];
  const filtered = useMemo(
    () => trades.filter((t) =>
      (!statusFilter || t.status === statusFilter) &&
      (!instrumentFilter || t.instrument === instrumentFilter)),
    [trades, statusFilter, instrumentFilter]);
  const options = useMemo(() => ({
    status: Array.from(new Set(trades.map((t) => t.status).filter(Boolean))).sort(),
    instrument: Array.from(new Set(trades.map((t) => t.instrument).filter(Boolean))).sort() as string[],
  }), [trades]);

  const summary = data?.summary;
  const ccy = summary?.accountCurrency ?? null;
  const select = 'text-2xs mono px-1.5 py-1 rounded-sm border bg-transparent';

  return (
    <section className="rounded-md border border-[color:var(--border)] bg-[color:var(--panel)] p-3"
             aria-label="Trade ledger" data-testid="trade-ledger-panel">
      <header className="flex flex-wrap items-baseline justify-between gap-2 mb-1.5">
        <h3 className="text-xs uppercase tracking-wider text-text-muted">Trade ledger</h3>
        <span className={BADGE} style={C.muted} data-testid="ledger-scope">
          READ ONLY · HISTORICAL ACCOUNTING
        </span>
      </header>

      {isError && (
        <div className="text-2xs" style={{ color: 'var(--negative)' }} data-testid="ledger-error">
          UNAVAILABLE — the trade ledger could not be read. No ledger values are
          shown; nothing is reconstructed locally.
        </div>
      )}
      {isLoading && (
        <div className="text-2xs text-text-muted" data-testid="ledger-loading">
          Loading ledger…
        </div>
      )}

      {data && !data.available && (
        <div className="text-2xs" style={{ color: 'var(--negative)' }} data-testid="ledger-unavailable">
          UNAVAILABLE — {txt(data.code)}.
        </div>
      )}

      {data?.available && summary && (
        <>
          <div className="grid grid-cols-2 md:grid-cols-4 gap-x-4 gap-y-1 mb-2"
               data-testid="ledger-summary">
            <Row label="Finalized" value={String(summary.finalizedTradeCount)} />
            <Row label="Incomplete" value={String(summary.incompleteTradeCount)} />
            <Row label="Conflicted" value={String(summary.conflictedTradeCount)} />
            <Row label="Amended" value={String(summary.amendedTradeCount)} />
            <Row label="Gross realized" value={money(summary.grossRealizedPnL, ccy)} />
            <Row label="Total costs" value={money(summary.totalCosts, ccy)} />
            <Row label="Net realized" value={money(summary.netRealizedPnL, ccy)} />
            <Row label="Latest close" value={txt(summary.latestClosedAt)} />
          </div>

          <div className="flex flex-wrap gap-2 mb-2" data-testid="ledger-filters">
            <select className={select} style={C.muted} value={statusFilter}
                    onChange={(e) => setStatusFilter(e.target.value)}
                    data-testid="ledger-filter-status" aria-label="All statuses">
              <option value="">All statuses</option>
              {options.status.map((o) => <option key={o} value={o}>{o}</option>)}
            </select>
            <select className={select} style={C.muted} value={instrumentFilter}
                    onChange={(e) => setInstrumentFilter(e.target.value)}
                    data-testid="ledger-filter-instrument" aria-label="All instruments">
              <option value="">All instruments</option>
              {options.instrument.map((o) => <option key={o} value={o}>{o}</option>)}
            </select>
          </div>

          {filtered.length === 0 ? (
            <div className="text-2xs text-text-muted" data-testid="ledger-empty">
              {trades.length === 0
                ? 'No ledger entries recorded yet. Entries appear once broker history is observed.'
                : 'No ledger entries match the current filters.'}
            </div>
          ) : (
            <div className="overflow-x-auto">
              <table className="w-full text-2xs" data-testid="ledger-table">
                <thead className="text-text-muted">
                  <tr>
                    {['Closed', 'Trade', 'Scenario', 'Instrument', 'Side', 'Qty',
                      'Avg entry', 'Avg exit', 'Gross', 'Costs', 'Net', 'R',
                      'Exit', 'Status', 'Provenance'].map((h) => (
                      <th key={h} className="text-left font-normal pr-2 pb-1">{h}</th>
                    ))}
                  </tr>
                </thead>
                <tbody className="mono text-text-2">
                  {filtered.map((t) => (
                    <tr key={t.tradeId} data-testid="ledger-row"
                        onClick={() => setSelected(selected === t.tradeId ? null : t.tradeId)}
                        style={{ cursor: 'pointer' }}>
                      <td className="pr-2">{txt(t.closedAt)}</td>
                      <td className="pr-2">{t.tradeId.slice(0, 12)}</td>
                      <td className="pr-2">{t.scenarioId ? t.scenarioId.slice(0, 12) : '—'}</td>
                      <td className="pr-2">{txt(t.instrument)}</td>
                      <td className="pr-2">{txt(t.side)}</td>
                      <td className="pr-2">{qty(t.quantity)}</td>
                      <td className="pr-2">{price(t.averageEntryPrice)}</td>
                      <td className="pr-2">{price(t.averageExitPrice)}</td>
                      <td className="pr-2">{money(t.grossRealizedPnL, null)}</td>
                      <td className="pr-2">{money(t.totalCosts, null)}</td>
                      <td className="pr-2">{money(t.netRealizedPnL, null)}</td>
                      <td className="pr-2">{rMultiple(t.realizedR)}</td>
                      <td className="pr-2">{txt(t.exitClassification)}</td>
                      <td className="pr-2"><StatusBadges trade={t} /></td>
                      <td className="pr-2">{txt(t.provenance)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}

          {selected && filtered.some((t) => t.tradeId === selected) && (
            <TradeDetail trade={filtered.find((t) => t.tradeId === selected)!}
                         events={historyData?.events ?? []} />
          )}
        </>
      )}

      <p className="text-2xs text-text-muted mt-2">
        Historical accounting only. Values are recorded by the ledger — this view performs
        no financial reconstruction, and an unavailable figure is shown as “—”, never as
        zero. There are no edit, delete or adjustment controls.
      </p>
    </section>
  );
}
