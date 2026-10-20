/** Project-specific source permission and literal document views, composed with LocalOps. */
import {ContentPanel, button, element} from '/localops/index.js';
import {createCollectionPicker} from './collection_picker.js';

export function projectBrowser(host, api, post, signal, reportSha, {selection, canSwitch, onConnected}) {
  let context, catalogController, documentController, generation = 0, documentGeneration = 0;
  let documentRoot, historyIndex = -1;
  const history = [];
  const status = element('p', {className: 'hint', role: 'status'});
  const documents = element('div', {className: 'code-browser-project-documents'});
  const connect = button({label: 'Connect source folder', onClick: () => openSetup()});
  connect.disabled = true; // Permission capability must load before the action can be offered.
  host.classList.add('panel', 'code-browser-project-access');
  host.append(element('h2', {text: 'Source and project reading'}), status, connect, documents);

  const setup = element('section');
  const form = element('form', {className: 'project-source-form'});
  const directory = element('input', {type: 'text', value: '.', autocomplete: 'off', spellcheck: false});
  const directoryLabel = element('label', {text: 'Project source folder (relative to collection root)'}, directory);
  const rootLabel = element('p', {className: 'mono'});
  const targetLabel = element('p', {className: 'hint'});
  const consent = element('input', {type: 'checkbox'});
  const consentLabel = element('label', {className: 'project-source-consent'}, consent,
    element('span', {text: 'Allow read-only source and related text documents from this folder.'}));
  const error = element('p', {className: 'hint', role: 'alert'});
  const submit = button({label: 'Connect and display source'}); submit.type = 'submit';
  const browse = button({label: 'Browse source folders', onClick: () => picker?.open({
    title: 'Choose project source folder',
    purpose: 'Choose the project folder containing the report-relative source paths. Selection alone does not connect it.',
    start: directory.value || '.', directoryOnly: true,
    onChoose: path => {directory.value = path; consent.checked = false; consent.focus();},
  })});
  form.append(rootLabel, targetLabel, directoryLabel, browse, consentLabel, submit, error);
  setup.append(form);
  const sourcePanel = new ContentPanel({id: 'project-source-connection', title: 'Connect source folder',
    content: setup, fallbackFocus: document.querySelector('#title'),
    onOpenChange: open => {if (!open) consent.checked = false;}});
  let picker;

  const reading = element('section');
  const readerTitle = element('h3');
  const readerNotice = element('p', {className: 'hint'});
  const readerText = element('pre', {className: 'project-document-text', tabindex: 0});
  const readerLinks = element('nav', {'aria-label': 'Related project documents'});
  const previous = button({label: 'Previous project document', onClick: () => moveDocument(-1)});
  const next = button({label: 'Next project document', onClick: () => moveDocument(1)});
  reading.append(element('div', {className: 'collection-browser-actions'}, previous, next),
    readerTitle, readerNotice, readerText, readerLinks);
  const reader = new ContentPanel({id: 'project-document-reader', title: 'Project document — read only',
    content: reading, fallbackFocus: document.querySelector('#title'),
    onOpenChange: open => {if (!open) {documentController?.abort(); documentGeneration += 1;}}});

  function openSetup() {
    if (!context?.can_connect || !post || !canSwitch()) return;
    consent.checked = false; error.textContent = '';
    rootLabel.textContent = `Collection root: ${context.collection_root}`;
    targetLabel.textContent = `Choose a folder containing ${selection().path || 'the saved source files'}. If paths start with src/, choose its parent, not src itself. No scan or workload runs.`;
    picker ??= createCollectionPicker(setup, api, signal, {root: context.collection_root});
    sourcePanel.open({opener: connect, initialFocus: directory});
  }

  form.addEventListener('input', event => {if (event.target === directory) consent.checked = false;});
  form.addEventListener('submit', async event => {
    event.preventDefault(); error.textContent = '';
    if (!consent.checked) {error.textContent = 'Confirm read-only access before connecting.'; return;}
    if (!canSwitch()) return;
    submit.disabled = true;
    try {
      const result = await post('/api/project/source', {report_sha256: reportSha,
        directory: directory.value, logical_path: selection().path || '', confirmed: true}, signal);
      signal.throwIfAborted();
      if (result.report_sha256 !== reportSha || !result.configured) throw new Error('Source connection does not match this saved report.');
      reader.close(); sourcePanel.close();
      await update(selection().path || '');
      await onConnected();
    } catch (failure) {
      if (!signal.aborted) error.textContent = failure.message;
    } finally {submit.disabled = false; consent.checked = false;}
  });

  async function showDocument(entry, {record = true} = {}) {
    documentController?.abort(); documentController = new AbortController();
    const local = documentController.signal, revision = ++documentGeneration;
    readerTitle.textContent = entry.path;
    readerNotice.textContent = 'Reading current project text; this is not saved analysis evidence.';
    readerText.textContent = 'Loading project document…'; readerLinks.replaceChildren();
    try {
      const params = new URLSearchParams({report_sha256: reportSha, root_identity: documentRoot,
        path: entry.path, sha256: entry.sha256});
      const result = await api(`/api/project/document?${params}`, local);
      if (signal.aborted || local.aborted || revision !== documentGeneration) return;
      if (result.report_sha256 !== reportSha || result.root_identity !== documentRoot || result.sha256 !== entry.sha256) {
        throw new Error('Project document identity changed; reopen its document list.');
      }
      readerText.textContent = result.text;
      readerNotice.textContent = `${result.limitations.join(' ')} ${result.omitted_links} additional local link targets not inspected.`;
      for (const link of result.links) readerLinks.append(button({label: `Open ${link.label}`, onClick: () => showDocument(link)}));
      if (record) {
        history.splice(historyIndex + 1); history.push(entry);
        if (history.length > 50) history.shift();
        historyIndex = history.length - 1;
      }
      previous.disabled = historyIndex <= 0; next.disabled = historyIndex >= history.length - 1;
      reader.content.scrollTop = 0;
    } catch (failure) {
      if (!signal.aborted && !local.aborted) {
        readerText.textContent = `Document unavailable: ${failure.message}`;
        readerLinks.append(button({label: 'Retry project document', onClick: () => showDocument(entry, {record: false})}));
      }
    }
  }

  function moveDocument(delta) {
    const index = historyIndex + delta;
    if (index < 0 || index >= history.length) return;
    historyIndex = index; showDocument(history[index], {record: false});
  }

  async function update(path = '') {
    catalogController?.abort(); catalogController = new AbortController();
    const local = catalogController.signal, revision = ++generation;
    try {
      const base = new URLSearchParams({report_sha256: reportSha});
      const source = await api(`/api/project/source?${base}`, local);
      if (signal.aborted || local.aborted || revision !== generation) return;
      if (source.report_sha256 !== reportSha) throw new Error('Source setup belongs to another saved report.');
      context = source;
      status.textContent = source.configured ? `${source.message} Folder: ${source.source_root}` : source.message;
      connect.textContent = source.configured ? 'Change source folder' : 'Connect source folder';
      connect.disabled = !source.can_connect || !post;
      if (!source.can_connect || !post) status.textContent += ' This session has no folder-selection capability. Relaunch with --source-root PROJECT_FOLDER or --collection-root PARENT_FOLDER.';
      const params = new URLSearchParams({report_sha256: reportSha, path});
      const catalog = await api(`/api/project/documents?${params}`, local);
      if (signal.aborted || local.aborted || revision !== generation) return;
      if (catalog.report_sha256 !== reportSha) throw new Error('Document list belongs to another saved report.');
      documents.replaceChildren();
      if (documentRoot !== catalog.root_identity) {
        reader.close(); history.length = 0; historyIndex = -1;
        documentRoot = catalog.root_identity;
      }
      if (catalog.state === 'disabled') return;
      for (const entry of catalog.records) {
        const control = button({label: `Read ${entry.label}`, onClick: () => {
          reader.open({opener: control}); showDocument(entry);
        }});
        documents.append(control);
      }
      documents.append(element('p', {className: 'hint', text: catalog.records.length
        ? `Related current project documents; not analyzed code. ${catalog.omitted} documents omitted.`
        : 'No readable README was found beside this file or in its ancestor folders.'}));
    } catch (failure) {
      if (!signal.aborted && !local.aborted) status.textContent = `Source setup or document list unavailable: ${failure.message}. Saved code navigation is still available.`;
    }
  }

  signal.addEventListener('abort', () => {
    catalogController?.abort(); documentController?.abort();
    picker?.close(); sourcePanel.destroy(); reader.destroy();
  }, {once: true});
  return {update};
}
