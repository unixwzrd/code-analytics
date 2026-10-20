/** Consumer-owned topic routing and trusted bundled-document loading. */
import {ContentPanel, button, element} from '/localops/index.js';

const DOCUMENTS = new Set(['getting-started', 'user-guide', 'cli-reference', 'examples', 'reports']);

export function installUserHelp() {
  let catalog;
  let selection;
  let documentReady = false;
  let controller;
  let revision = 0;
  let cursor = -1;
  const history = [];
  const positions = new Map();
  const root = element('section', {className: 'user-help-reader'});
  const controls = element('div', {className: 'help-controls'});
  const documents = element('select', {'aria-label': 'Documentation topic'});
  const search = element('input', {type: 'search', placeholder: 'Search topic titles and headings',
    'aria-label': 'Search documentation titles and headings', 'aria-describedby': 'help-search-scope'});
  const results = element('div', {className: 'help-results', 'aria-live': 'polite'});
  const toc = element('nav', {'aria-label': 'Document table of contents'});
  const article = element('article', {className: 'user-help-document help-document', 'aria-live': 'polite'});
  const back = button({label: 'Previous help topic', onClick: () => move(-1)});
  const forward = button({label: 'Next help topic', onClick: () => move(1)});
  const fullDocument = button({label: 'Browse full documentation', onClick: () => {
    if (selection) show(selection.document_id, '', {mode: 'full'});
  }});
  const scope = element('p', {className: 'hint', 'aria-live': 'polite'});
  controls.append(back, forward, fullDocument, documents, search);
  root.append(controls, element('p', {id: 'help-search-scope', className: 'hint',
    text: 'Search covers document titles and headings, not full document text.'}), scope, results, toc, article);
  const panel = new ContentPanel({id: 'user-help', title: 'Code Analysis guide', content: root,
    closeLabel: 'Close help', expandLabel: 'Expand help', restoreLabel: 'Restore help width',
    fallbackFocus: document.querySelector('#title'),
    onOpenChange: open => {
      if (!open) {savePosition(); controller?.abort(); revision += 1;}
    }});

  function savePosition() {
    if (selection && documentReady) {
      positions.set(positionKey(selection), panel.content.scrollTop);
      if (positions.size > 50) positions.delete(positions.keys().next().value);
    }
  }

  function positionKey(entry) {
    return JSON.stringify([entry.document_id, entry.mode, entry.anchor]);
  }

  function sectionContent(source, anchor) {
    const target = source.querySelector(`[id="${CSS.escape(anchor)}"]`);
    if (!target) throw new Error('This guide section is not in the packaged documentation.');
    const heading = target.closest('h1,h2,h3,h4,h5,h6');
    if (!heading) throw new Error('This guide section has no validated heading.');
    const level = Number(heading.tagName.slice(1));
    const next = [...source.querySelectorAll('h1,h2,h3,h4,h5,h6')].find(item =>
      Boolean(heading.compareDocumentPosition(item) & Node.DOCUMENT_POSITION_FOLLOWING) &&
      Number(item.tagName.slice(1)) <= level);
    // A DOM range preserves nested lists/tables and never reparses source text
    // or flattens markup to guess section boundaries.
    const range = source.ownerDocument.createRange();
    range.selectNodeContents(source);
    range.setStartBefore(heading);
    if (next) range.setEndBefore(next);
    return range.cloneContents();
  }

  function updateHistory() {
    back.disabled = cursor <= 0;
    forward.disabled = cursor < 0 || cursor >= history.length - 1;
  }

  function fail(message, retry) {
    article.replaceChildren(element('h3', {text: 'Help is unavailable'}),
      element('p', {text: `${message} Your analysis view and evidence selection are unchanged.`}),
      button({label: 'Retry help', onClick: retry}));
    toc.replaceChildren();
  }

  async function loadCatalog(signal) {
    if (catalog) return catalog;
    const response = await fetch('/help/index.json', {signal});
    if (!response.ok) throw new Error('Packaged documentation could not be loaded.');
    const value = await response.json();
    signal.throwIfAborted();
    if (!Array.isArray(value.documents) || value.documents.length !== DOCUMENTS.size ||
        value.documents.some(item => !DOCUMENTS.has(item.document_id)) ||
        new Set(value.documents.map(item => item.document_id)).size !== DOCUMENTS.size) {
      throw new Error('Packaged documentation index is invalid.');
    }
    catalog = value;
    documents.replaceChildren();
    for (const item of catalog.documents) {
      documents.append(element('option', {value: item.document_id, text: `${item.category}: ${item.title}`}));
    }
    return catalog;
  }

  function headingEntries(item) {
    return item.headings.map(heading => ({document_id: item.document_id,
      anchor: heading.anchor, title: `${item.title} — ${heading.title}`}));
  }

  async function show(document_id, anchor = '', {record = true, restore = false,
    mode = anchor ? 'section' : 'full'} = {}) {
    if (!DOCUMENTS.has(document_id)) return;
    savePosition();
    documentReady = false;
    controller?.abort();
    controller = new AbortController();
    const signal = controller.signal;
    const current = ++revision;
    fullDocument.disabled = true;
    scope.textContent = '';
    delete article.dataset.helpMode;
    article.replaceChildren(element('p', {text: 'Loading packaged guide…'}));
    toc.replaceChildren();
    try {
      const index = await loadCatalog(signal);
      const definition = index.documents.find(item => item.document_id === document_id);
      const response = await fetch(`/help/${document_id}.html`, {signal});
      if (!response.ok) throw new Error('This packaged guide could not be loaded.');
      const text = await response.text();
      signal.throwIfAborted();
      if (current !== revision) return;
      // Only server-validated, build-generated allowlisted assets enter this reader.
      const parsed = new DOMParser().parseFromString(text, 'text/html');
      const source = parsed.querySelector('main') || parsed.body;
      article.replaceChildren(...(mode === 'section' ? sectionContent(source, anchor).childNodes : source.childNodes));
      for (const link of article.querySelectorAll('a[href]')) {
        const url = new URL(link.getAttribute('href'), `${location.origin}/help/${document_id}.html`);
        if (!['https:', 'http:', 'mailto:'].includes(url.protocol)) {link.removeAttribute('href'); continue;}
        link.href = url.href;
        if (url.origin !== location.origin) {link.target = '_blank'; link.rel = 'noopener noreferrer';}
      }
      for (const heading of headingEntries(definition)) {
        if (!article.querySelector(`[id="${CSS.escape(heading.anchor)}"]`)) continue;
        toc.append(button({label: heading.title.split(' — ').slice(1).join(' — '),
          onClick: () => show(heading.document_id, heading.anchor)}));
      }
      selection = {document_id, anchor, mode};
      documentReady = true;
      fullDocument.disabled = mode === 'full';
      scope.textContent = mode === 'section' ? 'Focused guide section. Browse full documentation for the complete reference.' : 'Full reference document.';
      article.dataset.helpMode = mode;
      documents.value = document_id;
      if (record) {
        history.splice(cursor + 1);
        history.push(selection);
        if (history.length > 50) history.shift();
        cursor = history.length - 1;
      }
      updateHistory();
      const target = anchor ? article.querySelector(`[id="${CSS.escape(anchor)}"]`) : null;
      if (restore && positions.has(positionKey(selection))) panel.content.scrollTop = positions.get(positionKey(selection));
      else if (target) target.scrollIntoView({block: 'start'});
      else panel.content.scrollTop = 0;
    } catch (error) {
      if (signal.aborted || current !== revision) return;
      fail(error.message, () => show(document_id, anchor, {record, restore, mode}));
    }
  }

  function move(delta) {
    const next = cursor + delta;
    if (next < 0 || next >= history.length) return;
    cursor = next;
    const entry = history[cursor];
    show(entry.document_id, entry.anchor, {record: false, restore: true, mode: entry.mode});
    updateHistory();
  }

  documents.addEventListener('change', () => show(documents.value));
  search.addEventListener('input', () => {
    results.replaceChildren();
    const query = search.value.trim().toLocaleLowerCase();
    if (!query || !catalog) return;
    const matches = catalog.documents.flatMap(item => [{document_id: item.document_id,
      anchor: '', title: item.title}, ...headingEntries(item)]).filter(item => item.title.toLocaleLowerCase().includes(query));
    results.append(element('p', {className: 'hint', text: `${matches.length} topic/heading matches; showing at most 100.`}));
    for (const match of matches.slice(0, 100)) results.append(button({label: match.title,
      onClick: () => show(match.document_id, match.anchor)}));
  });

  root.addEventListener('click', event => {
    const link = event.target.closest('a[href]');
    if (!link) return;
    const url = new URL(link.href);
    const id = url.pathname.match(/^\/help\/([a-z-]+)\.html$/)?.[1];
    if (url.origin === location.origin && DOCUMENTS.has(id)) {
      // Loading replaces the article synchronously. Keep this internal click
      // from reaching the global opener with an already-detached link.
      event.preventDefault(); event.stopPropagation();
      show(id, decodeURIComponent(url.hash.slice(1)));
    }
  });
  document.addEventListener('click', event => {
    const link = event.target.closest('a[href]');
    if (!link || root.contains(link)) return;
    const url = new URL(link.href);
    const id = url.pathname.match(/^\/help\/([a-z-]+)\.html$/)?.[1];
    if (url.origin !== location.origin || !DOCUMENTS.has(id)) return;
    event.preventDefault();
    panel.open({opener: link});
    const anchor = decodeURIComponent(url.hash.slice(1));
    const keepLast = link.hasAttribute('data-global-help') && selection;
    const firstGlobal = link.hasAttribute('data-global-help') && !selection;
    show(keepLast ? selection.document_id : firstGlobal ? 'user-guide' : id,
      keepLast ? selection.anchor : firstGlobal ? 'your-first-investigation' : anchor,
      {record: !keepLast, restore: Boolean(keepLast),
        mode: keepLast ? selection.mode : firstGlobal || anchor ? 'section' : 'full'});
  });
  updateHistory();
  return panel;
}
