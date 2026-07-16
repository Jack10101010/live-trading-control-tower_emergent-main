import { useRepository } from '@/hooks/useRepository';
import {
  Badge,
  KeyValueGrid,
  LaneChip,
  MarketStateBadge,
  PackageVersionChip,
  PLValue,
  RValue,
  TimestampUTC,
} from '@/components/primitives';
import { parseScenarioKey } from '@/lib/utils';

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

export function GhostTradeRenderer({ ghostTradeId }: { ghostTradeId: string }) {
  const { world } = useRepository();
  const g = world.ghostTrades.find((x) => x.ghostTradeId === ghostTradeId);
  if (!g) return <div className="p-4 text-text-muted">Ghost trade not found</div>;
  const { session, structure, direction, marketState, instrument } = parseScenarioKey(g.scenarioKey);
  const pkg = world.packages.find((p) => p.packageHash === g.packageHash);

  return (
    <div className="p-4 space-y-5">
      <div className="space-y-1.5">
        <div className="mono text-xs text-text-muted truncate">{g.ghostTradeId}</div>
        <div className="flex items-center gap-2 flex-wrap">
          <Badge variant="ghost">{g.ghostModelId}</Badge>
          <LaneChip lane={g.lane} />
          {pkg && <PackageVersionChip version={pkg.version} hash={pkg.packageHash} />}
        </div>
      </div>

      <Section title="Scenario">
        <div className="flex items-center gap-2 flex-wrap text-xs text-text-2">
          <span className="text-text">{instrument}</span>
          <span>·</span>
          <span>{session}</span>
          <span>·</span>
          <span>{structure}</span>
          <span>·</span>
          <span style={{ color: direction === 'long' ? 'var(--positive)' : 'var(--negative)' }}>{direction}</span>
          <span>·</span>
          <MarketStateBadge state={marketState} />
        </div>
      </Section>

      <Section title="Ghost Outcome">
        <KeyValueGrid
          items={[
            {
              label: 'Outcome',
              value: (
                <Badge variant={g.ghostOutcome === 'WIN' ? 'validation' : g.ghostOutcome === 'LOSS' ? 'live' : 'neutral'}>
                  {g.ghostOutcome}
                </Badge>
              ),
            },
            { label: 'R', value: <RValue value={g.ghostR} />, mono: true },
            { label: 'MAE', value: <RValue value={g.ghostMae} />, mono: true },
            { label: 'MFE', value: <RValue value={g.ghostMfe} />, mono: true },
            { label: 'Fill delay', value: `${g.ghostFillDelayCandles} candles`, mono: true },
          ]}
        />
      </Section>

      <Section title="Note">
        <p className="text-xs text-text-2">{g.note}</p>
      </Section>
    </div>
  );
}
