/**
 * LIVE-1 — the read-only broker panel.
 *
 * Displays live broker read state (account, balances, positions/orders counts)
 * derived by the backend from the active adapter. It is DISPLAY-ONLY:
 *   - clearly labelled LIVE READ ONLY and NO EXECUTION;
 *   - provenance (live MT5 vs mock fixture) is explicit;
 *   - unavailable / failed reads are shown as such, never as zeros or health;
 *   - there is NO button, no toggle, no execution control anywhere in this file.
 */
import { useQuery } from '@tanstack/react-query';
import { isAuthoritative } from '@/lib/operationalProvenance';
import { api, type ExecutionBrokerState } from '@/lib/api';

function fmt(n: number | null | undefined, suffix = ''): string {
  return n === null || n === undefined ? '—' : `${n}${suffix}`;
}

function Row({ label, value }: { label: string; value: string }) {
  return (
    <div className="flex items-baseline justify-between gap-3 text-2xs">
      <span className="text-text-muted">{label}</span>
      <span className="mono text-text-2">{value}</span>
    </div>
  );
}

function BrokerBody({ broker }: { broker: ExecutionBrokerState }) {
  // M-FLEET-2: was an inline provenance string comparison. Provenance is
  // decided in one module so a future value cannot be judged differently here
  // than on the fleet surfaces.
  const live = isAuthoritative(broker);
  const acct = broker.account;
  const connected = broker.connection === 'Connected';
  return (
    <div className="space-y-1.5" data-testid="broker-read-body" data-provenance={broker.provenance}>
      <div className="flex flex-wrap items-center gap-2 mb-1">
        <span
          className="text-2xs mono px-1.5 py-0.5 rounded-sm border"
          style={{ borderColor: live ? 'var(--caution, var(--warning))' : 'var(--text-muted)',
                   color: live ? 'var(--caution, var(--warning))' : 'var(--text-muted)' }}
          data-testid="broker-provenance"
        >
          {live ? 'LIVE (MT5)' : 'MOCK FIXTURE'}
        </span>
        <span
          className="text-2xs mono px-1.5 py-0.5 rounded-sm border"
          style={{ borderColor: connected ? 'var(--positive)' : 'var(--negative)',
                   color: connected ? 'var(--positive)' : 'var(--negative)' }}
          data-testid="broker-connection"
        >
          {connected ? 'CONNECTED' : broker.connection.toUpperCase()}
        </span>
      </div>

      {acct.available ? (
        <>
          <Row label="Account" value={acct.login_masked ?? '—'} />
          <Row label="Broker" value={acct.broker_company ?? '—'} />
          <Row label="Server" value={acct.server ?? '—'} />
          <Row label="Balance" value={fmt(acct.balance, ` ${acct.currency ?? ''}`)} />
          <Row label="Equity" value={fmt(acct.equity, ` ${acct.currency ?? ''}`)} />
          <Row label="Margin level" value={fmt(acct.margin_level, '%')} />
          <Row label="Leverage" value={acct.leverage ? `1:${acct.leverage}` : '—'} />
        </>
      ) : (
        <div className="text-2xs" style={{ color: 'var(--text-muted)' }} data-testid="broker-account-unavailable">
          Account read unavailable — {acct.code ?? 'unknown'}. No values are shown because
          none were read (nothing is invented).
        </div>
      )}

      <div className="mt-1 pt-1 border-t" style={{ borderColor: 'var(--border-subtle)' }}>
        <Row label="Open positions" value={fmt(broker.openPositions)} />
        <Row label="Open orders" value={fmt(broker.openOrders)} />
        <Row label="Recent executions" value={fmt(broker.recentExecutions)} />
        <Row label="Adapter kind" value={broker.kind} />
      </div>
    </div>
  );
}

export function BrokerReadPanel() {
  const { data, isLoading, isError } = useQuery({
    queryKey: ['execution-state'],
    queryFn: () => api.executionState(),
    refetchInterval: 10_000,
    staleTime: 5_000,
    retry: false,
  });

  return (
    <section
      className="rounded-md border border-[color:var(--border)] bg-[color:var(--panel)] p-3"
      aria-label="Broker (read only)"
      data-testid="broker-read-panel"
    >
      <header className="flex items-baseline justify-between gap-2 mb-1">
        <h3 className="text-xs uppercase tracking-wider text-text-muted">Broker (read only)</h3>
        <span
          className="text-2xs mono px-1.5 py-0.5 rounded-sm border"
          style={{ borderColor: 'var(--text-muted)', color: 'var(--text-muted)' }}
          data-testid="broker-read-scope"
        >
          LIVE READ ONLY · NO EXECUTION
        </span>
      </header>

      {isError && (
        <div className="text-2xs text-text-muted" data-testid="broker-read-error">
          Broker read unavailable — the backend could not be reached.
        </div>
      )}
      {isLoading && <div className="text-2xs text-text-muted">Reading broker state…</div>}
      {data?.broker && <BrokerBody broker={data.broker} />}

      <p className="text-2xs text-text-muted mt-2">
        This panel reads only — it can execute nothing. The single execution control
        (one market order, LIVE-2) lives in the Market order panel; every other broker
        mutation remains structurally unavailable.
      </p>
    </section>
  );
}
