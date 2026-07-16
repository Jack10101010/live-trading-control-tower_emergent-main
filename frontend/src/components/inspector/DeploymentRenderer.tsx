import { useFleet, usePackages } from '@/hooks/useRepository';
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

export function DeploymentRenderer({ deploymentId }: { deploymentId: string }) {
  const { deployments, accounts, brokers } = useFleet();
  const packages = usePackages();
  const openInspector = useShellStore((s) => s.openInspector);
  const d = deployments.find((x) => x.deploymentId === deploymentId);
  if (!d) return <div className="p-4 text-text-muted">Deployment not found</div>;
  const account = accounts.find((a) => a.accountId === d.accountId);
  const broker = brokers.find((b) => b.brokerId === account?.brokerId);
  const pkg = packages.find((p) => p.packageHash === d.packageHash);
  // Runtime-only metadata the overlay stamps (not part of the frozen Deployment
  // contract) — read via a narrow local view, never added to the Track B type.
  const rt = d as { statusChangedAt?: string; lastCommandAt?: string; notes?: string };

  return (
    <div className="p-4 space-y-5">
      <div className="space-y-1.5">
        <div className="mono text-xs text-text-muted truncate">{d.deploymentId}</div>
        <div className="flex items-center gap-2 flex-wrap">
          <Badge variant="status">{d.status}</Badge>
          <LaneChip lane={d.lane} />
          <Badge
            variant="mode"
            color={
              d.executionMode === 'live'
                ? 'var(--mode-live)'
                : d.executionMode === 'demo'
                ? 'var(--mode-demo)'
                : 'var(--mode-mock)'
            }
          >
            {d.executionMode}
          </Badge>
          {pkg && <PackageVersionChip version={pkg.version} hash={pkg.packageHash} />}
        </div>
        <Button
          variant="outline"
          size="sm"
          fullWidth
          icon={<Layers size={12} />}
          onClick={() => openInspector({ kind: 'deploymentManifest', deploymentId })}
          data-testid="open-manifest"
        >
          Open Deployment Manifest
        </Button>
      </div>

      <Section title="Operator actions">
        <ActionBar actions={deploymentActions(d, { rollbackTo: pkg?.parentVersion ?? undefined })} className="gap-1.5" />
      </Section>

      <Section title="Risk state">
        <div className="grid grid-cols-3 gap-3">
          <MetricStat label="Daily P/L" value={<PLValue value={d.riskState.dailyPl} />} mono />
          <MetricStat label="Floating" value={<PLValue value={d.riskState.floatingPl} />} mono />
          <MetricStat label="Risk today" value={`${d.riskState.riskTodayPct.toFixed(2)}%`} mono />
          <MetricStat label="DD buffer" value={`${d.riskState.ddBufferPct.toFixed(0)}%`} mono />
          <MetricStat label="Open orders" value={d.riskState.openOrders} mono />
          <MetricStat label="Open trades" value={d.riskState.openTrades} mono />
        </div>
      </Section>

      <Section title="Context">
        <KeyValueGrid
          items={[
            { label: 'Pair', value: d.pair },
            { label: 'Broker', value: broker?.venue ?? '—' },
            { label: 'Account', value: account?.type ?? '—' },
            { label: 'Base ccy', value: account?.baseCurrency ?? '—' },
            ...(account
              ? [
                  { label: 'Balance', value: <PLValue value={account.balance} showSign={false} /> },
                  { label: 'Equity', value: <PLValue value={account.equity} showSign={false} /> },
                ]
              : []),
          ]}
        />
      </Section>

      <Section title="Last action">
        <p className="text-sm text-text-2">{d.lastAction}</p>
        {(rt.statusChangedAt || rt.lastCommandAt || rt.notes) && (
          <div className="mt-2">
            <KeyValueGrid
              items={[
                ...(rt.statusChangedAt
                  ? [{ label: 'Status changed', value: <TimestampUTC iso={rt.statusChangedAt} /> }]
                  : []),
                ...(rt.lastCommandAt
                  ? [{ label: 'Last command', value: <TimestampUTC iso={rt.lastCommandAt} /> }]
                  : []),
                ...(rt.notes ? [{ label: 'Notes', value: <span className="text-2xs">{rt.notes}</span> }] : []),
              ]}
            />
          </div>
        )}
      </Section>
    </div>
  );
}
