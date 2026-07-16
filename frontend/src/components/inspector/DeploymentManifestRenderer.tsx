import { useDeploymentManifest } from '@/hooks/useRepository';
import { DeploymentManifestPanel } from '@/components/domain/DeploymentManifestPanel';
import { ErrorState } from '@/components/structures/Panel';

/** Inspector body for a Deployment Manifest. Shell is provided by InspectorHost. */
export function DeploymentManifestRenderer({ deploymentId }: { deploymentId: string }) {
  const manifest = useDeploymentManifest(deploymentId);
  if (!manifest) {
    return <ErrorState title="Manifest not found" description={`No deployment for ${deploymentId}`} />;
  }
  return (
    <div className="p-3">
      <DeploymentManifestPanel manifest={manifest} deploymentId={deploymentId} />
    </div>
  );
}
