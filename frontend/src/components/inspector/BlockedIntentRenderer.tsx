import { useRepository } from '@/hooks/useRepository';
import { Badge, KeyValueGrid, LaneChip, TimestampUTC } from '@/components/primitives';
import { parseScenarioKey } from '@/lib/utils';

const REASON_LABEL: Record<string, string> = {
  COHORT_DISABLED: 'Cohort disabled',
  PM_POLICY_BLOCK: 'PM policy block',
  REGIME_BLOCKED: 'Regime blocked',
  STATE_BLOCKED: 'State blocked',
  PROTECTION_VETO: 'Protection veto',
};

export function BlockedIntentRenderer({ blockedIntentId }: { blockedIntentId: string }) {
  const { world } = useRepository();
  const b = world.blockedIntents.find((x) => x.blockedIntentId === blockedIntentId);
  if (!b) return <div className="p-4 text-text-muted">Blocked intent not found</div>;
  const { session, structure, direction, marketState, instrument } = parseScenarioKey(b.scenarioKey);

  return (
    <div className="p-4 space-y-5">
      <div className="space-y-1.5">
        <div className="mono text-xs text-text-muted truncate">{b.blockedIntentId}</div>
        <div className="flex items-center gap-2 flex-wrap">
          <Badge variant="live" color="var(--blocked)">
            {REASON_LABEL[b.blockReason] ?? b.blockReason}
          </Badge>
          <LaneChip lane={b.lane} />
        </div>
      </div>

      <section>
        <h4
          className="text-[10px] uppercase tracking-widest text-text-muted font-medium border-b pb-1 mb-2"
          style={{ borderColor: 'var(--border-subtle)' }}
        >
          Rule fired
        </h4>
        <div className="mono text-xs text-text bg-[color:var(--panel-2)] rounded-md p-2 border" style={{ borderColor: 'var(--border-subtle)' }}>
          {b.ruleFired}
        </div>
      </section>

      <section>
        <h4
          className="text-[10px] uppercase tracking-widest text-text-muted font-medium border-b pb-1 mb-2"
          style={{ borderColor: 'var(--border-subtle)' }}
        >
          Scenario
        </h4>
        <KeyValueGrid
          items={[
            { label: 'Instrument', value: instrument },
            { label: 'Session', value: session },
            { label: 'Structure', value: structure },
            { label: 'Direction', value: direction },
            { label: 'Market state', value: marketState },
            { label: 'At', value: <TimestampUTC iso={b.at} /> },
            { label: 'MS ref', value: b.marketStateRef, mono: true },
          ]}
        />
      </section>

      <div className="rounded-md border p-3 text-xs text-text-2" style={{ borderColor: 'var(--border-subtle)', background: 'var(--panel-2)' }}>
        <strong className="text-text">Nothing ever just says "Blocked" — it explains why.</strong>{' '}
        This intent never became a trade because the fired rule prevented it. Toggle the lens on
        the Policy Matrix to see the cell that produced this verdict.
      </div>
    </div>
  );
}
