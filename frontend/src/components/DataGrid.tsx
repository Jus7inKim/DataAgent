import { useMemo, useState } from "react";

interface DataGridProps {
  columns: string[];
  rows: (string | number | null)[][];
}

type SortDir = "asc" | "desc" | null;

export default function DataGrid({ columns, rows }: DataGridProps) {
  const [sortCol, setSortCol] = useState<number | null>(null);
  const [sortDir, setSortDir] = useState<SortDir>(null);

  const sortedRows = useMemo(() => {
    if (sortCol === null || sortDir === null) return rows;
    const copy = [...rows];
    copy.sort((a, b) => {
      const av = a[sortCol];
      const bv = b[sortCol];
      if (av === null || av === undefined) return 1;
      if (bv === null || bv === undefined) return -1;
      const an = typeof av === "number" ? av : parseFloat(String(av));
      const bn = typeof bv === "number" ? bv : parseFloat(String(bv));
      let cmp: number;
      if (!Number.isNaN(an) && !Number.isNaN(bn)) {
        cmp = an - bn;
      } else {
        cmp = String(av).localeCompare(String(bv));
      }
      return sortDir === "asc" ? cmp : -cmp;
    });
    return copy;
  }, [rows, sortCol, sortDir]);

  function toggleSort(idx: number) {
    if (sortCol !== idx) {
      setSortCol(idx);
      setSortDir("asc");
    } else if (sortDir === "asc") {
      setSortDir("desc");
    } else if (sortDir === "desc") {
      setSortCol(null);
      setSortDir(null);
    } else {
      setSortDir("asc");
    }
  }

  return (
    <div className="grid-wrapper">
      <div className="grid-meta">전체 {rows.length}건</div>
      <div className="grid-scroll">
        <table className="data-grid">
          <thead>
            <tr>
              <th className="row-num">#</th>
              {columns.map((c, i) => (
                <th key={i} onClick={() => toggleSort(i)} title="클릭하여 정렬">
                  {c}
                  {sortCol === i && (
                    <span className="sort-ind">
                      {sortDir === "asc" ? " ▲" : sortDir === "desc" ? " ▼" : ""}
                    </span>
                  )}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {sortedRows.map((row, r) => (
              <tr key={r}>
                <td className="row-num">{r + 1}</td>
                {row.map((cell, c) => (
                  <td key={c}>{cell === null || cell === undefined ? "" : String(cell)}</td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}
