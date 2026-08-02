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

/**
 * M-REC-1: resolved the FIXTURE recommendation by id, rendering its authored
 * p-value, sample size and NATIVE validation badge. Durable recommendations are
 * inspected through the operator store; there is no fixture lookup any more.
 */
export function RecommendationRenderer({ recommendationId }: { recommendationId: string }) {
  return (
    <div className="p-4 space-y-2" data-testid="recommendation-inspector-unavailable">
      <div className="mono text-xs text-text-muted truncate">{recommendationId}</div>
      <div className="text-sm text-text">No fixture recommendation</div>
      <div className="text-xs text-text-muted">
        Recommendations are recorded in the durable operator store. Authored
        fixture recommendations are no longer resolvable from ordinary routes.
      </div>
    </div>
  );
}
