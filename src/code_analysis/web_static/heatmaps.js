// Heatmaps are presentation-only views of exact cells in retained table projections.
// Each metric is scaled independently; no aggregate score is calculated here.

const CONFIG = Object.freeze({
  complexity: Object.freeze({
    label: 'Complexity heatmap',
    rowColumns: Object.freeze(['module', 'symbol']),
    metrics: Object.freeze([
      'physical-loc',
      'branches',
      'nesting',
      'arguments',
      'cyclomatic',
    ]),
    disclosure: 'Colors are scaled independently for each metric over retained rows only. Weighted score remains available in the table and is not used as a heatmap score.',
  }),
  'structure-lifecycle': Object.freeze({
    label: 'Ownership evidence heatmap',
    rowColumns: Object.freeze(['owner', 'scope', 'type']),
    metrics: Object.freeze([
      'producers',
      'readers',
      'writers',
      'mutators',
      'serializers',
      'conversions',
      'aliases',
    ]),
    disclosure: 'Counts describe statically recorded roles and flows; they do not prove runtime ownership. Colors are scaled independently for each metric over retained rows only.',
  }),
});

function numberValue(cell) {
  if (cell?.integer_value !== null && cell?.integer_value !== undefined) return cell.integer_value;
  if (cell?.number_value !== null && cell?.number_value !== undefined) return cell.number_value;
  return null;
}

function stringValue(cell) {
  return cell?.string_value ?? '—';
}

function bounds(values) {
  const present = values.filter(value => value !== null && Number.isFinite(value));
  return present.length ? {minimum: Math.min(...present), maximum: Math.max(...present), count: present.length} : null;
}

function heatLevel(value, range) {
  if (value === null || !range) return null;
  if (range.maximum === range.minimum) return value === 0 ? 0 : 3;
  return Math.min(5, Math.floor(6 * ((value - range.minimum) / (range.maximum - range.minimum))));
}

export function heatmapView(name, projection, escape) {
  const config = CONFIG[name];
  if (!config || projection.projection_kind !== 'table') return null;
  const positions = new Map(projection.columns.map((column, index) => [column.column_id, index]));
  if (![...config.rowColumns, ...config.metrics].every(column => positions.has(column))) return null;

  const metrics = config.metrics.map(columnId => {
    const column = projection.columns[positions.get(columnId)];
    const values = projection.rows.map(row => numberValue(row.cells[positions.get(columnId)]));
    return {columnId, label: column.label, values, range: bounds(values)};
  });
  const headings = config.rowColumns.map(columnId => projection.columns[positions.get(columnId)].label);
  const header = [...headings, ...metrics.map(metric => metric.label)];
  const rows = projection.rows.map((row, rowIndex) => {
    const identityCells = config.rowColumns.map(columnId => `<th scope="row">${escape(stringValue(row.cells[positions.get(columnId)]))}</th>`).join('');
    const metricCells = metrics.map(metric => {
      const value = metric.values[rowIndex];
      if (value === null || !Number.isFinite(value)) {
        return `<td class="heatmap-cell missing" aria-label="${escape(metric.label)}: missing"><span aria-hidden="true">—</span><span class="sr-only">Missing</span></td>`;
      }
      const level = heatLevel(value, metric.range);
      const range = metric.range;
      return `<td class="heatmap-cell heat-${level}" aria-label="${escape(metric.label)}: ${escape(value)}; retained range ${escape(range.minimum)} to ${escape(range.maximum)}"><span>${escape(value)}</span></td>`;
    }).join('');
    return `<tr data-row-id="${escape(row.row_id)}">${identityCells}${metricCells}</tr>`;
  }).join('');
  const legends = metrics.map(metric => {
    const summary = metric.range
      ? `min ${metric.range.minimum} · max ${metric.range.maximum} · ${metric.range.count} retained values`
      : 'no retained numeric values';
    return `<li><strong>${escape(metric.label)}</strong><span class="heatmap-ramp" aria-hidden="true"></span><span>${escape(summary)} · six linear bins</span></li>`;
  }).join('');

  return `<section class="heatmap-panel" aria-labelledby="heatmap-title">
    <h3 id="heatmap-title">${escape(config.label)}</h3>
    <p class="hint">${escape(config.disclosure)}</p>
    <div class="table-wrap heatmap-wrap" role="region" aria-label="${escape(config.label)} values" tabindex="0"><table class="heatmap-table"><thead><tr>${header.map(value => `<th scope="col">${escape(value)}</th>`).join('')}</tr></thead><tbody>${rows}</tbody></table></div>
    <ul class="heatmap-legends" aria-label="Per-metric heatmap scales">${legends}</ul>
    <p class="hint">A constant all-zero column uses the lowest shade. An equal nonzero column uses the midpoint shade; equal values are not ranked within that column.</p>
    <p class="hint">An em dash means the saved projection has no numeric value; it is not treated as zero.</p>
  </section>`;
}
