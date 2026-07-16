import { cn } from '@/lib/utils';
import { useMemo, useState, type ReactNode } from 'react';
import { ArrowUp, ArrowDown, Search } from 'lucide-react';

export interface Column<T> {
  key: string;
  header: ReactNode;
  cell: (row: T) => ReactNode;
  align?: 'left' | 'right' | 'center';
  width?: number | string;
  mono?: boolean;
  sticky?: boolean;
  /** Enable click-to-sort on this column. */
  sortable?: boolean;
  /** Value used for sorting (defaults to none). */
  sortAccessor?: (row: T) => string | number;
}

interface DataTableProps<T> {
  columns: Column<T>[];
  data: T[];
  rowKey: (row: T) => string;
  onRowClick?: (row: T) => void;
  selectedKey?: string;
  emptyMessage?: string;
  dense?: boolean;
  className?: string;
  /** Show a search box above the table. */
  searchable?: boolean;
  searchAccessor?: (row: T) => string;
  searchPlaceholder?: string;
}

/**
 * DataTable — ONE config-driven table for every list. Now with optional
 * click-to-sort (per column) and search (whole-table). Never spawn per-domain
 * tables — pass a different `columns` array and `rowKey`.
 */
export function DataTable<T>({
  columns,
  data,
  rowKey,
  onRowClick,
  selectedKey,
  emptyMessage = 'No records',
  dense = true,
  className,
  searchable = false,
  searchAccessor,
  searchPlaceholder = 'Search…',
}: DataTableProps<T>) {
  const [query, setQuery] = useState('');
  const [sortKey, setSortKey] = useState<string | null>(null);
  const [sortDir, setSortDir] = useState<'asc' | 'desc'>('asc');

  const filtered = useMemo(() => {
    if (!searchable || !query.trim() || !searchAccessor) return data;
    const q = query.trim().toLowerCase();
    return data.filter((r) => searchAccessor(r).toLowerCase().includes(q));
  }, [data, query, searchable, searchAccessor]);

  const sorted = useMemo(() => {
    if (!sortKey) return filtered;
    const col = columns.find((c) => c.key === sortKey);
    if (!col?.sortAccessor) return filtered;
    const acc = col.sortAccessor;
    const dir = sortDir === 'asc' ? 1 : -1;
    return [...filtered].sort((a, b) => {
      const va = acc(a);
      const vb = acc(b);
      if (va < vb) return -1 * dir;
      if (va > vb) return 1 * dir;
      return 0;
    });
  }, [filtered, sortKey, sortDir, columns]);

  const toggleSort = (key: string) => {
    if (sortKey !== key) {
      setSortKey(key);
      setSortDir('asc');
    } else if (sortDir === 'asc') {
      setSortDir('desc');
    } else {
      setSortKey(null);
    }
  };

  return (
    <div className={cn('flex flex-col min-h-0', className)}>
      {searchable && (
        <div className="flex items-center gap-2 px-3 py-2 border-b" style={{ borderColor: 'var(--border-subtle)' }}>
          <Search size={13} className="text-text-muted" />
          <input
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            placeholder={searchPlaceholder}
            className="flex-1 bg-transparent border-0 outline-none text-sm text-text placeholder:text-text-muted"
            data-testid="table-search"
          />
          {query && (
            <span className="text-2xs text-text-muted mono">
              {sorted.length}/{data.length}
            </span>
          )}
        </div>
      )}
      <div className="relative overflow-auto">
        <table className="w-full border-collapse text-sm tabular">
          <thead className="sticky top-0 z-10 text-2xs uppercase tracking-widest text-text-muted" style={{ background: 'var(--panel-2)' }}>
            <tr>
              {columns.map((col) => {
                const sortedHere = sortKey === col.key;
                return (
                  <th
                    key={col.key}
                    scope="col"
                    className={cn(
                      'font-medium border-b py-2 px-3 whitespace-nowrap',
                      col.align === 'right' && 'text-right',
                      col.align === 'center' && 'text-center',
                      col.align !== 'right' && col.align !== 'center' && 'text-left',
                      col.sortable && 'cursor-pointer select-none hover:text-text'
                    )}
                    style={{ borderColor: 'var(--border-subtle)', width: col.width, minWidth: col.width }}
                    onClick={col.sortable ? () => toggleSort(col.key) : undefined}
                  >
                    <span className={cn('inline-flex items-center gap-1', col.align === 'right' && 'flex-row-reverse')}>
                      {col.header}
                      {col.sortable && sortedHere && (sortDir === 'asc' ? <ArrowUp size={10} /> : <ArrowDown size={10} />)}
                    </span>
                  </th>
                );
              })}
            </tr>
          </thead>
          <tbody>
            {sorted.length === 0 && (
              <tr>
                <td colSpan={columns.length} className="text-center text-text-muted italic py-8">
                  {emptyMessage}
                </td>
              </tr>
            )}
            {sorted.map((row) => {
              const key = rowKey(row);
              const selected = selectedKey === key;
              return (
                <tr
                  key={key}
                  onClick={() => onRowClick?.(row)}
                  className={cn(
                    'border-b transition-colors duration-fast',
                    onRowClick && 'cursor-pointer hover:bg-[color:var(--panel-2)]',
                    selected && 'bg-[color:var(--selection)]'
                  )}
                  style={{ borderColor: 'var(--border-subtle)' }}
                >
                  {columns.map((col) => (
                    <td
                      key={col.key}
                      className={cn(
                        'py-2 px-3 align-middle',
                        dense ? 'h-8' : 'h-10',
                        col.align === 'right' && 'text-right',
                        col.align === 'center' && 'text-center',
                        col.mono && 'mono'
                      )}
                    >
                      {col.cell(row)}
                    </td>
                  ))}
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </div>
  );
}
