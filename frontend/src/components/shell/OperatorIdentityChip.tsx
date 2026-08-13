import type { OperatorIdentity } from '@/lib/api';

/**
 * M-WORLD-ORDINARY-1 — who this Control Tower is acting as, in the shell.
 *
 * THE DEFECT THIS REPLACES
 *   The shell rendered `world.operators[0]` — the development FIXTURE's authored
 *   operator. Its display name sat in the chrome of every ordinary route and its
 *   first letter was used as an avatar, so the application fetched all 22
 *   fixture collections on every page in order to show an invented person. An
 *   asserted human identity in an operator console is not decoration: it is the
 *   name an operator would expect to see attached to their own actions.
 *
 * WHAT IS SHOWN, AND WHAT IS DELIBERATELY ABSENT
 *   The operator id this process is CONFIGURED to act as — the same id every
 *   audit record, broker context and safety `operator_ref` already carries —
 *   with its source stated. When nothing is configured it says so, because
 *   `UNATTRIBUTED` is a real and reportable state rather than a failure.
 *
 *   No display name, avatar, role, timezone, email or permission level. This
 *   deployment has no per-user authentication at all — the API boundary
 *   authenticates one shared token, with no users and no sessions — so every
 *   one of those would have to be invented. Notably there is no avatar: the old
 *   one took `displayName.charAt(0)`, which with no name source renders the
 *   letter "u" from the word "undefined", a monogram for a person nobody
 *   established.
 *
 * Extracted from `CommandSafetyBar` so this surface can be rendered and asserted
 * on its own. Identity shown to an operator deserves a test that does not depend
 * on the rest of the shell booting.
 */
export function OperatorIdentityChip({ operator }: { operator: OperatorIdentity }) {
  const attributed = operator.attributed && Boolean(operator.operatorId);
  return (
    <div className="flex items-center gap-2 px-3 h-full" data-testid="shell-operator">
      <div className="flex flex-col leading-tight">
        <span
          className="text-xs mono"
          style={{ color: attributed ? 'var(--text)' : 'var(--text-muted)' }}
        >
          {attributed ? operator.operatorId : 'Operator not configured'}
        </span>
        <span
          className="text-2xs text-text-muted mono"
          title={
            attributed
              ? `Asserted from ${operator.configVar}. This Control Tower has no ` +
                'per-user authentication, so this identity is configuration, not a login.'
              : `Set ${operator.configVar} to attribute actions to an operator. ` +
                `Until then actions are recorded as ${operator.operatorId || 'UNATTRIBUTED'}.`
          }
        >
          {attributed ? `configured · ${operator.assurance}` : 'unattributed'}
        </span>
      </div>
    </div>
  );
}
