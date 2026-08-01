import { useDecisionChain } from '@/hooks/useRepository';
import type { Package } from '@/types/domain';
import { DecisionChainView } from '@/components/domain/DecisionChain';
import { PackageVersionChip } from '@/components/primitives';
import { useRepository } from '@/hooks/useRepository';

export function DecisionChainRenderer({ decisionId }: { decisionId: string }) {
  const chain = useDecisionChain(decisionId);
  const { world } = useRepository();
  if (!chain) return <div className="p-4 text-text-muted">Decision chain not found</div>;
  // M-PKG-1: resolved the FIXTURE package behind this record's hash, so the
  // inspector displayed an authored version and validation badge. No package
  // registry exists.
  const pkg = undefined as Package | undefined;

  return (
    <div className="p-4 space-y-4">
      <div>
        <div className="mono text-xs text-text-muted truncate">{chain.decisionId}</div>
        <div className="mono text-xs text-text mt-1 truncate">{chain.scenarioKey}</div>
        <div className="mt-2">{pkg && <PackageVersionChip version={pkg?.version} hash={pkg?.packageHash} />}</div>
      </div>
      <DecisionChainView chain={chain} context={{ packageVersion: pkg?.version, packageHash: pkg?.packageHash }} />
    </div>
  );
}
