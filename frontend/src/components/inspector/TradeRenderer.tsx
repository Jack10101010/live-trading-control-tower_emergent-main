import { useNavigate } from 'react-router-dom';
import type { Recommendation } from '@/types/domain';
import type { Package } from '@/types/domain';
import type { EventEntry } from '@/types/domain';
import type { LiveTrade } from '@/types/domain';
import {
  useRepository,
  useDecisionChain,
  useMarketState,
  useDeploymentManifest,
} from '@/hooks/useRepository';
import { useShellStore } from '@/store/shellStore';
import {
  Badge,
  KeyValueGrid,
  LaneChip,
  MarketStateBadge,
  PackageVersionChip,
  PLValue,
  RValue,
  TimestampUTC,
  ValidationBadgeChip,
  WhyButton,
} from '@/components/primitives';
import { Button } from '@/components/primitives/Button';
import { DecisionChainView } from '@/components/domain/DecisionChain';
import { ActionBar } from '@/components/structures/ActionBar';
import { tradeActions, tradeAutoActions } from '@/components/domain/tradeActions';
import { parseScenarioKey } from '@/lib/utils';
import { fmtPrice, fmtR } from '@/lib/format';
import { isoToUnix } from '@/lib/chartData';
import { PlayCircle, Layers, Grid3x3, GitBranch, Zap } from 'lucide-react';

function SectionHeader({ children }: { children: React.ReactNode }) {
  return (
    <h4 className="text-[10px] uppercase tracking-widest text-text-muted font-medium border-b pb-1" style={{ borderColor: 'var(--border-subtle)' }}>
      {children}
    </h4>
  );
}

export function TradeRenderer({ tradeId }: { tradeId: string }) {
  const { world } = useRepository();
  // M-TRADES-1: resolved against fixture live trades.
  const live: LiveTrade[] = [];
  const navigate = useNavigate();
  const openInspector = useShellStore((s) => s.openInspector);

  // Source the trade from the runtime-overlaid view (not the fixture) so the
  // inspector reflects live state after close/partial/reduce; static lookups
  // (package, events, recommendations) stay on the immutable world.
  const trade = live.find((t) => t.tradeId === tradeId);
  const decision = useDecisionChain(trade?.decisionId ?? '');
  const manifest = useDeploymentManifest(trade?.deploymentId ?? '');
  // M-PKG-1: resolved the FIXTURE package behind this record's hash, so the
  // inspector displayed an authored version and validation badge. No package
  // registry exists.
  const pkg = undefined as Package | undefined;
  const { instrument, session, structure, direction, marketState } = parseScenarioKey(trade?.scenarioKey ?? 'X:x:BOS:long:BullExpand');
  const currentMs = useMarketState(instrument);

  if (!trade) return <div className="p-4 text-text-muted">Trade not found</div>;

  const cellKey = trade.scenarioKey.split(':').slice(1).join(':');
  // M-EVENTS-1: read the FIXTURE world's events directly, so the inspector
  // showed authored audit entries beside a trade. The runtime audit stream is
  // /api/events; there is no per-scenario authoritative event query yet.
  const relatedEvents: EventEntry[] = [];
  // M-REC-1: joined FIXTURE recommendations by scenario key. Durable
  // recommendations use their own identity space and are not joinable to a
  // fixture scenario key.
  const relatedRecs: Recommendation[] = [];

  const toReplay = (iso: string) => navigate(`/pair/${instrument}/replay?t=${isoToUnix(iso)}`);

  // Full lifecycle timeline: Signal → Filled → management → Closed.
  const lifecycle: Array<{ type: string; at: string; detail?: string; actor?: string; before?: Record<string, number>; after?: Record<string, number> }> = [];
  const signalNode = decision?.nodes.find((n) => n.node === 'Signal');
  if (signalNode) lifecycle.push({ type: 'Signal', at: signalNode.at, detail: signalNode.value });
  lifecycle.push({ type: 'Filled', at: trade.openedAt, detail: `Entry ${fmtPrice(trade.entry)}` });
  trade.management.forEach((m) => lifecycle.push({ type: m.type, at: m.at, detail: m.reason, actor: m.actor, before: m.before, after: m.after }));
  if (trade.closedAt) lifecycle.push({ type: 'Closed', at: trade.closedAt, detail: trade.realizedR != null ? `${trade.realizedR >= 0 ? '+' : ''}${trade.realizedR}R` : undefined });
  lifecycle.sort((a, b) => (a.at < b.at ? -1 : 1));

  return (
    <div className="p-4 space-y-5">
      {/* Overview */}
      <div className="space-y-2">
        <div className="mono text-xs text-text-muted truncate">{trade.tradeId}</div>
        <div className="flex items-center gap-2 flex-wrap">
          <Badge variant="status">{trade.state}</Badge>
          <LaneChip lane={trade.lane} />
          {pkg && <PackageVersionChip version={pkg?.version} hash={pkg?.packageHash} />}
        </div>
        <div className="grid grid-cols-3 gap-2">
          <MiniStat label="Floating R" value={<RValue value={trade.currentR} />} />
          <MiniStat label="Floating P/L" value={<PLValue value={trade.floatingPl} />} />
          <MiniStat label="Protection" value={<span className="text-xs">{trade.protectionStatus}</span>} />
        </div>
      </div>

      {/* Commands */}
      <section className="space-y-2">
        <SectionHeader>Commands</SectionHeader>
        <ActionBar actions={tradeActions(trade)} />
        <ActionBar actions={tradeAutoActions(trade)} />
        <div className="flex items-center gap-1.5 pt-1">
          <Button variant="outline" size="sm" icon={<PlayCircle size={12} />} onClick={() => toReplay(trade.openedAt)}>Replay this trade</Button>
        </div>
      </section>

      {/* Links: Manifest · Policy Cell · Decision Chain · Version History */}
      <section className="space-y-2">
        <SectionHeader>Context</SectionHeader>
        <div className="flex flex-wrap gap-1.5">
          <Button variant="ghost" size="sm" icon={<Layers size={12} />} onClick={() => openInspector({ kind: 'deploymentManifest', deploymentId: trade.deploymentId })}>Manifest</Button>
          <Button variant="ghost" size="sm" icon={<Grid3x3 size={12} />} onClick={() => openInspector({ kind: 'policyCell', instrument, cellKey })}>Policy Cell</Button>
          {trade.decisionId && <Button variant="ghost" size="sm" icon={<Zap size={12} />} onClick={() => openInspector({ kind: 'decisionChain', decisionId: trade.decisionId! })}>Decision Chain</Button>}
          <Button variant="ghost" size="sm" icon={<GitBranch size={12} />} onClick={() => navigate('/version-history')}>Version History</Button>
        </div>
        <div className="flex items-center gap-2 flex-wrap text-xs text-text-2">
          <span className="text-text">{instrument}</span><span>·</span><span>{session}</span><span>·</span><span>{structure}</span><span>·</span>
          <span style={{ color: direction === 'long' ? 'var(--positive)' : 'var(--negative)' }}>{direction}</span>
        </div>
        <div className="flex items-center gap-3 text-2xs">
          <div className="flex items-center gap-1.5"><span className="text-text-muted uppercase tracking-widest">Original</span><MarketStateBadge state={marketState} /></div>
          {currentMs && <div className="flex items-center gap-1.5"><span className="text-text-muted uppercase tracking-widest">Current</span><MarketStateBadge state={currentMs.state} confidence={currentMs.confidence} confirmed={currentMs.confirmed} /></div>}
        </div>
        <div className="mono text-2xs text-text-muted break-all">scenario: {trade.scenarioKey}</div>
      </section>

      {/* Plan vs Execution */}
      <section className="space-y-2">
        <SectionHeader>Plan vs Execution</SectionHeader>
        <KeyValueGrid
          items={[
            { label: 'Entry', value: fmtPrice(trade.entry), mono: true },
            { label: 'Stop', value: fmtPrice(trade.sl), mono: true },
            { label: 'Target', value: fmtPrice(trade.tp), mono: true },
            { label: 'RR', value: fmtR(trade.originalPlan.rr, { sign: false }), mono: true },
            { label: 'Size', value: `${trade.size} lots`, mono: true },
            { label: 'Risk', value: `${trade.riskPct.toFixed(2)}%`, mono: true },
            { label: 'Opened', value: <TimestampUTC iso={trade.openedAt} /> },
            { label: 'Closed', value: trade.closedAt ? <TimestampUTC iso={trade.closedAt} /> : '—' },
          ]}
        />
      </section>

      {/* Management Timeline (lifecycle) */}
      <section className="space-y-2">
        <SectionHeader>Management Timeline</SectionHeader>
        <ul className="space-y-2">
          {lifecycle.map((e, i) => (
            <li key={i} className="rounded-md p-2 border" style={{ borderColor: 'var(--border-subtle)', background: 'var(--panel-2)' }}>
              <div className="flex items-baseline justify-between gap-2">
                <span className="text-xs font-medium text-text">{e.type}</span>
                <div className="flex items-center gap-2">
                  <WhyButton onClick={() => toReplay(e.at)} />
                  <TimestampUTC iso={e.at} />
                </div>
              </div>
              {e.detail && <div className="text-xs text-text-2 mt-1">{e.detail}</div>}
              {e.before && e.after && (
                <div className="mono text-2xs text-text-muted mt-0.5">
                  {Object.keys(e.after).map((k) => `${k}: ${e.before![k]} → ${e.after![k]}`).join(' · ')}
                </div>
              )}
              {e.actor && <div className="mono text-2xs text-text-muted mt-0.5">by {e.actor}</div>}
            </li>
          ))}
        </ul>
      </section>

      {/* Decision Chain */}
      {decision && (
        <section className="space-y-2">
          <SectionHeader>Decision Chain</SectionHeader>
          <DecisionChainView chain={decision} context={{ packageVersion: pkg?.version, packageHash: pkg?.packageHash, deploymentId: trade.deploymentId, manifestId: manifest?.manifestId }} />
        </section>
      )}

      {/* Recommendations */}
      {relatedRecs.length > 0 && (
        <section className="space-y-2">
          <SectionHeader>Recommendations</SectionHeader>
          <ul className="space-y-1.5">
            {relatedRecs.map((r) => (
              <li key={r.recommendationId} className="flex items-center gap-2 rounded-md border p-2 cursor-pointer hover:bg-[color:var(--panel-2)]" style={{ borderColor: 'var(--border-subtle)' }} onClick={() => openInspector({ kind: 'recommendation', recommendationId: r.recommendationId })}>
                <Badge variant="recommendation" size="sm">{r.status}</Badge>
                <span className="text-2xs text-text-2 mono truncate">{r.proposedChange.field} {String(r.proposedChange.from)}→{String(r.proposedChange.to)}</span>
              </li>
            ))}
          </ul>
        </section>
      )}

      {/* Validation */}
      {pkg && (
        <section className="space-y-2">
          <SectionHeader>Validation</SectionHeader>
          <div className="flex items-center gap-2">
            <ValidationBadgeChip badge={pkg?.validation.badge} />
            <span className="text-2xs text-text-muted mono">Δ netR</span>
            <RValue value={pkg?.validation.portfolioDeltas.netR} />
            <span className="text-2xs text-text-muted mono">n={pkg?.validation.nDecided}</span>
          </div>
        </section>
      )}

      {/* Events */}
      {relatedEvents.length > 0 && (
        <section className="space-y-2">
          <SectionHeader>Events</SectionHeader>
          <ul className="space-y-1">
            {relatedEvents.map((ev) => (
              <li key={ev.eventId} className="flex items-center gap-2 text-2xs">
                <span className="mono text-text-muted w-12 shrink-0">#{ev.seq}</span>
                <span className="mono text-text w-36 shrink-0 truncate">{ev.code}</span>
                <span className="text-text-2 truncate flex-1">{ev.humanExplanation}</span>
              </li>
            ))}
          </ul>
        </section>
      )}
    </div>
  );
}

function MiniStat({ label, value }: { label: string; value: React.ReactNode }) {
  return (
    <div className="rounded-md border p-2" style={{ borderColor: 'var(--border-subtle)', background: 'var(--panel-2)' }}>
      <div className="text-[9px] uppercase tracking-widest text-text-muted">{label}</div>
      <div className="text-sm font-semibold tabular mt-0.5">{value}</div>
    </div>
  );
}
