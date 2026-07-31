import { useRecommendations } from '@/hooks/useRepository';
import { Panel } from '@/components/structures/Panel';
import {
  Badge,
  ConfidenceMeter,
  KeyValueGrid,
  MetricStat,
  RValue,
  SampleSize,
  TimestampUTC,
  ValidationBadgeChip,
} from '@/components/primitives';
import { useShellStore } from '@/store/shellStore';
import { fmtPercent, fmtProbability } from '@/lib/format';
import { DiffView } from '@/components/structures/Panel';

/**
 * EdgeMonitor — predicts. Feeds the recommendation pipeline.
 * Not Analytics (which describes what happened).
 */
export function EdgeMonitorView() {
  const recs = useRecommendations();
  const openInspector = useShellStore((s) => s.openInspector);

  return (
    <div className="grid grid-cols-12 gap-4 p-6 h-full min-h-0 overflow-auto">
      <div className="col-span-12">
        <h1 className="text-2xl font-semibold text-text tracking-tight">Edge Monitor</h1>
        <p className="text-xs text-text-muted mt-1">
          Predictive surface for the recommendation pipeline. Edge performance metrics are not computed.
        </p>
      </div>

      {/* M-EDGE-1: the fabricated edge metrics (expectancy, win rate, edge/
          distribution/feature drift, policy health, operator confidence,
          research-vs-live, ghost-vs-live, forward-test health, future
          candidates) are GONE. No edge or performance model exists, so the
          surface states that plainly rather than showing invented figures. */}
      <Panel provenance="placeholder" title="Edge Performance" className="col-span-12">
        <div className="py-6 px-2 max-w-2xl" data-testid="edge-monitor-not-computed">
          <div className="text-sm text-text font-medium">Edge performance is not computed.</div>
          <p className="text-xs text-text-muted mt-2">
            No edge or performance model is implemented. Genuine expectancy, win
            rate and drift must derive from the authoritative trade ledger and
            real broker history — none of which is available yet. Nothing is
            estimated or carried over from demonstration data.
          </p>
        </div>
      </Panel>

      <Panel
        provenance="fixture"
        title={<>Recommendation Pipeline <span className="text-text-muted mono ml-2">({recs.length})</span></>}
        className="col-span-12"
      >
        <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
          {recs.map((r) => (
            <button
              key={r.recommendationId}
              onClick={() => openInspector({ kind: 'recommendation', recommendationId: r.recommendationId })}
              className="text-left rounded-md border p-3 hover:bg-[color:var(--panel-2)] transition-colors"
              style={{ borderColor: 'var(--border-subtle)' }}
            >
              <div className="flex items-center gap-2 mb-2">
                <Badge variant="recommendation">{r.status}</Badge>
                <ValidationBadgeChip badge={r.evidence.nativeValidation.badge} />
                <span className="mono text-2xs text-text-muted ml-auto"><TimestampUTC iso={r.createdAt} /></span>
              </div>
              <div className="text-xs text-text mb-2 mono truncate">{r.scenarioKey}</div>
              <DiffView label={r.proposedChange.field} before={String(r.proposedChange.from)} after={String(r.proposedChange.to)} />
              <div className="mt-2 text-2xs text-text-2 line-clamp-2">{r.evidence.evidenceSummary}</div>
              <div className="mt-2 flex items-center gap-2">
                <SampleSize n={r.evidence.sampleSize} />
                <span className="mono text-2xs text-text-muted">{fmtProbability(r.evidence.supportingStats.pBetter ?? 0)}</span>
                <ConfidenceMeter value={r.evidence.confidence} width={60} />
              </div>
            </button>
          ))}
        </div>
      </Panel>
    </div>
  );
}
