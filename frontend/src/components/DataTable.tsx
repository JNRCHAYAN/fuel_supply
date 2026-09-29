import type { ReactNode } from 'react'

export interface Column<T> {
  key: string
  header: string
  numeric?: boolean
  render: (row: T) => ReactNode
}

export interface DataTableProps<T> {
  columns: Array<Column<T>>
  rows: T[] | undefined
  getRowKey: (row: T) => string | number
  caption?: string
  emptyMessage?: string
  minWidth?: number
}

/**
 * A dense table. `rows` is typed as possibly undefined because a real
 * simulator can return nothing where the guide's example always has data;
 * an absent collection renders an empty state rather than crashing on .map.
 */
export function DataTable<T>({
  columns, rows, getRowKey, caption, emptyMessage = 'No records', minWidth = 640,
}: DataTableProps<T>) {
  const safeRows = Array.isArray(rows) ? rows : []

  if (safeRows.length === 0) {
    return <p className="px-1 py-6 text-center text-[13px] text-muted">{emptyMessage}</p>
  }

  return (
    <div className="-mx-4 overflow-x-auto px-4">
      <table className="w-full border-collapse text-[13px]" style={{ minWidth }}>
        {caption ? <caption className="sr-only">{caption}</caption> : null}
        <thead>
          <tr className="border-b border-hairline">
            {columns.map((column) => (
              <th
                key={column.key}
                scope="col"
                className={`px-2.5 py-2 text-[11px] font-medium uppercase tracking-wide text-subtle ${
                  column.numeric ? 'text-right' : 'text-left'
                }`}
              >
                {column.header}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {safeRows.map((row) => (
            <tr key={getRowKey(row)} className="border-b border-hairline last:border-0 hover:bg-surface-raised">
              {columns.map((column) => (
                <td
                  key={column.key}
                  className={`px-2.5 py-2 align-middle text-ink ${column.numeric ? 'mono text-right' : ''}`}
                >
                  {column.render(row)}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}
