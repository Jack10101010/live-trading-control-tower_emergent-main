import { useEdgeMonitor, useRecommendations } from '@/hooks/useRepository';
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
  const edge = useEdgeMonitor();
  const recs = useRecommendations();
  const openInspector = useShellStore((s) => s.openInspector);

  return (
    <div className="grid grid-cols-12 gap-4 p-6 h-full min-h-0 overflow-auto">
      <div className="col-span-12">
        <h1 className="text-2xl font-semibold text-text tracking-tight">Edge Monitor</h1>
        <p className="text-xs text-text-muted mt-1">
          Drift · confidence · research-vs-live · ghost-vs-live · forward-test health · candidate pipeline. Predictive; feeds recommendations.
        </p>
      </div>

      <Panel title="Live Edge Signals" className="col-span-12">
        <div className="grid grid-cols-6 gap-6">
          <MetricStat label="Expectancy" value={<RValue value={edge.metrics.expectancyR} />} emphasise />
          <MetricStat
            label="Win rate"
            value={<span className="mono">{fmtPercent(edge.metrics.winRate * 100, 1)}</span>}
            emphasise
          />
          <MetricStat
            label="Edge drift"
            value={<span className="mono">{edge.metrics.edgeDrift}</span>}
            emphasise
          />
          <MetricStat
            label="Distribution drift"
            value={<Badge variant="validation" color="var(--positive)">{edge.metrics.distributionDrift}</Badge>}
            emphasise
          />
          <MetricStat
            label="Policy health"
            value={<Badge variant="health" color="var(--positive)">{edge.metrics.policyHealth}</Badge>}
            emphasise
          />
          <MetricStat
            label="Operator confidence"
            value={<Badge variant="status">{edge.metrics.operatorConfidence}</Badge>}
            emphasise
          />
        </div>
      </Panel>

      <Panel title="Comparisons" className="col-span-6">
        <KeyValueGrid
          items={[
            { label: 'Research vs Live', value: <span className="mono">{edge.metrics.researchVsLive}</span> },
            { label: 'Ghost vs Live', value: <span className="mono">{edge.metrics.ghostVsLive}</span> },
            { label: 'Forward-test', value: edge.metrics.forwardTestHealth },
            { label: 'Recommendation gen', value: edge.metrics.recommendationGeneration, mono: true },
            { label: 'Feature drift', value: edge.metrics.featureDrift, mono: true },
            { label: 'As of', value: <TimestampUTC iso={edge.asOf} /> },
          ]}
        />
      </Panel>

      <Panel title="Future Candidates" className="col-span-6">
        <ul className="space-y-2">
          {edge.metrics.futureCandidates.map((c, i) => (
            <li key={i} className="flex items-center gap-2 text-xs text-text-2">
              <Badge variant="recommendation" size="sm">candidate</Badge>
              <span className="mono">{c}</span>
            </li>
          ))}
          {edge.metrics.futureCandidates.length === 0 && (
            <li className="text-xs text-text-muted italic">No candidates pending</li>
          )}
        </ul>
      </Panel>

      <Panel
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
