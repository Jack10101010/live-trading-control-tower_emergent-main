import { EmptyState } from '@/components/structures/Panel';
import { PACKAGES_UNAVAILABLE_DETAIL } from '@/lib/operationalProvenance';
import { Boxes } from 'lucide-react';

/**
 * M-PKG-1 — the Policy Engine has no authoritative source.
 *
 * This view rendered six panels built entirely from the development fixture's
 * strategy packages: an active-package card with a version, hash and promotion
 * date; a native-validation card with sample sizes and p-values; a policy
 * matrix of 144 cells with eligibility verdicts; package-comparison summaries.
 *
 * None of it had a source. There is no package registry module anywhere in the
 * backend — it was never built — so every version, hash, promotion timestamp
 * and policy verdict an operator has seen here was written by hand into a
 * fixture file.
 *
 * This resolves to unavailable, not to replacement data. A policy matrix
 * asserts which trade setups are permitted; synthesising one from configuration
 * would be a more dangerous fabrication than the fixture, because it would look
 * derived rather than authored.
 */
export function PolicyEngineView() {
  return (
    <div className="p-6 h-full min-h-0 overflow-auto" data-testid="policy-engine-unavailable">
      <h1 className="text-2xl font-semibold text-text tracking-tight mb-1">Policy Engine</h1>
      <p className="text-xs text-text-muted mb-6">
        Strategy package · eligibility matrix · validation
      </p>
      <EmptyState
        title="No strategy-package registry"
        description={PACKAGES_UNAVAILABLE_DETAIL}
        icon={<Boxes size={16} />}
      />
    </div>
  );
}
