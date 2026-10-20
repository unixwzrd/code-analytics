import {wireGraphDisplay} from './projections.js';
import {DraftSession, mountDraftPane} from './code_drafts.js';

// This view presents backend-owned identities and containment. It never parses
// source, invents call relationships, writes a repository or persists text.
const HISTORY_KEY = 'codeAnalysisBrowse';
const BROWSE_PANES = new Set(['outline', 'map', 'source']);
const SESSION_FIELDS = ['report_sha256', 'path', 'identity', 'search', 'offset', 'line',
  'revision', 'layer', 'collapsed', 'active_pane'];

export function codeBrowserSelectionMetadata(session) {
  return Object.fromEntries(SESSION_FIELDS.filter(key => Object.hasOwn(session, key))
    .map(key => [key, session[key]]));
}

function element(tag, text, className) {
  const node = document.createElement(tag);
  if (text !== undefined) node.textContent = text;
  if (className) node.className = className;
  return node;
}

function action(label, callback) {
  const button = element('button', label, 'detail-button');
  button.type = 'button';
  button.addEventListener('click', callback);
  return button;
}

function shell(content, activePane, onPaneChange) {
  const root = element('div', undefined, 'code-browser');
  const toolbar = element('div', undefined, 'code-browser-toolbar');
  const panels = new Map();
  for (const [key, title] of [['outline', 'Files and entities'], ['map', 'Recorded relationships'], ['source', 'Verified original source']]) {
    const panel = element('section', undefined, `panel code-browser-${key}`);
    panel.setAttribute('aria-label', title);
    panel.dataset.pane = key;
    const heading = element('h2', title); heading.tabIndex = -1;
    const body = element('div', undefined, 'code-browser-pane');
    panel.append(heading, body); root.append(panel);
    panels.set(key, body);
    const button = action(title, () => {
      root.dataset.activePane = key;
      onPaneChange(key);
      heading.focus();
    });
    toolbar.append(button);
  }
  root.dataset.activePane = activePane;
  root.prepend(toolbar);
  const status = element('p', 'Loading saved file inventory…', 'hint code-browser-status');
  status.setAttribute('role', 'status');
  content.replaceChildren(status, root);
  return {root, panels, status};
}

function entryButton(entry, selected, select) {
  const button = action(`${entry.label} · ${entry.kind}`, () => select(entry));
  button.dataset.browseIdentity = entry.identity;
  button.setAttribute('aria-pressed', String(entry.identity === selected));
  button.title = `${entry.logical_path} · ${entry.language} · ${entry.identity}`;
  return button;
}

function renderOutline(host, page, selection, select, navigate) {
  const label = element('label', 'Search saved files / entities');
  const search = element('input'); search.type = 'search'; search.value = selection.search || '';
  search.maxLength = 256; search.setAttribute('aria-label', 'Search saved files / entities');
  label.append(search);
  const form = element('form');
  form.append(label, action('Search', () => {}));
  form.lastChild.type = 'submit';
  form.addEventListener('submit', event => {
    event.preventDefault(); navigate({search: search.value, offset: 0});
  });
  host.replaceChildren(form);
  if (page.logical_path) host.append(action('Return to files', () => navigate({path: '', identity: '', line: null, search: '', offset: 0})));
  host.append(element('p', page.logical_path || 'Saved source files', 'mono'));
  const list = element('ul', undefined, 'code-browser-list');
  for (const entry of page.records) {
    const item = element('li');
    item.append(entryButton(entry, page.selected?.identity, select));
    if (entry.parent_relation) item.append(element('small', `${entry.parent_relation}: ${entry.parent_identity}`, 'hint'));
    list.append(item);
  }
  host.append(list);
  if (!page.records.length) host.append(element('p', 'No saved entries match this selection.', 'hint'));
  const pager = element('div', undefined, 'code-browser-pager');
  const previous = action('Previous entries', () => navigate({offset: Math.max(0, page.offset - page.limit)}));
  previous.disabled = page.offset === 0;
  const next = action('Next entries', () => navigate({offset: page.next_offset}));
  next.disabled = page.next_offset === null;
  pager.append(previous, next);
  host.append(element('p', `${page.records.length} entries shown of ${page.eligible}; paging is not analysis truncation.`, 'hint'), pager);
}

const GRAPH_LAYERS = [
  ['containment', 'Containment (not ownership)'], ['imports', 'Imports'],
  ['static-calls', 'Recorded callee names (not runtime dispatch)'],
  ['static-call-sites', 'Static call-site containment'], ['data-flow', 'Dictionary data flow'],
  ['runtime-calls', 'Runtime calls (observed workload only)'],
];

function graphPositions(nodes, edges, containment) {
  if (!containment) return new Map(nodes.map((node, index) => {
    const angle = index * 2 * Math.PI / Math.max(1, nodes.length);
    return [node.node_id, {x: 430 + 280 * Math.cos(angle), y: 340 + 270 * Math.sin(angle)}];
  }));
  const parents = new Map(edges.map(edge => [edge.target_id, edge.source_id]));
  return new Map(nodes.map((node, index) => {
    const seen = new Set([node.node_id]); let parent = parents.get(node.node_id), depth = 0;
    while (parent && !seen.has(parent)) { seen.add(parent); depth += 1; parent = parents.get(parent); }
    return [node.node_id, {x: 30 + depth * 40, y: 36 + index * 52}];
  }));
}

function collapsedNodes(graph, collapsed) {
  const children = new Map();
  for (const edge of graph.edges) {
    if (!children.has(edge.source_id)) children.set(edge.source_id, []);
    children.get(edge.source_id).push(edge.target_id);
  }
  const hidden = new Set(), stack = [...collapsed].flatMap(id => children.get(id) || []);
  while (stack.length) {
    const id = stack.pop(); if (hidden.has(id)) continue;
    hidden.add(id); stack.push(...(children.get(id) || []));
  }
  return hidden;
}

function relationshipMap(host, result, session, navigate, signal) {
  host.replaceChildren(element('p', result.message, 'hint'));
  for (const limitation of result.limitations) host.append(element('p', limitation, 'hint'));
  if (result.state !== 'available') return;
  const graph = result.graph, containment = result.layer === 'containment';
  const collapsed = new Set(session.collapsed || []);
  const hidden = containment ? collapsedNodes(graph, collapsed) : new Set();
  const nodes = graph.nodes.filter(node => !hidden.has(node.node_id));
  const edges = graph.edges.filter(edge => !hidden.has(edge.source_id) && !hidden.has(edge.target_id));
  const positions = graphPositions(nodes, edges, containment);
  const links = new Map(result.links.map(link => [link.node_id, link]));
  const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
  svg.setAttribute('viewBox', containment ? `0 0 780 ${Math.max(120, nodes.length * 52 + 35)}` : '0 0 1160 720');
  svg.setAttribute('role', 'img'); svg.setAttribute('aria-label', `Saved ${result.layer} map`);
  const markerId = `code-browser-arrow-${result.revision}`;
  const defs = document.createElementNS(svg.namespaceURI, 'defs');
  const marker = document.createElementNS(svg.namespaceURI, 'marker');
  for (const [name, value] of Object.entries({id: markerId, viewBox: '0 0 10 10', refX: 9, refY: 5, markerWidth: 6, markerHeight: 6, orient: 'auto'})) marker.setAttribute(name, value);
  const arrow = document.createElementNS(svg.namespaceURI, 'path'); arrow.setAttribute('d', 'M 0 0 L 10 5 L 0 10 z'); arrow.setAttribute('fill', 'currentColor');
  marker.append(arrow); defs.append(marker); svg.append(defs);
  for (const edge of edges) {
    const source = positions.get(edge.source_id), target = positions.get(edge.target_id);
    const line = document.createElementNS(svg.namespaceURI, 'path');
    const length = Math.hypot(target.x - source.x, target.y - source.y);
    const inset = length ? Math.min(12 / length, 0.3) : 0;
    line.setAttribute('d', length ? `M ${source.x} ${source.y} L ${target.x - (target.x - source.x) * inset} ${target.y - (target.y - source.y) * inset}` : `M ${source.x} ${source.y} c 45 -45 45 45 0 0`);
    line.setAttribute('fill', 'none'); line.setAttribute('class', 'graph-edge');
    if (graph.direction === 'directed') line.setAttribute('marker-end', `url(#${markerId})`);
    const title = document.createElementNS(svg.namespaceURI, 'title'); title.textContent = `${edge.source_id} → ${edge.target_id}: ${edge.label || edge.kind} · ${edge.resolution}`;
    line.append(title); svg.append(line);
  }
  const card = element('div', undefined, 'code-browser-node-card'); card.setAttribute('role', 'status');
  function inspect(node) {
    for (const group of svg.querySelectorAll('[data-code-graph-node]')) {
      group.setAttribute('aria-pressed', String(group.getAttribute('data-code-graph-node') === node.node_id));
    }
    card.replaceChildren(element('h3', node.label), element('p', `${node.kind} · ${node.node_id}`, 'mono'));
    const link = links.get(node.node_id);
    if (link) card.append(action('Browse recorded source', () => navigate({identity: link.identity,
      path: link.path || '', line: link.line ?? null, offset: 0, search: ''})));
    else card.append(element('p', 'No exact saved source identity is bound to this node. No target is inferred from its name.', 'hint'));
    if (containment && graph.edges.some(edge => edge.source_id === node.node_id)) card.append(action(collapsed.has(node.node_id) ? 'Expand children' : 'Collapse children', () => {
      if (collapsed.has(node.node_id)) collapsed.delete(node.node_id); else collapsed.add(node.node_id);
      session.collapsed = [...collapsed].sort(); relationshipMap(host, result, session, navigate, signal);
    }));
  }
  for (const node of nodes) {
    const position = positions.get(node.node_id);
    const group = document.createElementNS(svg.namespaceURI, 'g');
    group.setAttribute('class', 'graph-node'); group.setAttribute('role', 'button'); group.setAttribute('tabindex', '0');
    group.setAttribute('aria-label', `Inspect ${node.label}`); group.dataset.codeGraphNode = node.node_id;
    const hit = document.createElementNS(svg.namespaceURI, 'rect');
    for (const [name, value] of Object.entries({x: position.x - 10, y: position.y - 16, width: Math.min(55, node.label.length) * 8.5 + 34, height: 32, fill: 'transparent', 'pointer-events': 'all'})) hit.setAttribute(name, value);
    const circle = document.createElementNS(svg.namespaceURI, 'circle');
    circle.setAttribute('cx', position.x); circle.setAttribute('cy', position.y); circle.setAttribute('r', '7');
    const text = document.createElementNS(svg.namespaceURI, 'text');
    text.setAttribute('x', position.x + 18); text.setAttribute('y', position.y + 5);
    text.textContent = node.label.length > 55 ? `${node.label.slice(0, 54)}…` : node.label;
    const title = document.createElementNS(svg.namespaceURI, 'title'); title.textContent = node.label;
    group.append(hit, circle, text, title);
    group.addEventListener('click', () => inspect(node));
    group.addEventListener('keydown', event => { if (['Enter', ' '].includes(event.key)) { event.preventDefault(); inspect(node); } });
    svg.append(group);
  }
  const viewport = element('div', undefined, 'graph-wrap'); viewport.append(svg);
  host.append(element('p', `${graph.nodes.length}/${graph.original_node_count} nodes; ${graph.edges.length}/${graph.original_edge_count} edges retained. Omitted: ${graph.omitted_node_count} nodes, ${graph.omitted_edge_count} edges. ${result.completeness}; ${graph.truncated ? 'bounded partial output' : 'no display-bound omissions'}. ${hidden.size} retained nodes hidden by display collapse.`, 'hint'));
  if (result.profile_report_sha256) host.append(element('p', `Observed profile: ${result.profile_report_sha256} · workload identity: ${result.profile_identity_sha256}. This is not exhaustive program coverage.`, 'hint'));
  for (const limitation of graph.limitations) host.append(element('p', limitation, 'hint'));
  if (containment) host.append(action('Expand all retained nodes', () => { session.collapsed = []; relationshipMap(host, result, session, navigate, signal); }));
  host.append(viewport, card);
  const selectedNode = session.identity && nodes.find(node => links.get(node.node_id)?.identity === session.identity);
  if (selectedNode) inspect(selectedNode);
  const details = element('details'); details.append(element('summary', 'Accessible relationship table'));
  const table = element('table'), head = element('thead'), header = element('tr');
  for (const name of ['Source', 'Target', 'Relationship', 'Resolution', 'Confidence']) { const cell = element('th', name); cell.scope = 'col'; header.append(cell); }
  head.append(header); table.append(head);
  const labels = new Map(graph.nodes.map(node => [node.node_id, node.label]));
  const rows = element('tbody');
  for (const edge of graph.edges) {
    const row = element('tr');
    for (const value of [labels.get(edge.source_id), labels.get(edge.target_id), edge.label || edge.kind, edge.resolution, edge.confidence || 'not recorded']) row.append(element('td', value));
    rows.append(row);
  }
  table.append(rows); const tableWrap = element('div', undefined, 'table-wrap'); tableWrap.append(table);
  details.append(tableWrap); host.append(details);
  wireGraphDisplay(host, signal);
}

function sourceLines(host, response, selected, navigate) {
  const preview = response.preview;
  host.replaceChildren(element('p', `${preview.state}: ${preview.message}`, 'hint'));
  if (preview.state !== 'bound') return;
  const code = element('div', undefined, 'code-browser-lines');
  code.setAttribute('role', 'region'); code.setAttribute('aria-label', 'Verified source lines');
  for (const line of preview.lines) {
    const row = element('div', undefined, 'code-browser-line');
    const button = action(String(line.number), () => navigate({line: line.number, identity: '', offset: 0}));
    button.setAttribute('aria-label', `Select original line ${line.number}`);
    const active = selected?.spans.some(span => span.path === preview.logical_path && span.start_line <= line.number && span.end_line >= line.number);
    if (active) row.dataset.selected = 'true';
    const text = element('code', line.text);
    row.append(button, text); code.append(row);
  }
  host.append(code);
  const pager = element('div', undefined, 'code-browser-pager');
  const start = preview.lines[0]?.number || 1;
  const previous = action('Previous source lines', () => navigate({line: Math.max(1, start - 200), identity: ''}));
  previous.disabled = start <= 1;
  const next = action('Next source lines', () => navigate({line: preview.next_line, identity: ''}));
  next.disabled = preview.next_line === null;
  pager.append(previous, next); host.append(pager);
}

function detailsForSelection(host, page, select) {
  const selected = page.selected || (page.line_matches.length === 1 ? page.line_matches[0] : null);
  if (page.selection_locations.length > 1) {
    host.append(element('h3', 'Recorded source locations'));
    for (const entry of page.selection_locations) {
      for (const span of entry.spans) host.append(action(`${span.path}:${span.start_line}–${span.end_line}`, () => select(entry, span)));
    }
    host.append(element('p', `${page.selection_locations.length} of ${page.selection_location_count} recorded file locations shown.`, 'hint'));
  }
  if (selected) {
    host.append(element('h3', selected.label), element('p', `${selected.kind} · ${selected.module} · ${selected.language}`, 'hint'));
    host.append(element('p', `Saved identity: ${selected.identity}`, 'mono'));
    const locations = element('div', undefined, 'code-browser-pager');
    for (const span of selected.spans) locations.append(action(`${span.path}:${span.start_line}–${span.end_line}`, () => select(selected, span)));
    host.append(locations);
  }
  if (page.line_matches.length > 1) {
    host.append(element('h3', 'Choose an equally specific recorded entity'));
    for (const entry of page.line_matches) host.append(entryButton(entry, null, select));
  }
}

export async function codeBrowser(content, api, signal, reportSha256, session = {}, drafts = new DraftSession()) {
  if (session.report_sha256 !== reportSha256) Object.assign(session, {report_sha256: reportSha256, path: '', identity: '', search: '', offset: 0, line: null, revision: 0, layer: 'containment', collapsed: [], active_pane: 'outline'});
  const saved = history.state?.[HISTORY_KEY];
  if (saved?.report_sha256 === reportSha256 && location.hash === '#browse') Object.assign(session, codeBrowserSelectionMetadata(saved));
  if (!BROWSE_PANES.has(session.active_pane)) session.active_pane = session.path ? 'source' : 'outline';
  let paneIntent = 0;
  const {root, panels, status} = shell(content, session.active_pane, key => {
    session.active_pane = key; paneIntent += 1; remember(true);
  });
  let pending;
  signal.addEventListener('abort', () => pending?.abort(), {once: true});

  function remember(replace = false) {
    const value = {[HISTORY_KEY]: codeBrowserSelectionMetadata(session)};
    history[replace ? 'replaceState' : 'pushState'](value, '', '#browse');
  }
  async function select(entry, span = entry.spans[0]) {
    await navigateSource({path: span?.path || entry.logical_path, identity: entry.identity, line: span?.start_line || 1, offset: 0, search: ''});
  }
  async function navigateSource(patch) {
    const intent = paneIntent;
    const accepted = await navigate({...patch, active_pane: 'source'});
    if (!accepted || signal.aborted || !root.isConnected || intent !== paneIntent) return;
    root.dataset.activePane = 'source'; remember(true);
    panels.get('source').parentElement.querySelector('h2').focus();
  }
  async function loadRelationships(page, params, current, local) {
    const host = panels.get('map'); host.replaceChildren();
    const label = element('label', 'Relationship layer');
    const layer = element('select'); layer.setAttribute('aria-label', 'Relationship layer');
    for (const [value, title] of GRAPH_LAYERS) { const option = element('option', title); option.value = value; layer.append(option); }
    layer.value = session.layer || 'containment'; label.append(layer); host.append(label);
    layer.addEventListener('change', () => navigate({layer: layer.value}));
    const body = element('div'); host.append(body);
    if (!page.logical_path) { body.append(element('p', 'Choose a single recorded source location for a focused graph.', 'hint')); return; }
    const graphParams = new URLSearchParams({report_sha256: reportSha256, revision: params.get('revision'), path: page.logical_path, identity: session.identity || '', layer: layer.value, depth: 1, max_nodes: 100, max_edges: 250});
    body.append(element('p', 'Loading saved relationships…', 'hint'));
    try {
      let profile;
      if (layer.value === 'runtime-calls') {
        profile = await api('/api/runtime', local);
        if (signal.aborted || local.aborted) return;
        if (!profile.available) { body.replaceChildren(element('p', 'No explicitly selected saved runtime profile. Static relationship layers remain available.', 'hint')); return; }
        graphParams.set('profile_report_sha256', profile.profile_report_sha256);
        graphParams.set('profile_identity_sha256', profile.profile_identity);
      }
      const result = await api(`/api/code-browser/graph?${graphParams}`, local);
      const boundProfile = !profile || (result.profile_report_sha256 === profile.profile_report_sha256
        && result.profile_identity_sha256 === profile.profile_identity);
      if (signal.aborted || local.aborted) return;
      if (!current(result) || result.layer !== layer.value || !boundProfile) {
        throw new Error('Saved graph identities disagree with the selected report, profile or revision');
      }
      relationshipMap(body, result, session, navigateSource, local);
    } catch (error) {
      if (!signal.aborted && !local.aborted) body.replaceChildren(element('p', `Relationship layer unavailable: ${error.message}. Other saved views remain available.`, 'hint'));
    }
  }
  async function navigate(patch = {}, recordHistory = true) {
    if (signal.aborted || !root.isConnected) return false;
    if (!drafts.canNavigate()) return false;
    if (patch.path !== undefined && patch.path !== session.path
        && !drafts.allowSwitch('Switch original files? Dirty drafts stay in this session; export them before leaving.')) return false;
    Object.assign(session, patch); session.revision += 1;
    pending?.abort(); pending = new AbortController();
    const local = pending.signal;
    const revision = session.revision;
    const params = new URLSearchParams({report_sha256: reportSha256, revision, path: session.path || '', identity: session.identity || '', search: session.search || '', offset: session.offset || 0, limit: 50});
    if (session.line !== null && session.line !== undefined) params.set('line', session.line);
    const current = response => !signal.aborted && !local.aborted && response.report_sha256 === reportSha256 && response.revision === revision;
    status.textContent = 'Loading saved outline…';
    try {
      const page = await api(`/api/code-browser?${params}`, local);
      if (signal.aborted || local.aborted || !root.isConnected) return false;
      if (!current(page)) throw new Error('Saved outline identity disagrees with the selected report or revision');
      const active = page.selected || (page.line_matches.length === 1 ? page.line_matches[0] : null);
      const displayed = {...page, selected: active};
      session.path = page.logical_path || '';
      root.dataset.activePane = session.active_pane;
      if (recordHistory) remember();
      status.textContent = `${page.completeness} saved evidence · report ${reportSha256} · source remains read-only`;
      renderOutline(panels.get('outline'), displayed, session, select, navigate);
      const graphLoading = loadRelationships(page, params, current, local);
      const source = panels.get('source'); source.replaceChildren();
      detailsForSelection(source, page, select);
      for (const limitation of page.limitations) source.append(element('p', limitation, 'hint'));
      if (!page.logical_path) { source.append(element('p', 'Select a saved file or entity. Source requires explicit --source-root.', 'hint')); return true; }
      const body = element('div'); source.append(body);
      body.append(element('p', 'Checking original source identity…', 'hint'));
      const sourceParams = new URLSearchParams({report_sha256: reportSha256, revision, path: page.logical_path, line: session.line || page.selected?.spans[0]?.start_line || 1});
      try {
        const response = await api(`/api/code-browser/source?${sourceParams}`, local);
        if (signal.aborted || local.aborted || !root.isConnected) return false;
        if (current(response)) {
          sourceLines(body, response, active, navigateSource);
          if (response.preview.state === 'bound' || drafts.size() > 0) {
            const draftHost = element('div'); source.insertBefore(draftHost, body);
            const mapping = element('p', '', 'hint'); source.insertBefore(mapping, body);
            const highlighted = [...body.querySelectorAll('[data-selected="true"]')];
            mountDraftPane(draftHost, {api, signal: local, reportSha256,
              path: page.logical_path, revision, identity: session.identity || '',
              sourceAvailable: response.preview.state === 'bound',
              onDirtyChange: dirty => {
                for (const row of highlighted) {
                  if (dirty) delete row.dataset.selected;
                  else row.dataset.selected = 'true';
                }
                mapping.textContent = dirty
                  ? 'Draft—not analyzed. Original-span highlighting and draft-line mapping are disabled; the verified original and saved analytics remain unchanged.'
                  : '';
              }}, drafts);
          }
        }
        else {
          body.replaceChildren(element('p', 'Source unavailable: saved preview identity disagrees with the selected report or revision. Saved analytics remain available.', 'hint'));
        }
      } catch (error) {
        if (!signal.aborted && !local.aborted) body.replaceChildren(element('p', `Source unavailable: ${error.message}. Saved analytics remain available.`, 'hint'));
      }
      await graphLoading;
      return !signal.aborted && !local.aborted && root.isConnected;
    } catch (error) {
      if (!signal.aborted && !local.aborted) status.textContent = `Unable to load this selection: ${error.message}`;
      return false;
    }
  }
  remember(true);
  const accepted = await navigate({}, false);
  if (accepted && saved?.report_sha256 === reportSha256 && !signal.aborted
      && root.isConnected && document.activeElement === document.body) {
    panels.get(session.active_pane).parentElement.querySelector('h2').focus();
  }
}
