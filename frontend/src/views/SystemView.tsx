import { useEffect } from 'react';
import { ConnectionPanel } from '@/components/domain/ConnectionPanel';
import { SecurityBaselinePanel } from '@/components/domain/SecurityBaselinePanel';
import { OperatorCommandPanel } from '@/components/domain/OperatorCommandPanel';
import { AuthSessionPanel } from '@/components/domain/AuthSessionPanel';
import { BrokerReadPanel } from '@/components/domain/BrokerReadPanel';
import { MarketOrderPanel } from '@/components/domain/MarketOrderPanel';
import { ManualExecutionPanel } from '@/components/domain/ManualExecutionPanel';
import { OperationalDashboard } from '@/components/domain/OperationalDashboard';
import { ScenarioPanel } from '@/components/domain/ScenarioPanel';
import { TradeLedgerPanel } from '@/components/domain/TradeLedgerPanel';
import { RecommendationPanel } from '@/components/domain/RecommendationPanel';
import { useFleet, usePackages, useFeatureFlags, useRuntimeHealth, useBrokerReconciliation, useStrategyEvaluation, useSchedulerStatus, useMarketSnapshot, useRiskLimits, useActivePackage, useBackendHealth } from '@/hooks/useRepository';
import { api, QK } from '@/lib/api';
import { queryClient } from '@/lib/queryClient';
import { Panel } from '@/components/structures/Panel';
import { Badge, PackageVersionChip, TimestampUTC, KeyValueGrid, HealthDot, ProvenanceChip } from '@/components/primitives';
import { fmtHash } from '@/lib/format';

/** Broker poll loop cadence — simple timer, no threads, no websocket (Phase 7). */
const SYNC_POLL_MS = 2000;

/**
 * System — engine health · logs · storage · MD feeds · Deployments & Manifests
 * · Feature Flags · settings.
 */
export function SystemView() {
  const { deployments, brokers } = useFleet();
  const packages = usePackages();
  const flags = useFeatureFlags();
  const runtime = useRuntimeHealth();
  const recon = useBrokerReconciliation();
  const strategy = useStrategyEvaluation();
  const scheduler = useSchedulerStatus();
  const snapshot = useMarketSnapshot();
  const riskLimits = useRiskLimits();
  const activePackage = useActivePackage();
  const { health } = useBackendHealth();

  // UI-0: real runtime-health values (or `undefined` = unknown). Never fabricated.
  const rt = runtime;
  const componentVersions: Array<[string, unknown]> = Object.entries(
    (activePackage?.componentVersions ?? {}) as Record<string, unknown>
  );

  // Poll loop: while this view is open, tick reconciliation + the Scheduler every
  // 2s. No background thread. Reconciliation appends an event only on change; the
  // Scheduler decides WHEN a strategy evaluation runs (interval trigger) and
  // returns it — the frontend no longer evaluates the strategy directly.
  useEffect(() => {
    let cancelled = false;
    const tick = () => {
      api
        .brokerSync()
        .then((r) => {
          if (!cancelled) queryClient.setQueryData(QK.brokerReconciliation, r);
        })
        .catch((err) => {
          if (!cancelled) console.warn('[broker-sync] reconciliation poll failed', err);
        });
      api
        .schedulerTick()
        .then((r) => {
          if (cancelled) return;
          queryClient.setQueryData(QK.schedulerStatus, r.scheduler);
          if (r.strategy) queryClient.setQueryData(QK.strategyDecisions, r.strategy);
          // The scheduler tick produced a fresh snapshot on the engine; pull the
          // current snapshot + engine health so the Market Data section stays live.
          Promise.all([api.marketDataSnapshot(), api.runtimeHealth()])
            .then(([snap, health]) => {
              if (cancelled) return;
              queryClient.setQueryData(QK.marketDataSnapshot, snap);
              queryClient.setQueryData(QK.runtimeHealth, health);
            })
            .catch((err) => {
              if (!cancelled) console.warn('[market-data] refresh failed', err);
            });
        })
        .catch((err) => {
          if (!cancelled) console.warn('[scheduler] tick failed', err);
        });
    };
    const id = setInterval(tick, SYNC_POLL_MS);
    return () => {
      cancelled = true;
      clearInterval(id);
    };
  }, []);

  const runtimeFlags: Array<{ label: string; ok: boolean; detail?: string }> = [
    { label: 'Overlay loaded', ok: runtime.overlayLoaded, detail: `${runtime.overlayCount} overlays` },
    { label: 'Runtime DB', ok: runtime.runtimeDbHealthy, detail: 'sqlite' },
    { label: 'Event store', ok: runtime.eventStoreHealthy, detail: `${runtime.eventCount} events` },
    { label: 'Commands enabled', ok: runtime.commandsEnabled },
    { label: 'Persistence enabled', ok: runtime.persistenceEnabled },
    { label: 'Replay state', ok: runtime.replayStateAvailable, detail: 'available' },
  ];

  return (
    <div className="p-6 h-full min-h-0 overflow-auto space-y-4">
      <div>
        <h1 className="text-2xl font-semibold text-text tracking-tight">System</h1>
        <p className="text-xs text-text-muted mt-1">
          Deployments, manifests, feature flags, engine health, storage
        </p>
      </div>

      {/* LIVE-4A — the operational dashboard. ONE projection query feeds every
          card; no card re-derives operational truth. Placed first: it is the
          canonical operational view of the system. */}
      <OperationalDashboard />

      {/* LIVE-4B — the canonical Scenario domain: the parent object of every
          recommendation, intent, order, position and future ledger entry. */}
      <ScenarioPanel />

      {/* LIVE-4C — the canonical Trade Ledger: the historical economic result
          of every closed trade. Read-only; no financial reconstruction here. */}
      {/* LIVE-4D — the canonical Recommendation domain: proposals to act on a
          Scenario, with their decision history. Read-only; acceptance never
          executes. */}
      <RecommendationPanel />

      <TradeLedgerPanel />

      {/* UI-1 — the live relationship between this Control Tower, the execution
          node and MT5. Placed first: it is the only thing on this page that
          reports the REAL system rather than the fixture world. */}
      <div className="grid grid-cols-12 gap-4">
        <div className="col-span-12 lg:col-span-6">
          <ConnectionPanel />
        </div>
        {/* UI-9 — security configuration status. Value-free by construction: the
            backend reports configured/missing/invalid and never a value. */}
        <div className="col-span-12 lg:col-span-6">
          <SecurityBaselinePanel />
        </div>
        {/* UI-17 — read-only operator controls. Three read-only diagnostics only;
            no execution control, disabled by default. */}
        <div className="col-span-12 lg:col-span-6">
          <OperatorCommandPanel />
        </div>
        {/* ARCH-3 — operator authentication session (memory-only token entry). */}
        <div className="col-span-12 lg:col-span-6">
          <AuthSessionPanel />
        </div>
        {/* LIVE-1 — read-only broker state (live MT5 or mock). No execution. */}
        <div className="col-span-12 lg:col-span-6">
          <BrokerReadPanel />
        </div>
        {/* LIVE-2 — the ONE execution control (gate-disabled, confirmed). */}
        <div className="col-span-12 lg:col-span-6">
          <MarketOrderPanel />
        </div>
        {/* LIVE-3 — manual position/order management (governed, confirmed). */}
        <div className="col-span-12 lg:col-span-6">
          <ManualExecutionPanel />
        </div>
      </div>

      <div className="grid grid-cols-12 gap-4">
        {/* UI-0: previously five hardcoded component versions with forced-green
            dots. Component versions now come from the active package (fixture) and
            carry no health claim — the Control Tower cannot observe engine health
            until a node publishes it. */}
        <Panel
          title={
            <span className="flex items-center gap-2">
              Engine Components
              <ProvenanceChip provenance="fixture" detail="From the active strategy package." />
            </span>
          }
          className="col-span-4"
        >
          {componentVersions.length > 0 ? (
            <ul className="space-y-2 text-xs" data-testid="engine-components">
              {componentVersions.map(([name, v]) => (
                <li key={name} className="flex items-center gap-2">
                  <span className="text-text">{name}</span>
                  <span className="ml-auto mono text-text-muted">{String(v)}</span>
                </li>
              ))}
            </ul>
          ) : (
            <div className="text-xs text-text-muted italic">
              No component versions in the active package.
            </div>
          )}
          <div className="mt-3 pt-2 border-t text-2xs text-text-muted leading-relaxed" style={{ borderColor: 'var(--border-subtle)' }}>
            Engine health is not observable from the Control Tower. A live execution node
            must publish it.
          </div>
        </Panel>

        <Panel title={<>Feature Flags <span className="text-text-muted mono ml-1">({Object.keys(flags).length})</span></>} className="col-span-4">
          <ul className="space-y-1.5 text-xs">
            {Object.entries(flags).map(([k, v]) => (
              <li key={k} className="flex items-center gap-2">
                <span
                  className="w-1.5 h-1.5 rounded-full"
                  style={{ background: v ? 'var(--positive)' : 'var(--text-muted)' }}
                />
                <span className="text-text-2 truncate">{k}</span>
                <span className="ml-auto text-2xs mono uppercase" style={{ color: v ? 'var(--positive)' : 'var(--text-muted)' }}>
                  {v ? 'on' : 'off'}
                </span>
              </li>
            ))}
          </ul>
        </Panel>

        {/* UI-0: this panel previously asserted a document-store connection this
            stack has no client for, plus a fixed feed-lag figure. Rows now come from
            the real runtime-health contract, or state plainly that nothing is wired. */}
        <Panel title="Storage & Feeds" className="col-span-4">
          <ul className="space-y-1.5 text-xs" data-testid="storage-feeds">
            <li className="flex items-center gap-2">
              <HealthDot state={rt?.runtimeDbHealthy ? 'ok' : 'critical'} />
              <span className="text-text">Runtime overlay (SQLite)</span>
              <span className="ml-auto mono text-text-muted">
                {rt ? (rt.runtimeDbHealthy ? 'healthy' : 'unhealthy') : 'unknown'}
              </span>
            </li>
            <li className="flex items-center gap-2">
              <HealthDot state={rt?.eventStoreHealthy ? 'ok' : 'critical'} />
              <span className="text-text">Event store</span>
              <span className="ml-auto mono text-text-muted">
                {rt ? (rt.eventStoreHealthy ? 'healthy' : 'unhealthy') : 'unknown'}
              </span>
            </li>
            <li className="flex items-center gap-2">
              <HealthDot state="muted" />
              <span className="text-text">Market-data provider</span>
              <span className="ml-auto mono text-text-muted">
                {rt?.marketData?.provider ?? 'unknown'} · {rt?.marketData?.connection ?? 'unknown'}
              </span>
            </li>
            <li className="flex items-center gap-2">
              <HealthDot state="muted" />
              <span className="text-text">Node telemetry</span>
              <span className="ml-auto flex items-center gap-1.5">
                <ProvenanceChip
                  provenance={health?.liveNodeConnected ? 'live-node' : 'placeholder'}
                  detail="No execution node has published to this backend."
                />
              </span>
            </li>
            <li className="flex items-center gap-2">
              <HealthDot state="muted" />
              <span className="text-text">Replay dataset</span>
              <span className="ml-auto flex items-center gap-1.5">
                <ProvenanceChip provenance="placeholder" detail="Replay pinning is not wired." />
              </span>
            </li>
          </ul>
        </Panel>
      </div>

      <Panel
        title={
          <>
            Runtime Health <span className="text-text-muted mono ml-1">runtime layer · not broker</span>
          </>
        }
      >
        <div className="grid grid-cols-2 md:grid-cols-3 gap-x-6 gap-y-2">
          {runtimeFlags.map((f) => (
            <div key={f.label} className="flex items-center gap-2 text-xs">
              <HealthDot state={f.ok ? 'ok' : 'critical'} />
              <span className="text-text">{f.label}</span>
              {f.detail && <span className="ml-auto mono text-2xs text-text-muted">{f.detail}</span>}
            </div>
          ))}
        </div>

        {runtime.broker && (
          <div className="mt-3 pt-3 border-t" style={{ borderColor: 'var(--border-subtle)' }}>
            <div className="flex items-center gap-2 text-xs mb-2">
              <HealthDot state={runtime.broker.connection === 'Connected' ? 'ok' : runtime.broker.connection === 'Disconnected' || runtime.broker.connection === 'Offline' ? 'critical' : 'warn'} />
              <span className="text-text">Broker</span>
              <span className="mono text-2xs text-text-muted">{runtime.broker.kind}</span>
              <span className="ml-auto mono text-2xs" style={{ color: runtime.broker.connection === 'Connected' ? 'var(--positive)' : 'var(--warning)' }}>
                {runtime.broker.connection}
              </span>
            </div>
            <div className="flex flex-wrap gap-1.5">
              {Object.entries(runtime.broker.capabilities).map(([cap, on]) => (
                <span
                  key={cap}
                  className="text-2xs mono px-1.5 py-0.5 rounded-sm border"
                  style={{
                    borderColor: 'var(--border-subtle)',
                    color: on ? 'var(--positive)' : 'var(--text-muted)',
                    background: on ? 'color-mix(in srgb, var(--positive) 8%, transparent)' : 'transparent',
                  }}
                  title={on ? 'supported' : 'unsupported'}
                >
                  {cap.replace(/^supports/, '')}
                </span>
              ))}
            </div>
          </div>
        )}

        {/* Reconciliation (Phase 7) — read-only; no automatic actions. */}
        <div className="mt-3 pt-3 border-t" style={{ borderColor: 'var(--border-subtle)' }}>
          <div className="flex items-center gap-2 text-xs mb-2">
            <HealthDot
              state={recon.reconciliation.summary.errors > 0 ? 'critical' : recon.reconciliation.summary.warnings > 0 ? 'warn' : 'ok'}
            />
            <span className="text-text">Reconciliation</span>
            <span
              className="mono text-2xs uppercase px-1.5 py-0.5 rounded-sm"
              style={{
                color:
                  recon.status === 'error' ? 'var(--negative)' : recon.status === 'warning' ? 'var(--warning)' : 'var(--positive)',
                background:
                  recon.status === 'error'
                    ? 'color-mix(in srgb, var(--negative) 10%, transparent)'
                    : recon.status === 'warning'
                    ? 'color-mix(in srgb, var(--warning) 10%, transparent)'
                    : 'color-mix(in srgb, var(--positive) 10%, transparent)',
              }}
            >
              {recon.reconciliation.summary.healthy ? 'healthy' : recon.status}
            </span>
            <span className="ml-auto mono text-2xs text-text-muted">
              {recon.reconciliation.summary.warnings} warn · {recon.reconciliation.summary.errors} err
            </span>
          </div>
          <div className="grid grid-cols-2 gap-x-6 gap-y-1 text-2xs">
            <div className="flex justify-between">
              <span className="text-text-muted">Broker snapshot</span>
              <span className="mono text-text-2">
                {recon.brokerSnapshot.counts.positions} pos · {recon.brokerSnapshot.counts.orders} ord · {recon.brokerSnapshot.connection}
              </span>
            </div>
            <div className="flex justify-between">
              <span className="text-text-muted">Runtime snapshot</span>
              <span className="mono text-text-2">
                {recon.runtimeSnapshot.counts.positions} pos · {recon.runtimeSnapshot.counts.orders} ord · seq {recon.runtimeSnapshot.eventSeq}
              </span>
            </div>
            <div className="flex justify-between">
              <span className="text-text-muted">Last sync</span>
              {/* From `recon` (live each 2s poll); sync events are now deduped so
                  runtime.sync only refreshes on state change. */}
              <span className="mono text-text-2">{recon.at ? <TimestampUTC iso={recon.at} /> : '—'}</span>
            </div>
            <div className="flex justify-between">
              <span className="text-text-muted">Sync duration</span>
              <span className="mono text-text-2">{recon.durationMs}ms · #{recon.syncSequence ?? runtime.sync?.syncSequence ?? 0}</span>
            </div>
          </div>
          {recon.reconciliation.findings.length > 0 && (
            <ul className="mt-2 space-y-1">
              {recon.reconciliation.findings.slice(0, 8).map((f, i) => (
                <li key={`${f.type}-${f.entityId}-${i}`} className="flex items-center gap-2 text-2xs">
                  <span
                    className="w-1.5 h-1.5 rounded-full shrink-0"
                    style={{ background: f.severity === 'error' ? 'var(--negative)' : 'var(--warning)' }}
                  />
                  <span className="mono text-text-2">{f.type}</span>
                  <span className="text-text-muted truncate">{f.detail}</span>
                </li>
              ))}
            </ul>
          )}
        </div>

        {/* Execution pipeline (Phase 8) — the orchestrator between Command and Broker. */}
        {runtime.orchestrator && (
          <div className="mt-3 pt-3 border-t" style={{ borderColor: 'var(--border-subtle)' }}>
            <div className="flex items-center gap-2 text-xs mb-2">
              <HealthDot state={runtime.orchestrator.orchestratorHealthy ? 'ok' : 'critical'} />
              <span className="text-text">Execution Orchestrator</span>
              {runtime.orchestrator.dryRunEnabled && (
                <span
                  className="mono text-2xs uppercase px-1.5 py-0.5 rounded-sm"
                  style={{ color: 'var(--warning)', background: 'color-mix(in srgb, var(--warning) 12%, transparent)' }}
                >
                  dry-run
                </span>
              )}
              <span className="ml-auto mono text-2xs text-text-muted">{runtime.orchestrator.executions} exec</span>
            </div>
            <div className="grid grid-cols-2 gap-x-6 gap-y-1 text-2xs">
              <div className="flex justify-between">
                <span className="text-text-muted">Avg latency</span>
                <span className="mono text-text-2">{runtime.orchestrator.averageExecutionMs}ms</span>
              </div>
              <div className="flex justify-between">
                <span className="text-text-muted">Broker latency</span>
                <span className="mono text-text-2">{runtime.orchestrator.brokerLatencyMs}ms</span>
              </div>
              <div className="flex justify-between">
                <span className="text-text-muted">Validation failures</span>
                <span className="mono text-text-2">{runtime.orchestrator.validationFailures}</span>
              </div>
              <div className="flex justify-between">
                <span className="text-text-muted">Policy failures</span>
                <span className="mono text-text-2">{runtime.orchestrator.policyFailures}</span>
              </div>
              {runtime.orchestrator.lastExecution && (
                <div className="flex justify-between col-span-2">
                  <span className="text-text-muted">Last</span>
                  <span className="mono text-text-2">
                    {runtime.orchestrator.lastExecution.command} · {runtime.orchestrator.lastExecution.status}
                    {runtime.orchestrator.lastExecution.dryRun ? ' (dry-run)' : ''} · {runtime.orchestrator.lastExecution.totalMs}ms
                  </span>
                </div>
              )}
            </div>
            {/* Observable pipeline stages (Objective 2). */}
            <div className="flex flex-wrap items-center gap-1 mt-2">
              {['Received', 'Validated', 'Policy', 'Broker Dispatch', 'Broker Result', 'Runtime Update', 'Audit Event', 'Completed'].map(
                (stage, i, arr) => (
                  <span key={stage} className="flex items-center gap-1">
                    <span
                      className="text-2xs mono px-1.5 py-0.5 rounded-sm border"
                      style={{ borderColor: 'var(--border-subtle)', color: 'var(--text-2)' }}
                    >
                      {stage}
                    </span>
                    {i < arr.length - 1 && <span className="text-text-muted text-2xs">›</span>}
                  </span>
                )
              )}
            </div>
          </div>
        )}

        {/* Strategy Engine (Phase 9) — evaluation only; never executes. */}
        <div className="mt-3 pt-3 border-t" style={{ borderColor: 'var(--border-subtle)' }}>
          <div className="flex items-center gap-2 text-xs mb-2">
            <HealthDot state={strategy.metrics.strategyHealthy ? 'ok' : 'critical'} />
            <span className="text-text">Strategy Engine</span>
            <span className="mono text-2xs text-text-muted">{strategy.strategyName}</span>
            <span className="ml-auto mono text-2xs text-text-muted">
              {strategy.report.fired} fired · {strategy.report.held} hold · {strategy.report.rejected} rej
            </span>
          </div>
          <div className="grid grid-cols-2 gap-x-6 gap-y-1 text-2xs">
            <div className="flex justify-between">
              <span className="text-text-muted">Evaluation latency</span>
              <span className="mono text-text-2">{strategy.report.durationMs}ms</span>
            </div>
            <div className="flex justify-between">
              <span className="text-text-muted">Evaluations</span>
              <span className="mono text-text-2">{strategy.metrics.evaluations}</span>
            </div>
            <div className="flex justify-between">
              <span className="text-text-muted">Decisions</span>
              <span className="mono text-text-2">{strategy.report.evaluated}</span>
            </div>
            <div className="flex justify-between">
              <span className="text-text-muted">Candidates · executed</span>
              <span className="mono text-text-2">
                {strategy.report.candidateCommands} · {strategy.report.executed}
              </span>
            </div>
            <div className="flex justify-between col-span-2">
              <span className="text-text-muted">Last strategy</span>
              <span className="mono text-text-2">{strategy.metrics.lastStrategy ?? '—'}</span>
            </div>
          </div>
          {strategy.decisions.length > 0 && (
            <ul className="mt-2 space-y-1">
              {strategy.decisions.slice(0, 4).map((d) => (
                <li key={d.decisionId} className="flex items-center gap-2 text-2xs">
                  <span
                    className="w-1.5 h-1.5 rounded-full shrink-0"
                    style={{
                      background:
                        d.signal === 'fired' ? 'var(--positive)' : d.signal === 'rejected' ? 'var(--negative)' : 'var(--warning)',
                    }}
                  />
                  <span className="mono text-text-2">{d.signal}</span>
                  <span className="text-text-muted truncate">
                    {d.pair} · {d.policyCell ?? '—'} · {Math.round(d.confidence * 100)}%
                    {d.candidateCommands.length ? ` → ${d.candidateCommands.map((c) => c.name).join(', ')}` : ''}
                  </span>
                </li>
              ))}
            </ul>
          )}
        </div>

        {/* Scheduler (Phase 10) — decides WHEN evaluations run; never executes. */}
        <div className="mt-3 pt-3 border-t" style={{ borderColor: 'var(--border-subtle)' }}>
          <div className="flex items-center gap-2 text-xs mb-2">
            <HealthDot state={scheduler.schedulerHealthy ? 'ok' : 'critical'} />
            <span className="text-text">Scheduler</span>
            {scheduler.running && (
              <span className="mono text-2xs uppercase px-1.5 py-0.5 rounded-sm" style={{ color: 'var(--warning)', background: 'color-mix(in srgb, var(--warning) 12%, transparent)' }}>
                running
              </span>
            )}
            <span className="ml-auto mono text-2xs text-text-muted">
              {scheduler.completed} done · {scheduler.failed} fail
            </span>
          </div>
          <div className="grid grid-cols-2 gap-x-6 gap-y-1 text-2xs">
            <div className="flex justify-between">
              <span className="text-text-muted">Queue depth</span>
              <span className="mono text-text-2">{scheduler.queueDepth}</span>
            </div>
            <div className="flex justify-between">
              <span className="text-text-muted">Avg duration</span>
              <span className="mono text-text-2">{scheduler.averageDurationMs}ms</span>
            </div>
            <div className="flex justify-between">
              <span className="text-text-muted">Current trigger</span>
              <span className="mono text-text-2">{scheduler.currentTrigger ?? 'idle'}</span>
            </div>
            <div className="flex justify-between">
              <span className="text-text-muted">Active triggers</span>
              <span className="mono text-text-2">{scheduler.activeTriggers.join(', ')}</span>
            </div>
            {scheduler.lastExecution && (
              <div className="flex justify-between col-span-2">
                <span className="text-text-muted">Last run</span>
                <span className="mono text-text-2">
                  {scheduler.lastExecution.trigger} · {scheduler.lastExecution.status} · {scheduler.lastExecution.durationMs}ms
                  {scheduler.lastExecution.result?.executed != null ? ` · exec ${scheduler.lastExecution.result.executed}` : ''}
                </span>
              </div>
            )}
          </div>
          {scheduler.recentHistory.length > 0 && (
            <ul className="mt-2 space-y-1">
              {scheduler.recentHistory.slice(0, 4).map((j) => (
                <li key={j.id} className="flex items-center gap-2 text-2xs">
                  <span
                    className="w-1.5 h-1.5 rounded-full shrink-0"
                    style={{ background: j.status === 'completed' ? 'var(--positive)' : j.status === 'failed' ? 'var(--negative)' : 'var(--warning)' }}
                  />
                  <span className="mono text-text-2">{j.trigger}</span>
                  <span className="text-text-muted truncate">
                    {j.status} · {j.result?.evaluated ?? 0} eval · {j.result?.fired ?? 0} fired · exec {j.result?.executed ?? 0}
                  </span>
                </li>
              ))}
            </ul>
          )}
        </div>

        {/* Market Data Engine (Phase 11) — single owner of snapshots; executes nothing. */}
        {runtime.marketData && (
          <div className="mt-3 pt-3 border-t" style={{ borderColor: 'var(--border-subtle)' }}>
            <div className="flex items-center gap-2 text-xs mb-2">
              <HealthDot state={runtime.marketData.marketDataHealthy && runtime.marketData.dataIntegrity !== false ? 'ok' : 'critical'} />
              <span className="text-text">Market Data</span>
              <span className="mono text-2xs uppercase px-1.5 py-0.5 rounded-sm" style={{ color: 'var(--text-2)', background: 'var(--panel-2)' }}>
                {runtime.marketData.provider ?? '—'}
              </span>
              {runtime.marketData.connection && (
                <span
                  className="mono text-2xs px-1.5 py-0.5 rounded-sm"
                  style={{
                    color: runtime.marketData.connection === 'Connected' ? 'var(--positive)' : runtime.marketData.connection === 'local' ? 'var(--text-muted)' : 'var(--warning)',
                    background: 'var(--panel-2)',
                  }}
                >
                  {runtime.marketData.connection}
                </span>
              )}
              <span className="ml-auto mono text-2xs text-text-muted">
                {runtime.marketData.snapshotsProduced} snapshots
              </span>
            </div>
            <div className="grid grid-cols-2 gap-x-6 gap-y-1 text-2xs">
              <div className="flex justify-between">
                <span className="text-text-muted">Snapshot age</span>
                <span className="mono text-text-2">{runtime.marketData.snapshotAgeMs != null ? `${runtime.marketData.snapshotAgeMs}ms` : '—'}</span>
              </div>
              <div className="flex justify-between">
                <span className="text-text-muted">Feed latency</span>
                <span className="mono text-text-2">
                  {runtime.marketData.feedLatencyMs != null ? `${runtime.marketData.feedLatencyMs}ms` : `${runtime.marketData.updateLatencyMs}ms`}
                </span>
              </div>
              <div className="flex justify-between">
                <span className="text-text-muted">Integrity</span>
                <span className="mono" style={{ color: runtime.marketData.dataIntegrity === false ? 'var(--negative)' : 'var(--positive)' }}>
                  {runtime.marketData.dataIntegrity === false ? 'fail' : 'ok'}
                </span>
              </div>
              <div className="flex justify-between">
                <span className="text-text-muted">Symbols</span>
                <span className="mono text-text-2">{runtime.marketData.symbolCount != null ? runtime.marketData.symbolCount : '—'}</span>
              </div>
              <div className="flex justify-between">
                <span className="text-text-muted">Symbol · TF</span>
                <span className="mono text-text-2">{runtime.marketData.currentSymbol ?? '—'} · {runtime.marketData.currentTimeframe ?? '—'}</span>
              </div>
              <div className="flex justify-between">
                <span className="text-text-muted">Session</span>
                <span className="mono text-text-2">{snapshot.session}</span>
              </div>
              <div className="flex justify-between">
                <span className="text-text-muted">Close · spread</span>
                <span className="mono text-text-2">{snapshot.ohlc.close} · {(snapshot.spread * 1e4).toFixed(1)}p</span>
              </div>
              <div className="flex justify-between col-span-2">
                <span className="text-text-muted">State · trend · vol</span>
                <span className="mono text-text-2">
                  {snapshot.marketState.state ?? '—'} @ {(snapshot.marketState.confidence * 100).toFixed(0)}% · {snapshot.trend.direction}/{snapshot.trend.strength} · {snapshot.volatility.regime}
                </span>
              </div>
            </div>
          </div>
        )}

        {/* Risk Engine (Phase 12) — assesses proposed actions; never executes or auto-blocks. */}
        {runtime.risk && (
          <div className="mt-3 pt-3 border-t" style={{ borderColor: 'var(--border-subtle)' }}>
            <div className="flex items-center gap-2 text-xs mb-2">
              <HealthDot state={runtime.risk.riskHealthy ? 'ok' : 'critical'} />
              <span className="text-text">Risk Engine</span>
              {runtime.risk.lastSeverity && (
                <span
                  className="mono text-2xs uppercase px-1.5 py-0.5 rounded-sm"
                  style={{
                    color: runtime.risk.lastSeverity === 'deny' ? 'var(--negative)' : runtime.risk.lastSeverity === 'warn' ? 'var(--warning)' : 'var(--positive)',
                    background: 'var(--panel-2)',
                  }}
                >
                  {runtime.risk.lastSeverity}
                </span>
              )}
              <span className="ml-auto mono text-2xs" style={{ color: 'var(--positive)' }}>
                {runtime.risk.allowed} allow
                <span className="text-text-muted"> · </span>
                <span style={{ color: 'var(--warning)' }}>{runtime.risk.warnings} warn</span>
                <span className="text-text-muted"> · </span>
                <span style={{ color: 'var(--negative)' }}>{runtime.risk.denials} deny</span>
              </span>
            </div>
            <div className="grid grid-cols-2 gap-x-6 gap-y-1 text-2xs">
              <div className="flex justify-between">
                <span className="text-text-muted">Assessments</span>
                <span className="mono text-text-2">{runtime.risk.assessments}</span>
              </div>
              <div className="flex justify-between">
                <span className="text-text-muted">Latency</span>
                <span className="mono text-text-2">{runtime.risk.lastAssessmentMs}ms</span>
              </div>
              {runtime.risk.lastAssessment && (
                <div className="flex justify-between col-span-2">
                  <span className="text-text-muted">Last assessment</span>
                  <span className="mono text-text-2 truncate ml-2" style={{ maxWidth: '60%' }}>
                    {runtime.risk.lastAssessment.symbol ?? '—'} · {runtime.risk.lastAssessment.severity} · {runtime.risk.lastAssessment.reason}
                  </span>
                </div>
              )}
              <div className="flex justify-between col-span-2 mt-1 pt-1 border-t" style={{ borderColor: 'var(--border-subtle)' }}>
                <span className="text-text-muted">Limits</span>
                <span className="mono text-text-2">
                  {riskLimits.maxOpenTrades} trades · {riskLimits.maxExposureLots} lots · ${riskLimits.maxDailyLoss} daily · ${riskLimits.maxFloatingLoss} floating
                </span>
              </div>
            </div>
          </div>
        )}

        {/* Portfolio Engine (Phase 14) — allocates capital across opportunities; never executes. */}
        {runtime.portfolio && (
          <div className="mt-3 pt-3 border-t" style={{ borderColor: 'var(--border-subtle)' }}>
            <div className="flex items-center gap-2 text-xs mb-2">
              <HealthDot state={runtime.portfolio.portfolioHealthy ? 'ok' : 'critical'} />
              <span className="text-text">Portfolio Engine</span>
              <span className="mono text-2xs px-1.5 py-0.5 rounded-sm" style={{ color: 'var(--text-2)', background: 'var(--panel-2)' }}>
                {runtime.portfolio.utilisationPct}% used
              </span>
              <span className="ml-auto mono text-2xs">
                <span style={{ color: 'var(--positive)' }}>{runtime.portfolio.allocations} alloc</span>
                <span className="text-text-muted"> · </span>
                <span style={{ color: 'var(--warning)' }}>{runtime.portfolio.deferred} defer</span>
                <span className="text-text-muted"> · </span>
                <span style={{ color: 'var(--negative)' }}>{runtime.portfolio.rejected} reject</span>
              </span>
            </div>
            <div className="grid grid-cols-2 gap-x-6 gap-y-1 text-2xs">
              <div className="flex justify-between">
                <span className="text-text-muted">Current capital</span>
                <span className="mono text-text-2">${runtime.portfolio.totalCapital.toLocaleString()}</span>
              </div>
              <div className="flex justify-between">
                <span className="text-text-muted">Allocated</span>
                <span className="mono text-text-2">${runtime.portfolio.allocatedCapital.toLocaleString()}</span>
              </div>
              <div className="flex justify-between">
                <span className="text-text-muted">Available</span>
                <span className="mono text-text-2">${runtime.portfolio.availableCapital.toLocaleString()}</span>
              </div>
              <div className="flex justify-between">
                <span className="text-text-muted">Cycle latency</span>
                <span className="mono text-text-2">{runtime.portfolio.lastCycleMs}ms</span>
              </div>
            </div>
          </div>
        )}

        <div className="mt-3 pt-2 border-t text-2xs text-text-muted" style={{ borderColor: 'var(--border-subtle)' }}>
          checked <TimestampUTC iso={runtime.checkedAt} />
        </div>
      </Panel>

      <Panel title={<>Deployments & Manifests <span className="text-text-muted mono ml-1">({deployments.length})</span></>}>
        <div className="grid grid-cols-1 xl:grid-cols-2 gap-3">
          {deployments.map((d) => (
            <div
              key={d.deploymentId}
              className="rounded-md border p-3"
              style={{ borderColor: 'var(--border-subtle)', background: 'var(--panel-2)' }}
            >
              <div className="flex items-center gap-2 mb-2">
                <Badge variant="status">{d.status}</Badge>
                <Badge variant="mode" color={d.executionMode === 'live' ? 'var(--mode-live)' : 'var(--mode-mock)'}>
                  {d.executionMode}
                </Badge>
                <span className="ml-auto mono text-2xs text-text-muted">{d.deploymentId.slice(0, 24)}…</span>
              </div>
              <KeyValueGrid
                items={[
                  { label: 'Pair', value: d.pair, mono: true },
                  { label: 'Lane', value: d.lane },
                  { label: 'Package hash', value: fmtHash(d.packageHash, 16), mono: true },
                  { label: 'Action', value: <span className="text-2xs">{d.lastAction}</span> },
                ]}
              />
            </div>
          ))}
        </div>
      </Panel>

      <Panel title="Packages">
        <ul className="space-y-2">
          {packages.map((pkg) => (
            <li
              key={`${pkg.packageId}-v${pkg.version}`}
              className="rounded-md border p-3 flex items-center gap-3"
              style={{ borderColor: 'var(--border-subtle)' }}
            >
              <PackageVersionChip version={pkg.version} hash={pkg.packageHash} />
              <span className="text-xs text-text">{pkg.label}</span>
              <Badge variant="status">{pkg.status}</Badge>
              <Badge variant="validation">{pkg.validation.badge}</Badge>
              <span className="ml-auto text-2xs text-text-muted">
                {pkg.promotedAt ? <TimestampUTC iso={pkg.promotedAt} /> : 'not promoted'}
              </span>
            </li>
          ))}
        </ul>
      </Panel>

      <Panel title="Brokers" dense>
        <ul className="space-y-1.5 text-xs">
          {brokers.map((b) => (
            <li key={b.brokerId} className="flex items-center gap-2">
              <HealthDot state={b.status === 'connected' ? 'ok' : 'critical'} />
              <span className="text-text">{b.venue}</span>
              <span className="mono text-text-muted">{b.adapterType}</span>
              <span className="ml-auto text-2xs text-text-muted">last recon <TimestampUTC iso={b.lastReconcileAt} /></span>
            </li>
          ))}
        </ul>
      </Panel>
    </div>
  );
}
