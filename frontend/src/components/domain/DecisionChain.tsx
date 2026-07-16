import { useState } from 'react';
import { CheckCircle2, XCircle, Circle, ArrowRight } from 'lucide-react';
import { TimestampUTC, WhyButton, PackageVersionChip, MarketStateBadge, CohortChip } from '@/components/primitives';
import { parseScenarioKey } from '@/lib/utils';
import type { DecisionChain } from '@/types/domain';

/**
 * DecisionChain — the backbone of explainability, shared component.
 * Each node = value + why + reference + timestamp, and a one-click "Why?" that
 * expands the full rationale + the governing context (package · policy cell ·
 * market state · deployment). Vertical timeline. Reused by every inspector.
 */
export function DecisionChainView({
  chain,
  context,
}: {
  chain: DecisionChain;
  context?: { packageVersion?: number; packageHash?: string; deploymentId?: string; manifestId?: string };
}) {
  const [open, setOpen] = useState<Set<number>>(new Set());
  const toggle = (i: number) =>
    setOpen((prev) => {
      const next = new Set(prev);
      next.has(i) ? next.delete(i) : next.add(i);
      return next;
    });

  const sk = parseScenarioKey(chain.scenarioKey);

  return (
    <div className="space-y-0" data-testid="decision-chain">
      {chain.nodes.map((node, i) => {
        const isLast = i === chain.nodes.length - 1;
        const expanded = open.has(i);
        const icon =
          node.status === 'pass' ? (
            <CheckCircle2 size={14} className="text-[color:var(--positive)]" />
          ) : node.status === 'fail' ? (
            <XCircle size={14} className="text-[color:var(--negative)]" />
          ) : (
            <Circle size={14} className="text-text-muted" />
          );

        return (
          <div key={i} className="relative flex gap-3 pb-3">
            <div className="flex flex-col items-center shrink-0">
              <div className="flex items-center justify-center rounded-full" style={{ background: 'var(--panel)', border: '1px solid var(--border)', width: 22, height: 22 }}>
                {icon}
              </div>
              {!isLast && <span className="w-px flex-1 mt-1" style={{ background: 'var(--border-subtle)' }} />}
            </div>

            <div className="flex-1 min-w-0 pb-2">
              <div className="flex items-baseline justify-between gap-2">
                <div className="text-2xs uppercase tracking-widest text-text-muted">{node.node}</div>
                <div className="flex items-center gap-2">
                  <WhyButton onClick={() => toggle(i)} />
                  <TimestampUTC iso={node.at} mode="relative" />
                </div>
              </div>
              <div className="text-sm text-text mt-0.5 truncate" title={node.value}>
                {node.value}
              </div>
              <div className="text-xs text-text-2 mt-0.5">{node.why}</div>

              {expanded && (
                <div className="mt-2 rounded-md border p-2.5 space-y-2" style={{ borderColor: 'var(--border-subtle)', background: 'var(--panel-2)' }} data-testid={`why-${node.node}`}>
                  <div className="text-2xs uppercase tracking-widest text-text-muted">Why</div>
                  <div className="text-xs text-text-2">{node.why}</div>
                  {node.ref && (
                    <div className="mono text-2xs text-text-muted break-all">
                      ref: {node.ref}
                    </div>
                  )}
                  <div className="flex items-center gap-2 flex-wrap pt-1.5 border-t" style={{ borderColor: 'var(--border-subtle)' }}>
                    {context?.packageVersion != null && <PackageVersionChip version={context.packageVersion} hash={context.packageHash} />}
                    <CohortChip session={sk.session} structure={sk.structure} direction={sk.direction} />
                    <MarketStateBadge state={sk.marketState} />
                  </div>
                  {(context?.deploymentId || context?.manifestId) && (
                    <div className="mono text-2xs text-text-muted">
                      {context?.deploymentId && <span>deployment {context.deploymentId.slice(0, 12)}… </span>}
                      {context?.manifestId && <span>· manifest {context.manifestId}</span>}
                    </div>
                  )}
                </div>
              )}
            </div>
          </div>
        );
      })}

      {chain.downstreamInfluence?.contributesTo?.length > 0 && (
        <div className="mt-4 pt-3 border-t" style={{ borderColor: 'var(--border-subtle)' }}>
          <div className="text-2xs uppercase tracking-widest text-text-muted mb-2">Downstream Influence</div>
          <ul className="space-y-1">
            {chain.downstreamInfluence.contributesTo.map((ref) => (
              <li key={ref} className="mono text-2xs text-text-2 flex items-center gap-1.5 truncate">
                <ArrowRight size={10} strokeWidth={2} />
                {ref}
              </li>
            ))}
          </ul>
        </div>
      )}
    </div>
  );
}
