import { useCallback } from 'react';
import { toast } from 'sonner';
import { api } from '@/lib/api';
import { commandMeta, type Command } from '@/lib/commands';
import { applyEvents } from '@/lib/realtime';
import { useCommandStore } from '@/store/commandStore';

/**
 * The single command dispatcher. Every mutating action calls `dispatch(command)`.
 * - `silent`         → executes immediately.
 * - `confirm` / `confirm+reason` → routes to the one ConfirmDialog.
 *
 * There is no other path to a mutation. v1 executes against the mock backend
 * (`POST /api/commands/{name}`) which acknowledges without touching a broker.
 */
export function useCommand() {
  const requestConfirm = useCommandStore((s) => s.requestConfirm);

  return useCallback(
    (command: Command) => {
      const meta = commandMeta[command.name];
      if (meta.confirmation === 'silent') {
        void executeCommand(command);
      } else {
        requestConfirm(command);
      }
    },
    [requestConfirm]
  );
}

/**
 * Execute a command against the mock backend. Called directly for `silent`
 * commands and by the ConfirmDialog after confirmation (optionally with a reason).
 */
export async function executeCommand(command: Command, reason?: string): Promise<void> {
  const meta = commandMeta[command.name];
  try {
    const res = await api.command(command.name, { ...command.payload, reason });
    // Phase 5: apply the returned event through the SAME realtime path used by the
    // delta stream. The issuing client updates instantly (and resiliently if the
    // stream is lagging); the shared long-poll propagates to every other client.
    // Dedup by seq means the event is applied at most once across both paths.
    if (res.events?.length) applyEvents(res.events);
    const seq = res.events?.[0]?.seq;
    toast.success(`${meta.label} — accepted`, {
      description: `mock · ${res.commandId ?? 'ack'} · ${
        seq ? `audit event #${seq} logged` : 'no live broker action'
      }`,
    });
  } catch (err) {
    toast.error(`${meta.label} — failed`, {
      description: err instanceof Error ? err.message : 'Command was not accepted.',
    });
  }
}
