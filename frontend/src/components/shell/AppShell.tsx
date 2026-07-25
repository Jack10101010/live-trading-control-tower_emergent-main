import type { ReactNode } from 'react';
import { CommandSafetyBar } from './CommandSafetyBar';
import { ScopeNavigator } from './ScopeNavigator';
import { ContextBar } from './ContextBar';
import { LiveOperationsStrip } from './LiveOperationsStrip';
import { EventDock } from './EventDock';
import { CommandPalette } from './CommandPalette';
import { InspectorHost } from '@/components/inspector/InspectorHost';
import { ConfirmDialog } from '@/components/structures/ConfirmDialog';
import { useShellStore } from '@/store/shellStore';
import { usePreferenceSync, useRealtimeSync } from '@/hooks/useRepository';

/**
 * AppShell — universal page template (§C). Consistent skeleton, variable
 * emphasis (never removes safety bar, health strip, inspector host, or dock).
 */
export function AppShell({ children }: { children: ReactNode }) {
  const inspectorOpen = useShellStore((s) => s.inspector !== null);
  const inspectorWidth = useShellStore((s) => s.inspectorWidth);
  usePreferenceSync();
  useRealtimeSync();
  return (
    <div className="flex flex-col h-screen w-screen overflow-hidden" style={{ background: 'var(--bg)' }}>
      <CommandSafetyBar />
      <div className="flex flex-1 min-h-0">
        <ScopeNavigator />
        <div className="flex flex-col flex-1 min-w-0">
          <ContextBar />
          {/* UI-3 — persistent, read-only live-operations strip. Placed above the
              workspace so safety-critical node state is visible on every screen. */}
          <LiveOperationsStrip />
          <main
            className="flex-1 min-h-0 overflow-hidden"
            style={{ paddingRight: inspectorOpen ? inspectorWidth : 0, transition: 'padding-right 180ms cubic-bezier(.2,0,0,1)' }}
          >
            {children}
          </main>
          <EventDock />
        </div>
      </div>
      <InspectorHost />
      <CommandPalette />
      <ConfirmDialog />
    </div>
  );
}
