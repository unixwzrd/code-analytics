import {CodeEditor} from './localops/index.js';

// Session-memory drafts only: no repository writes, autosave, workspace persistence or analysis.
const MAX_OPEN = 8;
const MAX_BYTES = 1024 * 1024;
const MAX_LINES = 20_000;
const SHA256_RE = /^[a-f0-9]{64}$/;
const NEWLINE_STYLES = new Set(['none', 'lf', 'crlf', 'mixed']);
const encoder = new TextEncoder();
let nextEditorSerial = 0;

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

function exactShape(value, required, optional = []) {
  const allowed = new Set([...required, ...optional]);
  if (!value || typeof value !== 'object' || Array.isArray(value)) throw new TypeError('invalid object shape');
  for (const key of required) {
    if (!Object.hasOwn(value, key)) throw new TypeError('invalid object shape');
  }
  for (const key of Object.keys(value)) {
    if (!allowed.has(key)) throw new TypeError('unexpected field in wire object');
  }
}

function strictBool(value, field) {
  if (value !== true && value !== false) throw new TypeError(`${field} must be a boolean`);
  return value;
}

function strictInt(value, field, min, max) {
  if (!Number.isSafeInteger(value) || value < min || value > max) {
    throw new TypeError(`${field} must be an integer within bounds`);
  }
  return value;
}

function strictString(value, field) {
  if (typeof value !== 'string') throw new TypeError(`${field} must be a string`);
  return value;
}

function sessionKey(reportSha256, logicalPath, sourceSha256) {
  return `${reportSha256}\0${logicalPath}\0${sourceSha256}`;
}

function allocateEditorId() {
  nextEditorSerial += 1;
  return `draft-${nextEditorSerial}`;
}

function hasLoneSurrogates(text) {
  for (const char of text) {
    const point = char.codePointAt(0);
    if (point >= 0xd800 && point <= 0xdfff) return true;
  }
  return false;
}

function logicalLineCount(text) {
  return text.split('\n').length;
}

function withinBounds(text) {
  const bytes = encoder.encode(text).length;
  return bytes <= MAX_BYTES && logicalLineCount(text) <= MAX_LINES && !hasLoneSurrogates(text);
}

async function sha256Hex(text) {
  const digest = await crypto.subtle.digest('SHA-256', encoder.encode(text));
  return [...new Uint8Array(digest)].map(byte => byte.toString(16).padStart(2, '0')).join('');
}

/** Python splitlines() line count for wire validation. */
export function splitLinesCount(text) {
  if (text === '') return 0;
  let index = 0;
  let count = 0;
  while (index < text.length) {
    count += 1;
    while (index < text.length) {
      const char = text[index];
      if (char === '\r') {
        index += text[index + 1] === '\n' ? 2 : 1;
        break;
      }
      if (
        char === '\n' ||
        char === '\v' ||
        char === '\f' ||
        char === '\x1c' ||
        char === '\x1d' ||
        char === '\x1e' ||
        char === '\u0085' ||
        char === '\u2028' ||
        char === '\u2029'
      ) {
        index += 1;
        break;
      }
      index += 1;
    }
  }
  return count;
}

function documentNewlineStyle(text) {
  const breaks = new Set();
  for (let index = 0; index < text.length; index += 1) {
    if (text[index] === '\r' && text[index + 1] === '\n') {
      breaks.add('\r\n');
      index += 1;
    } else if (text[index] === '\r' || text[index] === '\n') {
      breaks.add(text[index]);
    }
  }
  if (breaks.size === 0) return 'none';
  if (breaks.size === 1 && breaks.has('\n')) return 'lf';
  if (breaks.size === 1 && breaks.has('\r\n')) return 'crlf';
  return 'mixed';
}

function hasFinalNewlineWire(text) {
  return text.endsWith('\n') || text.endsWith('\r');
}

/** Verified CRLF/LF originals become LF in the widget; mixed/bare CR stay read-only. */
function normalizeEditorText(text, newlineStyle) {
  if (newlineStyle === 'crlf') return text.replace(/\r\n/g, '\n');
  if (newlineStyle === 'lf' || newlineStyle === 'none') return text;
  return text.replace(/\r\n/g, '\n').replace(/\r/g, '\n');
}

function terminalBreakPresent(body, newlineStyle) {
  return newlineStyle === 'crlf' ? body.endsWith('\r\n') : body.endsWith('\n');
}

function stripOneTerminalBreak(body, newlineStyle) {
  if (newlineStyle === 'crlf' && body.endsWith('\r\n')) return body.slice(0, -2);
  if (newlineStyle !== 'crlf' && body.endsWith('\n')) return body.slice(0, -1);
  return body;
}

/** Restore export bytes using recorded newline metadata without trimming terminal blank runs. */
export function serializeDraftText(lfText, {newlineStyle, hasFinalNewline}) {
  if (!NEWLINE_STYLES.has(newlineStyle)) throw new TypeError('invalid newline style');
  if (newlineStyle === 'mixed') throw new TypeError('mixed-newline originals must be exported unchanged');
  strictBool(hasFinalNewline, 'hasFinalNewline');
  if (typeof lfText !== 'string' || lfText.includes('\r')) {
    throw new TypeError('draft export requires normalized LF text');
  }
  let body = newlineStyle === 'crlf' ? lfText.replace(/\n/g, '\r\n') : lfText;
  const breakSeq = newlineStyle === 'crlf' ? '\r\n' : '\n';
  if (hasFinalNewline) {
    if (!terminalBreakPresent(body, newlineStyle)) body += breakSeq;
  } else if (terminalBreakPresent(body, newlineStyle)) {
    body = stripOneTerminalBreak(body, newlineStyle);
    if (terminalBreakPresent(body, newlineStyle)) {
      throw new Error(
        'draft text still ends with a line break after removing one; edit the draft to match the original final-newline setting',
      );
    }
  }
  return body;
}

function exportTextForEntry(entry, snapshot) {
  if (!entry.editable) return entry.originalText;
  return serializeDraftText(snapshot.text, entry);
}

function safeBasename(logicalPath) {
  const leaf = logicalPath.split('/').pop() || 'draft.txt';
  const cleaned = leaf.replace(/[^\w.\-()+@]+/g, '_').replace(/^\.+/, '_') || 'draft.txt';
  return cleaned.endsWith('.txt') ? cleaned : `${cleaned}.txt`;
}

export class DraftSession {
  constructor() {
    this._entries = new Map();
    this._navigationGuards = new Set();
    this.wrapLines = false;
  }

  registerNavigationGuard(guard) {
    this._navigationGuards.add(guard);
    return () => this._navigationGuards.delete(guard);
  }

  canNavigate() {
    return [...this._navigationGuards].every(guard => guard());
  }

  static key(reportSha256, logicalPath, sourceSha256) {
    return sessionKey(reportSha256, logicalPath, sourceSha256);
  }

  size() {
    return this._entries.size;
  }

  atCapacity() {
    return this._entries.size >= MAX_OPEN;
  }

  hasDirty() {
    if (!this.canNavigate()) return true;
    for (const entry of this._entries.values()) if (entry.dirty) return true;
    return false;
  }

  /** Native confirm only when dirty; never discards on its own. */
  allowSwitch(message) {
    if (!this.hasDirty()) return true;
    return globalThis.confirm(message);
  }

  /** Discard every open draft after explicit confirmation (caller or native confirm). */
  discardAll({confirmed = false, message = 'Discard all open drafts? They cannot be recovered after this session.'} = {}) {
    const approved = confirmed || globalThis.confirm(message);
    if (!approved) return false;
    this._entries.clear();
    return true;
  }

  /** Clear session memory after a confirmed collection switch. */
  clear() {
    this._entries.clear();
  }

  get(key) {
    return this._entries.get(key) ?? null;
  }

  list() {
    return [...this._entries.values()].map(entry => ({
      key: entry.key,
      logicalPath: entry.logicalPath,
      sourceSha256: entry.sourceSha256,
      dirty: entry.dirty,
    }));
  }

  remember(entry) {
    if (this._entries.size >= MAX_OPEN && !this._entries.has(entry.key)) {
      throw new Error('draft session is full; close or discard an open draft explicitly');
    }
    this._entries.set(entry.key, entry);
  }

  remove(key) {
    this._entries.delete(key);
  }

  detachedSnapshot(entry) {
    if (!entry?.draftSnapshot) return null;
    return structuredClone(entry.draftSnapshot);
  }
}

async function validateBrowseDocument(payload, expected) {
  exactShape(payload, ['report_sha256', 'revision', 'document']);
  const reportSha256 = strictString(payload.report_sha256, 'report_sha256');
  const revision = strictInt(payload.revision, 'revision', 0, Number.MAX_SAFE_INTEGER);
  if (!SHA256_RE.test(reportSha256)) throw new TypeError('report_sha256 must be a 64-char hex digest');
  if (reportSha256 !== expected.reportSha256 || revision !== expected.revision) {
    throw new Error('document response disagrees with the selected report or revision');
  }
  const document = payload.document;
  exactShape(
    document,
    ['logical_path', 'expected_sha256', 'state', 'message'],
    ['text', 'editable', 'newline_style', 'has_final_newline', 'line_count', 'byte_count'],
  );
  const logicalPath = strictString(document.logical_path, 'logical_path');
  const expectedSha = strictString(document.expected_sha256, 'expected_sha256');
  const state = strictString(document.state, 'state');
  strictString(document.message, 'message');
  if (!SHA256_RE.test(expectedSha)) throw new TypeError('expected_sha256 must be a 64-char hex digest');
  if (logicalPath !== expected.path) throw new Error('document response path does not match the requested file');
  if (state !== 'bound') throw new Error(document.message || 'full document is unavailable');
  const text = strictString(document.text, 'text');
  const editable = strictBool(document.editable, 'editable');
  const newlineStyle = strictString(document.newline_style, 'newline_style');
  const hasFinalNewline = strictBool(document.has_final_newline, 'has_final_newline');
  const lineCount = strictInt(document.line_count, 'line_count', 0, MAX_LINES);
  const byteCount = strictInt(document.byte_count, 'byte_count', 0, MAX_BYTES);
  if (!NEWLINE_STYLES.has(newlineStyle)) throw new TypeError('newline_style is out of range');
  if (byteCount !== encoder.encode(text).length) throw new Error('document byte count mismatch');
  if (lineCount !== splitLinesCount(text)) throw new Error('document line count mismatch');
  const inferredStyle = documentNewlineStyle(text);
  if (newlineStyle !== inferredStyle) throw new Error('document newline metadata disagrees with original text');
  if (hasFinalNewline !== hasFinalNewlineWire(text)) {
    throw new Error('document final-newline metadata disagrees with original text');
  }
  if (editable !== (newlineStyle !== 'mixed')) {
    throw new Error('mixed or unsupported newline sequences must stay read-only');
  }
  const editorText = normalizeEditorText(text, newlineStyle);
  if (!withinBounds(editorText)) throw new Error('document exceeds draft bounds or contains invalid Unicode');
  const digest = await sha256Hex(text);
  if (digest !== expectedSha) throw new Error('document hash does not match disclosed source identity');
  return {
    logical_path: logicalPath,
    expected_sha256: expectedSha,
    text,
    editable,
    newline_style: newlineStyle,
    has_final_newline: hasFinalNewline,
    line_count: lineCount,
    byte_count: byteCount,
  };
}

export function mountDraftPane(host, options, session) {
  const {
    api,
    signal,
    reportSha256,
    path,
    revision,
    identity = '',
    sourceAvailable = true,
    onDirtyChange,
    findings,
    selectedRange,
  } = options;
  if (!host || !api || !signal || !reportSha256 || !path || revision === undefined || !session) {
    throw new TypeError('mountDraftPane requires host, api, signal, reportSha256, path, revision and session');
  }

  const root = element('section', undefined, 'code-drafts');
  root.setAttribute('aria-label', 'Session draft editor');
  const status = element('p', 'Draft—not analyzed. Full source loads only after Edit draft.', 'hint code-drafts-status');
  status.setAttribute('role', 'status');
  status.setAttribute('aria-live', 'polite');
  const identityLine = element('div', undefined, 'mono code-drafts-identity');
  const warning = element('p',
    'Drafts exist only in this browser session. Closing or refreshing loses them and resets editor history on reopen.',
    'hint code-drafts-warning');
  const openList = element('ul', undefined, 'code-drafts-open');
  openList.setAttribute('aria-label', 'Open session drafts');
  const toolbar = element('div', undefined, 'code-drafts-toolbar');
  const editButton = action('Edit draft', () => void openDraft());
  editButton.setAttribute('aria-label', 'Edit draft from verified original source');
  editButton.disabled = !sourceAvailable;
  if (!sourceAvailable) status.textContent = 'Original source is unavailable. Previously opened drafts remain session-only and can be exported; no new original is loaded.';
  toolbar.append(editButton);
  const editorHost = element('div', undefined, 'code-drafts-editor');
  root.append(status, identityLine, warning, toolbar, openList, editorHost);
  host.replaceChildren(root);

  let editor;
  let composing = false;
  let activeKey = null;
  let loadGeneration = 0;
  let dirtyNotified = false;
  const listeners = [];

  function listen(node, kind, callback, capture = false) {
    node.addEventListener(kind, callback, capture);
    listeners.push([node, kind, callback, capture]);
  }

  function canNavigate() {
    if (composing) setStatus('Finish IME composition before navigating away from this draft.');
    return !composing;
  }

  const unregisterNavigationGuard = session.registerNavigationGuard(canNavigate);

  function setStatus(message) {
    status.textContent = message;
  }

  function entryMatchesBrowse(entry) {
    return entry && entry.reportSha256 === reportSha256 && entry.logicalPath === path;
  }

  function refreshIdentity() {
    const entry = activeKey ? session.get(activeKey) : null;
    if (entry) {
      const details = element('details');
      details.append(element('summary', 'Report and original source identity'),
        element('p', `report ${entry.reportSha256} · source ${entry.sourceSha256}`, 'mono'));
      identityLine.replaceChildren(element('span', `Draft—not analyzed · path ${entry.logicalPath}`), details);
      return;
    }
    identityLine.textContent = `Draft—not analyzed · report ${reportSha256} · path ${path}`;
  }

  refreshIdentity();

  function notifyDirty(dirty) {
    dirtyNotified = dirty;
    onDirtyChange?.(dirty);
  }

  function notifyDirtyForBrowse() {
    const entry = activeKey ? session.get(activeKey) : null;
    notifyDirty(Boolean(entry?.dirty && entryMatchesBrowse(entry)));
  }

  function renderOpenList() {
    openList.replaceChildren();
    for (const item of session.list()) {
      const row = element('li');
      const label = element('span', `${item.logicalPath} · ${item.sourceSha256.slice(0, 12)}…`);
      const open = action('Open draft', () => void switchDraft(item.key));
      open.setAttribute('aria-label', `Open draft for ${item.logicalPath}`);
      const close = action('Close draft', () => void closeDraft(item.key, {discard: false}));
      close.setAttribute('aria-label', `Close draft for ${item.logicalPath}`);
      const discard = action('Discard draft', () => void closeDraft(item.key, {discard: true}));
      discard.setAttribute('aria-label', `Discard draft for ${item.logicalPath}`);
      if (item.dirty) label.append(element('strong', ' (dirty)'));
      row.append(label, open, close, discard);
      openList.append(row);
    }
  }

  function detachActiveSnapshot() {
    if (!editor || !activeKey) return;
    const entry = session.get(activeKey);
    if (!entry) return;
    captureEntrySnapshot(entry);
  }

  function destroyEditor() {
    if (!editor) return;
    if (!composing) detachActiveSnapshot();
    composing = false;
    editor.destroy();
    editor = null;
    editorHost.replaceChildren();
  }

  function listenComposition() {
    listen(editorHost, 'compositionstart', () => { composing = true; }, true);
    listen(editorHost, 'compositionend', () => { composing = false; }, true);
  }

  function captureEntrySnapshot(entry) {
    if (!editor || activeKey !== entry.key) return;
    const snapshot = editor.getSnapshot();
    entry.draftSnapshot = structuredClone({
      documentId: snapshot.documentId,
      revision: snapshot.revision,
      text: snapshot.text,
      selection: snapshot.selection,
    });
    entry.dirty = snapshot.dirty;
  }

  function applySnapshotToEditor(entry, {discardChanges = false} = {}) {
    const readOnly = !entry.editable;
    const document = {
      id: entry.editorId,
      revision: entry.revision,
      text: entry.baselineLfText,
      readOnly,
    };
    const decision = discardChanges ? {discardChanges: true} : {};
    try {
      editor.setDocument(document, decision);
    } catch (error) {
      if (!discardChanges && String(error.message).includes('dirty')) {
        if (!session.allowSwitch('Discard visible edits and show the selected draft?')) return false;
        editor.setDocument(document, {discardChanges: true});
      } else throw error;
    }
    if (!readOnly && entry.draftSnapshot && entry.draftSnapshot.revision === entry.revision) {
      editor.setDraft(structuredClone(entry.draftSnapshot));
    }
    notifyDirtyForBrowse();
    return true;
  }

  function applyOriginalAnnotations(entry) {
    if (!editor || !entryMatchesBrowse(entry) || !findings
        || findings.report_sha256 !== entry.reportSha256 || findings.path !== entry.logicalPath
        || findings.source_sha256 !== entry.sourceSha256 || editor.getSnapshot().dirty) return;
    // Retained observations are reusable only on this freshly verified clean
    // original. No asynchronous result is relabelled with a new editor token.
    const context = editor.getAnnotationContext();
    const annotations = findings.records.map(item => ({id: item.annotation_id,
      label: item.label, description: item.description,
      startLine: item.start_line, endLine: item.end_line, tone: item.severity}));
    const result = editor.setAnnotations({context, annotations});
    if (!result.ok) setStatus(`Original annotations unavailable: ${result.error.message}`);
  }

  function mountEditor(entry, {replace = true} = {}) {
    if (replace) destroyEditor();
    if (!entry.editorId) entry.editorId = allocateEditorId();
    editor = new CodeEditor({
      id: entry.editorId,
      label: `Draft for ${entry.logicalPath}`,
      onChange: snapshot => {
        if (composing) return;
        entry.draftSnapshot = structuredClone({
          documentId: snapshot.documentId,
          revision: snapshot.revision,
          text: snapshot.text,
          selection: snapshot.selection,
        });
        entry.dirty = snapshot.dirty;
        notifyDirtyForBrowse();
        renderOpenList();
        if (!snapshot.dirty) applyOriginalAnnotations(entry);
      },
      onCopy: async snapshot => {
        try {
          const text = exportTextForEntry(entry, snapshot);
          await navigator.clipboard.writeText(text);
          setStatus('Draft copied with original newline settings.');
        } catch (error) {
          setStatus(`Copy failed: ${error.message}`);
        }
      },
      onExport: snapshot => {
        try {
          const text = exportTextForEntry(entry, snapshot);
          const blob = new Blob([text], {type: 'text/plain;charset=utf-8'});
          const url = URL.createObjectURL(blob);
          const anchor = element('a');
          anchor.href = url;
          anchor.download = safeBasename(entry.logicalPath);
          anchor.click();
          globalThis.setTimeout(() => URL.revokeObjectURL(url), 0);
          setStatus('Draft downloaded; repository source remains unchanged.');
        } catch (error) {
          setStatus(`Download failed: ${error.message}`);
        }
      },
    });
    editorHost.append(editor.root);
    applySnapshotToEditor(entry);
    const wrap = editor.root.querySelector('label input[type="checkbox"]');
    if (wrap.checked !== session.wrapLines) wrap.click();
    listen(wrap, 'change', () => { session.wrapLines = wrap.checked; });
    applyOriginalAnnotations(entry);
    if (selectedRange && entryMatchesBrowse(entry) && !editor.getSnapshot().dirty) {
      const end = logicalLineCount(entry.baselineLfText);
      const marker = findings?.records.find(item => item.start_line <= selectedRange.startLine
        && item.end_line >= selectedRange.startLine);
      const revealed = marker && editor.revealAnnotation({context: editor.getAnnotationContext(),
        id: marker.annotation_id}, {focus: false});
      if (!revealed?.ok) editor.revealLines({startLine: Math.min(selectedRange.startLine, end),
        endLine: Math.min(selectedRange.endLine, end)}, {focus: false});
    }
  }

  async function switchDraft(key) {
    if (!canNavigate()) {
      setStatus('Finish IME composition before switching drafts.');
      return;
    }
    if (activeKey === key) return;
    const entry = session.get(key);
    if (!entry) return;
    const previousKey = activeKey;
    const current = previousKey ? session.get(previousKey) : null;
    if (current) captureEntrySnapshot(current);
    if (current?.dirty && !session.allowSwitch(
      'Switch drafts? The current draft stays in memory; editor history resets when you return.',
    )) {
      return;
    }
    destroyEditor();
    activeKey = key;
    mountEditor(entry, {replace: false});
    refreshIdentity();
    setStatus(`Editing draft for ${entry.logicalPath} (Draft—not analyzed).`);
    renderOpenList();
  }

  async function closeDraft(key, {discard}) {
    if (!canNavigate()) {
      setStatus('Finish IME composition before closing this draft.');
      return;
    }
    const entry = session.get(key);
    if (!entry) return;
    if (discard) {
      if (!globalThis.confirm(`Discard draft for ${entry.logicalPath}? This cannot be undone.`)) return;
    } else if (entry.dirty) {
      if (!globalThis.confirm(
        `Close draft for ${entry.logicalPath}? Unsaved edits will be lost and the draft slot will be freed.`,
      )) return;
    }
    session.remove(key);
    if (activeKey === key) {
      activeKey = null;
      destroyEditor();
      refreshIdentity();
      setStatus('Draft closed. Verified original source is unchanged.');
    }
    notifyDirtyForBrowse();
    renderOpenList();
  }

  function applyVerifiedDocument(entry, document) {
    // The widget revision identifies its immutable original baseline, not a
    // browse request. A verified same-hash reload must not invalidate a draft.
    entry.originalText = document.text;
    entry.baselineLfText = normalizeEditorText(document.text, document.newline_style);
    entry.newlineStyle = document.newline_style;
    entry.hasFinalNewline = document.has_final_newline;
    entry.editable = document.editable;
  }

  async function openDraft() {
    if (signal.aborted || !sourceAvailable) return;
    if (!canNavigate()) {
      setStatus('Finish IME composition before loading a draft.');
      return;
    }
    const generation = ++loadGeneration;
    const params = new URLSearchParams({
      report_sha256: reportSha256,
      revision: String(revision),
      path,
    });
    if (identity) params.set('identity', identity);
    setStatus('Loading verified original for draft…');
    try {
      const payload = await api(`/api/code-browser/document?${params}`, signal);
      if (signal.aborted || generation !== loadGeneration) return;
      const document = await validateBrowseDocument(payload, {reportSha256, revision, path});
      if (signal.aborted || generation !== loadGeneration) return;
      if (!canNavigate()) return;
      const key = sessionKey(reportSha256, document.logical_path, document.expected_sha256);
      const existing = session.get(key);
      if (!existing && session.atCapacity()) {
        setStatus(`Already ${MAX_OPEN} open drafts. Close or discard one explicitly before opening another.`);
        renderOpenList();
        return;
      }
      if (existing?.dirty && activeKey === key) {
        if (!session.allowSwitch('Reload verified original? Current draft edits are kept unless you discard them first.')) {
          return;
        }
      } else if (existing?.dirty && activeKey !== key) {
        if (!session.allowSwitch('Open cached dirty draft? Other dirty drafts remain in this session.')) return;
      }
      const entry = existing ?? {
        key,
        reportSha256,
        logicalPath: document.logical_path,
        sourceSha256: document.expected_sha256,
        revision,
        identity,
        originalText: document.text,
        baselineLfText: normalizeEditorText(document.text, document.newline_style),
        newlineStyle: document.newline_style,
        hasFinalNewline: document.has_final_newline,
        editable: document.editable,
        editorId: allocateEditorId(),
        draftSnapshot: null,
        dirty: false,
      };
      applyVerifiedDocument(entry, document);
      session.remember(entry);
      await switchDraft(key);
      if (!document.editable) {
        setStatus('Verified original is read-only (mixed or unsupported newlines). Draft—not analyzed.');
      } else {
        setStatus(`Draft open for ${document.logical_path}. Analytical findings remain bound to the original.`);
      }
      renderOpenList();
    } catch (error) {
      if (!signal.aborted) setStatus(`Draft unavailable: ${error.message}`);
    }
  }

  function destroy() {
    signal.removeEventListener('abort', destroy);
    unregisterNavigationGuard();
    loadGeneration += 1;
    if (composing) {
      setStatus('Navigation aborted during IME composition; last committed draft snapshot retained.');
    } else if (activeKey) {
      const entry = session.get(activeKey);
      if (entry) {
        try {
          captureEntrySnapshot(entry);
        } catch (error) {
          setStatus(`Draft detach failed: ${error.message}`);
        }
      }
    }
    destroyEditor();
    for (const [node, kind, callback, capture] of listeners) {
      node.removeEventListener(kind, callback, capture);
    }
    listeners.length = 0;
    host.replaceChildren();
  }

  signal.addEventListener('abort', destroy, {once: true});
  listenComposition();
  renderOpenList();

  return {destroy, openDraft, switchDraft, closeDraft, canNavigate};
}
