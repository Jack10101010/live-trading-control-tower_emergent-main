import { create } from 'zustand';
import type { Command } from '@/lib/commands';

/**
 * Holds the command awaiting confirmation. The ConfirmDialog reads it; the
 * dispatcher (`useCommand`) writes it. One pending command at a time.
 */
interface CommandState {
  pending: Command | null;
  requestConfirm: (command: Command) => void;
  clear: () => void;
}

export const useCommandStore = create<CommandState>((set) => ({
  pending: null,
  requestConfirm: (command) => set({ pending: command }),
  clear: () => set({ pending: null }),
}));
