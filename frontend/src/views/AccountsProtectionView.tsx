import { useAccountsProtection } from '@/hooks/useRepository';
import { Panel, EmptyState } from '@/components/structures/Panel';
import { PLValue, Badge, MetricStat } from '@/components/primitives';
import { numericOrNull } from '@/lib/operationalProvenance';
import { Wallet } from 'lucide-react';

/**
 * M-FLEET-2 — Accounts & Protection renders AUTHORITATIVE accounts only.
 *
 * Until now this page rendered the fixture world's funded and demo accounts:
 * a $100,000 balance, $100,412 equity, a "funded" badge. M-FLEET-1 labelled
 * them; this milestone removes them from the ordinary route entirely.
 *
 * The accounts now come from the operational projection, passed through the
 * single provenance gate. Only `live_mt5` records survive. Under the
 * development-default mock adapter the projection's own records are stamped
 * `mock-fixture` and carry those very same invented figures, so they are
 * rejected and this page reports `unavailable` — the honest answer, and the
 * reason the page is deliberately empty in development.
 *
 * Every numeric field is `number | null` at the source (`realizedPnLToday`,
 * `openRisk` are null when not derivable). That distinction is preserved to the
 * pixel: `null` renders "—", never 0.
 */

const NOT_DERIVABLE = '—';

export function AccountsProtectionView() {
  const { accounts, status, detail } = useAccountsProtection();

  if (status === 'unavailable') {
    return (
      <Shell>
        <EmptyState
          title="No authoritative account source"
          description={detail}
          icon={<Wallet size={16} />}
        />
      </Shell>
    );
  }

  if (status === 'empty' || accounts.length === 0) {
    return (
      <Shell>
        <EmptyState
          title="No accounts reported"
          description="The authoritative operational source answered and reported no accounts. This is a genuine empty result, not missing data."
          icon={<Wallet size={16} />}
        />
      </Shell>
    );
  }

  return (
    <Shell>
      {status === 'stale' && (
        <p className="text-2xs text-[color:var(--warning)] mb-3" data-testid="accounts-stale">
          These records are older than their freshness budget. Shown as last
          reported, not as current state.
        </p>
      )}
      {accounts.map((acct, i) => {
        const key = acct.accountFingerprint ?? `account-${i}`;
        return (
          <Panel
            provenance="live"
            key={key}
            title={
              <span className="flex items-center gap-2">
                {acct.broker ?? 'Broker unreported'} · {acct.currency ?? '—'}
                <span className="mono text-text-muted ml-2 text-2xs">{key.slice(0, 20)}…</span>
              </span>
            }
            actions={
              <span data-testid="account-live-badge">
                <Badge variant="mode" color="var(--mode-live)">
                  {acct.connectionState ?? 'unknown'}
                </Badge>
              </span>
            }
          >
            <div className="grid grid-cols-4 gap-6 mb-4">
              <Stat label="Balance" value={numericOrNull(acct.balance)} money emphasise />
              <Stat label="Equity" value={numericOrNull(acct.equity)} money emphasise />
              <Stat label="Realised P/L today" value={numericOrNull(acct.realizedPnLToday)} money emphasise />
              <Stat label="Unrealised P/L" value={numericOrNull(acct.unrealizedPnL)} money emphasise />
              <Stat label="Open risk" value={numericOrNull(acct.openRisk)} money />
              <Stat label="Margin" value={numericOrNull(acct.margin)} money />
              <Stat label="Margin level" value={numericOrNull(acct.marginLevel)} />
              <Stat label="Leverage" value={numericOrNull(acct.leverage)} />
            </div>

            {/* M-RISK-1: unchanged. Funded-account rules must be real
                configuration, real telemetry, or honestly unavailable. */}
            <div
              className="rounded-md p-3 border"
              data-testid="funded-rules-unavailable"
              style={{ borderColor: 'var(--border-subtle)', background: 'var(--panel-2)' }}
            >
              <div className="text-2xs uppercase tracking-widest text-text-muted mb-2">
                Funded Ruleset
              </div>
              <div className="text-xs text-text-muted">
                No funded-account rule source is configured — external
                account-program limits are unavailable. Node-enforced safeguards
                (daily-loss, position caps) are published via node telemetry when
                an execution node is connected.
              </div>
            </div>
          </Panel>
        );
      })}
    </Shell>
  );
}

function Shell({ children }: { children: React.ReactNode }) {
  return (
    <div className="p-6 h-full min-h-0 overflow-auto space-y-4">
      <div className="mb-4">
        <h1 className="text-2xl font-semibold text-text tracking-tight">Accounts &amp; Protection</h1>
        <p className="text-xs text-text-muted mt-1">
          Funded rulesets · RiskState · buffers · lockouts · overrides (reason-gated)
        </p>
      </div>
      {children}
    </div>
  );
}

/**
 * `null` is not zero. The projection reports null when a figure is not
 * derivable from the evidence it has; rendering that as 0 would invent a
 * measurement — a flat P/L, an unlevered account, a zero margin requirement.
 */
function Stat({
  label,
  value,
  money = false,
  emphasise = false,
}: {
  label: string;
  value: number | null;
  money?: boolean;
  emphasise?: boolean;
}) {
  return (
    <MetricStat
      label={label}
      emphasise={emphasise}
      value={
        value === null ? (
          <span
            className="mono text-text-muted"
            data-testid="value-not-reported"
            title="Not reported by the operational source. Unknown, not zero."
          >
            {NOT_DERIVABLE}
          </span>
        ) : money ? (
          <PLValue value={value} showSign={false} />
        ) : (
          <span className="mono">{value}</span>
        )
      }
    />
  );
}
