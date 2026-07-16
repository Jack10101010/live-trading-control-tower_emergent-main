import { useRepository } from '@/hooks/useRepository';
import {
  Badge,
  ConfidenceMeter,
  KeyValueGrid,
  SampleSize,
  TimestampUTC,
  ValidationBadgeChip,
} from '@/components/primitives';
import { fmtProbability } from '@/lib/format';

function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <section className="space-y-2">
      <h4 className="text-[10px] uppercase tracking-widest text-text-muted font-medium border-b pb-1" style={{ borderColor: 'var(--border-subtle)' }}>
        {title}
      </h4>
      {children}
    </section>
  );
}

export function RecommendationRenderer({ recommendationId }: { recommendationId: string }) {
  const { world } = useRepository();
  const r = world.recommendations.find((x) => x.recommendationId === recommendationId);
  if (!r) return <div className="p-4 text-text-muted">Recommendation not found</div>;

  return (
    <div className="p-4 space-y-5">
      <div className="space-y-1.5">
        <div className="mono text-xs text-text-muted truncate">{r.recommendationId}</div>
        <div className="flex items-center gap-2 flex-wrap">
          <Badge variant="recommendation">{r.status}</Badge>
          <ValidationBadgeChip badge={r.evidence.nativeValidation.badge} />
        </div>
      </div>

      <Section title="Proposed change">
        <div
          className="rounded-md border p-3"
          style={{ borderColor: 'var(--border-subtle)', background: 'var(--panel-2)' }}
        >
          <div className="text-2xs uppercase tracking-widest text-text-muted mb-1">
            {r.proposedChange.field}
          </div>
          <div className="flex items-baseline gap-2 tabular">
            <span className="text-text-muted line-through">{String(r.proposedChange.from)}</span>
            <span className="text-text-muted">→</span>
            <span className="text-lg font-semibold text-[color:var(--draft)]">
              {String(r.proposedChange.to)}
            </span>
          </div>
        </div>
      </Section>

      <Section title="Evidence">
        <KeyValueGrid
          items={[
            { label: 'Sample', value: <SampleSize n={r.evidence.sampleSize} /> },
            { label: 'P(better)', value: fmtProbability(r.evidence.supportingStats.pBetter ?? 0), mono: true },
            { label: 'Δ Expectancy', value: `+${(r.evidence.supportingStats.expectancyDelta ?? 0).toFixed(2)}R`, mono: true },
            { label: 'Win rate', value: `${((r.evidence.supportingStats.winRate ?? 0) * 100).toFixed(1)}%`, mono: true },
            { label: 'PF', value: (r.evidence.supportingStats.profitFactor ?? 0).toFixed(2), mono: true },
            { label: 'Confidence', value: <ConfidenceMeter value={r.evidence.confidence} width={90} /> },
            { label: 'Research', value: r.evidence.researchVersion, mono: true },
            { label: 'Created', value: <TimestampUTC iso={r.createdAt} /> },
          ]}
        />
      </Section>

      <Section title="Explainability">
        <p className="text-sm text-text-2">{r.evidence.explainabilityText}</p>
        {r.evidence.inSampleCaveat && (
          <p className="text-xs text-[color:var(--warning)] mt-1">
            Caveat: {r.evidence.inSampleCaveat}
          </p>
        )}
      </Section>

      <Section title="Lineage">
        <KeyValueGrid
          items={[
            { label: 'Scenario', value: r.scenarioKey, mono: true },
            { label: 'Draft', value: r.lineage.draftId ?? '—', mono: true },
            {
              label: 'Resulting version',
              value: r.lineage.resultingPackageVersion ? `v${r.lineage.resultingPackageVersion}` : '—',
              mono: true,
            },
          ]}
        />
      </Section>
    </div>
  );
}
