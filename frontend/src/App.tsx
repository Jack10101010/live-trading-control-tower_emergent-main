import { Routes, Route, Navigate } from 'react-router-dom';
import { lazy, Suspense, type ReactNode } from 'react';
import { AppShell } from '@/components/shell/AppShell';
import { FleetOverview } from '@/views/FleetOverview';
import { PairWorkspace } from '@/views/PairWorkspace';
import {
  PairDashboardView,
  PairOrdersView,
  PairTradesView,
  PairEdgeMonitorView,
  PairStrategyHealthView,
  PairActivityTimelineView,
} from '@/views/pair/PairViews';
import { BrokerHealthView } from '@/views/BrokerHealthView';
import { AccountsProtectionView } from '@/views/AccountsProtectionView';
import { SystemView } from '@/views/SystemView';
import {
  MarketDataView,
  DeploymentsView,
  StrategyPackagesView,
  SettingsView,
} from '@/views/global/GlobalViews';
import { FeatureGate } from '@/components/FeatureGate';

/* Lazy-loaded heavy workspaces (charts / matrix / analytics) — code-split. */
const PolicyEngineView = lazy(() => import('@/views/PolicyEngineView').then((m) => ({ default: m.PolicyEngineView })));
const AnalyticsView = lazy(() => import('@/views/AnalyticsView').then((m) => ({ default: m.AnalyticsView })));
const ReplayView = lazy(() => import('@/views/ReplayView').then((m) => ({ default: m.ReplayView })));
const EdgeMonitorView = lazy(() => import('@/views/EdgeMonitorView').then((m) => ({ default: m.EdgeMonitorView })));
const VersionHistoryView = lazy(() => import('@/views/VersioningViews').then((m) => ({ default: m.VersionHistoryView })));
const PackageComparisonView = lazy(() => import('@/views/VersioningViews').then((m) => ({ default: m.PackageComparisonView })));

function Lazy({ children }: { children: ReactNode }) {
  return (
    <Suspense
      fallback={
        <div className="h-full w-full flex items-center justify-center text-xs text-text-muted mono" data-testid="workspace-loading">
          loading workspace…
        </div>
      }
    >
      {children}
    </Suspense>
  );
}

export default function App() {
  return (
    <AppShell>
      <Routes>
        <Route path="/" element={<Navigate to="/fleet" replace />} />

        {/* Global workspaces */}
        <Route path="/fleet" element={<FleetOverview />} />
        <Route path="/broker-health" element={<FeatureGate flag="brokerHealth"><BrokerHealthView /></FeatureGate>} />
        <Route path="/accounts" element={<FeatureGate flag="accountsProtection"><AccountsProtectionView /></FeatureGate>} />
        <Route path="/market-data" element={<MarketDataView />} />
        <Route path="/deployments" element={<DeploymentsView />} />
        <Route path="/strategy-packages" element={<StrategyPackagesView />} />
        <Route path="/edge-monitor" element={<FeatureGate flag="edgeMonitor"><Lazy><EdgeMonitorView /></Lazy></FeatureGate>} />
        <Route path="/system" element={<SystemView />} />
        <Route path="/settings" element={<SettingsView />} />
        <Route path="/version-history" element={<FeatureGate flag="versionHistory"><Lazy><VersionHistoryView /></Lazy></FeatureGate>} />
        <Route path="/package-comparison" element={<FeatureGate flag="packageComparison"><Lazy><PackageComparisonView /></Lazy></FeatureGate>} />

        {/* Pair workspace (reusable across instruments) */}
        <Route path="/pair/:pairId" element={<PairWorkspace />}>
          <Route path="dashboard" element={<PairDashboardView />} />
          <Route path="policy" element={<Lazy><PolicyEngineView /></Lazy>} />
          <Route path="replay" element={<FeatureGate flag="replay"><Lazy><ReplayView /></Lazy></FeatureGate>} />
          <Route path="orders" element={<PairOrdersView />} />
          <Route path="trades" element={<PairTradesView />} />
          <Route path="analytics" element={<FeatureGate flag="analytics"><Lazy><AnalyticsView /></Lazy></FeatureGate>} />
          <Route path="edge-monitor" element={<FeatureGate flag="edgeMonitor"><PairEdgeMonitorView /></FeatureGate>} />
          <Route path="strategy-health" element={<PairStrategyHealthView />} />
          <Route path="activity" element={<PairActivityTimelineView />} />
        </Route>

        <Route path="*" element={<Navigate to="/fleet" replace />} />
      </Routes>
    </AppShell>
  );
}
