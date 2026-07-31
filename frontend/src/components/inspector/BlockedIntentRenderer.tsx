import { useRepository } from '@/hooks/useRepository';
import type { BlockedIntent } from '@/types/domain';
import { Badge, KeyValueGrid, LaneChip, TimestampUTC } from '@/components/primitives';
import { parseScenarioKey } from '@/lib/utils';

const REASON_LABEL: Record<string, string> = {
  COHORT_DISABLED: 'Cohort disabled',
  PM_POLICY_BLOCK: 'PM policy block',
  REGIME_BLOCKED: 'Regime blocked',
  STATE_BLOCKED: 'State blocked',
  PROTECTION_VETO: 'Protection veto',
};

/**
 * M-TRADES-1: this inspector read the FIXTURE world directly (blocked intent records),
 * bypassing /api/trades entirely. There is no authoritative blocked-intent source, so it
 * states the absence rather than rendering an authored record.
 */
export function BlockedIntentRenderer({ blockedIntentId }: { blockedIntentId: string }) {
  return (
    <div className="p-4 space-y-2" data-testid="blocked-intent-inspector-unavailable">
      <div className="mono text-xs text-text-muted truncate">{blockedIntentId}</div>
      <div className="text-sm text-text">No authoritative blocked intent record</div>
      <div className="text-xs text-text-muted">
        The Control Tower has no authoritative blocked-intent source. Nothing can be
        reported for this identifier.
      </div>
    </div>
  );
}
