import { EmptyState } from '@/components/structures/Panel';
import { PACKAGES_UNAVAILABLE_DETAIL } from '@/lib/operationalProvenance';
import { GitBranch } from 'lucide-react';

/**
 * M-PKG-1 — version history and package comparison have no authoritative source.
 *
 * These views listed the fixture's authored packages as a promotion timeline,
 * and diffed them cell-by-cell as though builds had genuinely changed. Both
 * described a release history that never happened: no package registry exists.
 *
 * Comparison is the sharper of the two. A diff asserts that one build succeeded
 * another and that specific policy cells changed between them — a causal claim
 * about deployment history, from a file.
 */
export function VersionHistoryView() {
  return (
    <Unavailable
      title="Version History"
      subtitle="Package promotion timeline · rollback points"
      empty="No package version history"
    />
  );
}

export function PackageComparisonView() {
  return (
    <Unavailable
      title="Package Comparison"
      subtitle="Cell-level diff between package versions"
      empty="No packages to compare"
    />
  );
}

function Unavailable({ title, subtitle, empty }: { title: string; subtitle: string; empty: string }) {
  return (
    <div className="p-6 h-full min-h-0 overflow-auto" data-testid="versioning-unavailable">
      <h1 className="text-2xl font-semibold text-text tracking-tight mb-1">{title}</h1>
      <p className="text-xs text-text-muted mb-6">{subtitle}</p>
      <EmptyState title={empty} description={PACKAGES_UNAVAILABLE_DETAIL} icon={<GitBranch size={16} />} />
    </div>
  );
}
