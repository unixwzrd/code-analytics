import {graphView, wireGraphDisplay} from './projections.js';
import {disclosure, element} from '/localops/index.js';

const formats = ['json', 'json-compact', 'markdown', 'dot', 'mermaid', 'svg', 'png', 'html'];

export function dependencyExplorer(content, api, request, escape, signal, openEvidence) {
  const source = content.dataset.dependencySource || '';
  content.innerHTML = `<section class="panel investigation-setup"><h2>Investigate a dependency</h2>
    <p class="hint">Choose a source module and a target to trace a saved import path, or inspect the source's mutual component (SCC). Select an overview node below to use it as the source.</p>
    <form id="dependency-controls" class="projection-controls investigation-controls">
    <label>Source logical module<input name="source" required value="${escape(source)}" placeholder="package.module"></label>
    <label>Mode<select name="mode" aria-label="Dependency mode"><option value="path">Path to target</option><option value="component">Mutual component (SCC)</option></select></label>
    <label>Target logical module<input name="target" aria-label="Target logical module" required placeholder="package.target"><span id="dependency-target-note" class="hint">Required for a path to target.</span></label>
    <label>Traversal nodes<input name="max_nodes" type="number" min="1" max="2000" value="500"></label>
    <label>Traversal edges<input name="max_edges" type="number" min="1" max="20000" value="5000"></label>
    <label>Graph nodes<input name="graph_max_nodes" type="number" min="1" max="100" value="100"></label>
    <label>Graph edges<input name="graph_max_edges" type="number" min="0" max="250" value="250"></label>
    <div class="investigation-submit"><button class="detail-button" type="submit">Apply investigation</button></div>
  </form><p class="hint">Saved local import evidence only. External, unresolved, and runtime dependencies are not inferred.</p></section><section id="dependency-result" aria-live="polite"></section>`;
  const form = content.querySelector('#dependency-controls');
  const result = content.querySelector('#dependency-result');
  const bounds = element('div', {className: 'investigation-bounds'});
  for (const name of ['max_nodes', 'max_edges', 'graph_max_nodes', 'graph_max_edges']) {
    bounds.append(form.elements[name].closest('label'));
  }
  bounds.append(element('p', {className: 'hint', text: 'Traversal bounds limit the search. Graph bounds limit the saved-evidence drawing and its downloads. Reaching a bound remains explicit partial evidence.'}));
  const summary = element('p', {className: 'hint', id: 'dependency-bounds-summary'});
  const advanced = disclosure({title: 'Advanced settings', content: bounds});
  form.addEventListener('invalid', event => {
    if (advanced.contains(event.target)) advanced.open = true;
  }, true);
  const submit = form.querySelector('.investigation-submit');
  submit.before(summary, advanced);
  const summarize = () => {
    summary.textContent = `Traversal: ${form.elements.max_nodes.value} nodes / ${form.elements.max_edges.value} edges. Graph: ${form.elements.graph_max_nodes.value} nodes / ${form.elements.graph_max_edges.value} edges.`;
  };
  summarize();
  let pending;
  signal.addEventListener('abort', () => pending?.abort(), {once: true});
  const mode = form.elements.mode, target = form.elements.target;
  const updateMode = () => {
    const component = mode.value === 'component';
    target.disabled = component; target.required = !component;
    if (component) target.value = '';
    target.setAttribute('aria-describedby', 'dependency-target-note');
    content.querySelector('#dependency-target-note').textContent = component
      ? 'Not needed: a mutual component uses the source module only.' : 'Required for a path to target.';
  };
  const invalidate = () => {
    summarize();
    pending?.abort(); pending = undefined;
    if (!signal.aborted) result.textContent = 'Controls changed. Apply investigation to refresh saved dependency evidence.';
  };
  mode.addEventListener('change', () => { updateMode(); invalidate(); });
  form.addEventListener('input', invalidate);
  form.addEventListener('change', event => { if (event.target !== mode) invalidate(); });

  async function download(params, format, currentSignal) {
    const response = await request(`/api/dependencies/download?${params}&format=${format}`, currentSignal);
    const blob = await response.blob(); currentSignal.throwIfAborted();
    const url = URL.createObjectURL(blob), anchor = document.createElement('a');
    anchor.href = url; anchor.download = `dependencies.${format}`; anchor.click();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  }

  async function load() {
    pending?.abort(); pending = new AbortController();
    const currentSignal = pending.signal;
    if (signal.aborted) return;
    const params = new URLSearchParams(new FormData(form));
    for (const [key, value] of [...params]) if (!value) params.delete(key);
    result.textContent = 'Investigating bounded dependencies…';
    try {
      const investigation = await api(`/api/dependencies?${params}`, currentSignal);
      currentSignal.throwIfAborted();
      const graph = await api(`/api/dependencies/graph?${params}`, currentSignal);
      currentSignal.throwIfAborted();
      const identities = investigation.request.mode === 'path' ? investigation.path : investigation.members;
      const heading = investigation.request.mode === 'path' ? 'Retained path' : 'Proven mutual component';
      const finding = identities.length
        ? `${identities.length} retained module${identities.length === 1 ? '' : 's'}.`
        : investigation.request.mode === 'path' && !investigation.truncated && investigation.source_completeness === 'complete'
          ? 'No local path found.'
          : 'No path evidenced; absence remains unresolved with incomplete source or traversal.';
      result.innerHTML = `<h2>${heading}</h2><p>Source completeness: <strong>${escape(investigation.source_completeness)}</strong> · ${investigation.visited_nodes} visited nodes / ${investigation.examined_edges} examined edges · ${investigation.truncated ? 'Traversal bound reached' : 'Traversal finished'}.</p><p>${escape(finding)}</p>${investigation.limitations.map(item => `<p class="hint">${escape(item)}</p>`).join('')}<div class="table-wrap"><table><thead><tr><th>${investigation.request.mode === 'path' ? 'Step' : 'Module'}</th><th>Logical module</th></tr></thead><tbody>${identities.map((item, index) => `<tr><td>${investigation.request.mode === 'path' ? index : ''}</td><td><button class="detail-button" data-dependency-module="${escape(item)}">${escape(item)}</button></td></tr>`).join('')}</tbody></table></div><div class="downloads">${formats.map(format => `<button class="detail-button" data-dependency-format="${format}">Download ${format.toUpperCase()}</button>`).join('')}</div><div id="dependency-graph">${graphView(graph, escape)}</div><p id="dependency-status" role="status"></p>`;
      wireGraphDisplay(result, currentSignal);
      result.querySelectorAll('[data-dependency-module]').forEach(node => node.addEventListener('click', () => openEvidence('navigation', node.dataset.dependencyModule)));
      result.querySelectorAll('[data-projection-node]').forEach(node => {
        const select = () => openEvidence('navigation', node.dataset.projectionNode);
        node.addEventListener('click', select);
        node.addEventListener('keydown', event => { if (event.key === 'Enter' || event.key === ' ') { event.preventDefault(); select(); } });
      });
      result.querySelectorAll('[data-dependency-format]').forEach(button => button.addEventListener('click', async () => {
        const status = result.querySelector('#dependency-status');
        try { await download(params, button.dataset.dependencyFormat, currentSignal); status.textContent = `Downloaded ${button.dataset.dependencyFormat.toUpperCase()} dependency graph.`; }
        catch (error) { if (error.name !== 'AbortError') status.textContent = error.message; }
      }));
    } catch (error) {
      if (error.name !== 'AbortError' && !signal.aborted) result.textContent = `Dependency evidence unavailable: ${error.message}`;
    }
  }
  form.addEventListener('submit', event => { event.preventDefault(); load(); });
  updateMode();
  return {load, setSource(value) { form.elements.source.value = value; invalidate(); }};
}
