import { useAccountsProtection } from '@/hooks/useRepository';
import { Panel, EmptyState } from '@/components/structures/Panel';
import { PLValue, Badge, MetricStat } from '@/components/primitives';
import { fmtPercent } from '@/lib/format';
import { Wallet } from 'lucide-react';

/**
 * M-FLEET-1 — Accounts & Protection, truth-first.
 *
 * Two genuine fabrications lived in the aggregates on this page, and both came
 * from the same mistake: treating "no deployments" as "zero".
 *
 *   Math.min(...[], 100)  ->  a drawdown BUFFER of 100% for an account with
 *                             nothing deployed — an invented safety margin, and
 *                             the most dangerous number on the page.
 *   [].reduce(..., 0)     ->  Daily P/L "$0.00" for an account whose P/L is
 *                             simply unknown.
 *
 * An empty set has no minimum and no meaningful sum. Both now render an explicit
 * "—", because an operator reading "100% buffer, $0.00 loss" would conclude the
 * account is safe and flat. Neither is a fact this page possesses.
 *
 * Balance and equity remain FIXTURE values (no production account source is
 * wired yet — the real projection is `/api/operations/accounts`), so they are
 * labelled as fixture rather than presented as operational truth.
 */

/** An empty set has no minimum. Never substitute a full buffer. */
const NOT_DERIVABLE = '—';

export function AccountsProtectionView() {
  const { accounts, deployments, available, provenance, provenanceDetail } = useAccountsProtection();

  if (!available) {
    return (
      <div className="p-6 h-full min-h-0 overflow-auto">
        <Header />
        <EmptyState
          title="Accounts unavailable"
          description={
            provenanceDetail ||
            'Account and protection records have no production source in this process. No balances can be reported.'
          }
          icon={<Wallet size={16} />}
        />
      </div>
    );
  }

  if (accounts.length === 0) {
    return (
      <div className="p-6 h-full min-h-0 overflow-auto">
        <Header />
        <EmptyState
          title="No accounts"
          description="The account source returned no records. This is a genuine empty result, not missing data."
          icon={<Wallet size={16} />}
        />
      </div>
    );
  }

  const isFixture = provenance === 'fixture';

  return (
    <div className="p-6 h-full min-h-0 overflow-auto space-y-4">
      <Header />

      {accounts.map((acct) => {
        const acctDeployments = deployments.filter((d) => d.accountId === acct.accountId);
        // Aggregates are DERIVABLE only when there is something to aggregate.
        // `null` means "not derivable from the evidence this page has" and is
        // rendered as "—", never as 0 and never as a full buffer.
        const derivable = acctDeployments.length > 0;
        const totalDailyPl = derivable
          ? acctDeployments.reduce((s, d) => s + d.riskState.dailyPl, 0)
          : null;
        const totalFloating = derivable
          ? acctDeployments.reduce((s, d) => s + d.riskState.floatingPl, 0)
          : null;
        const totalRiskToday = derivable
          ? acctDeployments.reduce((s, d) => s + d.riskState.riskTodayPct, 0)
          : null;
        const minDdBuffer = derivable
          ? Math.min(...acctDeployments.map((d) => d.riskState.ddBufferPct))
          : null;

        return (
          <Panel
            provenance="fixture"
            key={acct.accountId}
            title={
              <span className="flex items-center gap-2">
                {acct.type.toUpperCase()} · {acct.baseCurrency}
                <span className="mono text-text-muted ml-2 text-2xs">
                  {acct.accountId.slice(0, 20)}…
                </span>
              </span>
            }
            actions={
              <span className="flex items-center gap-2">
                {/* M-FLEET-1: a fixture account must never wear the live colour.
                    The record's own type is still shown; what changes is that it
                    can no longer be read as an operational account. */}
                {isFixture && (
                  <span data-testid={`account-fixture-tag-${acct.accountId}`}>
                    <Badge variant="mode" color="var(--mode-mock)">FIXTURE</Badge>
                  </span>
                )}
                <Badge variant="mode" color={isFixture ? 'var(--mode-mock)' : 'var(--mode-live)'}>
                  {acct.type}
                </Badge>
              </span>
            }
          >
            {isFixture && (
              <p
                className="text-2xs text-text-muted mb-3"
                data-testid={`account-fixture-note-${acct.accountId}`}
              >
                Balance and equity below are development fixture values, not
                broker-reported figures. Genuine account state is projected at
                <span className="mono"> /api/operations/accounts</span> when a real
                adapter is connected.
              </p>
            )}

            <div className="grid grid-cols-4 gap-6 mb-4">
              <MetricStat label="Balance" value={<PLValue value={acct.balance} showSign={false} />} emphasise />
              <MetricStat label="Equity" value={<PLValue value={acct.equity} showSign={false} />} emphasise />
              <MetricStat
                label="Daily P/L"
                value={<Derived value={totalDailyPl} render={(v) => <PLValue value={v} />} />}
                emphasise
              />
              <MetricStat
                label="Floating"
                value={<Derived value={totalFloating} render={(v) => <PLValue value={v} />} />}
                emphasise
              />
              <MetricStat
                label="Risk today"
                value={
                  <Derived
                    value={totalRiskToday}
                    render={(v) => <span className="mono">{fmtPercent(v)}</span>}
                  />
                }
              />
              <MetricStat
                label="DD buffer"
                value={
                  <Derived
                    value={minDdBuffer}
                    render={(v) => (
                      <span
                        className="mono"
                        style={{ color: v < 40 ? 'var(--warning)' : 'var(--positive)' }}
                      >
                        {fmtPercent(v, 0)}
                      </span>
                    )}
                  />
                }
              />
              <MetricStat label="Deployments" value={acctDeployments.length} mono />
              <MetricStat label="Timezone" value={acct.timezone} />
            </div>

            {/* M-RISK-1: the fixture funded-rules grid is GONE. Funded-account
                rules must be real configuration, real telemetry, or honestly
                unavailable — never fixture-derived. */}
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
    </div>
  );
}

function Header() {
  return (
    <div className="mb-4">
      <h1 className="text-2xl font-semibold text-text tracking-tight">Accounts &amp; Protection</h1>
      <p className="text-xs text-text-muted mt-1">
        Funded rulesets · RiskState · buffers · lockouts · overrides (reason-gated)
      </p>
    </div>
  );
}

/**
 * Renders a derived aggregate, or an explicit "not derivable" marker.
 * `null` is NOT zero: an account with no deployments has an unknown P/L and an
 * unknown drawdown buffer, and saying "0" or "100%" would invent both.
 */
function Derived({
  value,
  render,
}: {
  value: number | null;
  render: (v: number) => React.ReactNode;
}) {
  if (value === null) {
    return (
      <span
        className="mono text-text-muted"
        data-testid="aggregate-not-derivable"
        title="No deployments on this account, so this value cannot be derived. It is unknown, not zero."
      >
        {NOT_DERIVABLE}
      </span>
    );
  }
  return <>{render(value)}</>;
}
