import { useLedgerAnalytics } from '@/hooks/useRepository';
import { Panel, EmptyState } from '@/components/structures/Panel';
import { MetricStat, PLValue } from '@/components/primitives';
import { BarChart3 } from 'lucide-react';

/**
 * M-TRADES-2 — performance from the authoritative ledger, or nothing.
 *
 * This view previously ran `computeMetrics(trades.live)` over the FIXTURE
 * trades: expectancy, win rate, equity curve and grouped breakdowns, all
 * derived from two authored records. Worse than the fabrication itself was the
 * empty case — `computeMetrics([])` returns 0 trades / 0% / $0, a
 * mathematically valid report of a flat result nobody observed.
 *
 * Every number here is now computed by the backend from records that passed the
 * admission policy (MT5 execution origin, settled status, fully closed, outcome
 * and P&L evidence present). The frontend derives NOTHING — there is one set of
 * formulas and it lives beside the ledger.
 *
 * Under the development mock adapter every ledger record is `mock`, so this
 * correctly reports the empty state with an exclusion account. That is the
 * honest outcome, not a shortfall.
 */
export function AnalyticsView() {
  const a = useLedgerAnalytics();

  if (a.availability === 'unavailable') {
    return (
      <Shell>
        <EmptyState
          title="Analytics unavailable"
          description="The trade ledger could not be read, so no performance can be reported. This is not a statement that performance was flat."
          icon={<BarChart3 size={16} />}
        />
      </Shell>
    );
  }

  if (a.availability === 'empty') {
    return (
      <Shell>
        <EmptyState
          title="No admissible completed trades"
          description="The ledger answered and holds no trades that qualify for performance measurement. Deriving a win rate or expectancy from an empty set would report a flat result that was never observed."
          icon={<BarChart3 size={16} />}
        />
        {a.excludedCount > 0 && <Exclusions analytics={a} />}
      </Shell>
    );
  }

  const small = a.admittedCount < 30;
  return (
    <Shell>
      {/* Sample size is stated factually. No confidence score, no "healthy",
          no significance claim — M-CONF-1 removed fabricated certainty and
          this must not reintroduce it in statistical clothing. */}
      <p className="text-2xs text-text-muted mb-3" data-testid="analytics-sample">
        {a.admittedCount} completed trade{a.admittedCount === 1 ? '' : 's'} admitted
        {a.excludedCount > 0 ? ` · ${a.excludedCount} excluded` : ''}
        {small ? ' · small sample: rates below are arithmetic over these trades only' : ''}
        {a.accountCurrency ? ` · ${a.accountCurrency}` : ''}
      </p>

      <Panel provenance="live" title="Realised performance (gross)">
        <div className="grid grid-cols-4 gap-6">
          <Stat label="Trades" value={a.admittedCount} />
          <Stat label="Wins" value={a.wins} />
          <Stat label="Losses" value={a.losses} />
          <Stat label="Break-even" value={a.breakEven} />
          <Stat label="Win rate" value={a.winRate} percent />
          <Stat label="Gross P/L" value={a.grossRealizedPnL} money />
          <Stat label="Gross profit" value={a.grossProfit} money />
          <Stat label="Gross loss" value={a.grossLoss} money />
          <Stat label="Average win" value={a.averageWin} money />
          <Stat label="Average loss" value={a.averageLoss} money />
          <Stat label="Profit factor" value={a.profitFactor} />
          <Stat label="Expectancy / trade" value={a.expectancyGross} money />
          <Stat label="Max drawdown" value={a.maxDrawdownGross} money />
        </div>
      </Panel>

      <Panel provenance="live" title="Net performance">
        {a.netAvailable ? (
          <div className="grid grid-cols-4 gap-6">
            <Stat label="Net realised P/L" value={a.netRealizedPnL} money emphasise />
          </div>
        ) : (
          <div className="text-xs text-text-muted" data-testid="net-unavailable">
            {a.netUnavailableReason}
          </div>
        )}
      </Panel>

      <Panel provenance="live" title="R multiples">
        {a.rAvailable ? (
          <div className="grid grid-cols-4 gap-6">
            <Stat label="Average R" value={a.averageR} />
            <Stat label="Expectancy (R)" value={a.expectancyR} />
          </div>
        ) : (
          <div className="text-xs text-text-muted" data-testid="r-unavailable">
            {a.rUnavailableReason}
          </div>
        )}
      </Panel>

      {a.excludedCount > 0 && <Exclusions analytics={a} />}
    </Shell>
  );
}

/** Exclusions are auditable: an operator can see what was refused and why. */
function Exclusions({ analytics }: { analytics: ReturnType<typeof useLedgerAnalytics> }) {
  const byReason = analytics.exclusions.reduce<Record<string, number>>((acc, e) => {
    acc[e.reason] = (acc[e.reason] ?? 0) + 1;
    return acc;
  }, {});
  return (
    <Panel provenance="live" title={`Excluded records (${analytics.excludedCount})`}>
      <ul className="text-xs mono space-y-1" data-testid="analytics-exclusions">
        {Object.entries(byReason).map(([reason, count]) => (
          <li key={reason}>{count} × {reason}</li>
        ))}
      </ul>
    </Panel>
  );
}

function Shell({ children }: { children: React.ReactNode }) {
  return (
    <div className="p-6 h-full min-h-0 overflow-auto space-y-4" data-testid="analytics-view">
      <div className="mb-2">
        <h1 className="text-2xl font-semibold text-text tracking-tight">Analytics</h1>
        <p className="text-xs text-text-muted mt-1">
          Realised performance from admissible broker-executed trades
        </p>
      </div>
      {children}
    </div>
  );
}

/** `null` renders "—". A metric that is not derivable is never shown as zero. */
function Stat({
  label, value, money = false, percent = false, emphasise = false,
}: {
  label: string; value: number | null; money?: boolean; percent?: boolean; emphasise?: boolean;
}) {
  return (
    <MetricStat
      label={label}
      emphasise={emphasise}
      value={
        value === null || value === undefined ? (
          <span className="mono text-text-muted" data-testid="metric-not-derivable"
                title="Not derivable from the admitted records. Unknown, not zero.">
            —
          </span>
        ) : money ? (
          <PLValue value={value} />
        ) : percent ? (
          <span className="mono">{(value * 100).toFixed(1)}%</span>
        ) : (
          <span className="mono">{value}</span>
        )
      }
    />
  );
}
