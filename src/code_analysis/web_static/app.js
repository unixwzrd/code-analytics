import {finalizeAcceptedNavigation, reportTable} from './localops_adapter.js';
import {evidenceViewer} from './evidence.js';
import {projectionExplorer, wireGraphDisplay} from './projections.js';
import {dependencyExplorer} from './dependencies.js';
import {runtimeView} from './runtime.js';
import {collectionView} from './collection.js';
import {codeBrowser, codeBrowserSelectionMetadata} from './code_browser.js';
import {DraftSession} from './code_drafts.js';
import {installSessionControl} from './session.js';
import {wireComparisonSources, wireComparisonDownloads} from './comparison.js';
import {installUserHelp} from './help.js';
import {disclosure, element} from '/localops/index.js';
import {mountWorkflowNavigation, revealWorkflowView, workflowDestination, renderWorkflowGuidance,
  showWorkflowUnavailable, workflowOverview} from './workflow.js';

const token = new URLSearchParams(location.search).get("token") || "";
const content = document.querySelector("#content");
const title = document.querySelector("#title");
const description = document.querySelector("#view-description");
const filter = document.querySelector("#filter");
const search = filter.closest(".search");
let view = "overview";
let bootstrap;
let ownedBootstrap;
let offset = 0;
const pageSize = 50;
let renderController;
let refreshRunCatalog = false;
let runSelection;
let profileSelection;
let findingSelection = {severity: '', category: '', confidence: ''};
let comparisonSelection = {change: '', kind: ''};
let facetSelection = {language: '', module_prefix: '', owner: ''};
let candidateReviewSession;
let candidateReturnFocusSession;
let projectionHandoffRequest;
let runtimeHandoffRequest;
let runtimeSession = false;
let workflow;
let workflowFailure;
const collectionSession = {};
const codeBrowserSession = {};
const draftSession = new DraftSession();
let sessionClosed = false;
installSessionControl(token, draftSession, () => {
  sessionClosed = true;
  renderController?.abort();
  handoffController?.abort();
  clearTimeout(filterTimer);
});
// One investigation, retained only in this tab's memory. History contains a
// reference, never filters, evidence bodies, source bytes, or draft contents.
let investigationReturn;
let investigationSequence = 0;
let evidenceFocus;
let findingSourceController;
let findingSourceContext;
let refusedInvestigationBack = false;

function investigationBinding() {
  return JSON.stringify({reports: (bootstrap?.reports || []).map(report => report.report_sha256),
    run: [runSelection?.primary || '', runSelection?.comparison || ''],
    profile: [profileSelection?.primary || '', profileSelection?.comparison || ''],
    profileReport: workflow?.runtime?.profile_report_sha256 || '',
    comparisonProfile: workflow?.comparison_profile_report_sha256 || ''});
}

function currentInvestigation() {
  if (investigationReturn?.binding !== investigationBinding()) investigationReturn = undefined;
  return investigationReturn;
}

async function returnToInvestigation({fromHistory = false} = {}) {
  const origin = currentInvestigation();
  if (!origin || !draftSession.canNavigate()) return false;
  if (view === 'browse' && !draftSession.allowSwitch(
    'Return to saved evidence? Dirty drafts remain in this session; export them before leaving.')) return false;
  if (!fromHistory) history.pushState({codeAnalysisInvestigation: origin.id}, '', origin.url);
  view = origin.view;
  filter.value = origin.filter;
  offset = origin.offset;
  findingSelection = {...origin.findings};
  facetSelection = {...origin.facets};
  comparisonSelection = {...origin.comparison};
  document.querySelectorAll('.nav').forEach(button => button.classList.toggle('active', button.dataset.view === view));
  await render();
  if (view !== origin.view || currentInvestigation() !== origin) return false;
  const selected = [...content.querySelectorAll('[data-kind][data-identity]')].find(button =>
    button.dataset.kind === origin.focus?.kind && button.dataset.identity === origin.focus?.identity);
  finalizeAcceptedNavigation(selected || document.querySelector('#title'));
  (selected || document.querySelector('#title')).focus();
  const scroller = document.querySelector('.lui-shell__content');
  if (scroller) scroller.scrollTop = origin.scrollTop;
  return true;
}
installUserHelp();
window.addEventListener('beforeunload', event => {
  if (sessionClosed || !draftSession.hasDirty()) return;
  event.preventDefault();
  event.returnValue = '';
});

async function request(path, signal) {
  if (sessionClosed) throw new DOMException('Code Analysis session is closed', 'AbortError');
  const url = new URL(path, location.origin);
  // Handoff routes accept their exact selection fields alone; a catalog choice must
  // never travel with them or redirect the artifact their selection is validated against.
  const handoff = url.pathname === '/api/handoff' || url.pathname === '/api/runtime/handoff';
  if (!handoff && runSelection && !url.pathname.startsWith('/api/collection') && url.pathname !== '/api/runs' && !url.searchParams.has('run')) {
    url.searchParams.set('run', runSelection.primary);
    url.searchParams.set('compare_run', runSelection.comparison);
  }
  const profileRoute = url.pathname.startsWith('/api/runtime')
    || url.pathname === '/api/code-browser/runtime-source'
    || (url.pathname === '/api/code-browser/graph' && url.searchParams.get('layer') === 'runtime-calls');
  if (!handoff && profileSelection && profileRoute && !url.searchParams.has('profile_run')) {
    url.searchParams.set('profile_run', profileSelection.primary);
    url.searchParams.set('compare_profile_run', profileSelection.comparison);
  }
  const response = await fetch(url.pathname + url.search, {signal, headers: {"X-Code-Analysis-Token": token}});
  if (!response.ok) {
    const payload = await response.json();
    signal?.throwIfAborted();
    throw new Error(payload.error?.message || `HTTP ${response.status}`);
  }
  return response;
}

async function api(path, signal) {
  const response = await request(path, signal);
  const payload = await response.json();
  signal?.throwIfAborted();
  if (!response.ok) throw new Error(payload.error?.message || `HTTP ${response.status}`);
  return payload;
}

async function post(path, body, signal) {
  if (sessionClosed) throw new DOMException('Code Analysis session is closed', 'AbortError');
  const response = await fetch(path, {method: 'POST', signal, headers: {
    'X-Code-Analysis-Token': token, 'Content-Type': 'application/json'
  }, body: JSON.stringify(body)});
  const payload = await response.json();
  signal?.throwIfAborted();
  if (!response.ok) throw new Error(payload.error?.message || `HTTP ${response.status}`);
  return payload;
}

async function refreshWorkflow(signal) {
  try {
    const params = new URLSearchParams();
    if (profileSelection) {
      params.set('profile_run', profileSelection.primary);
      params.set('compare_profile_run', profileSelection.comparison);
    }
    const payload = await api(`/api/workflow?${params}`, signal);
    signal?.throwIfAborted();
    workflow = payload;
    workflowFailure = undefined;
    mountWorkflowNavigation(workflow);
    return payload;
  } catch (error) {
    if (error.name === 'AbortError') throw error;
    workflow = undefined;
    workflowFailure = error.message;
    // Guidance failure must not erase independently available evidence views.
    return undefined;
  }
}

async function navigateView(destination, {operation} = {}) {
  if (sessionClosed) return false;
  const definition = workflowDestination(workflow, destination);
  if (definition && ['missing', 'failed'].includes(definition.readiness.state)) {
    showWorkflowUnavailable(workflow, destination, (next, options) => navigateView(next, options).catch(showError));
    finalizeAcceptedNavigation(document.querySelector('#workflow-notice'));
    return false;
  }
  if (!draftSession.canNavigate()) return false;
  handoffController?.abort();
  candidateReviewSession = undefined;
  candidateReturnFocusSession = undefined;
  projectionHandoffRequest = undefined;
  runtimeHandoffRequest = undefined;
  const dialog = document.querySelector('#evidence-dialog');
  if (dialog.open) dialog.close();
  if (operation) collectionSession.requestedOperation = operation;
  view = destination;
  if (!['state-effects', 'contracts', 'dictionary-shapes'].includes(view)) facetSelection.owner = '';
  offset = 0;
  filter.value = '';
  document.querySelector('#workflow-notice').hidden = true;
  document.querySelectorAll('.nav').forEach(item => item.classList.toggle('active', item.dataset.view === view));
  revealWorkflowView(view, true);
  await render();
  finalizeAcceptedNavigation();
  return true;
}

const escapeHtml = (value) => String(value ?? "").replace(/[&<>"']/g, (char) => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"})[char]);
const FINDINGS_INLINE_CHAR_LIMIT = 72;

function findingsTextCell(value) {
  const text = String(value ?? "");
  if (!text) return "";
  const escaped = escapeHtml(text);
  if (text.length <= FINDINGS_INLINE_CHAR_LIMIT && !text.includes("\n")) {
    return `<span class="findings-text-inline">${escaped}</span>`;
  }
  const preview = text.length > FINDINGS_INLINE_CHAR_LIMIT
    ? `${text.slice(0, FINDINGS_INLINE_CHAR_LIMIT - 1)}…`
    : `${text.split("\n", 1)[0]}…`;
  return `<details class="findings-text-details"><summary><span class="findings-text-summary">${escapeHtml(preview)}</span></summary><div class="findings-text-full">${escaped}</div></details>`;
}

const badge = (value) => `<span class="badge ${escapeHtml(value)}">${escapeHtml(value)}</span>`;
function providerObservationsCell(values) {
  if (!values.length) return 'No recorded observations.';
  if (values.length === 1 && values[0].length <= 96 && !values[0].includes('\n')) return escapeHtml(values[0]);
  const details = disclosure({title: `${values.length} recorded observation${values.length === 1 ? '' : 's'}`,
    content: element('ol', {}, values.map(value => element('li', {text: value})))});
  details.classList.add('provider-observations');
  return details;
}

function cloneModulesCell(values) {
  if (values.length === 1 && values[0].length <= 80) return escapeHtml(values[0]);
  if (!values.length) return 'No recorded modules.';
  const details = disclosure({title: `${values.length} recorded module${values.length === 1 ? '' : 's'}`,
    content: element('div', {className: 'clone-module-list', role: 'region', 'aria-label': 'Recorded clone module identities'},
      element('ul', {}, values.map(value => element('li', {text: value}))))});
  details.classList.add('clone-modules');
  return details;
}
const openEvidence = evidenceViewer(api, escapeHtml, {
  reportIdentity: () => bootstrap?.reports[0]?.report_sha256,
  onBrowseSource: jumpToCode,
});
const evidenceButton = (kind, identity, label) => `<button class="detail-button" data-kind="${escapeHtml(kind)}" data-identity="${escapeHtml(identity)}">${escapeHtml(label)}</button>`;
const findingButton = (value, row) => `${row.symbol ? `<div class="hint">${escapeHtml(row.symbol)}</div>` : ''}<button class="detail-button" data-kind="finding" data-identity="${escapeHtml(row.finding_id)}" data-finding-symbol="${escapeHtml(row.symbol || '')}">${escapeHtml(value)}</button> <button class="detail-button" data-finding-details="${escapeHtml(row.finding_id)}">Details</button>`;

async function jumpToCode(selection, signal) {
  if (!draftSession.canNavigate()) return;
  signal?.throwIfAborted();
  const originView = view;
  const binding = investigationBinding();
  const origin = {view, binding, filter: filter.value, offset,
    url: location.pathname + location.search + location.hash,
    scrollTop: document.querySelector('.lui-shell__content')?.scrollTop || 0,
    findings: {...findingSelection}, facets: {...facetSelection}, comparison: {...comparisonSelection},
    focus: evidenceFocus && {...evidenceFocus}};
  const params = new URLSearchParams({...selection, revision: 0});
  const accepted = await api(`/api/code-browser?${params}`, signal);
  signal?.throwIfAborted();
  if (view !== originView || !draftSession.canNavigate()) return;
  if (binding !== investigationBinding() || accepted.report_sha256 !== bootstrap?.reports[0]?.report_sha256) {
    throw new Error('The originating report changed; reselect its evidence.');
  }
  if (view !== 'browse') {
    investigationReturn = {...origin, id: ++investigationSequence};
    history.replaceState({codeAnalysisInvestigation: investigationReturn.id}, '');
  }
  Object.assign(codeBrowserSession, {report_sha256: accepted.report_sha256,
    path: accepted.logical_path, line: selection.line,
    identity: accepted.selected?.identity || selection.identity || '', offset: 0, search: '', revision: 0,
    active_pane: 'source'});
  history.pushState({codeAnalysisBrowse: codeBrowserSelectionMetadata(codeBrowserSession)}, '', '#browse');
  const dialog = document.querySelector('#evidence-dialog');
  if (dialog.open) dialog.close();
  if (findingSourceContext?.binding === binding) {
    const selected = findingSourceContext.choices.find(item => item.span.path === selection.path && item.span.start_line === Number(selection.line));
    if (selected) { findingSourceContext.choosing = false; findingSourceContext.selected = selected; }
    else findingSourceContext = undefined;
  }
  view = 'browse';
  document.querySelectorAll('.nav').forEach(button => button.classList.toggle('active', button.dataset.view === view));
  await render();
  const sourceHeading = content.querySelector('.code-browser-source h2');
  finalizeAcceptedNavigation(sourceHeading || document.querySelector('#title'));
  sourceHeading?.scrollIntoView({block: 'start', inline: 'nearest'});
}

async function openFindingSource(button) {
  if (!draftSession.canNavigate()) return;
  findingSourceController?.abort();
  const current = findingSourceController = new AbortController();
  const parentSignal = renderController.signal;
  parentSignal.addEventListener('abort', () => current.abort(), {once: true});
  const binding = investigationBinding();
  const identity = button.dataset.identity;
  const report = bootstrap.reports[0].report_sha256;
  try {
    const params = new URLSearchParams({kind: 'finding', identity, limit: '25', report_sha256: report});
    const result = await api(`/api/evidence?${params}`, current.signal);
    current.signal.throwIfAborted();
    if (binding !== investigationBinding() || !button.isConnected) return;
    const item = result.items.find(item => item.kind === 'finding' && item.identity === identity);
    const locations = new Map();
    for (const evidence of item?.evidence || []) {
      const span = evidence.span;
      if (!span?.path || !Number.isInteger(span.start_line) || span.start_line < 1 || !Number.isInteger(span.end_line) || span.end_line < span.start_line) continue;
      locations.set(JSON.stringify([span.path, span.start_line, span.end_line]), evidence);
    }
    if (!locations.size || result.next_identity) {
      await openEvidence('finding', identity, '', 0, {report_sha256: report, signal: parentSignal});
      return;
    }
    findingSourceContext = {binding, identity, title: button.textContent, symbol: button.dataset.findingSymbol, statement: item.statement,
      cautions: item.limitations || [], choices: [...locations.values()], choosing: locations.size > 1};
    if (locations.size === 1) {
      const span = [...locations.values()][0].span;
      await jumpToCode({report_sha256: report, path: span.path, line: span.start_line}, current.signal);
    } else {
      investigationReturn = {id: ++investigationSequence, binding, view, filter: filter.value, offset,
        url: location.pathname + location.search + location.hash,
        scrollTop: document.querySelector('.lui-shell__content')?.scrollTop || 0,
        findings: {...findingSelection}, facets: {...facetSelection}, comparison: {...comparisonSelection},
        focus: {...evidenceFocus}};
      history.replaceState({codeAnalysisInvestigation: investigationReturn.id}, '');
      history.pushState({codeAnalysisFindingChoices: investigationReturn.id}, '', '#browse');
      view = 'browse';
      document.querySelectorAll('.nav').forEach(item => item.classList.toggle('active', item.dataset.view === view));
      await render();
      finalizeAcceptedNavigation();
    }
  } catch (error) {
    if (!current.signal.aborted && button.isConnected) {
      const message = element('p', {className: 'hint', role: 'status', text: `Code browser unavailable: ${error.message}. Use Details to inspect the saved evidence.`});
      button.parentElement.append(message);
    }
  }
}

function findingContextPanel(context, signal) {
  const concise = value => String(value || '').length > 280 ? `${String(value).slice(0, 279)}…` : String(value || '');
  const panel = element('section', {className: 'panel finding-source-context', 'aria-label': 'Finding context'});
  panel.append(element('h2', {text: context.title}), element('p', {text: concise(context.statement)}));
  if (context.symbol) panel.append(element('p', {className: 'mono', text: context.symbol}));
  if (context.cautions.length) panel.append(element('p', {className: 'hint', text: concise(context.cautions[0])}));
  if (context.choosing) panel.append(element('p', {text: 'Choose a saved evidence location. No location has been selected automatically.'}));
  const choices = context.choosing ? context.choices.slice(0, 25) : [context.selected || context.choices[0]];
  if (context.choosing && context.choices.length > choices.length) panel.append(element('p', {className: 'hint', text: `${context.choices.length - choices.length} additional saved locations omitted from this bounded chooser. Use Details for all saved evidence.`}));
  for (const evidence of choices) {
    const span = evidence.span;
    const row = element('p', {text: `${span.path}:${span.start_line}–${span.end_line} — ${concise(evidence.observation)}`});
    if (context.choosing) {
      const choose = element('button', {className: 'detail-button', type: 'button', text: `Open ${span.path}:${span.start_line}–${span.end_line}`});
      choose.addEventListener('click', async () => {
        findingSourceController?.abort();
        const current = findingSourceController = new AbortController();
        signal.addEventListener('abort', () => current.abort(), {once: true});
        try {
          await jumpToCode({report_sha256: bootstrap.reports[0].report_sha256, path: span.path, line: span.start_line}, current.signal);
        } catch (error) { if (!current.signal.aborted) row.append(element('span', {text: ` Code browser unavailable: ${error.message}`})); }
      });
      row.append(choose);
    }
    panel.append(row);
  }
  const details = element('button', {className: 'detail-button', type: 'button', text: 'Details'});
  details.addEventListener('click', () => {
    findingSourceController?.abort();
    openEvidence('finding', context.identity, '', 0, {report_sha256: bootstrap.reports[0].report_sha256, signal});
  });
  panel.append(details);
  return panel;
}

function facetControls(includeOwner = false) {
  const language = (value, label) => `<option value="${value}" ${facetSelection.language === value ? 'selected' : ''}>${label}</option>`;
  return `<form id="facet-controls" class="downloads">
    <div><label for="facet-language">Language</label><select id="facet-language" name="language">${language('', 'All languages')}${language('python', 'Python')}${language('javascript', 'JavaScript')}${language('ruby', 'Ruby')}</select></div>
    <div><label for="facet-module">Logical module prefix</label><input id="facet-module" name="module_prefix" value="${escapeHtml(facetSelection.module_prefix)}" placeholder="package.module or path/file.js"></div>
    ${includeOwner ? `<div><label for="facet-owner">Exact saved owner</label><input id="facet-owner" name="owner" value="${escapeHtml(facetSelection.owner)}"></div>` : ''}
    <button class="detail-button">Apply scope filters</button>
  </form>`;
}

function wireFacetControls() {
  const controls = content.querySelector('#facet-controls');
  controls.addEventListener('submit', event => {
    event.preventDefault();
    facetSelection = {owner: '', ...Object.fromEntries(new FormData(controls))};
    offset = 0;
    render().catch(showError);
  });
}

function scopedFacets(includeOwner = false) {
  const scoped = new URLSearchParams(facetSelection);
  if (!includeOwner) scoped.delete('owner');
  return scoped;
}

function coverageSummary(page) {
  const coverage = page.coverage;
  if (!coverage) return '';
  return `<p class="hint">Section coverage: ${badge(coverage.state)} · ${coverage.parsed.toLocaleString()} parsed · ${coverage.skipped.toLocaleString()} skipped · ${coverage.failed.toLocaleString()} failed · ${coverage.unresolved.toLocaleString()} unresolved · ${coverage.truncated.toLocaleString()} truncated</p>`;
}
content.addEventListener('click', event => {
  const details = event.target.closest('[data-finding-details]');
  if (details) {
    findingSourceController?.abort();
    evidenceFocus = {kind: 'finding', identity: details.dataset.findingDetails};
    findingSourceContext = undefined;
    openEvidence('finding', details.dataset.findingDetails, '', 0,
      {report_sha256: bootstrap.reports[0].report_sha256, signal: renderController.signal});
    return;
  }
  const button = event.target.closest('[data-kind][data-identity]');
  if (button) findingSourceController?.abort();
  if (button) evidenceFocus = {kind: button.dataset.kind, identity: button.dataset.identity};
  if (button?.dataset.kind === 'finding') { openFindingSource(button); return; }
  if (button) findingSourceContext = undefined;
  if (button) openEvidence(button.dataset.kind, button.dataset.identity, '', 0,
    button.dataset.kind === 'test' ? {report_sha256: bootstrap.reports[0].report_sha256, signal: renderController.signal} : null);
});

const pendingTables = new Map();
let tableSequence = 0;

function table(columns, page, {wrapClass = ""} = {}) {
  const start = page.retained ? page.offset + 1 : 0;
  const end = page.offset + page.retained;
  const wrapClasses = wrapClass ? `table-wrap ${wrapClass}` : "table-wrap";
  const tableId = `report-table-${++tableSequence}`;
  if (page.records.length) pendingTables.set(tableId, {columns, page, caption: title.textContent});
  const body = page.records.length
    ? `<div id="${tableId}" class="${wrapClasses}"></div>`
    : `<div class="empty"><h2>No matching evidence</h2><p>The saved report contains no records for this view and filter.</p></div>`;
  const omissions = page.omitted ? `<p class="hint">${page.omitted.toLocaleString()} comparison records omitted by the analysis bound; ${page.eligible.toLocaleString()} are available to browse.</p>` : "";
  const completeness = page.completeness ? `<p class="hint">Evidence completeness: ${badge(page.completeness)}</p>` : "";
  return `${body}${omissions}${completeness}<div class="pager"><span>Showing ${start.toLocaleString()}–${end.toLocaleString()} of ${page.eligible.toLocaleString()}</span><div><button id="previous-page" ${page.offset === 0 ? "disabled" : ""}>Previous</button><button id="next-page" ${page.truncated ? "" : "disabled"}>Next</button></div></div>`;
}

async function wirePager(page, signal) {
  for (const [identity, options] of pendingTables) {
    pendingTables.delete(identity);
    const host = document.getElementById(identity);
    if (!host) continue;
    const root = await reportTable(options.columns, options.page, options.caption);
    signal.throwIfAborted();
    if (host.isConnected) host.replaceChildren(root);
  }
  document.querySelector("#previous-page")?.addEventListener("click", () => {
    offset = Math.max(0, page.offset - page.limit);
    render().catch(showError);
  });
  document.querySelector("#next-page")?.addEventListener("click", () => {
    offset = page.offset + page.retained;
    render().catch(showError);
  });
}

// Labels are stacked in two columns outside the ring so they never cover nodes or each other.
// Each label keeps its node's vertical order; ties resolve by node order for a stable layout.
function architectureLabelColumns(nodes, positions, {cx, columnLeft, columnRight, top, bottom, spacing}) {
  const columns = [
    {x: columnLeft, anchor: "end", side: "left", entries: []},
    {x: columnRight, anchor: "start", side: "right", entries: []},
  ];
  nodes.forEach((node, index) => {
    const point = positions.get(node.node_id);
    columns[point.x <= cx ? 0 : 1].entries.push({node_id: node.node_id, index, y: point.y});
  });
  const placements = new Map();
  for (const column of columns) {
    const ordered = column.entries.sort((first, second) => first.y - second.y || first.index - second.index);
    const step = Math.min(spacing, (bottom - top) / Math.max(1, ordered.length - 1));
    let cursor = top;
    ordered.forEach((entry, position) => {
      const latest = bottom - step * (ordered.length - position - 1);
      const y = Math.min(Math.max(entry.y, cursor), latest);
      placements.set(entry.node_id, {x: column.x, y, anchor: column.anchor, side: column.side});
      cursor = y + step;
    });
  }
  return placements;
}

function architectureGraph(graph) {
  if (!graph.nodes.length) return `<div class="empty"><h2>No dependency evidence</h2><p>This saved report contains no local module edges.</p></div>`;
  const width = 900, height = 620, cx = width / 2, cy = height / 2, radius = 245;
  const leaderGap = 34, labelSpacing = 24, labelMargin = 30, leaderInset = 7;
  const positions = new Map(graph.nodes.map((node, index) => {
    const angle = (Math.PI * 2 * index / graph.nodes.length) - Math.PI / 2;
    return [node.node_id, {x: cx + Math.cos(angle) * radius, y: cy + Math.sin(angle) * radius}];
  }));
  const edges = graph.edges.map((edge) => {
    const from = positions.get(edge.source_id), to = positions.get(edge.target_id);
    return `<line x1="${from.x}" y1="${from.y}" x2="${to.x}" y2="${to.y}" class="graph-edge"><title>${escapeHtml(edge.source_id)} → ${escapeHtml(edge.target_id)} (${escapeHtml(edge.kind)})</title></line>`;
  }).join("");
  const labels = architectureLabelColumns(graph.nodes, positions, {
    cx, columnLeft: cx - radius - leaderGap, columnRight: cx + radius + leaderGap,
    top: labelMargin, bottom: height - labelMargin, spacing: labelSpacing,
  });
  const nodes = graph.nodes.map((node) => {
    const point = positions.get(node.node_id);
    const short = node.label.split(node.language === 'python' ? '.' : '/').at(-1).slice(0, 15);
    const size = Math.min(15, 6 + Math.log2(node.finding_count + 2) * 1.7);
    const label = labels.get(node.node_id);
    const tipX = label.x + (label.side === "left" ? leaderInset : -leaderInset);
    const span = Math.hypot(tipX - point.x, label.y - point.y) || 1;
    const leaderX = point.x + (tipX - point.x) / span * size, leaderY = point.y + (label.y - point.y) / span * size;
    return `<g class="graph-node" data-module="${escapeHtml(node.node_id)}" tabindex="0" role="button" aria-label="Open module ${escapeHtml(node.label)}"><circle cx="${point.x}" cy="${point.y}" r="${size}" class="${escapeHtml(node.language)}"><title>${escapeHtml(node.label)} · ${node.lines} lines · ${node.finding_count} findings</title></circle><line x1="${leaderX}" y1="${leaderY}" x2="${tipX}" y2="${label.y}" class="graph-label-leader" aria-hidden="true"></line><text x="${label.x}" y="${label.y}" text-anchor="${label.anchor}" dominant-baseline="middle">${escapeHtml(short)}</text></g>`;
  }).join("");
  return `<div class="graph-summary"><strong>${graph.retained_nodes} of ${graph.eligible_nodes} modules</strong><span>${graph.retained_edges} of ${graph.eligible_edges} local edges</span>${graph.truncated ? badge("bounded view") : badge("complete view")}</div><div class="graph-wrap"><svg viewBox="0 0 ${width} ${height}" role="img" aria-label="Bounded module dependency graph"><g>${edges}</g><g>${nodes}</g></svg></div><p class="hint">Node size reflects finding count. Short module names are listed in the side columns; the dotted leaders are display aids, not dependency edges. Select a node to open its module record. Hover an edge for its import kind.</p>`;
}

async function render() {
  if (sessionClosed) return;
  if (!draftSession.canNavigate()) return;
  renderController?.abort();
  renderController = new AbortController();
  const signal = renderController.signal;
  revealWorkflowView(view);
  renderWorkflowGuidance(document.querySelector('#view-guidance'), workflow, view,
    (next, options) => navigateView(next, options).catch(showError));
  title.textContent = view[0].toUpperCase() + view.slice(1);
  const descriptions = {overview:"Start with the report's scope, completeness, and strongest signals.",modules:"Browse source inventory and find highly connected or finding-heavy modules.",architecture:"See the most connected modules and their local dependency direction.",findings:"Review concrete analyzer observations, ordered by severity.","state-effects":"Inspect saved syntax evidence for mutable state and side effects; this is not runtime proof.",contracts:"Inspect saved contract definitions and their defining owners.","dictionary-shapes":"Inspect saved dictionary shape variants and lifecycle evidence.",clones:"Inspect saved clone clusters while retaining every member of each matching cluster.","test-impact":"Inspect saved test inventories and their static relationships.",candidates:"Prioritize evidence-backed refactoring opportunities and blocked decisions.",comparison:"Inspect canonical evidence added, removed, or changed between runs.",providers:"Confirm which analyzers completed, failed, or produced partial evidence."};
  const purpose = workflowDestination(workflow, view)?.purpose;
  description.textContent = purpose || descriptions[view] || '';
  if (!purpose && view === 'diagnostics') description.textContent = 'Inspect provider failures, unresolved evidence, and the reasons analysis may be partial. Filter by code, provider, path, or message.';
  if (!purpose && view === 'runs') description.textContent = 'Choose a retained baseline and optional candidate. Compatibility and report integrity are checked before switching views.';
  if (!purpose && view === 'explore') description.textContent = 'Explore saved evidence by domain, focus a selection, and download the same projection without rescanning.';
  search.hidden = ["overview", "architecture", "comparison", "providers", "runs", "explore"].includes(view);
  content.innerHTML = `<div class="loading">Loading bounded evidence…</div>`;
  const q = encodeURIComponent(filter.value.trim());
  if (view === 'browse') {
    title.textContent = 'Browse code';
    if (!purpose) description.textContent = 'Navigate saved entities and recorded containment beside verified original source. No target code is executed or changed.';
    search.hidden = true;
    const findingContext = findingSourceContext?.binding === investigationBinding() ? findingSourceContext : null;
    if (findingContext?.choosing) content.replaceChildren();
    else await codeBrowser(content, api, signal, bootstrap.reports[0].report_sha256, codeBrowserSession, draftSession, {
      post: (path, body, local) => post(path, {...body, run: runSelection?.primary || 'current',
        compare_run: runSelection?.comparison || 'none'}, local),
      onSourceConnected: () => refreshWorkflow(signal),
    });
    signal.throwIfAborted();
    if (findingContext) content.prepend(findingContextPanel(findingContext, signal));
    const origin = currentInvestigation();
    if (origin) {
      const back = document.createElement('button');
      back.type = 'button'; back.className = 'detail-button'; back.id = 'return-to-investigation';
      back.textContent = `Return to ${origin.view[0].toUpperCase() + origin.view.slice(1)}`;
      back.addEventListener('click', () => returnToInvestigation().catch(showError));
      content.prepend(back);
    }
    return;
  }
  if (view === 'collection') {
    title.textContent = 'Collection setup';
    if (!purpose) description.textContent = 'Choose a project, review the exact command, and inspect collection status. Browser execution requires explicit server enablement and confirmation.';
    search.hidden = true;
    await collectionView(content, api, post, escapeHtml, signal, collectionSession);
    return;
  }
  if (view === 'runtime') {
    search.hidden = true;
    if (!purpose) description.textContent = 'Explore retained workload evidence without executing code.';
    const initialRequest = runtimeHandoffRequest;
    runtimeHandoffRequest = undefined;
    await runtimeView(content, api, escapeHtml, signal, request, {initialRequest,
      staticReportSha256: bootstrap?.reports[0]?.report_sha256, onBrowseSource: jumpToCode});
    return;
  }
  if (view === 'explore') {
    const reviewSession = candidateReviewSession;
    const initialRequest = projectionHandoffRequest;
    projectionHandoffRequest = undefined;
    if (reviewSession) {
      title.textContent = 'Candidate review';
      if (!purpose) description.textContent = 'Review the ranked saved-report worklist, optional decision snapshot, and candidate packets. Inventory filters are preserved for return, not converted into worklist policy.';
    }
    await projectionExplorer(content, api, request, escapeHtml, signal, openEvidence, {
      initialName: reviewSession ? 'candidate-worklist' : undefined,
      initialRequest,
      onReturnToCandidates: reviewSession ? async () => {
        if (signal.aborted || view !== 'explore' || candidateReviewSession !== reviewSession) return;
        candidateReviewSession = undefined;
        candidateReturnFocusSession = reviewSession;
        filter.value = reviewSession.filter;
        facetSelection = {...reviewSession.facets};
        offset = reviewSession.offset;
        view = 'candidates';
        document.querySelectorAll('.nav').forEach(button => {
          button.classList.toggle('active', button.dataset.view === view);
        });
        await render();
        if (candidateReturnFocusSession === reviewSession && view === 'candidates') {
          candidateReturnFocusSession = undefined;
          content.querySelector('#review-ranked-worklist')?.focus();
        }
      } : undefined,
    });
    return;
  }
  if (view === 'runs') {
    const profileOnly = !bootstrap;
    const refresh = refreshRunCatalog; refreshRunCatalog = false;
    const catalog = await api(`/api/runs?refresh=${refresh ? '1' : '0'}`, signal);
    const profiles = await api('/api/profile-runs', signal);
    const refreshButton = () => {
      const button = document.createElement('button'); button.className = 'detail-button';
      button.textContent = 'Refresh saved runs';
      button.addEventListener('click', () => { refreshRunCatalog = true; render().catch(showError); });
      content.prepend(button);
    };
    if (!catalog.records.length && !profiles.records.length) {
      content.innerHTML = '<div class="empty"><h2>No workspace runs available</h2><p>Open a report in its owned workspace, or launch with <code>--workspace PATH</code>.</p></div>';
      refreshButton();
      return;
    }
    const options = catalog.records.map(item => `<option value="${escapeHtml(item.generation_id)}">${escapeHtml(item.created_at)} · ${escapeHtml(item.generation_id)}</option>`).join('');
    content.innerHTML = `<section id="run-selection" class="panel"><h2>Choose saved evidence</h2><label>Baseline <select id="baseline-run"><option value="current">Initially opened report</option>${options}</select></label><label> Candidate <select id="candidate-run"><option value="none">No comparison</option><option value="current">Initially opened report</option>${options}</select></label><p><button id="apply-runs" class="detail-button">Open selected runs</button></p><p class="hint">Only validated saved analysis generations are listed. No source scan is performed.</p></section>`;
    if (profileOnly) {
      document.querySelector('#baseline-run').parentElement.hidden = true;
      document.querySelector('#candidate-run').parentElement.hidden = true;
      content.querySelector('.hint').textContent = 'No static report selected. Choose saved profiles for diagnostic comparison; no source scan or workload execution.';
    }
    if (catalog.omitted) content.insertAdjacentHTML('beforeend', `<p class="hint">${catalog.omitted} older runs omitted by the 500-run catalog bound.</p>`);
    const profileOptions = profiles.records.map(item => `<option value="${escapeHtml(item.generation_id)}">${escapeHtml(item.created_at)} · ${escapeHtml(item.generation_id)}</option>`).join('');
    document.querySelector('#apply-runs').parentElement.insertAdjacentHTML('beforebegin', `<fieldset><legend>Saved runtime evidence (optional)</legend><label>Profile baseline <select id="baseline-profile"><option value="current">Initially supplied profiles</option><option value="none">No runtime evidence</option>${profileOptions}</select></label><label>Profile candidate <select id="candidate-profile"><option value="none">No saved comparison</option>${profileOptions}</select></label><p class="hint">Choose profiles explicitly; dates do not bind them to static reports. Comparisons remain diagnostic-only. ${profiles.omitted} older profiles omitted by the 500-run catalog bound.</p></fieldset>`);
    refreshButton();
    for (const [id, identity] of [['baseline-run', runSelection?.primary || 'current'], ['candidate-run', runSelection?.comparison || 'none'], ['baseline-profile', profileSelection?.primary || (profileOnly ? profiles.records[0]?.generation_id || 'current' : 'current')], ['candidate-profile', profileSelection?.comparison || 'none']]) {
      const select = document.getElementById(id);
      if (![...select.options].some(option => option.value === identity)) {
        const option = document.createElement('option'); option.value = identity;
        option.textContent = `Loaded snapshot (not in catalog): ${identity}`; select.append(option);
      }
      select.value = identity;
    }
    document.querySelector('#apply-runs').addEventListener('click', async () => {
      if (!draftSession.canNavigate() || !draftSession.allowSwitch('Change analysis collections? Dirty drafts remain in this session and are not evidence for the new report. Export them before leaving.')) return;
      const primary = document.querySelector('#baseline-run').value;
      const comparison = document.querySelector('#candidate-run').value;
      const profilePrimary = document.querySelector('#baseline-profile').value;
      const profileComparison = document.querySelector('#candidate-profile').value;
      renderController.abort();
      renderController = new AbortController();
      try {
        const params = new URLSearchParams(profileOnly ? {} : {run: primary, compare_run: comparison});
        const payload = profileOnly ? null : await api(`/api/bootstrap?${params}`, renderController.signal);
        const runtimeParams = new URLSearchParams({...Object.fromEntries(params), profile_run: profilePrimary, compare_profile_run: profileComparison});
        const runtime = await api(`/api/runtime?${runtimeParams}`, renderController.signal);
        candidateReviewSession = undefined;
        candidateReturnFocusSession = undefined;
        projectionHandoffRequest = undefined;
        runtimeHandoffRequest = undefined;
        runSelection = profileOnly ? null : {primary, comparison};
        profileSelection = {primary: profilePrimary, comparison: profileComparison};
        bootstrap = payload;
        await refreshWorkflow(renderController.signal);
        findingSelection = {severity: '', category: '', confidence: ''};
        facetSelection = {language: '', module_prefix: '', owner: ''};
        document.querySelector('#evidence-dialog').close();
        document.querySelector('#report-state').textContent = profileOnly ? `${runtime.available ? 'Saved runtime evidence' : 'No runtime profile selected'} · no static report selected` : `${payload.reports[0].completeness} ${payload.reports[0].target_root}`;
        view = profileOnly && runtime.available ? 'runtime' : 'overview'; offset = 0; filter.value = '';
        document.querySelectorAll('.nav').forEach(item => item.classList.toggle('active', item.dataset.view === view));
        await render();
      } catch (error) { showError(error); }
    });
    return;
  }
  if (view === "overview") {
    if (!bootstrap) {
      content.replaceChildren();
      if (workflow) content.append(workflowOverview(workflow, (next, options) => navigateView(next, options).catch(showError)));
      else content.innerHTML = `<div class="empty error"><h2>Workflow guidance unavailable</h2><p>${escapeHtml(workflowFailure || 'No report selected.')}</p><p>Use Collect / plan or Saved runs to choose evidence.</p></div>`;
      return;
    }
    const report = bootstrap.reports[0];
    content.innerHTML = `<div class="cards">${bootstrap.counts.map((item) => `<article class="metric"><span>${escapeHtml(item.label)}</span><strong>${item.value.toLocaleString()}</strong></article>`).join("")}</div><div class="grid"><article class="panel"><p class="eyebrow">Primary report</p><h2>${escapeHtml(report.target_root)}</h2><dl><dt>Completeness</dt><dd>${badge(report.completeness)}</dd><dt>Command</dt><dd>${escapeHtml(report.command)}</dd><dt>Languages</dt><dd>${bootstrap.languages.map(badge).join(" ")}</dd><dt>SHA-256</dt><dd class="mono">${escapeHtml(report.report_sha256)}</dd></dl></article><article class="panel"><p class="eyebrow">Comparison</p>${bootstrap.comparison ? `<h2>${bootstrap.comparison.identical ? "No canonical changes" : "Evidence changed"}</h2><div class="delta"><span>+${bootstrap.comparison.added}</span><span>−${bootstrap.comparison.removed}</span><span>Δ${bootstrap.comparison.changed}</span></div>` : `<h2>Single report</h2><p>Start with <code>--compare REPORT</code> to inspect changes between two compatible analysis runs.</p>`}</article></div>`;
    if (workflow) content.prepend(workflowOverview(workflow, (next, options) => navigateView(next, options).catch(showError), bootstrap.counts.find(item => item.label === 'Findings')?.value));
    else if (workflowFailure) content.insertAdjacentHTML('afterbegin', `<p role="status">Workflow guidance unavailable: ${escapeHtml(workflowFailure)}. Saved evidence remains available.</p>`);
    return;
  }
  if (view === "modules") {
    const scoped = scopedFacets();
    const page = await api(`/api/modules?offset=${offset}&limit=${pageSize}&q=${q}&${scoped}`, signal);
    content.innerHTML = facetControls() + table([["Module","module",value => evidenceButton('navigation',value,value)],["Language","language",badge],["Lines","lines"],["Symbols","symbols"],["Fan in","fan_in"],["Fan out","fan_out"],["Findings","finding_count"]], page);
    wireFacetControls();
    await wirePager(page, signal);
  } else if (view === "architecture") {
    const graph = await api("/api/architecture?max_nodes=36&max_edges=72", signal);
    content.innerHTML = `<section id="dependency-explorer"></section><h2>Module dependency overview</h2>${architectureGraph(graph)}`;
    wireGraphDisplay(content, signal);
    const dependencies = dependencyExplorer(
      content.querySelector('#dependency-explorer'), api, request, escapeHtml, signal, openEvidence
    );
    document.querySelectorAll(".graph-node").forEach((node) => {
      const open = () => {
        content.querySelectorAll('.graph-node').forEach(item => item.setAttribute('aria-pressed', String(item === node)));
        dependencies.setSource(node.dataset.module);
        openEvidence('navigation', node.dataset.module);
      };
      node.addEventListener("click", open);
      node.addEventListener("keydown", (event) => { if (event.key === "Enter" || event.key === " ") { event.preventDefault(); open(); } });
    });
  } else if (view === "findings") {
    const selected = new URLSearchParams(findingSelection);
    const scoped = scopedFacets();
    const page = await api(`/api/findings?offset=${offset}&limit=${pageSize}&q=${q}&${selected}&${scoped}`, signal);
    const choice = (name, label, values) => `<div><label for="finding-${name}">${label}</label><select id="finding-${name}" name="${name}"><option value="">All</option>${values.map(value => `<option value="${escapeHtml(value)}">${escapeHtml(value)}</option>`).join('')}</select></div>`;
    const suppressionCell = (value, row) => {
      const rationale = row.suppression?.rationale || '';
      if (!rationale) return badge(value);
      return `${badge(value)}<div class="hint findings-suppression-rationale">${findingsTextCell(rationale)}</div>`;
    };
    content.innerHTML = facetControls() + `<form id="finding-controls" class="downloads">${choice('severity','Severity',['error','warning','info'])}${choice('category','Category',bootstrap.finding_categories)}${choice('confidence','Confidence',bootstrap.finding_confidences)}<button class="detail-button">Apply finding filters</button></form>` + table([["Severity","severity",badge],["Confidence","confidence",badge],["Finding","title",findingButton],["Suppression","suppression_summary",suppressionCell],["Explanation","explanation",findingsTextCell],["Language","language",badge],["Module","module"],["Rule","rule_id"],["Category","category",badge]], page, {wrapClass: "findings-table"});
    wireFacetControls();
    const controls = content.querySelector('#finding-controls');
    for (const [key, value] of Object.entries(findingSelection)) controls.elements[key].value = value;
    controls.addEventListener('submit', event => {
      event.preventDefault();
      findingSelection = Object.fromEntries(new FormData(controls));
      offset = 0; render().catch(showError);
    });
    await wirePager(page, signal);
  } else if (view === "state-effects") {
    const scoped = scopedFacets(true);
    const page = await api(`/api/state-effects?offset=${offset}&limit=${pageSize}&q=${q}&${scoped}`, signal);
    content.innerHTML = facetControls(true) + table([["Identity","identity"],["Kind","record_kind",badge],["Category","category"],["Module","module"],["Owner","owner"],["Language","language",badge],["Target","target"],["Confidence","confidence",badge],["Completeness","completeness",badge]], page);
    wireFacetControls();
    await wirePager(page, signal);
  } else if (view === "contracts") {
    const scoped = scopedFacets(true);
    const page = await api(`/api/contracts?offset=${offset}&limit=${pageSize}&q=${q}&${scoped}`, signal);
    content.innerHTML = facetControls(true) + coverageSummary(page) + table([["Identity","identity",value => evidenceButton('contract',value,value)],["Module","module"],["Owner","owner"],["Language","language",badge],["Kind","kind",badge],["Members","member_count"],["Boundary classes","boundary_classes"],["Unresolved","unresolved_count"]], page);
    wireFacetControls();
    await wirePager(page, signal);
  } else if (view === "dictionary-shapes") {
    const scoped = scopedFacets(true);
    const page = await api(`/api/dictionary-shapes?offset=${offset}&limit=${pageSize}&q=${q}&${scoped}`, signal);
    content.innerHTML = facetControls(true) + coverageSummary(page) + table([["Identity","identity",value => evidenceButton('shape',value,value)],["Module","module"],["Owner","owner"],["Language","language",badge],["Variants","variant_count"],["Field occurrences","field_occurrences"],["Lifecycle states","lifecycle_state_count"],["Unknown-field policy","unknown_field_policy",badge]], page);
    wireFacetControls();
    await wirePager(page, signal);
  } else if (view === "candidates") {
    const scoped = scopedFacets();
    const page = await api(`/api/candidates?offset=${offset}&limit=${pageSize}&q=${q}&${scoped}`, signal);
    content.innerHTML = facetControls() + `<section class="panel candidate-review-entry"><div><h2>Ranked refactor review</h2><p class="hint">Build the saved-report worklist when you are ready to inspect proposal gates, optional workspace decisions, and candidate packets. Inventory search and participant facets are not transferred because worklist policy uses different, explicit semantics.</p></div><button id="review-ranked-worklist" class="detail-button" type="button">Review ranked worklist</button></section>` + table([["State","state",badge],["Kind","kind"],["Languages","languages",value => value.map(badge).join(' ')],["Subjects","subject_count"],["Proposals","proposal_count"],["Failed gates","failed_gates"],["Unresolved","unresolved_gates"],["Evidence","candidate_id",value => evidenceButton('refactor-candidate', value, 'Inspect candidate')]], page);
    wireFacetControls();
    content.querySelector('#review-ranked-worklist').addEventListener('click', () => {
      if (signal.aborted || view !== 'candidates') return;
      candidateReviewSession = {filter: filter.value, facets: {...facetSelection}, offset};
      candidateReturnFocusSession = undefined;
      view = 'explore';
      document.querySelectorAll('.nav').forEach(button => {
        button.classList.toggle('active', button.dataset.view === view);
      });
      render().catch(showError);
    });
    await wirePager(page, signal);
  } else if (view === "clones") {
    const scoped = scopedFacets();
    const page = await api(`/api/clones?offset=${offset}&limit=${pageSize}&q=${q}&${scoped}`, signal);
    content.innerHTML = facetControls() + coverageSummary(page) + table([["Cluster","cluster_id",value => evidenceButton('clone',value,value),260],["Kind","kind",badge,185],["Language","language",badge,105],["Modules","modules",cloneModulesCell,360],["Subjects","subject_count",null,95],["Fragments","fragment_count",null,95],["Matched lines","matched_nonblank_lines",null,120],["Confidence","confidence",badge,115],["Completeness","completeness",badge,120]], page, {wrapClass: 'clones-table'});
    wireFacetControls();
    await wirePager(page, signal);
  } else if (view === "test-impact") {
    const scoped = scopedFacets();
    const page = await api(`/api/test-impact?offset=${offset}&limit=${pageSize}&q=${q}&${scoped}`, signal);
    content.innerHTML = facetControls() + coverageSummary(page) + table([["Identity","identity",value => evidenceButton('test',value,value)],["Kind","record_kind",badge],["Module","module"],["Name","name"],["Language","language",badge],["Relationship","relationship_kind"],["Target","target"],["Resolved","resolved"]], page);
    wireFacetControls();
    await wirePager(page, signal);
  } else if (view === "providers") {
    const page = await api(`/api/providers?offset=${offset}&limit=${pageSize}`, signal);
    content.innerHTML = '<p class="hint">Saved provider states, not a live installation check. Expand recorded observations for exact provenance and limitations; unavailable providers are not empty successes.</p>' + table([["Provider","provider_id",null,200],["State","state",badge,100],["Completeness","completeness",badge,120],["Observations","observations",providerObservationsCell,280]], page, {wrapClass: 'providers-table'});
    await wirePager(page, signal);
  } else if (view === "diagnostics") {
    const page = await api(`/api/diagnostics?offset=${offset}&limit=${pageSize}&q=${q}`, signal);
    content.innerHTML = table([["Level","level",badge],["Code","code"],["Provider","provider"],["Path","path"],["Message","message"]], page);
    await wirePager(page, signal);
  } else {
    const selection = new URLSearchParams({offset, limit: pageSize, q: filter.value});
    for (const [key, value] of Object.entries(comparisonSelection)) if (value) selection.set(key, value);
    const page = await api(`/api/comparison?${selection}`, signal);
    page.records = page.records.map(row => ({...row, source_position: row.position}));
    content.innerHTML = bootstrap.comparison ? '<section id="comparison-source" class="panel" role="region" aria-labelledby="comparison-source-heading" tabindex="-1" hidden></section>' + table([["Change","change",badge],["Subject","subject_id"],["Explanation","explanation"],["Source","source_position", value => `<button class="detail-button" data-comparison-position="${value}" aria-controls="comparison-source" aria-expanded="false">Source choices</button>`]], page) : `<div class="empty"><h2>No comparison report loaded</h2><p>Choose a baseline and candidate in Saved runs, or start with <code>code-analysis ui PRIMARY --compare CANDIDATE</code>.</p></div>`;
    if (bootstrap.comparison) {
      content.insertAdjacentHTML('afterbegin', `<form id="comparison-filters"><label>Change<select name="change"><option value="">All changes</option>${['added','removed','changed','unchanged'].map(value => `<option value="${value}" ${comparisonSelection.change === value ? 'selected' : ''}>${value}</option>`).join('')}</select></label><label>Record kind<input name="kind" placeholder="e.g. finding or module" value="${escapeHtml(comparisonSelection.kind)}"></label><button class="detail-button">Apply comparison filters</button></form><p>${page.eligible} matching retained records; ${page.filtered_out} filtered out; ${page.omitted} analytically omitted. Search matches exact case in subject or explanation.</p>`);
      content.querySelector('#comparison-filters').addEventListener('submit', event => {
        event.preventDefault();
        comparisonSelection = {change: event.target.elements.change.value, kind: event.target.elements.kind.value.trim()};
        offset = 0;
        render().catch(showError);
      });
    await wirePager(page, signal);
      wireComparisonSources({content, api, signal, reports: bootstrap.reports, escape: escapeHtml});
      wireComparisonDownloads({content, request, signal, reports: bootstrap.reports, selection, search: filter});
    }
  }
}

document.querySelectorAll('.nav').forEach(button => button.addEventListener('click', () => {
  navigateView(button.dataset.view).catch(showError);
}));
let filterTimer;
filter.addEventListener("input", () => { offset = 0; clearTimeout(filterTimer); filterTimer = setTimeout(() => render().catch(showError), 180); });
function showError(error) { if (sessionClosed || error.name === "AbortError") return; content.innerHTML = `<div class="empty error"><h2>Unable to render report</h2><p>${escapeHtml(error.message)}</p></div>`; }

let handoffController;
const runtimeHandoffFragment = () =>
  new URLSearchParams(location.hash.slice(1)).has('profile_report_sha256');

// A profile selection names no analysis report, so it is dispatched before the
// static discriminator and validated against the runtime service only.
async function followRuntimeHandoff(handoff) {
  if (!draftSession.canNavigate() || !draftSession.allowSwitch('Open the runtime collection? Dirty drafts remain in this session; export them before leaving.')) return true;
  renderController?.abort();
  handoffController = new AbortController();
  const signal = handoffController.signal;
  document.querySelector('#evidence-dialog').close();
  // A superseded or refused selection must leave no earlier graph or download on screen.
  content.innerHTML = `<div class="loading">Validating the handed-off runtime selection…</div>`;
  const selection = await api(`/api/runtime/handoff?${handoff}`, signal);
  if (signal.aborted) return true;
  // The verified handoff binds the server's supplied profile, not a prior catalog choice.
  runSelection = null;
  profileSelection = null;
  runtimeHandoffRequest = selection.graph_request;
  await refreshWorkflow(signal);
  facetSelection = {language: '', module_prefix: '', owner: ''};
  findingSelection = {severity: '', category: '', confidence: ''};
  view = 'runtime';
  offset = 0;
  filter.value = '';
  document.querySelectorAll('.nav').forEach(button => button.classList.toggle('active', button.dataset.view === view));
  await render();
  return true;
}

// A verified static handoff binds the session's own report, so a saved-run catalog
// selection must not survive it. A refused or superseded refresh restores that
// selection rather than leaving the report state half-switched.
function restoreOwnedReport() {
  // Handoff validation names the server's immutable initial snapshot. Restore it
  // synchronously: no aborted refresh may roll back a newer catalog selection.
  bootstrap = ownedBootstrap;
  runSelection = null;
  profileSelection = null;
  document.querySelector('#report-state').innerHTML = `${badge(bootstrap.reports[0].completeness)} ${escapeHtml(bootstrap.reports[0].target_root)}`;
}

async function followHandoff() {
  handoffController?.abort();
  candidateReviewSession = undefined;
  candidateReturnFocusSession = undefined;
  projectionHandoffRequest = undefined;
  runtimeHandoffRequest = undefined;
  const handoff = new URLSearchParams(location.hash.slice(1));
  if (handoff.has('profile_report_sha256')) return followRuntimeHandoff(handoff);
  if (!handoff.has('report_sha256')) return false;
  if (!draftSession.canNavigate()) return true;
  if (bootstrap?.reports[0]?.report_sha256 !== ownedBootstrap?.reports[0]?.report_sha256
      && !draftSession.allowSwitch('Change analysis collections? Dirty drafts remain in this session; export them before leaving.')) return true;
  renderController?.abort();
  handoffController = new AbortController();
  const signal = handoffController.signal;
  document.querySelector('#evidence-dialog').close();
  const selection = await api(`/api/handoff?${handoff}`, signal);
  if (signal.aborted) return true;
  restoreOwnedReport();
  await refreshWorkflow(signal);
  facetSelection = {language: '', module_prefix: '', owner: ''};
  findingSelection = {severity: '', category: '', confidence: ''};
  view = {report: 'overview', finding: 'findings', symbol: 'modules', graph: 'architecture', projection: 'explore'}[selection.kind];
  if (selection.kind === 'projection') projectionHandoffRequest = selection.graph_request;
  offset = 0;
  filter.value = '';
  document.querySelectorAll('.nav').forEach(button => button.classList.toggle('active', button.dataset.view === view));
  await render();
  if (!signal.aborted && selection.identity && selection.kind !== 'projection') await openEvidence(selection.kind === 'graph' ? 'navigation' : selection.kind, selection.identity);
  return true;
}
window.addEventListener('hashchange', () => {
  if (sessionClosed) return;
  // Origin history entries already passed handoff validation. Even an expired
  // return reference must not replay an old handoff after switching reports.
  if (Number.isSafeInteger(history.state?.codeAnalysisInvestigation)
      && history.state.codeAnalysisInvestigation > 0) return;
  if (bootstrap || runtimeSession) followHandoff().catch(showError);
});
window.addEventListener('popstate', () => {
  if (sessionClosed) return;
  if (refusedInvestigationBack) { refusedInvestigationBack = false; return; }
  if (history.state?.codeAnalysisFindingChoices) {
    const origin = currentInvestigation();
    if (!origin || origin.id !== history.state.codeAnalysisFindingChoices || !findingSourceContext) return;
    if (!draftSession.canNavigate() || !draftSession.allowSwitch('Return to saved source choices? Dirty drafts remain in this session; export them before leaving.')) {
      refusedInvestigationBack = true; history.forward(); return;
    }
    findingSourceContext.choosing = true;
    view = 'browse';
    document.querySelectorAll('.nav').forEach(button => button.classList.toggle('active', button.dataset.view === view));
    render().catch(showError);
    return;
  }
  if (history.state?.codeAnalysisInvestigation) {
    const origin = currentInvestigation();
    if (!origin || history.state.codeAnalysisInvestigation !== origin.id) return;
    returnToInvestigation({fromHistory: true}).then(accepted => {
      // A refused Back must leave the visible draft and the current Browse
      // history selection together. No render/abort while IME is active.
      if (!accepted && view === 'browse') { refusedInvestigationBack = true; history.forward(); }
    }).catch(showError);
    return;
  }
  if (!bootstrap || location.hash !== '#browse' || !history.state?.codeAnalysisBrowse) return;
  if (!draftSession.canNavigate()) return;
  if (findingSourceContext) findingSourceContext.choosing = false;
  view = 'browse';
  document.querySelectorAll('.nav').forEach(button => button.classList.toggle('active', button.dataset.view === view));
  render().catch(showError);
});

api('/api/collection').then(async context => {
  if (!context.report_available) {
    runtimeSession = context.runtime_available;
    view = context.runtime_available && (location.hash === '#runtime' || runtimeHandoffFragment())
      ? 'runtime'
      : 'overview';
    document.querySelector('#report-state').textContent = context.runtime_available
      ? 'Saved runtime evidence · no static report selected'
      : context.execution_available ? 'No report loaded · collection enabled' : 'No report loaded · preview only';
    document.querySelectorAll('.nav').forEach(button => {
      button.disabled = !['overview', 'collection', 'runs'].includes(button.dataset.view) && !(context.runtime_available && button.dataset.view === 'runtime');
      button.classList.toggle('active', button.dataset.view === view);
    });
    document.querySelector('.eyebrow').textContent = 'Collection command planner';
    await refreshWorkflow();
    if (runtimeHandoffFragment() && await followHandoff()) return;
    return render();
  }
  const payload = await api('/api/bootstrap');
  if (location.hash === '#runtime') view = 'runtime';
  if (location.hash === '#browse') view = 'browse';
  bootstrap = payload;
  ownedBootstrap = payload;
  await refreshWorkflow();
  document.querySelector("#report-state").innerHTML = `${badge(payload.reports[0].completeness)} ${escapeHtml(payload.reports[0].target_root)}`;
  if (await followHandoff()) return;
  return render();
}).catch(showError);
