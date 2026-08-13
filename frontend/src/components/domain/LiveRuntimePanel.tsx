/**
 * LIVE-5A — the live dashboard.
 *
 * ONE query backs every card here (`api.liveRuntime()`), so the whole panel
 * always describes a single instant. The browser:
 *
 *   - never talks to MT5;
 *   - never reads the broker;
 *   - never calculates a spread, an age, a position or a runtime state.
 *
 * Every number below is printed exactly as the projection supplied it. A value
 * the broker could not supply renders "—", never 0.
 */
import { useQuery } from '@tanstack/react-query';
import type { CSSProperties } from 'react';
import {
  api,
  type LiveBrokerRuntime,
  type LiveMarketSymbol,
  type LiveRuntimeStatus,
} from '@/lib/api';

const BADGE = 'text-2xs mono px-1.5 py-0.5 rounded-sm border';
const C = {
  ok: { borderColor: 'var(--positive)', color: 'var(--positive)' },
  bad: { borderColor: 'var(--negative)', color: 'var(--negative)' },
  warn: { borderColor: 'var(--caution, var(--warning))', color: 'var(--caution, var(--warning))' },
  muted: { borderColor: 'var(--border-subtle)', color: 'var(--text-muted)' },
} as const;

/** Runtime state → badge colour. Driven entirely by the projected state. */
const STATE_STYLE: Record<string, CSSProperties> = {
  CONNECTED: C.ok, DEGRADED: C.warn, STALE: C.warn,
  RECONNECTING: C.warn, STARTING: C.muted, STOPPED: C.bad, UNKNOWN: C.bad,
};

function txt(v: string | number | null | undefined): string {
  return v === null || v === undefined || v === '' ? '—' : String(v);
}
function money(v: number | null | undefined, digits = 2): string {
  return v === null || v === undefined ? '—' : v.toFixed(digits);
}
function price(v: number | null | undefined): string {
  return v === null || v === undefined ? '—' : v.toFixed(5);
}
function ms(v: number | null | undefined): string {
  return v === null || v === undefined ? '—' : `${Math.round(v)}ms`;
}
/** Age formatting only — the age itself was computed by the projection. */
function age(seconds: number | null | undefined): string {
  if (seconds === null || seconds === undefined) return '—';
  if (seconds < 60) return `${Math.round(seconds)}s`;
  if (seconds < 3600) return `${Math.round(seconds / 60)}m`;
  return `${(seconds / 3600).toFixed(1)}h`;
}

function Row({ label, value, testId }: { label: string; value: string; testId?: string }) {
  return (
    <div className="flex items-baseline justify-between gap-3 text-2xs" data-testid={testId}>
      <span className="text-text-muted">{label}</span>
      <span className="mono text-text-2">{value}</span>
    </div>
  );
}

function Card({ title, testId, children }: {
  title: string; testId: string; children: React.ReactNode;
}) {
  return (
    <div className="rounded-sm border p-2 space-y-0.5"
         style={{ borderColor: 'var(--border-subtle)' }} data-testid={testId}>
      <div className="text-2xs uppercase tracking-wider text-text-muted mb-1">{title}</div>
      {children}
    </div>
  );
}

function BrokerCard({ broker }: { broker: LiveBrokerRuntime }) {
  return (
    <Card title="Broker" testId="live-broker-card">
      <div className="flex flex-wrap gap-1 mb-1">
        <span className={BADGE} style={broker.connected ? C.ok : C.bad}
              data-testid="broker-connection-badge">
          {broker.connected ? 'CONNECTED' : 'DISCONNECTED'}
        </span>
        <span className={BADGE} style={C.muted} data-testid="broker-adapter-badge">
          {(broker.adapterKind ?? 'unknown').toUpperCase()}
        </span>
        {broker.executionMode && (
          <span className={BADGE} style={C.muted}>{broker.executionMode.toUpperCase()}</span>
        )}
      </div>
      <div className="flex flex-wrap gap-1 mb-1">
        {/* Demo-vs-real is the single most important thing to see before
            trading. Absent evidence renders UNKNOWN — never assumed demo. */}
        <span className={BADGE}
              style={broker.accountType === 'real' ? C.bad
                     : broker.accountType ? C.ok : C.warn}
              data-testid="broker-account-type">
          {(broker.accountType ?? 'account type unknown').toUpperCase()}
        </span>
      </div>
      <Row label="Ping" value={ms(broker.pingMs)} testId="broker-ping" />
      <Row label="Gateway latency" value={ms(broker.gatewayLatencyMs)}
           testId="broker-gateway-latency" />
      <Row label="Server" value={txt(broker.server)} />
      <Row label="Broker" value={txt(broker.brokerCompany)} />
      <Row label="Login" value={txt(broker.login)} testId="broker-login" />
      <Row label="Account" value={txt(broker.accountFingerprint)} />
      <Row label="Leverage"
           value={broker.leverage === null ? '—' : `1:${broker.leverage}`} />
      <Row label="Currency" value={txt(broker.accountCurrency)} />
      <Row label="Balance" value={money(broker.balance)} testId="broker-balance" />
      <Row label="Equity" value={money(broker.equity)} testId="broker-equity" />
      <Row label="Margin" value={money(broker.margin)} />
      <Row label="Free margin" value={money(broker.freeMargin)} testId="broker-free-margin" />
      <Row label="Last heartbeat" value={age(broker.heartbeatAgeSeconds)} />
    </Card>
  );
}

function MarketCard({ symbols }: { symbols: LiveMarketSymbol[] }) {
  return (
    <Card title="Market" testId="live-market-card">
      {symbols.length === 0 ? (
        <div className="text-2xs text-text-muted" data-testid="live-market-empty">
          No symbols are being polled.
        </div>
      ) : (
        <div className="overflow-x-auto">
          <table className="w-full text-2xs" data-testid="live-market-table">
            <thead className="text-text-muted">
              <tr>
                {['Symbol', 'Bid', 'Ask', 'Spread', 'Last update', 'Candle age', ''].map((h) => (
                  <th key={h} className="text-left font-normal pr-2 pb-0.5">{h}</th>
                ))}
              </tr>
            </thead>
            <tbody className="mono text-text-2">
              {symbols.map((s) => (
                <tr key={s.symbol} data-testid="live-market-row">
                  <td className="pr-2">{s.symbol}</td>
                  <td className="pr-2">{price(s.bid)}</td>
                  <td className="pr-2">{price(s.ask)}</td>
                  <td className="pr-2">{s.spread === null ? '—' : s.spread.toFixed(5)}</td>
                  <td className="pr-2">{age(s.quoteAgeSeconds)}</td>
                  <td className="pr-2">{age(s.candleAgeSeconds)}</td>
                  <td className="pr-2">
                    <span className={BADGE}
                          style={s.availability === 'ok' && s.live ? C.ok
                                 : s.availability === 'stale' ? C.warn : C.bad}>
                      {s.live && s.availability === 'ok' ? 'LIVE'
                       : s.availability === 'stale' ? 'STALE' : 'UNAVAILABLE'}
                    </span>
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

function RuntimeCard({ runtime }: { runtime: LiveRuntimeStatus }) {
  return (
    <Card title="Runtime" testId="live-runtime-card">
      <div className="flex flex-wrap gap-1 mb-1">
        <span className={BADGE} style={STATE_STYLE[runtime.state] ?? C.muted}
              data-testid="runtime-state-badge">
          {runtime.state}
        </span>
        {!runtime.running && (
          <span className={BADGE} style={C.bad} data-testid="runtime-loop-stopped">
            LOOP STOPPED
          </span>
        )}
      </div>
      <Row label="Projection age" value={age(runtime.projectionAgeSeconds)}
           testId="runtime-projection-age" />
      <Row label="Broker age" value={age(runtime.brokerAgeSeconds)}
           testId="runtime-broker-age" />
      <Row label="Refresh interval"
           value={runtime.intervalSeconds === null ? '—' : `${runtime.intervalSeconds}s`} />
      <Row label="Ticks" value={String(runtime.tickCount)} />
      <Row label="Failures" value={String(runtime.consecutiveFailures)} />
      <Row label="Reconnects"
           value={`${runtime.reconnectSuccesses}/${runtime.reconnectAttempts}`}
           testId="runtime-reconnects" />
      {runtime.lastFailureDetail && (
        <Row label="Last failure" value={runtime.lastFailureDetail}
             testId="runtime-last-failure" />
      )}
      {runtime.warnings.length > 0 && (
        <ul className="pt-1 space-y-0.5" data-testid="runtime-warnings">
          {runtime.warnings.map((w) => (
            <li key={w} className="text-2xs" style={{ color: 'var(--caution, var(--warning))' }}>
              ⚠ {w}
            </li>
          ))}
        </ul>
      )}
    </Card>
  );
}

export function LiveRuntimePanel() {
  const { data, isLoading, isError } = useQuery({
    queryKey: ['live-runtime'],
    // Matches the backend's default 5s tick: polling faster would just re-read
    // the same cached snapshot.
    queryFn: () => api.liveRuntime(),
    refetchInterval: 5_000,
    staleTime: 2_000,
    retry: false,
  });

  return (
    <section className="rounded-md border border-[color:var(--border)] bg-[color:var(--panel)] p-3"
             aria-label="Live runtime" data-testid="live-runtime-panel">
      <header className="flex flex-wrap items-baseline justify-between gap-2 mb-2">
        <h3 className="text-xs uppercase tracking-wider text-text-muted">
          Live runtime
        </h3>
        <span className={BADGE} style={C.muted} data-testid="live-runtime-scope">
          READ ONLY · ONE PROJECTION · NO BROKER READS IN THE BROWSER
        </span>
      </header>

      {isLoading && (
        <div className="text-2xs text-text-muted" data-testid="live-runtime-loading">
          Loading live runtime…
        </div>
      )}
      {isError && (
        <div className="text-2xs" style={{ color: 'var(--negative)' }}
             data-testid="live-runtime-error">
          UNAVAILABLE — the live runtime projection could not be read. Nothing is
          shown; this is not a claim that the market or broker are idle.
        </div>
      )}

      {data && !data.available && (
        <div className="text-2xs" style={{ color: 'var(--caution, var(--warning))' }}
             data-testid="live-runtime-unavailable">
          Runtime not started ({txt(data.code)}). The refresh loop has produced no
          tick yet, so there is no live evidence to show.
        </div>
      )}

      {data && data.available && (
        <div className="grid grid-cols-1 lg:grid-cols-2 gap-2">
          <RuntimeCard runtime={data.runtime} />
          <BrokerCard broker={data.broker} />
          <div className="lg:col-span-2"><MarketCard symbols={data.symbols} /></div>
          <div className="lg:col-span-2">
            <Card title="Execution" testId="live-execution-card">
              <div className="grid grid-cols-2 md:grid-cols-4 gap-x-4">
                <Row label="Active positions"
                     value={data.execution.positionsAvailable
                            ? String(data.execution.activePositions) : '—'}
                     testId="exec-positions" />
                <Row label="Pending orders"
                     value={data.execution.positionsAvailable
                            ? String(data.execution.pendingOrders) : '—'} />
                <Row label="Open recommendations"
                     value={data.execution.recommendationsAvailable
                            ? String(data.execution.openRecommendations) : '—'}
                     testId="exec-recommendations" />
                <Row label="Active scenarios"
                     value={data.execution.scenariosAvailable
                            ? String(data.execution.activeScenarios) : '—'}
                     testId="exec-scenarios" />
              </div>
            </Card>
          </div>
        </div>
      )}

      <p className="text-2xs text-text-muted mt-2">
        One backend loop owns every broker and market read; this panel renders the
        snapshot it published. An unavailable figure shows “—” — it is never
        replaced with zero, and a stale reading is labelled stale rather than
        presented as current.
      </p>
    </section>
  );
}
