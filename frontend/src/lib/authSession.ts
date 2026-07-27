/**
 * ARCH-3 — the operator authentication session. MEMORY ONLY.
 *
 * The single owner of the operator API token on the frontend. The token:
 *   - lives in this module's closure only — never localStorage, sessionStorage,
 *     IndexedDB, cookies, URL parameters, Redux/devtools-visible state, or any
 *     other persistence. A page reload therefore CLEARS it, by design.
 *   - is applied to requests by exactly one owner (`lib/api.ts` merges
 *     `authHeader()` into every request); no component builds its own header.
 *   - is never rendered, logged, or included in an error message. Consumers may
 *     ask only WHETHER a token is held, never what it is.
 */

let operatorToken: string | null = null;

/** Hold the operator token in memory (trimmed; blank clears). */
export function setOperatorToken(token: string): void {
  const trimmed = (token ?? '').trim();
  operatorToken = trimmed.length > 0 ? trimmed : null;
}

/** Explicitly clear the token. Subsequent requests carry no Authorization. */
export function clearOperatorToken(): void {
  operatorToken = null;
}

/** Whether a token is currently held. Never the value. */
export function hasOperatorToken(): boolean {
  return operatorToken !== null;
}

/** The Authorization header for the current session — empty when no token. Used
 *  ONLY by the api client (the single header owner). */
export function authHeader(): Record<string, string> {
  return operatorToken ? { Authorization: `Bearer ${operatorToken}` } : {};
}

/** Typed authentication failure: 401 (credential missing/invalid) and 503
 *  (server-side auth misconfiguration) are DISTINCT from network failure, so the
 *  UI can say "unauthorized" instead of falsely claiming the backend is offline. */
export class ApiAuthError extends Error {
  constructor(public readonly status: 401 | 503, path: string) {
    super(status === 401 ? `unauthorized: ${path}` : `auth misconfigured on server: ${path}`);
    this.name = 'ApiAuthError';
  }
}

export function isAuthError(err: unknown): err is ApiAuthError {
  return err instanceof ApiAuthError;
}
