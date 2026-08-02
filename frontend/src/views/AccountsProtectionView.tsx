import { useAccountsProtection } from '@/hooks/useRepository';
import { Panel, EmptyState } from '@/components/structures/Panel';
import { PLValue, Badge, MetricStat } from '@/components/primitives';
import { numericOrNull, accountIdentityKey, PROV_NODE_MT5 } from '@/lib/operationalProvenance';
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
  // `rejections = []` is a render-boundary default, not laziness: a page that
  // white-screens because one field was absent tells an operator nothing at
  // all, which is strictly worse than the state it was trying to describe.
  const { accounts, status, detail, rejections = [] } = useAccountsProtection();

  // M-ACTIVATE-READINESS-1 — a NAMED refusal is shown before the generic empty
  // state. "No authoritative account source" is true when a node is reporting
  // the wrong account, and it is the wrong thing to tell an operator: the two
  // states require opposite actions — wait, versus stop and check the pinning.
  const refusals = rejections.length > 0 && (
    <div
      className="rounded-md p-3 border mb-3"
      data-testid="account-admission-refused"
      style={{ borderColor: 'var(--danger)', background: 'var(--panel-2)' }}
    >
      <div className="text-2xs uppercase tracking-widest mb-2"
           style={{ color: 'var(--danger)' }}>
        Account observation refused
      </div>
      {rejections.map((reason) => (
        <p key={reason} className="text-xs text-text-muted mb-1">{reason}</p>
      ))}
    </div>
  );

  if (status === 'unavailable') {
    return (
      <Shell>
        {refusals}
        <EmptyState
          title={rejections.length > 0
            ? 'Account observation refused'
            : 'No authoritative account source'}
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
      {refusals}
      {status === 'stale' && (
        <p className="text-2xs text-[color:var(--warning)] mb-3" data-testid="accounts-stale">
          These records are older than their freshness budget. Shown as last
          reported, not as current state.
        </p>
      )}
      {accounts.map((acct) => {
        // M-MT5-READ-1: a positional key would let one node's account inherit
        // another's row when a node goes quiet. An account with no identity at
        // all is not rendered rather than given an invented one.
        const key = accountIdentityKey(acct);
        if (key === null) return null;
        const relayed = acct.provenance === PROV_NODE_MT5;
        return (
          <Panel
            provenance="live"
            key={key}
            title={
              <span className="flex items-center gap-2">
                {acct.broker ?? acct.server ?? 'Broker unreported'} · {acct.currency ?? '—'}
                <span className="mono text-text-muted ml-2 text-2xs">
                  {(acct.accountFingerprint ?? 'unidentified').slice(0, 20)}…
                </span>
              </span>
            }
            actions={
              <span data-testid="account-live-badge">
                <Badge variant="mode" color="var(--mode-live)">
                  {relayed
                    ? `relayed by ${acct.nodeId ?? 'unnamed node'}`
                    : acct.connectionState ?? 'unknown'}
                </Badge>
              </span>
            }
          >
            {relayed && (
              <p className="text-2xs text-text-muted mb-3" data-testid="account-relay-note">
                Observed on the execution node&rsquo;s MT5 terminal
                {acct.observedAt ? ` at ${acct.observedAt}` : ''} and relayed to this
                Control Tower. The node is the broker authority; this tower holds no
                connection to the account and computes none of these figures.
              </p>
            )}
            <div className="grid grid-cols-4 gap-6 mb-4">
              <Stat label="Balance" value={numericOrNull(acct.balance)} money emphasise />
              <Stat label="Equity" value={numericOrNull(acct.equity)} money emphasise />
              <Stat label="Realised P/L today" value={numericOrNull(acct.realizedPnLToday)} money emphasise />
              <Stat label="Unrealised P/L" value={numericOrNull(acct.unrealizedPnL)} money emphasise />
              <Stat label="Open risk" value={numericOrNull(acct.openRisk)} money />
              <Stat label="Free margin" value={numericOrNull(acct.freeMargin)} money />
              <Stat label="Margin" value={numericOrNull(acct.margin)} money />
              <Stat label="Margin level" value={numericOrNull(acct.marginLevel)} />
              <Stat label="Leverage" value={numericOrNull(acct.leverage)} />
              <TriStat label="Terminal trading" value={acct.tradeAllowed} />
              <TriStat label="Algo trading" value={acct.tradeExpert} />
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
 * A tri-state permission flag from the terminal.
 *
 * `null` is not `false`. "The observer did not report whether trading is
 * permitted" and "the terminal forbids trading" are different facts, and the
 * second is alarming. Rendering the first as the second would raise a false
 * alarm; rendering it as "allowed" would suppress a real one.
 */
function TriStat({ label, value }: { label: string; value: boolean | null | undefined }) {
  // Anything that is not EXACTLY true or false is unreported. Testing `=== null`
  // alone let `undefined` fall through to the boolean branch and render
  // "blocked" — a fabricated denial, from a field the observer never sent.
  const reported = value === true || value === false;
  return (
    <MetricStat
      label={label}
      value={
        !reported ? (
          <span
            className="mono text-text-muted"
            data-testid="value-not-reported"
            title="Not reported by the observer. Unknown, not denied."
          >
            {NOT_DERIVABLE}
          </span>
        ) : (
          <span className="mono" style={{ color: value ? 'var(--positive)' : 'var(--warning)' }}>
            {value ? 'allowed' : 'blocked'}
          </span>
        )
      }
    />
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
