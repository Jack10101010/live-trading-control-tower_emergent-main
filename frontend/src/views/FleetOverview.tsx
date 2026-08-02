import { useOperationalFleet } from '@/hooks/useRepository';
import { Panel, EmptyState } from '@/components/structures/Panel';
import { nodeCardProvenance } from '@/lib/nodeProvenance';
import type { NodeOperationalView } from '@/lib/api';
import { AlertTriangle, Layers } from 'lucide-react';

/**
 * M-NODE-READ-1 — nodes are admitted through the NODE gate, accounts through
 * the BROKER gate, and the two statuses are reported separately. Passing nodes
 * through the broker gate dropped every genuinely reporting node, so this page
 * said "No authoritative operational source" while a VPS node published valid
 * telemetry every cycle.
 *
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
  const { nodes, accounts, status, nodeStatus, nodeDetail } = useOperationalFleet();
  const noNode = nodeStatus === 'absent';

  return (
    <div className="grid grid-cols-[1fr_320px] h-full min-h-0">
      <div className="flex flex-col overflow-hidden">
        <header className="px-6 pt-6 pb-4 shrink-0">
          <h1 className="text-2xl font-semibold text-text tracking-tight">Fleet Overview</h1>
          {/* Node and account counts are separate facts from separate
              authorities, so they are stated separately. "0 nodes" is never
              printed: with nothing observed there is nothing to count, and a
              zero would assert an empty fleet this page cannot vouch for. */}
          <p className="text-xs text-text-muted mt-1" data-testid="fleet-summary">
            {noNode ? (
              <>No execution node observed</>
            ) : (
              <>
                {nodes.length} node{nodes.length === 1 ? '' : 's'} reporting
                {nodeStatus === 'stale' ? ' · last reported, not current' : ''}
                {nodeStatus === 'degraded' ? ' · node reports a failure' : ''}
              </>
            )}
            {' · '}
            {status === 'unavailable'
              ? 'account source unavailable'
              : status === 'empty'
                ? 'no accounts reported'
                : `${accounts.length} account${accounts.length === 1 ? '' : 's'}`}
          </p>
        </header>

        <div className="flex-1 overflow-auto px-6 pb-6">
          {noNode ? (
            <EmptyState
              title="No execution node observed"
              description={nodeDetail}
              icon={<Layers size={16} />}
            />
          ) : (
            <div className="grid gap-3 grid-cols-1 xl:grid-cols-2 2xl:grid-cols-3">
              {nodes.map((n) => (
                <NodeCard key={n.nodeId} node={n} />
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

/**
 * One execution node, showing ONLY facts that node published about itself.
 *
 * M-NODE-READ-1 — WHAT THIS CARD DELIBERATELY DOES NOT SHOW.
 *
 * It previously showed Adapter, Broker, Connection, Execution mode,
 * Reconciliation and open position/order counts. Every one of those was the
 * Control Tower's own state — its local adapter kind, its adapter's connection,
 * its execution store's posture — displayed under the VPS node's name. Merely
 * restoring the card would have put a fabrication with a real node's name on it
 * back on screen, which is worse than the invisibility it replaced.
 *
 * Balance, equity, P&L, account number, order counts, risk utilisation and
 * trading posture are absent by design: they require broker authority, and a
 * node heartbeat does not grant it. When the node has not observed its terminal,
 * this card says so — it does not fall silent, and it does not show zeros.
 */
function NodeCard({ node }: { node: NodeOperationalView }) {
  const lifecycle = node.lifecycleState;
  return (
    <Panel
      provenance={nodeCardProvenance(node)}
      title={<span className="mono text-sm">{node.nodeId}</span>}
      actions={
        <span data-testid={`node-lifecycle-${node.nodeId}`}
              className="text-2xs uppercase tracking-widest mono"
              style={{ color: LIFECYCLE_COLOR[lifecycle] }}>
          {lifecycle}
        </span>
      }
    >
      <dl className="grid grid-cols-2 gap-x-4 gap-y-1.5 text-xs">
        <Row label="Deployment profile" value={node.deploymentProfile} />
        <Row label="Node mode" value={node.nodeMode} />
        <Row label="Cycle status" value={node.cycleStatus} />
        <Row label="Last boundary" value={node.lastBoundary} />
        <Row label="Last bar" value={node.lastBarTime} />
        <Row label="Engine" value={node.engineVersion} />
        <Row label="Symbol · timeframe"
             value={node.symbol && node.timeframe ? `${node.symbol} · ${node.timeframe}` : node.symbol} />
        <Row label="Kill switch" value={triText(node.killSwitchActive, 'ACTIVE', 'inactive')} />
        <Row label="Submission" value={triText(node.submissionDisabled, 'disabled', 'enabled')} />
        {/* The node's own count of its own mirror. Never the Mac adapter's. */}
        <Row label="Node open positions"
             value={typeof node.openPositionCount === 'number' ? String(node.openPositionCount) : null} />
        <Row label="Telemetry age"
             value={typeof node.livenessAgeSeconds === 'number'
               ? `${Math.round(node.livenessAgeSeconds)}s (by ${node.freshnessBasis ?? 'unknown basis'})`
               : null} />
      </dl>

      {/* MT5 observation is shown ONLY when the node explicitly said something.
          A cycle on which it did not sample the terminal is not a disconnection,
          and reading it as one would raise a false alarm on every idle cycle. */}
      <p className="text-2xs text-text-muted mt-2" data-testid={`node-mt5-${node.nodeId}`}>
        {node.mt5Observation === 'observed'
          ? 'This node read its MT5 terminal on its last sampled cycle.'
          : node.mt5Observation === 'unreachable'
            ? 'This node reported it could not read its broker position snapshot.'
            : 'This node has not reported an MT5 terminal observation. Account and ' +
              'position data remain unavailable — that is not a disconnection.'}
      </p>

      {node.legacySource && (
        <p className="text-2xs mt-1" style={{ color: 'var(--warning)' }}
           data-testid={`node-legacy-${node.nodeId}`}>
          Legacy payload — this node predates the versioned telemetry contract, so
          fields it never published are reported as unavailable rather than guessed.
        </p>
      )}

      {node.warnings.length > 0 && (
        <ul className="mt-2 pt-2 border-t space-y-0.5" data-testid={`node-warnings-${node.nodeId}`}
            style={{ borderColor: 'var(--border-subtle)' }}>
          {node.warnings.map((w) => (
            <li key={w} className="text-2xs"
                style={{ color: lifecycle === 'degraded' ? 'var(--negative)' : 'var(--warning)' }}>
              ⚠ {w}
            </li>
          ))}
        </ul>
      )}
    </Panel>
  );
}

/**
 * Lifecycle colour. A stale or degraded node stays on a LIVE card — it is real
 * data, and the red border means "not operational data", which would be a lie
 * about a node that is genuinely reporting trouble. The state is coloured; the
 * card's provenance is not downgraded.
 */
const LIFECYCLE_COLOR: Record<string, string> = {
  current: 'var(--positive)',
  stale: 'var(--warning)',
  degraded: 'var(--negative)',
  absent: 'var(--text-muted)',
};

/** Tri-state text. `null` is "not reported" — never the negative branch. */
function triText(value: boolean | null, whenTrue: string, whenFalse: string): string | null {
  if (value === true) return whenTrue;
  if (value === false) return whenFalse;
  return null;
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
