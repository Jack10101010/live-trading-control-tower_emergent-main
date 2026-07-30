import { useAccountsProtection } from '@/hooks/useRepository';
import { Panel } from '@/components/structures/Panel';
import { KeyValueGrid, PLValue, Badge, MetricStat } from '@/components/primitives';
import { fmtMoney, fmtPercent } from '@/lib/format';

export function AccountsProtectionView() {
  const { accounts, deployments } = useAccountsProtection();

  return (
    <div className="p-6 h-full min-h-0 overflow-auto space-y-4">
      <div>
        <h1 className="text-2xl font-semibold text-text tracking-tight">Accounts & Protection</h1>
        <p className="text-xs text-text-muted mt-1">
          Funded rulesets · RiskState · buffers · lockouts · overrides (reason-gated)
        </p>
      </div>

      {accounts.map((acct) => {
        const acctDeployments = deployments.filter((d) => d.accountId === acct.accountId);
        const totalDailyPl = acctDeployments.reduce((s, d) => s + d.riskState.dailyPl, 0);
        const totalFloating = acctDeployments.reduce((s, d) => s + d.riskState.floatingPl, 0);
        const minDdBuffer = Math.min(...acctDeployments.map((d) => d.riskState.ddBufferPct), 100);
        const totalRiskToday = acctDeployments.reduce((s, d) => s + d.riskState.riskTodayPct, 0);

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
              <Badge variant="mode" color={acct.type === 'funded' ? 'var(--mode-live)' : 'var(--mode-demo)'}>
                {acct.type}
              </Badge>
            }
          >
            <div className="grid grid-cols-4 gap-6 mb-4">
              <MetricStat label="Balance" value={<PLValue value={acct.balance} showSign={false} />} emphasise />
              <MetricStat label="Equity" value={<PLValue value={acct.equity} showSign={false} />} emphasise />
              <MetricStat label="Daily P/L" value={<PLValue value={totalDailyPl} />} emphasise />
              <MetricStat label="Floating" value={<PLValue value={totalFloating} />} emphasise />
              <MetricStat
                label="Risk today"
                value={<span className="mono">{fmtPercent(totalRiskToday)}</span>}
              />
              <MetricStat
                label="DD buffer"
                value={
                  <span
                    className="mono"
                    style={{ color: minDdBuffer < 40 ? 'var(--warning)' : 'var(--positive)' }}
                  >
                    {fmtPercent(minDdBuffer, 0)}
                  </span>
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
