import {heatmapView} from './heatmaps.js';
import {disclosure, element} from '/localops/index.js';

// Domain projections retain backend ordering, filtering and completeness.
const scalar = cell => cell.string_value ?? cell.integer_value ?? cell.number_value ?? cell.boolean_value ?? '—';

function table(headers, rows, escape) {
  return `<div class="table-wrap"><table><thead><tr>${headers.map(value => `<th>${escape(value)}</th>`).join('')}</tr></thead><tbody>${rows.map(row => `<tr>${row.map(value => `<td>${escape(value)}</td>`).join('')}</tr>`).join('')}</tbody></table></div>`;
}

function candidateWorklistTable(projection, escape) {
  const headers = [...projection.columns.map(column => column.label), 'Review'];
  const rows = projection.rows.map(row => {
    const candidateId = typeof row.row_id === 'string' && row.row_id.trim() === row.row_id && row.row_id
      ? row.row_id : null;
    const action = candidateId
      ? `<button class="detail-button" type="button" data-candidate-review-id="${escape(candidateId)}">Inspect candidate</button>`
      : '<span class="hint">Unavailable</span>';
    return `<tr>${row.cells.map(scalar).map(value => `<td>${escape(value)}</td>`).join('')}<td>${action}</td></tr>`;
  }).join('');
  return `<div class="table-wrap"><table><thead><tr>${headers.map(value => `<th>${escape(value)}</th>`).join('')}</tr></thead><tbody>${rows}</tbody></table></div>`;
}

// Display-only sizing: no analytical request, selection or export identity changes.
export function wireGraphDisplay(container, signal) {
  for (const viewport of container.querySelectorAll('.graph-wrap')) {
    if (viewport.dataset.displayReady || signal?.aborted) continue;
    const svg = viewport.querySelector('svg');
    const box = svg?.viewBox.baseVal;
    if (!box || box.width <= 0 || box.height <= 0) continue;
    const width = box.width, height = box.height;
    viewport.dataset.displayReady = 'true';
    viewport.tabIndex = 0;
    viewport.setAttribute('role', 'region');
    viewport.setAttribute('aria-label', `Graph viewport: ${svg.getAttribute('aria-label') || 'saved graph'}. Scroll to explore.`);
    const controls = document.createElement('div');
    controls.className = 'graph-display-controls';
    controls.setAttribute('role', 'group');
    controls.setAttribute('aria-label', 'Graph display');
    const status = document.createElement('span');
    status.className = 'graph-display-status';
    status.setAttribute('role', 'status');
    status.setAttribute('aria-live', 'polite');
    let scale = 1;
    const buttons = new Map();
    function resize(value, overview = false) {
      if (signal?.aborted || !viewport.isConnected) return;
      const centerX = (viewport.scrollLeft + viewport.clientWidth / 2) / scale;
      const centerY = (viewport.scrollTop + viewport.clientHeight / 2) / scale;
      scale = value;
      svg.style.width = `${width * scale}px`;
      svg.style.height = `${height * scale}px`;
      viewport.scrollLeft = overview ? 0 : centerX * scale - viewport.clientWidth / 2;
      viewport.scrollTop = overview ? 0 : centerY * scale - viewport.clientHeight / 2;
      status.textContent = `${Math.round(scale * 100)}%${overview ? ' · overview may shrink labels' : ' · scroll to explore'}`;
      buttons.get('Zoom out').disabled = scale <= 0.5;
      buttons.get('Zoom in').disabled = scale >= 3;
    }
    const actions = [
      ['Zoom out', () => resize(Math.max(0.5, scale - 0.25))],
      ['Zoom in', () => resize(Math.min(3, scale + 0.25))],
      ['Fit overview', () => resize(Math.max(0.01, Math.min(1, viewport.clientWidth / width, viewport.clientHeight / height)), true)],
      ['Actual size', () => resize(1)],
    ];
    for (const [label, action] of actions) {
      const button = document.createElement('button');
      button.type = 'button';
      button.className = 'detail-button';
      button.textContent = label;
      button.addEventListener('click', action);
      buttons.set(label, button);
      controls.append(button);
    }
    controls.append(status);
    viewport.before(controls);
    viewport.addEventListener('focusin', event => {
      if (!signal?.aborted && event.target !== viewport) {
        event.target.closest('.graph-node')?.scrollIntoView({block: 'nearest', inline: 'nearest'});
      }
    });
    resize(1);
  }
}

export function graphView(projection, escape) {
  const positions = new Map(projection.nodes.map((node, index) => {
    const angle = index * 2 * Math.PI / Math.max(1, projection.nodes.length);
    return [node.node_id, {x: 450 + 280 * Math.cos(angle), y: 330 + 250 * Math.sin(angle)}];
  }));
  const edges = projection.edges.map(edge => {
    const source = positions.get(edge.source_id), target = positions.get(edge.target_id);
    const title = `<title>${escape(edge.source_id)} — ${escape(edge.target_id)}: ${escape(edge.label || edge.kind)}</title>`;
    const marker = projection.direction === 'directed' ? 'marker-end="url(#projection-arrow)"' : '';
    if (edge.source_id === edge.target_id) return `<path class="graph-edge" fill="none" d="M ${source.x} ${source.y - 7} C ${source.x + 48} ${source.y - 45}, ${source.x + 48} ${source.y + 45}, ${source.x} ${source.y + 7}" ${marker}>${title}</path>`;
    const length = Math.hypot(target.x - source.x, target.y - source.y);
    const dx = (target.x - source.x) / length, dy = (target.y - source.y) / length;
    const startInset = Math.min(8, length / 3), endInset = Math.min(10, length / 3);
    return `<line class="graph-edge" x1="${source.x + dx * startInset}" y1="${source.y + dy * startInset}" x2="${target.x - dx * endInset}" y2="${target.y - dy * endInset}" ${marker}>${title}</line>`;
  }).join('');
  const nodes = projection.nodes.map(node => {
    const point = positions.get(node.node_id);
    const label = node.label.slice(-28);
    return `<g class="graph-node" tabindex="0" role="button" aria-label="Select ${escape(node.label)}" data-projection-node="${escape(node.node_id)}"><rect x="${point.x - 10}" y="${point.y - 16}" width="${Math.max(28, label.length * 8.5 + 24)}" height="32" fill="transparent"/><circle cx="${point.x}" cy="${point.y}" r="7"/><text x="${point.x + 10}" y="${point.y}">${escape(label)}</text><title>${escape(node.label)}</title></g>`;
  }).join('');
  const canvasWidth = Math.ceil(Math.max(900, ...projection.nodes.map(node => positions.get(node.node_id).x + node.label.slice(-28).length * 8.5 + 24)));
  const labels = new Map(projection.nodes.map(node => [node.node_id, node.label]));
  const edgeTable = table(['Source', 'Target', 'Relationship', 'Confidence', 'Resolution', 'Relationship identity', 'Evidence IDs'], projection.edges.map(edge => [labels.get(edge.source_id), labels.get(edge.target_id), edge.label || edge.kind, edge.confidence ?? 'not recorded', edge.resolution, edge.edge_id, edge.evidence_ids.join(', ')]), escape);
  return `<p>${projection.nodes.length} of ${projection.original_node_count} nodes; ${projection.edges.length} of ${projection.original_edge_count} edges. Omitted: ${projection.omitted_node_count} nodes and ${projection.omitted_edge_count} edges.</p><div class="graph-wrap"><svg viewBox="0 0 ${canvasWidth} 660" role="img" aria-label="${escape(projection.title)}"><defs><marker id="projection-arrow" viewBox="0 0 10 10" refX="10" refY="5" markerWidth="6" markerHeight="6" orient="auto-start-reverse"><path d="M 0 0 L 10 5 L 0 10 z" fill="#8faed3"/></marker></defs>${edges}${nodes}</svg></div><details class="projection-edges"><summary>Recorded relationships (${projection.edges.length})</summary><p class="hint">Distinct recorded relationships can share endpoints and overlap in the drawing. This table retains each relationship separately; an edge does not itself prove a call site.</p>${edgeTable}</details>` + table(['Node', 'Kind', 'Identity'], projection.nodes.map(node => [node.label, node.kind, node.node_id]), escape);
}

function hierarchyNodeLabel(node, escape) {
  const kind = `<span class="badge">${escape(node.kind)}</span>`;
  const label = `<span class="hierarchy-label">${escape(node.label)}</span>`;
  return `${label}${kind}`;
}

function hierarchyModuleButton(node, escape, parent = false) {
  const content = parent
    ? `<span class="hierarchy-action">Open module evidence</span><code>${escape(node.node_id)}</code><span class="badge">${escape(node.kind)}</span>`
    : hierarchyNodeLabel(node, escape);
  const parentClass = parent ? ' hierarchy-parent-action' : '';
  return `<button class="hierarchy-node hierarchy-module${parentClass}" type="button" aria-pressed="false" data-projection-node="${escape(node.node_id)}" title="Open saved evidence for ${escape(node.node_id)}">${content}</button>`;
}

export function hierarchyView(projection, escape) {
  const children = new Map([[null, []]]);
  for (const node of projection.nodes) {
    if (!children.has(node.parent_id)) children.set(node.parent_id, []);
    children.get(node.parent_id).push(node);
    if (!children.has(node.node_id)) children.set(node.node_id, []);
  }
  const roots = children.get(null);
  if (roots.length === 0) {
    return '<p class="empty">No package or module nodes were retained in this bounded view.</p>';
  }
  const html = ['<div class="hierarchy-wrap"><ul class="hierarchy-tree" aria-label="Package and module hierarchy">'];
  const work = roots.slice().reverse().map(node => ({node, closing: false, root: true}));
  while (work.length > 0) {
    const item = work.pop();
    const nodeChildren = children.get(item.node.node_id);
    const isGrouping = item.node.kind === 'package' || nodeChildren.length > 0;
    if (item.closing) {
      html.push('</ul></details></li>');
      continue;
    }
    if (!isGrouping) {
      html.push(`<li>${hierarchyModuleButton(item.node, escape)}</li>`);
      continue;
    }
    const open = item.root ? ' open' : '';
    html.push(`<li><details class="hierarchy-branch"${open}><summary>${hierarchyNodeLabel(item.node, escape)}</summary>`);
    if (item.node.kind === 'module') {
      html.push(`<div class="hierarchy-parent-module">${hierarchyModuleButton(item.node, escape, true)}</div>`);
    }
    html.push('<ul>');
    work.push({node: item.node, closing: true, root: item.root});
    for (let index = nodeChildren.length - 1; index >= 0; index -= 1) {
      work.push({node: nodeChildren[index], closing: false, root: false});
    }
  }
  html.push('</ul></div>');
  return html.join('');
}

export async function projectionExplorer(content, api, request, escape, signal, openEvidence, options = {}) {
  const catalog = await api('/api/projections', signal);
  const handoffRequest = options.initialRequest && typeof options.initialRequest === 'object'
    ? options.initialRequest : null;
  const initialName = handoffRequest && catalog.names.includes(handoffRequest.name)
    ? handoffRequest.name
    : (options.initialName && catalog.names.includes(options.initialName) ? options.initialName : 'modules');
  const selectedIds = handoffRequest && Array.isArray(handoffRequest.selected_ids)
    ? handoffRequest.selected_ids.filter(value => typeof value === 'string') : [];
  // Restored filters stay backend-vocabulary bound: malformed or unknown saved values block
  // the view with an explicit message instead of silently broadening it.
  const restoredFieldLabels = {
    module_prefixes: 'module prefixes',
    finding_categories: 'finding categories',
    finding_severities: 'finding severities',
    finding_rule_ids: 'finding rule IDs',
    languages: 'language scope',
  };
  const restoration = new Map();
  function rejectRestoration(field, reason) {
    restoration.set(field, reason);
  }
  const restoredValues = (field, vocabulary = null) => {
    const values = handoffRequest ? handoffRequest[field] : undefined;
    if (values === undefined || values === null) return [];
    if (!Array.isArray(values) || values.some(value => typeof value !== 'string')) {
      rejectRestoration(field, `the saved ${restoredFieldLabels[field]} are not a list of recorded text values`);
      return [];
    }
    const unknown = vocabulary === null ? [] : values.filter(value => !vocabulary.includes(value));
    if (unknown.length > 0) {
      rejectRestoration(field, `the saved ${restoredFieldLabels[field]} ${unknown.join(', ')} are not recorded in this report vocabulary`);
      return [];
    }
    return values;
  };
  const selectedLanguages = restoredValues('languages');
  if (initialName === 'subject-findings' && selectedLanguages.length > 0) {
    rejectRestoration('languages', 'subject findings are aggregated across all recorded languages, so the saved language scope cannot be applied');
  }
  const selectionInputs = selectedIds.length
    ? selectedIds.map(value => `<input name="select" aria-label="Exact selection ID" value="${escape(value)}" placeholder="Optional module, contract, clone or subject ID">`).join('')
    : '<input name="select" aria-label="Exact selection ID" placeholder="Optional module, contract, clone or subject ID">';
  const candidateKinds = catalog.candidate_kinds.map(value => `<option value="${escape(value)}">${escape(value.replaceAll('-', ' '))}</option>`).join('');
  const candidateDecisions = catalog.candidate_decisions.map(value => `<option value="${escape(value)}">${escape(value.replaceAll('-', ' '))}</option>`).join('');
  const findingOptions = (values, restored) => values.map(value =>
    `<option value="${escape(value)}" ${restored.includes(value) ? 'selected' : ''}>${escape(value.replaceAll('-', ' '))}</option>`).join('');
  const findingCategories = findingOptions(catalog.finding_categories, restoredValues('finding_categories', catalog.finding_categories));
  const findingSeverities = findingOptions(catalog.finding_severities, restoredValues('finding_severities', catalog.finding_severities));
  const findingPrefixes = restoredValues('module_prefixes').join('\n');
  const findingRuleIds = restoredValues('finding_rule_ids').join('\n');
  content.innerHTML = `<form id="projection-controls" class="panel projection-controls investigation-controls">
    <div class="investigation-intro"><h2>Choose saved evidence</h2><p class="hint">Choose an evidence view, optionally focus its scope, then Show evidence. Results and downloads use the saved report only.</p></div>
    <label>Evidence view<select name="name" aria-label="Evidence view">${catalog.names.map(name => `<option value="${escape(name)}" ${name === initialName ? 'selected' : ''}>${escape(name.replaceAll('-', ' '))}</option>`).join('')}</select></label>
    <fieldset class="projection-selection"><legend>Exact selection IDs (optional)</legend><span id="projection-selection-ids">${selectionInputs}</span><button id="add-projection-selection" class="detail-button" type="button">Add selection</button><p id="projection-selection-note" class="hint">Leave blank for the view's default scope. Supported IDs depend on the chosen view; an invalid selection is reported, never silently ignored.</p></fieldset>
    <label>Language<select name="language" multiple size="4"><option value="" ${selectedLanguages.length === 0 ? 'selected' : ''}>All languages</option><option value="javascript" ${selectedLanguages.includes('javascript') ? 'selected' : ''}>javascript</option><option value="python" ${selectedLanguages.includes('python') ? 'selected' : ''}>python</option><option value="ruby" ${selectedLanguages.includes('ruby') ? 'selected' : ''}>ruby</option></select><span class="hint">All languages clears the language scope.</span><span id="projection-language-note" class="hint" hidden>Subject findings are aggregated across all recorded languages, so this view has no language scope and the control is disabled. Reset finding filters also clears a restored language scope.</span></label>
    <label>Records<input name="max_records" type="number" min="1" max="500" value="${escape(handoffRequest?.max_records ?? 100)}"></label>
    <label>Nodes<input name="max_nodes" type="number" min="1" max="100" value="${escape(handoffRequest?.max_nodes ?? 50)}"></label>
    <label>Edges<input name="max_edges" type="number" min="0" max="250" value="${escape(handoffRequest?.max_edges ?? 100)}"></label>
    <label>Neighborhood depth<input name="depth" type="number" min="0" max="5" placeholder="Default"></label>
    <label>Collapse<select name="collapse"><option ${handoffRequest?.collapse === 'none' ? 'selected' : ''}>none</option><option ${handoffRequest?.collapse === 'module' ? 'selected' : ''}>module</option><option ${handoffRequest?.collapse === 'package' ? 'selected' : ''}>package</option></select></label>
    <fieldset id="finding-projection-controls" class="candidate-projection-controls" hidden disabled>
      <legend>Subject finding filters</legend>
      <p class="hint">Filters apply before subject totals: finding, severity, category and rule counts are recomputed from the retained findings. Values inside one filter match by OR; separate filters must all match. Module prefixes match whole dotted segments and cannot be combined with exact selection IDs.</p>
      <label>Logical module prefixes<textarea name="module_prefixes" rows="3" placeholder="package.combine_a">${escape(findingPrefixes)}</textarea><span class="hint">One dotted module prefix per line.</span></label>
      <label>Finding categories<select name="finding_categories" multiple size="6">${findingCategories}</select><span class="hint">Use Command or Control to choose more than one category.</span></label>
      <label>Finding severities<select name="finding_severities" multiple size="3">${findingSeverities}</select><span class="hint">Use Command or Control to choose more than one severity.</span></label>
      <label>Finding rule IDs<textarea name="finding_rule_ids" rows="3" placeholder="duplication.normalized-ast">${escape(findingRuleIds)}</textarea><span class="hint">One recorded rule ID per line.</span></label>
      <button id="reset-finding-filters" class="detail-button" type="button">Reset finding filters</button>
    </fieldset>
    <fieldset id="candidate-projection-controls" class="candidate-projection-controls" hidden disabled>
      <legend>Candidate worklist policy</legend>
      <p class="hint">Module patterns use full-name matching (<code>fnmatchcase</code>); exclusions win. Production-only uses only discovered test modules recorded in this report, and discovery may be incomplete.</p>
      <label>Include module patterns<textarea name="candidate_module_patterns" rows="3" placeholder="package.*&#10;services.api"></textarea><span class="hint">One full module pattern per line.</span></label>
      <label>Exclude module patterns<textarea name="candidate_exclude_patterns" rows="3" placeholder="package.tests.*"></textarea><span class="hint">One full module pattern per line.</span></label>
      <label>Candidate kinds<select name="candidate_kinds" multiple size="4">${candidateKinds}</select><span class="hint">Use Command or Control to choose more than one kind.</span></label>
      <label>Candidate states<select name="candidate_states" multiple size="4"><option value="observed">observed</option><option value="blocked">blocked</option><option value="reviewable-incomplete">reviewable-incomplete</option><option value="ready-for-maintainer-review">ready-for-maintainer-review</option></select><span class="hint">Use Command or Control to choose more than one state.</span></label>
      <section class="candidate-decision-selection" aria-labelledby="candidate-decision-heading">
        <h3 id="candidate-decision-heading">Maintainer decisions</h3>
        <p class="hint">Detached by default. Attach the selected owned workspace ledger as a read-only, report-bound snapshot.</p>
        <div class="candidate-decision-actions">
          <button id="attach-candidate-decisions" class="detail-button" type="button">Use workspace decisions</button>
          <button id="clear-candidate-decisions" class="detail-button" type="button" disabled>Clear decision selection</button>
        </div>
        <p id="candidate-decision-status" class="hint mono" role="status">Workspace decisions are not attached.</p>
        <label>Decision states<select name="candidate_decisions" multiple size="5" disabled>${candidateDecisions}</select><span class="hint">Decision filters become available only after a snapshot is attached.</span></label>
      </section>
      <label>Minimum shared dimensions<input name="min_shared_dimensions" type="number" min="0" max="32" value="0"></label>
      <label class="candidate-check"><input name="cross_module_only" type="checkbox">Cross-module candidates only</label>
      <label class="candidate-check"><input name="production_only" type="checkbox">Production subjects only</label>
      <label class="candidate-check"><input name="prefer_existing_owner" type="checkbox">Prefer an existing recorded owner</label>
      <button id="reset-candidate-filters" class="detail-button" type="button">Reset candidate filters</button>
    </fieldset>
    <div class="investigation-submit"><button class="detail-button" type="submit">Show evidence</button></div>${options.onReturnToCandidates ? '<p class="hint candidate-review-context">Candidate inventory search and participant facets are not transferred: this worklist has explicit saved-report policy controls.</p><button id="return-candidate-inventory" class="detail-button" type="button">Return to candidate inventory</button>' : ''}</form><div id="projection-result" aria-live="polite"></div>`;
  const form = content.querySelector('form'), result = content.querySelector('#projection-result');
  form.elements.language.closest('label').after(form.querySelector('.projection-selection'));
  const bounds = element('div', {className: 'investigation-bounds'});
  for (const name of ['max_records', 'max_nodes', 'max_edges', 'depth', 'collapse']) {
    bounds.append(form.elements[name].closest('label'));
  }
  bounds.append(element('p', {className: 'hint', text: 'Record bounds apply to tables and matrices; node/edge bounds apply to diagrams. Depth and collapse apply only where the chosen view supports them. Results disclose omissions and partial evidence; these settings do not recollect source.'}));
  const boundsSummary = element('p', {className: 'hint', id: 'projection-bounds-summary'});
  const advanced = disclosure({title: 'Advanced settings', content: bounds});
  form.addEventListener('invalid', event => {
    if (advanced.contains(event.target)) advanced.open = true;
  }, true);
  form.querySelector('.investigation-submit').before(boundsSummary, advanced);
  const summarizeBounds = () => {
    const focus = form.elements.depth.disabled
      ? 'Depth/collapse: not used by candidate worklist.'
      : `Depth: ${form.elements.depth.value || 'default'}; collapse: ${form.elements.collapse.value}.`;
    boundsSummary.textContent = `Bounds: ${form.elements.max_records.value} records; ${form.elements.max_nodes.value} nodes / ${form.elements.max_edges.value} edges. ${focus}`;
  };
  summarizeBounds();
  const name = form.elements.name, candidateControls = content.querySelector('#candidate-projection-controls');
  const findingControls = content.querySelector('#finding-projection-controls');
  const languageNote = content.querySelector('#projection-language-note');
  const decisionStatus = content.querySelector('#candidate-decision-status');
  const attachDecisions = content.querySelector('#attach-candidate-decisions');
  const clearDecisions = content.querySelector('#clear-candidate-decisions');
  let pending, nodePending, decisionPending, decisionSnapshot;
  signal.addEventListener('abort', () => { pending?.abort(); nodePending?.abort(); decisionPending?.abort(); }, {once: true});
  content.querySelector('#return-candidate-inventory')?.addEventListener('click', () => {
    pending?.abort();
    nodePending?.abort();
    decisionPending?.abort();
    options.onReturnToCandidates();
  });
  content.querySelector('#add-projection-selection').addEventListener('click', () => {
    const input = document.createElement('input');
    input.name = 'select';
    input.setAttribute('aria-label', 'Exact selection ID');
    input.placeholder = 'Optional module, contract, clone or subject ID';
    content.querySelector('#projection-selection-ids').append(input);
    input.focus();
  });
  form.elements.language.addEventListener('change', () => {
    const options = [...form.elements.language.options];
    const all = options.find(option => option.value === '');
    if (options.some(option => option.value && option.selected)) all.selected = false;
    else all.selected = true;
  });
  function uniqueLines(field) {
    return [...new Set(field.value.split('\n').map(value => value.trim()).filter(Boolean))].sort();
  }
  function candidateBoolean(field) {
    return field.checked ? 'true' : 'false';
  }
  function paramsForForm() {
    const params = new URLSearchParams();
    for (const [key, value] of new FormData(form)) {
      if (!key.startsWith('candidate_') && !key.startsWith('finding_') && key !== 'module_prefixes' && !['cross_module_only', 'production_only', 'min_shared_dimensions', 'prefer_existing_owner'].includes(key) && value) params.append(key, value);
    }
    if (name.value === 'subject-findings') {
      for (const value of uniqueLines(form.elements.module_prefixes)) params.append('module_prefixes', value);
      for (const option of form.elements.finding_categories.selectedOptions) params.append('finding_categories', option.value);
      for (const option of form.elements.finding_severities.selectedOptions) params.append('finding_severities', option.value);
      for (const value of uniqueLines(form.elements.finding_rule_ids)) params.append('finding_rule_ids', value);
    }
    if (name.value !== 'candidate-worklist') return params;
    for (const value of uniqueLines(form.elements.candidate_module_patterns)) params.append('candidate_module_patterns', value);
    for (const value of uniqueLines(form.elements.candidate_exclude_patterns)) params.append('candidate_exclude_patterns', value);
    for (const option of form.elements.candidate_kinds.selectedOptions) params.append('candidate_kinds', option.value);
    for (const option of form.elements.candidate_states.selectedOptions) params.append('candidate_states', option.value);
    if (decisionSnapshot) {
      params.set('decision_ledger_sha256', decisionSnapshot.ledger_sha256);
      params.set('decision_report_sha256', decisionSnapshot.report_sha256);
      for (const option of form.elements.candidate_decisions.selectedOptions) params.append('candidate_decisions', option.value);
    }
    params.set('cross_module_only', candidateBoolean(form.elements.cross_module_only));
    params.set('production_only', candidateBoolean(form.elements.production_only));
    params.set('min_shared_dimensions', form.elements.min_shared_dimensions.value);
    params.set('prefer_existing_owner', candidateBoolean(form.elements.prefer_existing_owner));
    return params;
  }
  function clearFindingFilters() {
    form.elements.module_prefixes.value = '';
    form.elements.finding_rule_ids.value = '';
    for (const option of form.elements.finding_categories.options) option.selected = false;
    for (const option of form.elements.finding_severities.options) option.selected = false;
  }
  function clearLanguageScope() {
    for (const option of form.elements.language.options) option.selected = option.value === '';
  }
  function syncFindingControls(explicitSwitch = false) {
    const active = name.value === 'subject-findings';
    findingControls.hidden = !active;
    findingControls.disabled = !active;
    // A different evidence view has no finding vocabulary: drop inapplicable filters.
    if (!active) {
      clearFindingFilters();
      restoration.clear();
    }
    // This view is never scoped by language, so an explicit switch retires the control.
    // A restored scope is kept visible instead: it must fail rather than be cleared silently.
    if (active && explicitSwitch) {
      clearLanguageScope();
      restoration.delete('languages');
    }
    form.elements.language.disabled = active && !restoration.has('languages');
    languageNote.hidden = !active;
  }
  function showNotice(text) {
    result.replaceChildren();
    const message = document.createElement('p');
    message.className = 'hint';
    message.textContent = text;
    result.append(message);
  }
  function restorationNotice() {
    return `Unable to restore this saved evidence selection: ${[...restoration.values()].join('; ')}. No projection was built and no download is offered. Reset finding filters or correct the highlighted selection, then choose Show evidence.`;
  }
  function noteUserFix(target) {
    const field = target?.name === 'language' ? 'languages' : target?.name;
    if (field === 'languages' && name.value === 'subject-findings'
        && [...form.elements.language.selectedOptions].some(option => option.value !== '')) {
      rejectRestoration('languages', 'subject findings are aggregated across all recorded languages, so the selected language scope cannot be applied');
      return;
    }
    if (field && restoration.delete(field) && !restoration.has('languages')) {
      form.elements.language.disabled = name.value === 'subject-findings';
    }
  }
  function syncCandidateControls() {
    const active = name.value === 'candidate-worklist';
    candidateControls.hidden = !active;
    candidateControls.disabled = !active;
    content.querySelector('#add-projection-selection').disabled = active;
    for (const fieldName of ['select', 'depth', 'collapse']) {
      for (const field of form.querySelectorAll(`[name="${fieldName}"]`)) field.disabled = active;
    }
    content.querySelector('#projection-selection-note').textContent = active
      ? 'Exact IDs are not used by the candidate worklist. Use its candidate policy filters below.'
      : "Leave blank for the view's default scope. Supported IDs depend on the chosen view; an invalid selection is reported, never silently ignored.";
    summarizeBounds();
    if (!active) clearDecisionSelection('Workspace decisions are not attached.');
  }
  function invalidate(cancelDecisionFetch = true) {
    summarizeBounds();
    pending?.abort(); nodePending?.abort();
    if (cancelDecisionFetch && decisionPending) {
      decisionPending.abort();
      decisionPending = undefined;
      attachDecisions.disabled = false;
      decisionStatus.textContent = 'Decision selection was canceled because the options changed. Select it again.';
    }
    showNotice(restoration.size > 0
      ? restorationNotice()
      : 'Options changed. Show evidence to build a current saved-report view.');
  }
  function clearDecisionSelection(message = 'Workspace decisions are not attached.') {
    decisionPending?.abort();
    decisionPending = undefined;
    decisionSnapshot = undefined;
    for (const option of form.elements.candidate_decisions.options) option.selected = false;
    form.elements.candidate_decisions.disabled = true;
    clearDecisions.disabled = true;
    attachDecisions.disabled = false;
    decisionStatus.textContent = message;
  }
  async function selectDecisionSnapshot() {
    clearDecisionSelection('Loading the selected workspace decision snapshot…');
    invalidate(false);
    decisionPending = new AbortController();
    const current = decisionPending;
    attachDecisions.disabled = true;
    try {
      const snapshot = await api('/api/candidate-ledger', current.signal);
      current.signal.throwIfAborted();
      if (decisionPending !== current) return;
      decisionPending = undefined;
      attachDecisions.disabled = false;
      if (!snapshot.available) {
        clearDecisionSelection(snapshot.unavailable_reason);
        return;
      }
      decisionSnapshot = snapshot;
      form.elements.candidate_decisions.disabled = false;
      clearDecisions.disabled = false;
      decisionStatus.textContent = `${snapshot.record_count} decision records · ledger ${snapshot.ledger_sha256} · report ${snapshot.report_sha256}`;
      invalidate(false);
    } catch (error) {
      if (error.name === 'AbortError') return;
      if (decisionPending === current) {
        decisionPending = undefined;
        clearDecisionSelection(`Unable to attach workspace decisions: ${error.message}`);
      }
    }
  }
  function rejectStaleDecision(error) {
    if (!decisionSnapshot || !/decision snapshot changed|selected report/i.test(error.message)) return false;
    clearDecisionSelection('The workspace decisions changed or no longer match this report. Select Use workspace decisions again.');
    return true;
  }
  function resetCandidateFilters() {
    form.elements.candidate_module_patterns.value = '';
    form.elements.candidate_exclude_patterns.value = '';
    for (const option of form.elements.candidate_kinds.options) option.selected = false;
    for (const option of form.elements.candidate_states.options) option.selected = false;
    for (const option of form.elements.candidate_decisions.options) option.selected = false;
    form.elements.min_shared_dimensions.value = '0';
    form.elements.cross_module_only.checked = false;
    form.elements.production_only.checked = false;
    form.elements.prefer_existing_owner.checked = false;
    invalidate();
  }
  function resetFindingFilters() {
    clearFindingFilters();
    // This view has no language scope, so an explicit reset retires a restored one too.
    if (name.value === 'subject-findings') clearLanguageScope();
    restoration.clear();
    form.elements.language.disabled = name.value === 'subject-findings';
    invalidate();
  }
  syncFindingControls();
  syncCandidateControls();
  form.addEventListener('input', event => {
    noteUserFix(event.target);
    invalidate();
  });
  form.addEventListener('change', event => {
    noteUserFix(event.target);
    if (event.target === name) {
      syncFindingControls(true);
      syncCandidateControls();
    }
    invalidate();
  });
  content.querySelector('#reset-finding-filters').addEventListener('click', resetFindingFilters);
  content.querySelector('#reset-candidate-filters').addEventListener('click', resetCandidateFilters);
  attachDecisions.addEventListener('click', () => { selectDecisionSnapshot(); });
  clearDecisions.addEventListener('click', () => {
    clearDecisionSelection();
    invalidate(false);
  });
  async function load() {
    pending?.abort(); nodePending?.abort(); pending = new AbortController();
    if (signal.aborted) return;
    // An unrestorable saved selection issues no request: a broadened view would misreport it.
    if (restoration.size > 0) {
      showNotice(restorationNotice());
      return;
    }
    const currentSignal = pending.signal;
    const params = paramsForForm();
    result.textContent = 'Loading projection…';
    try {
      const projection = await api(`/api/projection?${params}`, currentSignal);
      let body;
      if (projection.projection_kind === 'table') {
        const originalTable = params.get('name') === 'candidate-worklist'
          ? candidateWorklistTable(projection, escape)
          : table(projection.columns.map(column => column.label), projection.rows.map(row => row.cells.map(scalar)), escape);
        const heatmap = heatmapView(params.get('name'), projection, escape);
        body = heatmap === null ? originalTable : `<div class="projection-view-switch" role="group" aria-label="Projection display">
          <button class="detail-button active" type="button" data-projection-display="heatmap" aria-pressed="true">Heatmap</button>
          <button class="detail-button" type="button" data-projection-display="table" aria-pressed="false">Table</button>
        </div><div data-projection-panel="heatmap">${heatmap}</div><div data-projection-panel="table" hidden>${originalTable}</div>`;
      } else if (projection.projection_kind === 'graph') {
        body = graphView(projection, escape);
      } else if (projection.projection_kind === 'hierarchy') {
        body = hierarchyView(projection, escape);
      } else if (projection.projection_kind === 'matrix') {
        const rows = new Map(projection.rows.map(item => [item.item_id, item.label]));
        const columns = new Map(projection.columns.map(item => [item.item_id, item.label]));
        body = table(['Row', 'Column', 'Metrics', 'Evidence IDs'], projection.cells.map(cell => [rows.get(cell.row_id), columns.get(cell.column_id), cell.metrics.map(metric => `${metric.name}: ${scalar(metric.value)}`).join('; '), cell.evidence_ids.join(', ')]), escape);
      } else {
        const response = await request(`/api/download?${params}&format=markdown`, currentSignal);
        const markdown = await response.text(); currentSignal.throwIfAborted();
        body = `<pre class="report-text">${escape(markdown)}</pre>`;
      }
      const formats = ['json', 'markdown', 'html'];
      if (['table', 'matrix', 'comparison'].includes(projection.projection_kind)) formats.push('tsv');
      if (['graph', 'hierarchy'].includes(projection.projection_kind)) formats.push('dot', 'mermaid', 'svg', 'png');
      const limitations = projection.limitations.length === 0 ? '' : `<details class="projection-limitations"><summary>Evidence limitations (${projection.limitations.length})</summary>${projection.limitations.map(value => `<p class="hint">${escape(value)}</p>`).join('')}</details>`;
      result.innerHTML = `<h2>${escape(projection.title)}</h2><p>Completeness: <strong>${escape(projection.completeness)}</strong> · ${projection.record_count} retained records · ${projection.truncated ? 'Bounded partial view' : 'No projection truncation'}</p>${limitations}<div class="downloads">${formats.map(format => `<button class="detail-button" data-format="${format}">Download ${format.toUpperCase()}</button>`).join('')}</div>${body}<p id="projection-node-status" class="hint" role="status"></p><p id="download-status" role="status"></p>`;
      wireGraphDisplay(result, currentSignal);
      result.querySelectorAll('[data-projection-display]').forEach(button => button.addEventListener('click', () => {
        const display = button.dataset.projectionDisplay;
        result.querySelectorAll('[data-projection-display]').forEach(item => {
          const selected = item.dataset.projectionDisplay === display;
          item.classList.toggle('active', selected);
          item.setAttribute('aria-pressed', String(selected));
        });
        result.querySelectorAll('[data-projection-panel]').forEach(panel => {
          panel.hidden = panel.dataset.projectionPanel !== display;
        });
      }));
      result.querySelectorAll('[data-format]').forEach(button => button.addEventListener('click', async () => {
        try {
          const response = await request(`/api/download?${params}&format=${button.dataset.format}`, currentSignal);
          const blob = await response.blob(); currentSignal.throwIfAborted();
          const url = URL.createObjectURL(blob), anchor = document.createElement('a');
          anchor.href = url; anchor.download = `${params.get('name')}.${button.dataset.format}`; anchor.click();
          setTimeout(() => URL.revokeObjectURL(url), 1000);
          result.querySelector('#download-status').textContent = `Downloaded ${button.dataset.format.toUpperCase()} with ${projection.completeness} evidence.`;
        } catch (error) {
          if (error.name === 'AbortError') return;
          result.querySelector('#download-status').textContent = rejectStaleDecision(error)
            ? 'The selected decision snapshot is stale. Refresh or select the workspace decisions again.'
            : error.message;
        }
      }));
      const sourceInput = projection.inputs.find(item => item.role === 'source');
      const decisionLedgerSha256 = params.get('decision_ledger_sha256');
      result.querySelectorAll('[data-candidate-review-id]').forEach(button => {
        button.addEventListener('click', async () => {
          const candidateId = button.dataset.candidateReviewId;
          if (
            !candidateId
            || signal.aborted
            || currentSignal.aborted
            || !button.isConnected
            || !sourceInput?.report_sha256
          ) return;
          const binding = {
            report_sha256: sourceInput.report_sha256,
            ...(decisionLedgerSha256 ? {decision_ledger_sha256: decisionLedgerSha256} : {}),
            signal: currentSignal,
          };
          await openEvidence('refactor-candidate', candidateId, '', 0, binding);
        });
      });
      result.querySelectorAll('[data-projection-node]').forEach(node => {
        const select = async () => {
          result.querySelectorAll('[data-projection-node]').forEach(item => item.setAttribute('aria-pressed', String(item === node)));
          nodePending?.abort(); nodePending = new AbortController();
          const nodeSignal = nodePending.signal;
          const nodeId = node.dataset.projectionNode;
          const status = result.querySelector('#projection-node-status');
          status.textContent = `Resolving ${nodeId}…`;
          const nodeParams = new URLSearchParams(params);
          nodeParams.set('projection_id', projection.projection_id);
          nodeParams.set('node', nodeId);
          try {
            const resolved = await api(`/api/projection-node?${nodeParams}`, nodeSignal);
            nodeSignal.throwIfAborted();
            if (!node.isConnected || resolved.projection_id !== projection.projection_id || resolved.node_id !== nodeId) return;
            if (resolved.navigation_identity !== null) {
              status.textContent = `Opening saved source for ${resolved.label}.`;
              await openEvidence('navigation', resolved.navigation_identity);
            } else {
              status.textContent = resolved.unavailable_reason || `Source navigation is unavailable for ${resolved.label}.`;
            }
          } catch (error) {
            if (error.name !== 'AbortError' && node.isConnected) status.textContent = `Unable to resolve this node: ${error.message}`;
          }
        };
        node.addEventListener('click', () => { select(); });
        node.addEventListener('keydown', event => { if (event.key === 'Enter' || event.key === ' ') { event.preventDefault(); select(); } });
      });
    } catch (error) {
      if (error.name === 'AbortError') return;
      if (rejectStaleDecision(error)) {
        result.textContent = 'The selected decision snapshot is stale. Refresh or select the workspace decisions again.';
        return;
      }
      result.textContent = `Unable to build this view: ${error.message}`;
    }
  }
  form.addEventListener('submit', event => { event.preventDefault(); load(); });
  await load();
}
