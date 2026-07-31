import { useRepository } from '@/hooks/useRepository';
import type { GhostTrade } from '@/types/domain';
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

/**
 * M-TRADES-1: this inspector read the FIXTURE world directly (ghost trade records),
 * bypassing /api/trades entirely. There is no authoritative ghost-execution source, so it
 * states the absence rather than rendering an authored record.
 */
export function GhostTradeRenderer({ ghostTradeId }: { ghostTradeId: string }) {
  return (
    <div className="p-4 space-y-2" data-testid="ghost-execution-inspector-unavailable">
      <div className="mono text-xs text-text-muted truncate">{ghostTradeId}</div>
      <div className="text-sm text-text">No authoritative ghost trade record</div>
      <div className="text-xs text-text-muted">
        The Control Tower has no authoritative ghost-execution source. Nothing can be
        reported for this identifier.
      </div>
    </div>
  );
}
