import { X } from 'lucide-react';
import { useEffect } from 'react';
import { useShellStore } from '@/store/shellStore';
import { IconButton } from '@/components/primitives/Button';
import { PolicyCellRenderer } from './PolicyCellRenderer';
import { TradeRenderer } from './TradeRenderer';
import { DeploymentRenderer } from './DeploymentRenderer';
import { RecommendationRenderer } from './RecommendationRenderer';
import { BlockedIntentRenderer } from './BlockedIntentRenderer';
import { DecisionChainRenderer } from './DecisionChainRenderer';
import { GhostTradeRenderer } from './GhostTradeRenderer';
import { DeploymentManifestRenderer } from './DeploymentManifestRenderer';

/**
 * InspectorHost — ONE right-drawer with fixed shell (header · body · related).
 * The entity renderer changes; the shell never does.
 */
export function InspectorHost() {
  const inspector = useShellStore((s) => s.inspector);
  const close = useShellStore((s) => s.closeInspector);
  const width = useShellStore((s) => s.inspectorWidth);
  const setWidth = useShellStore((s) => s.setInspectorWidth);

  const startResize = (e: React.PointerEvent) => {
    e.preventDefault();
    const onMove = (ev: PointerEvent) => setWidth(window.innerWidth - ev.clientX);
    const onUp = () => {
      window.removeEventListener('pointermove', onMove);
      window.removeEventListener('pointerup', onUp);
    };
    window.addEventListener('pointermove', onMove);
    window.addEventListener('pointerup', onUp);
  };

  useEffect(() => {
    if (!inspector) return;
    const handler = (e: KeyboardEvent) => {
      if (e.key === 'Escape') close();
    };
    window.addEventListener('keydown', handler);
    return () => window.removeEventListener('keydown', handler);
  }, [inspector, close]);

  if (!inspector) return null;

  return (
    <>
      {/* Scrim on narrow viewports; here just a subtle overlay */}
      <div
        onClick={close}
        className="fixed inset-0 z-30 bg-black/30 lg:hidden"
        aria-hidden
      />
      <aside
        className="fixed right-0 top-[86px] bottom-[36px] z-40 flex flex-col border-l ct-elev-2"
        style={{
          width,
          borderColor: 'var(--border)',
          background: 'var(--bg-elevated)',
        }}
        role="dialog"
        aria-label="Inspector"
      >
        {/* Resize handle */}
        <div
          onPointerDown={startResize}
          className="absolute left-0 top-0 bottom-0 w-1 cursor-col-resize hover:bg-[color:var(--primary)] z-50"
          style={{ marginLeft: -2 }}
          role="separator"
          aria-label="Resize inspector"
          data-testid="inspector-resize"
        />
        <header
          className="flex items-center justify-between px-4 h-11 border-b shrink-0"
          style={{ borderColor: 'var(--border-subtle)', background: 'var(--panel)' }}
        >
          <div className="text-[11px] uppercase tracking-widest text-text-muted">
            Inspector · {inspector.kind === 'policyCell' ? 'Policy Cell' : inspector.kind === 'deploymentManifest' ? 'Manifest' : inspector.kind}
          </div>
          <IconButton ariaLabel="Close inspector" onClick={close} data-testid="inspector-close">
            <X size={14} />
          </IconButton>
        </header>
        <div className="flex-1 overflow-auto">
          {inspector.kind === 'policyCell' && (
            <PolicyCellRenderer instrument={inspector.instrument} cellKey={inspector.cellKey} />
          )}
          {inspector.kind === 'trade' && <TradeRenderer tradeId={inspector.tradeId} />}
          {inspector.kind === 'ghost' && (
            <GhostTradeRenderer ghostTradeId={inspector.ghostTradeId} />
          )}
          {inspector.kind === 'blocked' && (
            <BlockedIntentRenderer blockedIntentId={inspector.blockedIntentId} />
          )}
          {inspector.kind === 'deployment' && (
            <DeploymentRenderer deploymentId={inspector.deploymentId} />
          )}
          {inspector.kind === 'recommendation' && (
            <RecommendationRenderer recommendationId={inspector.recommendationId} />
          )}
          {inspector.kind === 'decisionChain' && (
            <DecisionChainRenderer decisionId={inspector.decisionId} />
          )}
        </div>
      </aside>
    </>
  );
}
