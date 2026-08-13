
import { useShellStore } from '@/store/shellStore';
import { Button } from '@/components/primitives/Button';
import { ActionBar } from '@/components/structures/ActionBar';
import { deploymentActions } from '@/components/domain/tradeActions';
import { Layers } from 'lucide-react';
import {
  Badge,
  KeyValueGrid,
  LaneChip,
  MetricStat,
  PackageVersionChip,
  PLValue,
  TimestampUTC,
} from '@/components/primitives';

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
 * M-FLEET-2: there is no authoritative deployment record. This inspector
 * resolved against the FIXTURE fleet, so opening it on an ordinary route
 * rendered an invented deployment complete with account, broker and package.
 * The component is retained — it has a genuine future role once a real
 * deployment concept exists — but its fixture wiring is removed and it states
 * the absence rather than fabricating a record.
 */
export function DeploymentRenderer({ deploymentId }: { deploymentId: string }) {
  return (
    <div className="p-4 space-y-2" data-testid="deployment-inspector-unavailable">
      <div className="mono text-xs text-text-muted truncate">{deploymentId}</div>
      <div className="text-sm text-text">No authoritative deployment record</div>
      <div className="text-xs text-text-muted">
        The Control Tower has no operational deployment model. Execution nodes are
        projected at <span className="mono">/api/operations/nodes</span>; this
        inspector will populate when a real deployment concept exists.
      </div>
    </div>
  );
}
