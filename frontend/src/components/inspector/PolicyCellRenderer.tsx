import { usePolicyMatrix, useActivePackage, useRecommendations, useMarketState } from '@/hooks/useRepository';
import {
  Badge,
  CohortChip,
  ConfidenceMeter,
  EligibilityBadge,
  KeyValueGrid,
  MarketStateBadge,
  MetricStat,
  PackageVersionChip,
  RValue,
  SampleSize,
  ValidationBadgeChip,
  WhyButton,
} from '@/components/primitives';
import { useShellStore } from '@/store/shellStore';
import { fmtExpectancy, fmtHash, fmtPct01, fmtR } from '@/lib/format';

export function PolicyCellRenderer({ instrument, cellKey }: { instrument: string; cellKey: string }) {
  const matrix = usePolicyMatrix(instrument);
  const pkg = useActivePackage();
  const cell = matrix.cells[cellKey];
  const recs = useRecommendations();
  const marketState = useMarketState(instrument);
  const openInspector = useShellStore((s) => s.openInspector);

  if (!cell) return <div className="p-4 text-text-muted">Cell not found</div>;

  const linkedRec = cell.provenanceRecommendationId
    ? recs.find((r) => r.recommendationId === cell.provenanceRecommendationId)
    : null;

  return (
    <div className="p-4 space-y-5">
      <div className="space-y-1.5">
        <div className="flex items-baseline justify-between gap-3">
          <div className="mono text-xs text-text-muted truncate">{cell.policyCellKey}</div>
          <PackageVersionChip version={pkg?.version} hash={pkg?.packageHash} />
        </div>
        <div className="flex items-center gap-2 flex-wrap">
          <CohortChip
            session={cell.cohort.session}
            structure={cell.cohort.structure}
            direction={cell.cohort.direction}
            size="md"
          />
          <MarketStateBadge state={cell.marketState} />
        </div>
      </div>

      <div className="grid grid-cols-3 gap-4">
        <MetricStat label="Target" value={fmtR(cell.target.rr, { sign: false })} sub={cell.target.source} emphasise mono />
        <MetricStat label="Risk" value={`${cell.risk.pct.toFixed(2)}%`} sub={cell.risk.source} emphasise mono />
        <MetricStat label="Expectancy" value={fmtExpectancy(cell.evidence.expectancyR)} sub={<>PF {cell.evidence.profitFactor.toFixed(2)}</>} emphasise mono />
      </div>

      <section className="space-y-2">
        <SectionHeader>Eligibility</SectionHeader>
        <div className="flex items-center gap-2 flex-wrap">
          <EligibilityBadge action={cell.eligibility.action} allowed={cell.eligibility.resolvedAllowed} />
          <span className="text-xs text-text-2">Mode: {cell.eligibility.mode}</span>
        </div>
      </section>

      <section className="space-y-2">
        <SectionHeader>Evidence</SectionHeader>
        <KeyValueGrid
          items={[
            { label: 'Badge', value: <ValidationBadgeChip badge={cell.evidence.badge} /> },
            { label: 'Sample', value: <SampleSize n={cell.evidence.sampleSize} /> },
            { label: 'Win rate', value: fmtPct01(cell.evidence.winRate) },
            { label: 'Profit factor', value: cell.evidence.profitFactor.toFixed(2), mono: true },
            {
              label: 'Confidence',
              value: <ConfidenceMeter value={cell.evidence.confidence} width={90} />,
            },
            ...(cell.evidence.inSampleCaveat
              ? [{ label: 'Caveat', value: <span className="text-[color:var(--warning)]">{cell.evidence.inSampleCaveat}</span> }]
              : []),
          ]}
        />
      </section>

      {marketState && (
        <section className="space-y-2">
          <SectionHeader>Current Market State</SectionHeader>
          <KeyValueGrid
            items={[
              { label: 'State', value: <MarketStateBadge state={marketState.state} confidence={marketState.confidence} /> },
              { label: 'Known at', value: marketState.stateKnownAt, mono: true },
              { label: 'Shifted', value: `${marketState.shiftedDays}d prior-day` },
              { label: 'Source', value: marketState.source },
            ]}
          />
        </section>
      )}

      {linkedRec && (
        <section className="space-y-2">
          <SectionHeader>Linked Recommendation</SectionHeader>
          <button
            onClick={() => openInspector({ kind: 'recommendation', recommendationId: linkedRec.recommendationId })}
            className="w-full text-left rounded-md border p-3 hover:bg-[color:var(--panel-2)]"
            style={{ borderColor: 'var(--border-subtle)', background: 'var(--panel-2)' }}
          >
            <div className="flex items-center justify-between">
              <Badge variant="recommendation">{linkedRec.status}</Badge>
              <span className="mono text-2xs text-text-muted">{fmtHash(linkedRec.recommendationId, 12)}</span>
            </div>
            <div className="text-sm text-text mt-2">{linkedRec.evidence.evidenceSummary}</div>
          </button>
        </section>
      )}

      <section className="space-y-2">
        <SectionHeader>Related</SectionHeader>
        <div className="flex flex-wrap gap-2">
          <WhyButton size="md" onClick={() => {/* would drill to decision chain */}} />
        </div>
      </section>
    </div>
  );
}

function SectionHeader({ children }: { children: React.ReactNode }) {
  return <h4 className="text-[10px] uppercase tracking-widest text-text-muted font-medium border-b pb-1" style={{ borderColor: 'var(--border-subtle)' }}>{children}</h4>;
}
