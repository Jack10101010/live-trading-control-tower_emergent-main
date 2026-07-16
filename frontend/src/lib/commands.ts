/**
 * Canonical command vocabulary (Track B §5). One typed union + one metadata
 * registry. All mutating actions flow through `useCommand()` → this vocabulary;
 * never call the API ad hoc, never duplicate command logic.
 *
 * v1: commands execute against the MOCK backend only (POST /api/commands/{name}
 * returns an acknowledgement). No live broker functionality.
 */

export type ConfirmationClass = 'silent' | 'confirm' | 'confirm+reason';

export type Command =
  // Policy lifecycle
  | { name: 'CreateDraft'; payload: { baseVersion: number } }
  | { name: 'DiscardDraft'; payload: { draftId: string } }
  | { name: 'PromoteDraft'; payload: { draftId: string } }
  | { name: 'RunNativeValidation'; payload: { target: string } }
  | { name: 'ApproveRecommendation'; payload: { recommendationId: string } }
  | { name: 'RejectRecommendation'; payload: { recommendationId: string } }
  | { name: 'ApplyOverride'; payload: { deploymentId: string; scenarioKey: string } }
  | { name: 'RemoveOverride'; payload: { overrideId: string } }
  // Package / deployment
  | { name: 'DeployPackage'; payload: { deploymentId?: string; packageVersion?: number } }
  | { name: 'RollbackPackage'; payload: { deploymentId?: string; toVersion: number } }
  | { name: 'PauseDeployment'; payload: { deploymentId: string } }
  | { name: 'ResumeDeployment'; payload: { deploymentId: string } }
  | { name: 'KillDeployment'; payload: { deploymentId: string } }
  | { name: 'FlattenDeployment'; payload: { deploymentId: string } }
  | { name: 'SetLaneMode'; payload: { deploymentId: string; executionMode: 'mock' | 'demo' | 'live' } }
  | { name: 'LockDeployment'; payload: { deploymentId: string } }
  | { name: 'UnlockDeployment'; payload: { deploymentId: string } }
  // Order management (pending / resting orders)
  | { name: 'CancelOrder'; payload: { orderId: string } }
  | { name: 'ReduceOrderRisk'; payload: { orderId: string } }
  | { name: 'ConvertOrderToGhost'; payload: { orderId: string } }
  // Trade management
  | { name: 'CloseTrade'; payload: { tradeId: string } }
  | { name: 'SLToBE'; payload: { tradeId: string } }
  | { name: 'MoveTradeSL'; payload: { tradeId: string } }
  | { name: 'MoveTradeTP'; payload: { tradeId: string } }
  | { name: 'PartialClose'; payload: { tradeId: string } }
  | { name: 'ReduceTradeRisk'; payload: { tradeId: string } }
  | { name: 'SetAutoManagement'; payload: { tradeId: string; enabled: boolean } }
  // Deployment Manifest (mock-only lifecycle)
  | { name: 'CloneManifest'; payload: { deploymentId: string } }
  | { name: 'ExportManifest'; payload: { deploymentId: string } }
  | { name: 'RestoreManifest'; payload: { manifestId: string } }
  | { name: 'RedeployManifest'; payload: { deploymentId: string } }
  // Safety
  | { name: 'GlobalKill'; payload: Record<string, never> }
  | { name: 'PausePair'; payload: { pair: string } };

export type CommandName = Command['name'];

/** Query-key roots a command's effects touch (see `QK` in lib/api.ts).
 * `events` is always invalidated by the dispatcher and is not listed here. */
export type InvalidationTarget = 'trades' | 'fleet' | 'packages';

interface CommandMeta {
  label: string;
  confirmation: ConfirmationClass;
  destructive?: boolean;
  /** operator-facing summary of the consequence */
  summary: string;
  /** entity queries to refetch after the command is accepted (besides events) */
  invalidates?: InvalidationTarget[];
}

export const commandMeta: Record<CommandName, CommandMeta> = {
  CreateDraft: { label: 'Create Draft', confirmation: 'silent', summary: 'Start a new policy draft from the active package.' },
  DiscardDraft: { label: 'Discard Draft', confirmation: 'confirm', summary: 'Permanently discard this draft. Live policy is unaffected.' },
  PromoteDraft: { label: 'Promote Draft', confirmation: 'confirm+reason', summary: 'Promote this draft to a new immutable package version via an atomic swap.', invalidates: ['packages'] },
  RunNativeValidation: { label: 'Run Native Validation', confirmation: 'confirm', summary: 'Validate the draft on the native backtester (pinned research data).' },
  ApproveRecommendation: { label: 'Approve Recommendation', confirmation: 'confirm', summary: 'Accept this recommendation into a draft for validation.' },
  RejectRecommendation: { label: 'Reject Recommendation', confirmation: 'confirm+reason', summary: 'Reject this recommendation.' },
  ApplyOverride: { label: 'Apply Override', confirmation: 'confirm+reason', summary: 'Apply a time-boxed live override to this cell. Auto-expires.' },
  RemoveOverride: { label: 'Remove Override', confirmation: 'confirm', summary: 'Remove the active override.' },
  DeployPackage: { label: 'Deploy Package', confirmation: 'confirm+reason', destructive: true, summary: 'Deploy a package version to the deployment (atomic version swap).', invalidates: ['fleet', 'packages'] },
  RollbackPackage: { label: 'Rollback Package', confirmation: 'confirm+reason', destructive: true, summary: 'Roll back to a previous package version.', invalidates: ['fleet', 'packages'] },
  PauseDeployment: { label: 'Pause Deployment', confirmation: 'confirm', summary: 'Pause this deployment. No new orders; open trades keep managing.', invalidates: ['fleet'] },
  ResumeDeployment: { label: 'Resume Deployment', confirmation: 'confirm', summary: 'Resume this deployment.', invalidates: ['fleet'] },
  KillDeployment: { label: 'Kill Deployment', confirmation: 'confirm+reason', destructive: true, summary: 'Cancel all working orders for this deployment.', invalidates: ['fleet', 'trades'] },
  FlattenDeployment: { label: 'Flatten Deployment', confirmation: 'confirm+reason', destructive: true, summary: 'Close all open positions for this deployment.', invalidates: ['fleet', 'trades'] },
  SetLaneMode: { label: 'Set Execution Mode', confirmation: 'confirm+reason', destructive: true, summary: 'Change the execution mode of this deployment.', invalidates: ['fleet'] },
  LockDeployment: { label: 'Lock Deployment', confirmation: 'confirm+reason', destructive: true, summary: 'Lock this deployment. No orders or trades until unlocked.', invalidates: ['fleet'] },
  UnlockDeployment: { label: 'Unlock Deployment', confirmation: 'confirm', summary: 'Unlock this deployment, restoring its prior status.', invalidates: ['fleet'] },
  CancelOrder: { label: 'Cancel Order', confirmation: 'confirm', summary: 'Cancel this pending order. No fill will occur.', invalidates: ['trades', 'fleet'] },
  ReduceOrderRisk: { label: 'Reduce Order Risk', confirmation: 'confirm', summary: 'Reduce the size/risk of this pending order.', invalidates: ['trades', 'fleet'] },
  ConvertOrderToGhost: { label: 'Convert to Ghost', confirmation: 'confirm', summary: 'Convert this pending order to a ghost (tracked, no broker order).', invalidates: ['trades', 'fleet'] },
  CloseTrade: { label: 'Close Trade', confirmation: 'confirm', summary: 'Close this trade at market.', invalidates: ['trades', 'fleet'] },
  SLToBE: { label: 'SL → Break-even', confirmation: 'confirm', summary: 'Move the stop-loss to break-even.', invalidates: ['trades'] },
  MoveTradeSL: { label: 'Move Stop', confirmation: 'confirm', summary: 'Move the stop-loss on this trade.', invalidates: ['trades'] },
  MoveTradeTP: { label: 'Move Target', confirmation: 'confirm', summary: 'Move the take-profit target on this trade.', invalidates: ['trades'] },
  PartialClose: { label: 'Partial Close', confirmation: 'confirm', summary: 'Partially close this position, banking part of the R.', invalidates: ['trades', 'fleet'] },
  ReduceTradeRisk: { label: 'Reduce Risk', confirmation: 'confirm', summary: 'Reduce the risk on this open trade.', invalidates: ['trades', 'fleet'] },
  SetAutoManagement: { label: 'Auto-management', confirmation: 'confirm', summary: 'Enable or disable automatic management for this trade.', invalidates: ['trades'] },
  CloneManifest: { label: 'Clone Manifest', confirmation: 'confirm', summary: 'Clone this deployment manifest as a new experimental deployment.', invalidates: ['fleet'] },
  ExportManifest: { label: 'Export Manifest', confirmation: 'silent', summary: 'Export this immutable manifest for backup/restore.' },
  RestoreManifest: { label: 'Restore Manifest', confirmation: 'confirm+reason', destructive: true, summary: 'Restore a deployment from a manifest with complete fidelity.', invalidates: ['fleet'] },
  RedeployManifest: { label: 'Redeploy Manifest', confirmation: 'confirm+reason', destructive: true, summary: 'Redeploy this deployment from its manifest.', invalidates: ['fleet'] },
  GlobalKill: { label: 'Global Kill', confirmation: 'confirm+reason', destructive: true, summary: 'Halt all live lanes: cancel all orders, flatten all positions, lock every deployment.', invalidates: ['fleet', 'trades'] },
  PausePair: { label: 'Pause Pair', confirmation: 'confirm', summary: 'Pause every deployment on this pair.', invalidates: ['fleet'] },
};

export function commandLabel(name: CommandName): string {
  return commandMeta[name].label;
}
