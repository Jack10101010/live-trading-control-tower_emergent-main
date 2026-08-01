import { useBrokerHealth } from '@/hooks/useRepository';
import { EmptyState } from '@/components/structures/Panel';
import { Activity } from 'lucide-react';

/**
 * M-HONESTY-FINAL — broker health has no authoritative source.
 *
 * This rendered the fixture's authored broker records with invented latency,
 * reconnect counts, execution speed, order rejects, spread and slippage. The
 * final certification audit found it still REACHABLE: the `brokerHealth`
 * capability reported `available`, so an operator could open this route today
 * and read fabricated connectivity metrics presented as broker truth.
 *
 * Real broker health needs MT5 health telemetry. Nothing publishes it, so this
 * reports unavailable and names the missing source.
 */
export function BrokerHealthView() {
  const { available, detail } = useBrokerHealth();
  return (
    <div className="p-6 h-full min-h-0 overflow-auto" data-testid="broker-health-view">
      <h1 className="text-2xl font-semibold text-text tracking-tight mb-1">Broker Health</h1>
      <p className="text-xs text-text-muted mb-6">
        Connectivity · latency · reconciliation
      </p>
      <EmptyState
        title={available ? 'No broker health reported' : 'Broker health unavailable'}
        description={detail}
        icon={<Activity size={16} />}
      />
    </div>
  );
}
