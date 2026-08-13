import type { ReactNode } from 'react';
import { Lock } from 'lucide-react';
import { useCapability } from '@/hooks/useRepository';
import { isUsable, stateOf, reasonFor, titleFor } from '@/lib/capability';
import type { FeatureFlag } from '@/types/domain';
import { EmptyState } from '@/components/structures/Panel';

/**
 * M-FLAGS-1 — FeatureGate reports WHY a capability is not usable.
 *
 * It previously showed "Module disabled" for every closed gate, which was
 * wrong in the majority of cases: nothing had been disabled. `versionHistory`
 * is not switched off — no strategy-package registry exists. `newsIntegration`
 * is not switched off — it was never built. Telling an operator a capability is
 * "disabled" implies someone could turn it on, which is a false affordance.
 *
 * The four non-usable states stay distinct, and anything unrecognised resolves
 * to `unknown` and gates OFF — the same fail-closed rule the provenance gate
 * applies to an unrecognised provenance.
 */
export function FeatureGate({
  flag,
  children,
  fallback,
}: {
  flag: FeatureFlag;
  children: ReactNode;
  fallback?: ReactNode;
}) {
  const capability = useCapability(flag);
  if (isUsable(capability)) return <>{children}</>;
  if (fallback !== undefined) return <>{fallback}</>;

  const state = stateOf(capability);
  return (
    <div
      className="h-full flex items-center justify-center"
      data-testid={`capability-${state}-${flag}`}
    >
      <EmptyState title={titleFor(state)} description={reasonFor(capability)} icon={<Lock size={18} />} />
    </div>
  );
}
