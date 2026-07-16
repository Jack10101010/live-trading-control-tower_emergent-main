import { Copy, Download, RotateCcw, Rocket, Layers, Package as PackageIcon } from 'lucide-react';
import type { DeploymentManifest } from '@/types/domain';
import { Panel } from '@/components/structures/Panel';
import { Button } from '@/components/primitives/Button';
import { Badge, KeyValueGrid, PackageVersionChip, LaneChip } from '@/components/primitives';
import { useCommand } from '@/hooks/useCommand';
import { fmtHash, fmtRelative } from '@/lib/format';

/**
 * DeploymentManifestPanel — renders the immutable manifest that WRAPS a Strategy
 * Package (the package is shown nested, never replaced). Clone / export /
 * restore / redeploy dispatch through the single command layer (mock).
 */
export function DeploymentManifestPanel({
  manifest,
  deploymentId,
}: {
  manifest: DeploymentManifest;
  deploymentId: string;
}) {
  const dispatch = useCommand();

  return (
    <Panel
      title={
        <span className="flex items-center gap-2">
          <Layers size={13} /> Deployment Manifest
          {manifest.derived && (
            <Badge variant="neutral" size="sm" title="Derived on the client from fixture data">
              derived
            </Badge>
          )}
        </span>
      }
      dense
      actions={
        <div className="flex items-center gap-1.5">
          <Button variant="ghost" size="sm" icon={<Copy size={11} />} onClick={() => dispatch({ name: 'CloneManifest', payload: { deploymentId } })}>
            Clone
          </Button>
          <Button variant="ghost" size="sm" icon={<Download size={11} />} onClick={() => dispatch({ name: 'ExportManifest', payload: { deploymentId } })}>
            Export
          </Button>
          <Button variant="ghost" size="sm" icon={<RotateCcw size={11} />} onClick={() => dispatch({ name: 'RestoreManifest', payload: { manifestId: manifest.manifestId } })}>
            Restore
          </Button>
          <Button variant="outline" size="sm" icon={<Rocket size={11} />} onClick={() => dispatch({ name: 'RedeployManifest', payload: { deploymentId } })}>
            Redeploy
          </Button>
        </div>
      }
    >
      <div className="space-y-3">
        <KeyValueGrid
          items={[
            { label: 'Manifest', value: manifest.manifestId, mono: true },
            { label: 'Hash', value: fmtHash(manifest.manifestHash), mono: true },
            { label: 'Environment', value: <Badge variant="status">{manifest.environment}</Badge> },
            { label: 'Lane', value: <LaneChip lane={manifest.lane} /> },
            { label: 'Pair', value: manifest.pair, mono: true },
            { label: 'Broker', value: manifest.broker, mono: true },
            { label: 'Account', value: manifest.account, mono: true },
            { label: 'MD version', value: manifest.marketDataVersion, mono: true },
            { label: 'Replay version', value: manifest.replayDataVersion, mono: true },
            { label: 'Created', value: fmtRelative(manifest.createdAt) },
            { label: 'Created by', value: manifest.createdBy, mono: true },
          ]}
        />

        {/* Wrapped Strategy Package (never replaced) */}
        <div
          className="rounded-md border p-2.5"
          style={{ borderColor: 'var(--border-subtle)', background: 'var(--panel-2)' }}
        >
          <div className="flex items-center gap-2 mb-1.5">
            <PackageIcon size={12} className="text-text-muted" />
            <span className="text-2xs uppercase tracking-widest text-text-muted">Wraps Strategy Package</span>
          </div>
          <div className="flex items-center gap-2">
            <PackageVersionChip version={manifest.strategyPackage.version} hash={manifest.strategyPackage.packageHash} />
            <span className="ml-auto text-2xs text-text-muted mono">{manifest.strategyPackage.packageId}</span>
          </div>
        </div>

        {/* Enabled modules / feature flags */}
        <div>
          <div className="text-2xs uppercase tracking-widest text-text-muted mb-1.5">Enabled Modules</div>
          <div className="flex flex-wrap gap-1">
            {manifest.enabledModules.length === 0 ? (
              <span className="text-2xs text-text-muted italic">none</span>
            ) : (
              manifest.enabledModules.map((m) => (
                <Badge key={m} variant="neutral" size="sm">
                  {m}
                </Badge>
              ))
            )}
          </div>
        </div>
      </div>
    </Panel>
  );
}
