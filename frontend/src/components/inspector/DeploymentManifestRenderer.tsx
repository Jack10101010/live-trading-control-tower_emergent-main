import { useDeploymentManifest } from '@/hooks/useRepository';
import { ErrorState } from '@/components/structures/Panel';

/** Inspector body for a Deployment Manifest. Shell is provided by InspectorHost. */
export function DeploymentManifestRenderer({ deploymentId }: { deploymentId: string }) {
  const manifest = useDeploymentManifest(deploymentId);
  if (!manifest) {
    // M-FLEET-2: "not found" implied a lookup that could have succeeded. There
    // is no authoritative manifest source at all, and the previous manifest was
    // synthesised from fixture records.
    return (
      <ErrorState
        title="No authoritative deployment manifest"
        description={`The Control Tower has no operational manifest source. Nothing can be reported for ${deploymentId}.`}
      />
    );
  }
}
