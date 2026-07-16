import { QueryClient } from '@tanstack/react-query';

/**
 * The single QueryClient. Lives in its own module (not main.tsx) so that
 * non-hook code — `executeCommand` in hooks/useCommand.ts — can invalidate
 * queries after a mutation. There must never be a second `new QueryClient`
 * (lazy-loading invariant, handoff §11).
 */
export const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      staleTime: Infinity,
      refetchOnWindowFocus: false,
      retry: 1,
    },
  },
});
