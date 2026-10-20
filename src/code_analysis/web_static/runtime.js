// All runtime metrics and compatibility decisions are supplied by Python.
import {graphView, wireGraphDisplay} from './projections.js';

const GRAPH_DEFAULTS = Object.freeze({max_nodes: 60, max_edges: 120, project_only: false});
const DRIFT_MESSAGE = 'Call graph bounds changed since this graph was rendered. '
  + 'Show runtime evidence again before downloading.';
const TABLE_DRIFT_MESSAGE = 'Runtime table selection changed since this table was rendered. '
  + 'Show runtime evidence again before downloading.';
const COMPARISON_DRIFT_MESSAGE = 'Runtime comparison selection changed since this comparison was rendered. '
  + 'Show runtime evidence again before downloading.';
const COMPARISON_SOURCE_DRIFT_MESSAGE = 'Runtime comparison selection changed since this source preview was requested. '
  + 'Show runtime evidence again before previewing source.';
const TABLE_SECTIONS = new Set([
  'hotspots', 'calls', 'variance', 'resources', 'categories', 'correlations', 'process',
  'lifecycle', 'steps',
]);
const TABLE_FORMATS = ['json', 'tsv', 'markdown'];

// The runtime graph request carries exactly these three fields; anything else is
// not an exact selection and must not be rendered as one.
function graphRequest(value) {
  if (!value || typeof value !== 'object' || typeof value.project_only !== 'boolean') return null;
  const request = {
    max_nodes: Number(value.max_nodes),
    max_edges: Number(value.max_edges),
    project_only: value.project_only,
  };
  if (!Number.isInteger(request.max_nodes) || !Number.isInteger(request.max_edges)) return null;
  return request;
}

function tableRequest(value) {
  if (!value || typeof value !== 'object') return null;
  const section = String(value.section || '');
  if (!TABLE_SECTIONS.has(section)) return null;
  return {section, frame: String(value.frame ?? '')};
}

const sameRequest = (left, right) => Boolean(
  left && right
  && left.max_nodes === right.max_nodes
  && left.max_edges === right.max_edges
  && left.project_only === right.project_only,
);

const sameTableRequest = (left, right) => Boolean(
  left && right && left.section === right.section && left.frame === right.frame,
);

const graphParams = (request) => new URLSearchParams({
  max_nodes: String(request.max_nodes),
  max_edges: String(request.max_edges),
  project_only: String(request.project_only),
});

const tableParams = (request) => {
  const params = new URLSearchParams({section: request.section, max_records: '100'});
  if (request.frame) params.set('frame', request.frame);
  return params;
};

export async function runtimeView(content, api, escape, signal, request, options = {}) {
  const overview = await api('/api/runtime', signal);
  if (!overview.available) { content.textContent = overview.message; return; }
  const handoff = options.initialRequest === undefined ? null : graphRequest(options.initialRequest);
  if (options.initialRequest !== undefined && handoff === null) {
    throw new Error('the handed-off runtime graph selection is not an exact bounded request');
  }
  const initial = handoff || GRAPH_DEFAULTS;
  content.innerHTML = `<p>${escape(overview.message)}</p><p>Profile: <code>${escape(overview.profile_identity)}</code> · ${escape(overview.completeness)}</p><p>${overview.source_matches ? 'Matches the selected static report.' : 'Does not match the selected static report; no cross-report source binding is claimed.'}</p>${overview.limitations.map(value => `<p class="hint">${escape(value)}</p>`).join('')}<form id="runtime-controls"><label>Runtime view<select name="section">${['hotspots','calls','variance','resources','categories','correlations','process','lifecycle','steps',...(overview.comparison_available ? ['comparison'] : [])].map(value => `<option>${value}</option>`).join('')}</select></label><label>Frame ID (hotspots/calls/variance only)<input name="frame"></label><fieldset id="runtime-graph-controls" hidden disabled><legend>Call graph bounds</legend><label>Maximum nodes<input name="max_nodes" type="number" min="1" max="100" step="1" value="${initial.max_nodes}"></label><label>Maximum edges<input name="max_edges" type="number" min="1" max="250" step="1" value="${initial.max_edges}"></label><label><input name="project_only" type="checkbox" ${initial.project_only ? 'checked' : ''}>Project frames only</label><p class="hint">These bounds apply to the call graph only. Downloads use the bounds of the graph on screen, never unapplied control values.</p></fieldset><button class="detail-button">Show runtime evidence</button></form><div id="runtime-results" aria-live="polite"></div>`;
  const form = content.querySelector('form'), result = content.querySelector('#runtime-results');
  form.elements.section.setAttribute('aria-label', 'Runtime view');
  const graphOption = document.createElement('option'); graphOption.value = 'graph';
  graphOption.textContent = 'call graph'; form.elements.section.append(graphOption);
  const graphControls = content.querySelector('#runtime-graph-controls');
  if (handoff) form.elements.section.value = 'graph';
  function syncGraphControls() {
    const graph = form.elements.section.value === 'graph';
    graphControls.hidden = !graph;
    graphControls.disabled = !graph;
  }
  syncGraphControls();
  function wireCodeBrowsing(frameIds, originSignal) {
    if (!options.staticReportSha256 || !options.onBrowseSource || !overview.profile_report_sha256) return;
    const controls = document.createElement('details');
    const summary = document.createElement('summary'); summary.textContent = 'Browse recorded frame source';
    controls.append(summary);
    const status = document.createElement('p'); status.className = 'hint'; status.setAttribute('role', 'status');
    let sourceRevision = 0;
    for (const frameId of [...new Set(frameIds)].sort()) {
      const button = document.createElement('button'); button.type = 'button'; button.className = 'detail-button';
      button.textContent = `Browse source: ${frameId}`; button.dataset.runtimeBrowseFrame = frameId;
      button.addEventListener('click', async () => {
        const revision = ++sourceRevision;
        button.disabled = true; status.textContent = 'Checking saved runtime/static source binding…';
        try {
          const params = new URLSearchParams({report_sha256: options.staticReportSha256,
            profile_report_sha256: overview.profile_report_sha256,
            profile_identity_sha256: overview.profile_identity, revision, frame_id: frameId});
          const mapping = await api(`/api/code-browser/runtime-source?${params}`, originSignal);
          originSignal.throwIfAborted();
          if (revision !== sourceRevision) return;
          if (mapping.report_sha256 !== options.staticReportSha256
            || mapping.profile_report_sha256 !== overview.profile_report_sha256
            || mapping.profile_identity_sha256 !== overview.profile_identity
            || mapping.revision !== revision
            || mapping.frame_id !== frameId) throw new Error('The originating runtime evidence changed.');
          if (mapping.state !== 'available') { status.textContent = mapping.message; return; }
          await options.onBrowseSource({report_sha256: mapping.selector.report_sha256,
            path: mapping.selector.path, line: mapping.selector.line,
            identity: mapping.selector.identity}, originSignal);
        } catch (error) { if (!originSignal.aborted && revision === sourceRevision) status.textContent = error.message; }
        finally { if (!originSignal.aborted) button.disabled = false; }
      });
      controls.append(button);
    }
    controls.append(status); result.append(controls);
  }
  const controlRequest = () => ({
    max_nodes: Number(form.elements.max_nodes.value),
    max_edges: Number(form.elements.max_edges.value),
    project_only: form.elements.project_only.checked,
  });
  const controlTableRequest = () => tableRequest({
    section: form.elements.section.value,
    frame: form.elements.frame.value,
  });
  // The exact selection behind the graph on screen, not the current control values.
  let rendered;
  let renderedTable;
  function syncGraphDownloads() {
    const buttons = result.querySelectorAll('[data-runtime-format]');
    if (!buttons.length) return;
    const drifted = !sameRequest(rendered, controlRequest());
    buttons.forEach(button => { button.disabled = drifted; });
    const status = result.querySelector('.runtime-download-status');
    if (status) status.textContent = drifted ? DRIFT_MESSAGE : '';
  }
  function syncTableDownloads() {
    const buttons = result.querySelectorAll('[data-runtime-table-format]');
    if (!buttons.length) return;
    const drifted = !sameTableRequest(renderedTable, controlTableRequest());
    buttons.forEach(button => { button.disabled = drifted; });
    const status = result.querySelector('.runtime-table-download-status');
    if (status) status.textContent = drifted ? TABLE_DRIFT_MESSAGE : '';
  }
  let renderedComparison = false;
  let selectedComparisonIdentity = null;
  let comparisonSourceToken = null;
  let comparisonSourceController;
  function syncComparisonDownloads() {
    const buttons = result.querySelectorAll('[data-runtime-comparison-format]');
    if (!buttons.length) return;
    const drifted = form.elements.section.value !== 'comparison' || !renderedComparison;
    buttons.forEach(button => { button.disabled = drifted; });
    const status = result.querySelector('.runtime-comparison-download-status');
    if (status) status.textContent = drifted ? COMPARISON_DRIFT_MESSAGE : '';
  }
  form.elements.section.addEventListener('change', () => {
    syncGraphControls();
    syncTableDownloads();
    syncComparisonDownloads();
    comparisonSourceController?.abort();
    comparisonSourceToken = null;
    selectedComparisonIdentity = null;
  });
  form.elements.frame.addEventListener('input', () => {
    syncTableDownloads();
    comparisonSourceController?.abort();
    comparisonSourceToken = null;
  });
  graphControls.addEventListener('input', syncGraphDownloads);
  const health = document.createElement('details');
  health.innerHTML = `<summary>Runtime provider health and diagnostics</summary>${overview.providers.map(item => `<p>${escape(item.provider_id)}: ${escape(item.state)} · ${escape(item.completeness)}</p>`).join('')}${overview.diagnostics.map(item => `<p>${escape(item.code)}: ${escape(item.message)}</p>`).join('')}<p>Omitted: ${overview.omitted_providers} providers and ${overview.omitted_diagnostics} diagnostics.</p>`;
  form.before(health);
  let pending;
  signal.addEventListener('abort', () => {
    pending?.abort();
    comparisonSourceController?.abort();
  }, {once: true});
  async function loadComparisonSource(side) {
    if (!selectedComparisonIdentity || form.elements.section.value !== 'comparison' || !renderedComparison) {
      const panel = result.querySelector('.runtime-comparison-source-preview');
      if (panel) panel.textContent = COMPARISON_SOURCE_DRIFT_MESSAGE;
      return;
    }
    const token = comparisonSourceToken = {
      side,
      frame_identity: selectedComparisonIdentity,
      section: form.elements.section.value,
    };
    comparisonSourceController?.abort();
    comparisonSourceController = new AbortController();
    const sourceSignal = comparisonSourceController.signal;
    let panel = result.querySelector('.runtime-comparison-source-preview');
    if (!panel) {
      panel = document.createElement('section');
      panel.className = 'runtime-comparison-source-preview source-preview';
      panel.setAttribute('aria-live', 'polite');
      result.append(panel);
    }
    panel.textContent = `Verifying ${side} source…`;
    try {
      const params = new URLSearchParams({
        frame_identity: selectedComparisonIdentity,
        side,
      });
      const value = await api(`/api/runtime/comparison-source?${params}`, sourceSignal);
      if (comparisonSourceToken !== token || form.elements.section.value !== 'comparison') return;
      if (value.frame_identity !== selectedComparisonIdentity || value.side !== side) return;
      const preview = value.preview;
      panel.replaceChildren();
      panel.append(`${value.side}${value.frame_id ? ` · ${value.frame_id}` : ''}: ${value.state}: ${value.message}`);
      if (value.state === 'bound' && preview) {
        const identity = document.createElement('p');
        identity.textContent = `${preview.logical_path} · SHA-256 ${preview.expected_sha256}`;
        panel.append(identity);
        const pre = document.createElement('pre');
        pre.className = 'report-text';
        pre.textContent = preview.lines.map(line => `${line.number}  ${line.text}`).join('\n');
        panel.append(pre);
      }
    } catch (error) {
      if (error.name !== 'AbortError' && comparisonSourceToken === token) {
        panel.textContent = `Runtime comparison source unavailable: ${error.message}`;
      }
    }
  }
  function wireComparisonSourceControls() {
    const activate = side => () => {
      if (form.elements.section.value !== 'comparison' || !renderedComparison) return;
      loadComparisonSource(side);
    };
    const baseline = result.querySelector('[data-runtime-comparison-baseline-source]');
    const candidate = result.querySelector('[data-runtime-comparison-candidate-source]');
    baseline?.addEventListener('click', activate('baseline'));
    candidate?.addEventListener('click', activate('candidate'));
    for (const button of [baseline, candidate]) {
      button?.addEventListener('keydown', event => {
        if (['Enter', ' '].includes(event.key)) {
          event.preventDefault();
          button.click();
        }
      });
    }
    result.querySelectorAll('[data-comparison-row]').forEach(row => {
      const select = () => {
        selectedComparisonIdentity = row.dataset.comparisonRow;
        comparisonSourceToken = null;
        comparisonSourceController?.abort();
        result.querySelectorAll('[data-comparison-row]').forEach(item => {
          item.classList.toggle('selected', item === row);
          item.setAttribute('aria-selected', item === row ? 'true' : 'false');
        });
        const panel = result.querySelector('.runtime-comparison-source-preview');
        if (panel) panel.textContent = `Selected frame identity: ${selectedComparisonIdentity}`;
      };
      row.addEventListener('click', select);
      row.addEventListener('keydown', event => {
        if (['Enter', ' '].includes(event.key)) {
          event.preventDefault();
          select();
        }
      });
    });
  }
  async function load() {
    pending?.abort(); pending = new AbortController();
    comparisonSourceController?.abort();
    comparisonSourceToken = null;
    selectedComparisonIdentity = null;
    if (signal.aborted) return;
    result.textContent = 'Loading runtime evidence…';
    const section = form.elements.section.value;
    try {
      if (section === 'graph') {
        const selection = controlRequest();
        rendered = undefined;
        renderedTable = undefined;
        renderedComparison = false;
        const graph = await api(`/api/runtime/graph?${graphParams(selection)}`, pending.signal);
        const downloadSignal = pending.signal;
        rendered = selection;
        const limitations = `<details class="projection-limitations"><summary>Runtime graph limitations (${graph.limitations.length})</summary>${graph.limitations.map(item => `<p class="hint">${escape(item)}</p>`).join('')}</details>`;
        result.innerHTML = `<p>Completeness: ${escape(graph.completeness)}. Cumulative timings overlap; this graph is not exhaustive.</p><p class="hint" id="runtime-graph-bounds">Rendered bounds: ${selection.max_nodes} nodes, ${selection.max_edges} edges, project frames only: ${selection.project_only ? 'yes' : 'no'}.</p>${limitations}${graphView(graph, escape)}<div class="downloads">${['json','markdown','dot','mermaid','svg','png','html'].map(format => `<button class="detail-button" data-runtime-format="${format}">Download ${format.toUpperCase()}</button>`).join('')}</div><p class="runtime-download-status" role="status"></p>`;
        wireGraphDisplay(result, downloadSignal);
        wireCodeBrowsing(graph.nodes.map(node => node.node_id), downloadSignal);
        result.querySelectorAll('[data-projection-node]').forEach(node => {
          const open = () => {
            form.elements.frame.value = node.dataset.projectionNode;
            form.elements.section.value = 'calls';
            syncGraphControls();
            load();
          };
          node.addEventListener('click', open);
          node.addEventListener('keydown', event => { if (['Enter',' '].includes(event.key)) { event.preventDefault(); open(); } });
        });
        result.querySelectorAll('[data-runtime-format]').forEach(button => button.addEventListener('click', async () => {
          const status = result.querySelector('.runtime-download-status');
          if (!sameRequest(rendered, controlRequest())) { status.textContent = DRIFT_MESSAGE; return; }
          const exact = graphParams(rendered);
          try {
            const response = await request(`/api/runtime/download?format=${button.dataset.runtimeFormat}&${exact}`, downloadSignal);
            const blob = await response.blob(); downloadSignal.throwIfAborted();
            const url = URL.createObjectURL(blob), link = document.createElement('a');
            link.href = url; link.download = `runtime-calls.${button.dataset.runtimeFormat}`; link.click();
            setTimeout(() => URL.revokeObjectURL(url), 1000);
          } catch (error) { if (error.name !== 'AbortError') status.textContent = error.message; }
        }));
        syncGraphDownloads();
        return;
      }
      rendered = undefined;
      renderedTable = undefined;
      renderedComparison = false;
      const comparison = section === 'comparison';
      const tableSelection = controlTableRequest();
      const params = comparison
        ? new URLSearchParams({section, frame: form.elements.frame.value})
        : tableParams(tableSelection);
      const value = await api(comparison ? '/api/runtime/comparison' : `/api/runtime/query?${params}`, pending.signal);
      const downloadSignal = pending.signal;
      if (comparison) renderedComparison = true;
      else renderedTable = tableRequest(tableSelection);
      const projection = value.projection;
      const records = comparison ? value.report.records : projection.records;
      const labels = new Map((value.labels || []).map(item => [item.frame_id, item.label]));
      const cell = value => value === null ? 'unavailable' : labels.get(value) || (typeof value === 'object' ? JSON.stringify(value) : String(value));
      const columns = records.length ? Object.keys(records[0]) : [];
      const downloads = comparison
        ? `<div class="downloads"><button type="button" class="detail-button" data-runtime-comparison-baseline-source>Baseline source</button><button type="button" class="detail-button" data-runtime-comparison-candidate-source>Candidate source</button>${TABLE_FORMATS.map(format => `<button class="detail-button" data-runtime-comparison-format="${format}">Download ${format.toUpperCase()}</button>`).join('')}</div><section class="runtime-comparison-source-preview source-preview" aria-live="polite">Select a comparison row, then preview baseline or candidate source.</section><p class="runtime-comparison-download-status" role="status"></p>`
        : `<div class="downloads">${TABLE_FORMATS.map(format => `<button class="detail-button" data-runtime-table-format="${format}">Download ${format.toUpperCase()}</button>`).join('')}</div><p class="runtime-table-download-status" role="status"></p>`;
      const identityIndex = columns.indexOf('frame_identity');
      result.innerHTML = `<p>${records.length} records; ${comparison ? value.omitted : projection.omitted_count} omitted by the view bound.</p>${comparison ? `<p>Diagnostic comparison only; claim eligible: ${value.report.claim_eligible}.</p>${value.report.decisions.map(item => `<p>${escape(item.field)}: ${escape(item.outcome)} — ${escape(item.reason)}</p>`).join('')}` : `<p>Source completeness: ${escape(projection.source_completeness)}</p>`}<div class="table-wrap"><table><thead><tr>${columns.map(key => `<th>${escape(key.replaceAll('_',' '))}</th>`).join('')}</tr></thead><tbody>${records.map(row => `<tr tabindex="0" role="row" data-comparison-row="${comparison && identityIndex >= 0 ? escape(row[columns[identityIndex]]) : ''}" aria-selected="false">${columns.map(key => `<td>${labels.has(row[key]) ? `<button class="detail-button" data-frame="${escape(row[key])}">${escape(cell(row[key]))}</button>` : escape(cell(row[key]))}</td>`).join('')}</tr>`).join('')}</tbody></table></div>${records.length ? '' : '<p>No retained records in this section.</p>'}${downloads}`;
      if (comparison) {
        wireComparisonSourceControls();
      } else {
        wireCodeBrowsing([...labels.keys()], downloadSignal);
      }
      result.querySelectorAll('[data-frame]').forEach(button => button.addEventListener('click', () => {
        form.elements.frame.value = button.dataset.frame;
        form.elements.section.value = 'calls';
        syncGraphControls();
        load();
      }));
      result.querySelectorAll('[data-runtime-table-format]').forEach(button => button.addEventListener('click', async () => {
        const status = result.querySelector('.runtime-table-download-status');
        if (!sameTableRequest(renderedTable, controlTableRequest())) {
          status.textContent = TABLE_DRIFT_MESSAGE;
          return;
        }
        const exact = tableParams(renderedTable);
        try {
          const response = await request(
            `/api/runtime/table-download?format=${button.dataset.runtimeTableFormat}&${exact}`,
            downloadSignal,
          );
          const blob = await response.blob(); downloadSignal.throwIfAborted();
          const url = URL.createObjectURL(blob), link = document.createElement('a');
          link.href = url;
          link.download = `runtime-${renderedTable.section}.${button.dataset.runtimeTableFormat}`;
          link.click();
          setTimeout(() => URL.revokeObjectURL(url), 1000);
        } catch (error) { if (error.name !== 'AbortError') status.textContent = error.message; }
      }));
      result.querySelectorAll('[data-runtime-comparison-format]').forEach(button => button.addEventListener('click', async () => {
        const status = result.querySelector('.runtime-comparison-download-status');
        if (form.elements.section.value !== 'comparison' || !renderedComparison) {
          status.textContent = COMPARISON_DRIFT_MESSAGE;
          return;
        }
        try {
          const response = await request(
            `/api/runtime/comparison-download?format=${button.dataset.runtimeComparisonFormat}`,
            downloadSignal,
          );
          const blob = await response.blob(); downloadSignal.throwIfAborted();
          const url = URL.createObjectURL(blob), link = document.createElement('a');
          link.href = url;
          link.download = `runtime-comparison.${button.dataset.runtimeComparisonFormat}`;
          link.click();
          setTimeout(() => URL.revokeObjectURL(url), 1000);
        } catch (error) { if (error.name !== 'AbortError') status.textContent = error.message; }
      }));
      syncTableDownloads();
      syncComparisonDownloads();
    } catch (error) { if (error.name !== 'AbortError') result.textContent = `Runtime view unavailable: ${error.message}`; }
  }
  form.addEventListener('submit', event => { event.preventDefault(); load(); });
  await load();
}
