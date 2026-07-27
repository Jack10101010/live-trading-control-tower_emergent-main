/**
 * ARCH-3 — operator authentication session (enter / clear / inspect only).
 *
 * The minimum UI needed to use the tower when inbound operator authentication is
 * enabled. NOT an execution control:
 *   - the token is held in memory only (see lib/authSession) — a page reload
 *     clears it, and that fact is stated in the panel;
 *   - the input is `type=password` and its value is wiped from local state the
 *     moment it is applied — the token is never rendered, stored or echoed;
 *   - the panel reports only WHETHER a token is held, never anything about it.
 */
import { useState } from 'react';
import {
  clearOperatorToken,
  hasOperatorToken,
  setOperatorToken,
} from '@/lib/authSession';

export function AuthSessionPanel() {
  const [draft, setDraft] = useState('');
  const [held, setHeld] = useState(hasOperatorToken());

  const apply = () => {
    setOperatorToken(draft);
    setDraft('');                       // never keep the value in component state
    setHeld(hasOperatorToken());
  };
  const clear = () => {
    clearOperatorToken();
    setHeld(false);
  };

  return (
    <section
      className="rounded-md border border-[color:var(--border)] bg-[color:var(--panel)] p-3"
      aria-label="Operator authentication"
      data-testid="auth-session-panel"
    >
      <header className="flex items-baseline justify-between gap-2 mb-1">
        <h3 className="text-xs uppercase tracking-wider text-text-muted">
          Operator authentication
        </h3>
        <span
          className="text-2xs mono px-1.5 py-0.5 rounded-sm border"
          style={{
            borderColor: held ? 'var(--positive)' : 'var(--text-muted)',
            color: held ? 'var(--positive)' : 'var(--text-muted)',
          }}
          data-testid="auth-session-state"
        >
          {held ? 'TOKEN HELD (MEMORY)' : 'NO TOKEN'}
        </span>
      </header>

      <p className="text-2xs text-text-muted mb-2">
        The operator API token is held in memory only and applied by the API client.
        It is never stored in the browser; reloading the page clears it. Required
        only when the backend has operator authentication enabled.
      </p>

      <div className="flex flex-wrap items-center gap-2">
        <label htmlFor="auth-token-input" className="text-2xs text-text-muted">
          Token:
        </label>
        <input
          id="auth-token-input"
          type="password"
          autoComplete="off"
          value={draft}
          onChange={(e) => setDraft(e.target.value)}
          className="text-2xs mono px-2 py-1 rounded-sm border bg-transparent min-w-56"
          style={{ borderColor: 'var(--border)' }}
          data-testid="auth-token-input"
        />
        <button
          type="button"
          onClick={apply}
          disabled={!draft.trim()}
          className="text-2xs px-2 py-1 rounded-sm border disabled:opacity-50"
          style={{ borderColor: 'var(--border)', color: 'var(--text-2)' }}
          data-testid="auth-token-apply"
        >
          Use token
        </button>
        <button
          type="button"
          onClick={clear}
          disabled={!held}
          className="text-2xs px-2 py-1 rounded-sm border disabled:opacity-50"
          style={{ borderColor: 'var(--border)', color: 'var(--text-2)' }}
          data-testid="auth-token-clear"
        >
          Clear
        </button>
      </div>
    </section>
  );
}
