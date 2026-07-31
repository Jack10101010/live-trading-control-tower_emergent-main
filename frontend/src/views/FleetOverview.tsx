import { useOperationalFleet } from '@/hooks/useRepository';
import { Panel, EmptyState } from '@/components/structures/Panel';
import { AlertTriangle, Layers } from 'lucide-react';

/**
 * M-FLEET-2 — Fleet Overview renders AUTHORITATIVE records only.
 *
 * This page was the single largest source of invented operational data in the
 * Control Tower: three fixture deployments (EUR/USD live "InTrade", a ghost
 * lane, an experimental lane), two fixture brokers, two fixture accounts, a
 * +$412 daily P/L and a 72% drawdown buffer — all authored demonstration values
 * rendered as an operator's fleet.
 *
 * M-FLEET-1 labelled them. This milestone removes them. The page now reads the
 * operational projection through the single provenance gate, and only
 * `live_mt5` records survive. Under the development-default mock adapter the
 * projection's own records carry `mock-fixture` provenance and those very same
 * invented figures, so nothing survives and the page is intentionally empty.
 *
 * There is deliberately no "deployment" card. `/api/operations/nodes` projects
 * execution NODES; manufacturing deployment cards from them would recreate the
 * fabrication this milestone exists to remove.
 */
export function FleetOverview() {
  const { nodes, accounts, status, detail } = useOperationalFleet();
  const unavailable = status === 'unavailable';

  return (
    <div className="grid grid-cols-[1fr_320px] h-full min-h-0">
      <div className="flex flex-col overflow-hidden">
        <header className="px-6 pt-6 pb-4 shrink-0">
          <h1 className="text-2xl font-semibold text-text tracking-tight">Fleet Overview</h1>
          {/* Counts derive from authoritative records only. With no
              authoritative source there is nothing to count, and "0 nodes"
              would assert an empty fleet — a claim this page cannot support. */}
          <p className="text-xs text-text-muted mt-1" data-testid="fleet-summary">
            {unavailable ? (
              <>No authoritative operational source</>
            ) : (
              <>
                {nodes.length} node{nodes.length === 1 ? '' : 's'} · {accounts.length} account
                {accounts.length === 1 ? '' : 's'}
                {status === 'stale' ? ' · last reported, not current' : ''}
              </>
            )}
          </p>
        </header>

        <div className="flex-1 overflow-auto px-6 pb-6">
          {unavailable ? (
            <EmptyState
              title="No authoritative operational source"
              description={detail}
              icon={<Layers size={16} />}
            />
          ) : nodes.length === 0 && accounts.length === 0 ? (
            <EmptyState
              title="No nodes or accounts reported"
              description="The authoritative operational source answered and reported nothing. This is a genuine empty result, not missing data."
              icon={<Layers size={16} />}
            />
          ) : (
            <div className="grid gap-3 grid-cols-1 xl:grid-cols-2 2xl:grid-cols-3">
              {nodes.map((n) => (
                <Panel
                  provenance="live"
                  key={n.nodeId}
                  title={<span className="mono text-sm">{n.nodeId}</span>}
                >
                  <dl className="grid grid-cols-2 gap-x-4 gap-y-1.5 text-xs">
                    <Row label="Adapter" value={n.adapter} />
                    <Row label="Broker" value={n.broker} />
                    <Row label="Connection" value={n.connectionState} />
                    <Row label="Health" value={n.health} />
                    <Row label="Execution mode" value={n.executionMode} />
                    <Row label="Reconciliation" value={n.reconciliationState} />
                    <Row
                      label="Open positions"
                      value={typeof n.openPositionCount === 'number' ? String(n.openPositionCount) : null}
                    />
                    <Row
                      label="Open orders"
                      value={typeof n.openOrderCount === 'number' ? String(n.openOrderCount) : null}
                    />
                  </dl>
                </Panel>
              ))}
            </div>
          )}
        </div>
      </div>

      <aside
        className="border-l overflow-y-auto"
        style={{ borderColor: 'var(--border-subtle)', background: 'var(--bg-elevated)' }}
      >
        <Panel
          provenance="placeholder"
          title={
            <span className="flex items-center gap-2">
              <AlertTriangle size={12} className="text-[color:var(--warning)]" />
              Attention Rail
            </span>
          }
          className="border-0 rounded-none h-full"
        >
          {/* M-CONF-1: no confidence model exists; this rail says so. */}
          <EmptyState
            title="No attention model"
            description="System confidence is not computed — no confidence model exists. Attention signals will populate from genuine node telemetry when implemented."
            icon={<AlertTriangle size={16} />}
          />
        </Panel>
      </aside>
    </div>
  );
}

/** `null`/absent renders "—". A missing field is unknown, never zero or blank. */
function Row({ label, value }: { label: string; value: string | null | undefined }) {
  return (
    <>
      <dt className="text-2xs uppercase tracking-widest text-text-muted">{label}</dt>
      <dd className="text-xs font-medium mono">
        {value ?? (
          <span className="text-text-muted" title="Not reported by the operational source.">
            —
          </span>
        )}
      </dd>
    </>
  );
}
