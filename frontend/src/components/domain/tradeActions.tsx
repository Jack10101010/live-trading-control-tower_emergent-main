import { Eye, Ban, TrendingDown, Ghost, Pause, Play, ArrowUpDown, Crosshair, ShieldCheck, Scissors, X, PauseCircle, PlayCircle, Lock, Unlock, RefreshCw, RotateCcw } from 'lucide-react';
import type { Deployment, LiveTrade } from '@/types/domain';
import type { ActionItem } from '@/components/structures/ActionBar';

/**
 * Single source of the operator action sets. Reused by the Orders workspace,
 * the Active Trades workspace and the Trade Inspector — so command wiring is
 * defined once, never duplicated across views.
 */

export function orderActions(order: LiveTrade, opts: { onInspect?: () => void } = {}): ActionItem[] {
  const orderId = order.brokerOrderId || order.tradeId;
  const items: ActionItem[] = [];
  if (opts.onInspect) items.push({ key: 'inspect', label: 'Inspect', icon: <Eye size={13} />, onClick: opts.onInspect });
  items.push(
    { key: 'cancel', label: 'Cancel', icon: <Ban size={13} />, command: { name: 'CancelOrder', payload: { orderId } }, variant: 'danger' },
    { key: 'reduce', label: 'Reduce Risk', icon: <TrendingDown size={13} />, command: { name: 'ReduceOrderRisk', payload: { orderId } } },
    { key: 'ghost', label: 'Convert to Ghost', icon: <Ghost size={13} />, command: { name: 'ConvertOrderToGhost', payload: { orderId } } },
    { key: 'pause', label: 'Pause', icon: <Pause size={13} />, command: { name: 'PauseDeployment', payload: { deploymentId: order.deploymentId } } },
    { key: 'resume', label: 'Resume', icon: <Play size={13} />, command: { name: 'ResumeDeployment', payload: { deploymentId: order.deploymentId } } }
  );
  return items;
}

export function tradeActions(trade: LiveTrade, opts: { onInspect?: () => void } = {}): ActionItem[] {
  const tradeId = trade.tradeId;
  const items: ActionItem[] = [];
  if (opts.onInspect) items.push({ key: 'inspect', label: 'Inspect', icon: <Eye size={13} />, onClick: opts.onInspect });
  items.push(
    { key: 'movesl', label: 'Move Stop', icon: <ArrowUpDown size={13} />, command: { name: 'MoveTradeSL', payload: { tradeId } } },
    { key: 'movetp', label: 'Move Target', icon: <Crosshair size={13} />, command: { name: 'MoveTradeTP', payload: { tradeId } } },
    { key: 'be', label: 'Move BE', icon: <ShieldCheck size={13} />, command: { name: 'SLToBE', payload: { tradeId } } },
    { key: 'partial', label: 'Partial Close', icon: <Scissors size={13} />, command: { name: 'PartialClose', payload: { tradeId } } },
    { key: 'reduce', label: 'Reduce Risk', icon: <TrendingDown size={13} />, command: { name: 'ReduceTradeRisk', payload: { tradeId } } },
    { key: 'close', label: 'Close', icon: <X size={13} />, command: { name: 'CloseTrade', payload: { tradeId } }, variant: 'danger' }
  );
  return items;
}

/**
 * Deployment operator actions — reused by the Deployment Inspector's ActionBar.
 * Same single-source pattern as order/trade actions; all flow through the one
 * command dispatcher. `rollbackTo` (the deployment's parent package version)
 * enables the Rollback action when a previous version exists.
 */
export function deploymentActions(d: Deployment, opts: { rollbackTo?: number } = {}): ActionItem[] {
  const deploymentId = d.deploymentId;
  const items: ActionItem[] = [
    { key: 'pause', label: 'Pause', icon: <Pause size={13} />, command: { name: 'PauseDeployment', payload: { deploymentId } } },
    { key: 'resume', label: 'Resume', icon: <Play size={13} />, command: { name: 'ResumeDeployment', payload: { deploymentId } } },
    { key: 'lock', label: 'Lock', icon: <Lock size={13} />, command: { name: 'LockDeployment', payload: { deploymentId } }, variant: 'danger' },
    { key: 'unlock', label: 'Unlock', icon: <Unlock size={13} />, command: { name: 'UnlockDeployment', payload: { deploymentId } } },
    { key: 'redeploy', label: 'Redeploy', icon: <RefreshCw size={13} />, command: { name: 'RedeployManifest', payload: { deploymentId } } },
  ];
  if (opts.rollbackTo != null) {
    items.push({ key: 'rollback', label: 'Rollback', icon: <RotateCcw size={13} />, command: { name: 'RollbackPackage', payload: { deploymentId, toVersion: opts.rollbackTo } }, variant: 'danger' });
  }
  return items;
}

export function tradeAutoActions(trade: LiveTrade): ActionItem[] {
  return [
    { key: 'auto-off', label: 'Disable Auto Management', icon: <PauseCircle size={13} />, command: { name: 'SetAutoManagement', payload: { tradeId: trade.tradeId, enabled: false } } },
    { key: 'auto-on', label: 'Enable Auto Management', icon: <PlayCircle size={13} />, command: { name: 'SetAutoManagement', payload: { tradeId: trade.tradeId, enabled: true } } },
  ];
}
