import type { ReactNode } from 'react';
import { Lock } from 'lucide-react';
import { useFeatureFlag } from '@/hooks/useRepository';
import type { FeatureFlag } from '@/types/domain';
import { EmptyState } from '@/components/structures/Panel';

/**
 * FeatureGate — the single flag-gating primitive (§B.1). A view/workspace/nav
 * item renders only when its flag resolves enabled; otherwise it shows a
 * "module disabled" state (or a supplied fallback / nothing). Adding or
 * retiring a module is configuration, never code.
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
  const enabled = useFeatureFlag(flag);
  if (enabled) return <>{children}</>;
  if (fallback !== undefined) return <>{fallback}</>;
  return (
    <div className="h-full flex items-center justify-center" data-testid={`feature-disabled-${flag}`}>
      <EmptyState
        title="Module disabled"
        description={`This workspace is turned off by the "${flag}" feature flag. Flags are currently hardcoded backend constants (see /api/feature-flags); environment/manifest control is not yet wired.`}
        icon={<Lock size={18} />}
      />
    </div>
  );
}
