import { useLocation } from 'react-router-dom';
import { useShellStore } from '@/store/shellStore';
import { useMarketState, useActivePackage } from '@/hooks/useRepository';
import { ChevronRight } from 'lucide-react';
import { HealthDot, MarketStateBadge, PackageVersionChip } from '@/components/primitives';
import { fmtRelative } from '@/lib/format';


/**
 * ContextBar — persistent breadcrumb + right-side health chips (§C row 2 & 3).
 * Consolidates ContextBar + HealthStrip so we save vertical space at first pass;
 * they can be split when >1680px real-estate lands.
 */
export function ContextBar() {
  const location = useLocation();

  const activePair = useShellStore((s) => s.activePair);
  const marketState = useMarketState(activePair);
  const pkg = useActivePackage();

  const path = location.pathname;
  const inPair = path.startsWith('/pair');

  const GLOBAL_LABELS: Record<string, string> = {
    '/fleet': 'Fleet Overview',
    '/edge-monitor': 'Edge Monitor',
    '/broker-health': 'Broker Health',
    '/accounts': 'Accounts & Protection',
    '/market-data': 'Market Data',
    '/deployments': 'Deployments',
    '/strategy-packages': 'Strategy Packages',
    '/system': 'System',
    '/settings': 'Settings',
    '/version-history': 'Version History',
    '/package-comparison': 'Package Comparison',
  };

  const crumbs: string[] = [];
  if (path === '/') crumbs.push('Global', 'Fleet Overview');
  else if (GLOBAL_LABELS[path]) crumbs.push('Global', GLOBAL_LABELS[path]);
  else if (inPair) {
    const parts = path.split('/').filter(Boolean);
    const pairId = parts[1] ?? activePair;
    // M-FLEET-2: the broker and account segments came from fixture records, so
    // the breadcrumb asserted a venue and an account type that did not exist.
    // The instrument is configuration and is real; the operational segments are
    // simply not shown until an authoritative source reports them.
    crumbs.push('Instrument', pairId);
    if (parts[2]) {
      const tabLabel = parts[2]
        .split('-')
        .map((s) => s.charAt(0).toUpperCase() + s.slice(1))
        .join(' ');
      crumbs.push(tabLabel);
    }
  }

  return (
    <div
      className="flex items-center h-9 border-b shrink-0"
      style={{ borderColor: 'var(--border-subtle)', background: 'var(--panel)' }}
    >
      <div className="flex items-center gap-1.5 px-4 min-w-0">
        {crumbs.map((c, i) => (
          <span key={i} className="flex items-center gap-1.5 shrink-0">
            {i > 0 && <ChevronRight size={11} className="text-text-muted" />}
            <span
              className={i === crumbs.length - 1 ? 'text-text text-xs font-medium' : 'text-text-2 text-xs'}
            >
              {c}
            </span>
          </span>
        ))}
      </div>

      <div className="flex-1" />

      <div className="flex items-center gap-4 px-4 shrink-0">
        {inPair && marketState && (
          <MarketStateBadge state={marketState?.state} confidence={marketState?.confidence} confirmed={marketState?.confirmed} />
        )}
        {inPair && (
          <div className="text-xs">
            <PackageVersionChip version={pkg?.version} hash={pkg?.packageHash} />
          </div>
        )}
        {/* M-CONF-1: the three fixture confidence signal chips are GONE —
            fabricated values may not render in chrome. Real node/bridge state
            lives in the live-operations strip and System → Connection. */}
        {/* M-FLEET-2: the fixture world's frozen `asOf` was rendered as a
            freshness timestamp. It was never one, and there is no authoritative
            fleet clock to replace it, so no timestamp is claimed here. */}
      </div>
    </div>
  );
}
