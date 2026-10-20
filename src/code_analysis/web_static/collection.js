/** Collection form with opt-in, explicitly confirmed backend jobs. */
import {collectionJobs} from './collection_jobs.js';
import {disclosure, element} from '/localops/index.js';

const OPERATIONS = {
  scan: "Static analysis scan of the selected target. Does not execute project code.",
  clones: "Clone and commonality collection for the selected target. Does not execute project code.",
  refactor: "Refactor proposal collection for the selected target. Does not execute project code.",
  profile:
    "cProfile runtime collection for a script or module. Preview never runs the workload; authorization only affects the generated command.",
  "import-profile": "Import saved cProfile/pstats evidence without executing application code. Missing capture provenance stays partial.",
};

const STATIC_OPS = new Set(["scan", "clones", "refactor"]);
const OPERATION_LABELS = {scan: 'Static analysis (scan)', clones: 'Find duplication (clones)',
  refactor: 'Review refactoring opportunities (refactor)', profile: 'Collect a Python profile',
  'import-profile': 'Import saved Python profiles'};
const LANGUAGES = ["auto", "python", "javascript", "ruby"];
const STRICTNESS = ["normal", "lenient", "strict"];
const DEFAULTS = Object.freeze({
  operation: "scan",
  interpreter_id: "current",
  target: ".",
  cwd: ".",
  languages: ["auto"],
  strictness: "normal",
  exclude: [],
  workspace: null,
  source_report: null,
  workload_kind: "script",
  workload: null,
  arguments: [],
  timeout: 300,
  repeat: 1,
  expected_status: "SUCCESS",
  allow_execution: false,
});

function lines(value) {
  return String(value || "")
    .split("\n")
    .map((line) => line.trim())
    .filter(Boolean);
}

function blankToNull(value) {
  const text = String(value ?? "");
  return text ? text : null;
}

function optionList(values, selected, labels = {}) {
  return values
    .map(
      (value) =>
        `<option value="${value}"${value === selected ? " selected" : ""}>${labels[value] || value}</option>`,
    )
    .join("");
}

function pathField(id, label, value, browseTarget) {
  return `<div class="collection-path-field">
    <label for="${id}">${label}
      <input id="${id}" name="${id}" type="text" value="${value}" autocomplete="off" spellcheck="false">
    </label>
    <button type="button" class="detail-button collection-browse" data-browse="${browseTarget}" aria-label="Browse for ${label}">Browse…</button>
  </div>`;
}

function staticFields() {
  return `<fieldset class="collection-fieldset" data-mode="static">
    <legend>Static collection</legend>
    ${pathField("target", "Target path", ".", "target")}
    <label for="language">Language
      <select id="language" name="language" aria-label="Language">${optionList(LANGUAGES, "auto")}</select>
    </label>
    <label for="strictness">Strictness
      <select id="strictness" name="strictness" aria-label="Strictness">${optionList(STRICTNESS, "normal")}</select>
    </label>
    <label for="exclude">Exclude directory names (one per line, exact names, not globs)
      <textarea id="exclude" name="exclude" rows="3" spellcheck="false" placeholder="tests&#10;vendor"></textarea>
    </label>
    ${pathField("cwd", "Working directory (cwd)", ".", "cwd")}
    ${pathField("workspace", "Workspace (optional relative path)", "", "workspace")}
  </fieldset>`;
}

function runtimeFields() {
  return `<fieldset class="collection-fieldset" data-mode="runtime" hidden>
    <legend>Runtime collection</legend>
    ${pathField("source_report", "Saved static source report (required)", "", "source_report")}
    <fieldset class="collection-inline">
      <legend>Workload kind</legend>
      <label><input type="radio" name="workload_kind" value="script" checked> Script (relative path)</label>
      <label><input type="radio" name="workload_kind" value="module"> Module (dotted name)</label>
    </fieldset>
    ${pathField("workload", "Workload", "", "workload")}
    <label for="arguments">Arguments (one per line; spaces preserved, blank lines are empty arguments)
      <textarea id="arguments" name="arguments" rows="3" spellcheck="false"></textarea>
    </label>
    ${pathField("runtime_cwd", "Working directory (cwd)", ".", "cwd")}
    ${pathField("runtime_workspace", "Workspace (optional relative path)", "", "workspace")}
    <label for="timeout">Timeout (seconds)
      <input id="timeout" name="timeout" type="number" min="1" value="300">
    </label>
    <label for="repeat">Repetitions (1–20)
      <input id="repeat" name="repeat" type="number" min="1" max="20" value="1">
    </label>
    <label for="expected_status">Expected status (symbolic name)
      <input id="expected_status" name="expected_status" type="text" value="SUCCESS" autocomplete="off" spellcheck="false">
    </label>
    <label class="collection-auth" for="allow_execution">
      <input id="allow_execution" name="allow_execution" type="checkbox">
      <span>I authorize including <code>--allow-execution</code> in the generated profile command.
      Building a preview does not run it. Execution requires a separately confirmed browser launch (when enabled) or running the copied command in a terminal.</span>
    </label>
    <p class="hint">cProfile does not imply worker-thread, subprocess, or GPU coverage. Preview never executes the workload.</p>
  </fieldset>`;
}

function importFields() {
  return `<fieldset class="collection-fieldset" data-mode="import" hidden>
    <legend>Import saved profiles</legend>
    <label for="profiles">Saved profile paths (one per line, relative to collection root)
      <textarea id="profiles" name="profiles" rows="3" spellcheck="false"></textarea>
    </label>
    <button type="button" class="detail-button collection-browse" data-browse="profiles">Browse and add profile…</button>
    ${pathField("import_metadata", "Capture metadata (optional)", "", "import_metadata")}
    ${pathField("import_source", "Saved static source report (optional)", "", "import_source")}
    ${pathField("import_workspace", "Existing owned workspace (required for managed import)", "", "import_workspace")}
    <p class="hint">Original profiles remain unchanged. Importing does not prove who captured them or which threads ran. A matching source report enables correlation; diagnostic views remain useful without one. Workspace writes require confirmation, not workload-execution authorization.</p>
  </fieldset>`;
}

function shell() {
  return `<section class="collection-view" aria-label="Collection planning">
    <p class="collection-banner" role="note">Preview only — copy this command to your terminal. No workload runs in this screen yet.</p>
    <p id="collection-continuity" class="hint">Ordinary settings stay in this tab while you investigate. Returning clears execution authorization and requires a fresh command preview.</p>
    <dl class="collection-meta panel">
      <dt>Host</dt><dd class="mono" data-meta="host"></dd>
      <dt>Collection root</dt><dd class="mono" data-meta="root"></dd>
      <dt>Interpreter</dt><dd class="mono" data-meta="interpreter"></dd>
      <dt>Reports available</dt><dd data-meta="reports"></dd>
    </dl>
    <form id="collection-form" class="collection-form panel" novalidate>
      <h2>1. Choose what to collect</h2>
      <label for="operation">Operation
        <select id="operation" name="operation" aria-label="Operation">${optionList(Object.keys(OPERATIONS), "scan", OPERATION_LABELS)}</select>
      </label>
      <p id="operation-help" class="hint" aria-live="polite">${OPERATIONS.scan}</p>
      ${staticFields()}
      ${runtimeFields()}
      ${importFields()}
      <label for="interpreter_id">Trusted interpreter
        <select id="interpreter_id" name="interpreter_id" aria-label="Trusted interpreter"></select>
      </label>
      <p id="collection-help-interpreter_id" class="hint">Only interpreters explicitly trusted when this UI started are available. The analyzer and profile workload use the same selection.</p>
      <p class="hint">Environment variable values and secrets are not collected in this form. Commands that include user arguments may contain sensitive values and should not be shared.</p>
      <p id="collection-error" class="collection-error" role="alert" hidden></p>
      <button type="submit" class="detail-button collection-build">Build command</button>
    </form>
    <dialog id="collection-browser" class="collection-browser" aria-labelledby="collection-browser-title">
      <form method="dialog" class="collection-browser-head">
        <h2 id="collection-browser-title">Choose path</h2>
        <button type="submit" value="cancel" class="detail-button">Close</button>
      </form>
      <p class="hint" id="collection-browser-purpose">Folder navigation does not overwrite the target field. Use Choose to apply the selected entry to the chosen field only.</p>
      <p id="browser-path" class="mono" aria-live="polite"></p>
      <p id="browser-boundary" class="hint" role="status"></p>
      <p id="browser-stats" class="hint"></p>
      <div id="browser-list" class="collection-browser-list" role="listbox" aria-label="Directory entries"></div>
      <div class="collection-browser-actions">
        <button type="button" id="browser-root" class="detail-button">Collection root</button>
        <button type="button" id="browser-up" class="detail-button" aria-describedby="browser-boundary">Up</button>
        <button type="button" id="browser-open" class="detail-button" disabled>Open directory</button>
        <button type="button" id="browser-choose-dir" class="detail-button">Choose this directory</button>
        <button type="button" id="browser-choose" class="detail-button" disabled>Choose selected entry</button>
      </div>
    </dialog>
    <aside class="collection-preview panel" aria-label="Command preview">
      <h2>2. Review the exact command</h2>
      <div class="collection-preview-meta">
        <span>Host: <code data-preview-host></code></span>
        <span>Root: <code data-preview-root></code></span>
        <span>Interpreter: <code data-preview-interpreter></code></span>
        <span>cwd: <code data-preview-cwd></code></span>
      </div>
      <label for="collection-command">Command preview
        <textarea id="collection-command" readonly rows="4" spellcheck="false" placeholder="Build a command to preview the exact CLI invocation."></textarea>
      </label>
      <div class="collection-preview-actions">
        <button type="button" id="copy-command" class="detail-button" disabled>Copy command</button>
        <p id="preview-status" class="hint" role="status"></p>
      </div>
      <details><summary>Command requirements and limitations</summary><div id="preview-details" class="collection-preview-details"></div></details>
    </aside>
  </section>`;
}

function applyMeta(root, status, cwd, interpreter) {
  root.querySelector('[data-meta="host"]').textContent = status.host;
  root.querySelector('[data-meta="root"]').textContent = status.root == null ? "(none)" : status.root;
  root.querySelector('[data-meta="interpreter"]').textContent = status.interpreter;
  root.querySelector('[data-meta="reports"]').textContent = status.report_available ? "yes" : "no";
  root.querySelector("[data-preview-host]").textContent = status.host;
  root.querySelector("[data-preview-root]").textContent = status.root == null ? "(none)" : status.root;
  root.querySelector("[data-preview-interpreter]").textContent = interpreter || status.interpreter;
  root.querySelector("[data-preview-cwd]").textContent = cwd;
}

function buildBody(form) {
  const operation = form.elements.operation.value;
  const body = {
    operation,
    interpreter_id: form.elements.interpreter_id.value || DEFAULTS.interpreter_id,
    target: DEFAULTS.target,
    cwd: DEFAULTS.cwd,
    languages: [...DEFAULTS.languages],
    strictness: DEFAULTS.strictness,
    exclude: [...DEFAULTS.exclude],
    workspace: DEFAULTS.workspace,
    source_report: DEFAULTS.source_report,
    workload_kind: DEFAULTS.workload_kind,
    workload: DEFAULTS.workload,
    arguments: [...DEFAULTS.arguments],
    timeout: DEFAULTS.timeout,
    repeat: DEFAULTS.repeat,
    expected_status: DEFAULTS.expected_status,
    allow_execution: false,
  };
  if (STATIC_OPS.has(operation)) {
    body.target = form.elements.target.value || ".";
    body.cwd = form.elements.cwd.value || ".";
    body.languages = [form.elements.language.value || "auto"];
    body.strictness = form.elements.strictness.value || "normal";
    body.exclude = lines(form.elements.exclude.value);
    body.workspace = blankToNull(form.elements.workspace.value);
  } else if (operation === "import-profile") {
    body.profiles = lines(form.elements.profiles.value);
    body.metadata = blankToNull(form.elements.import_metadata.value);
    body.source_report = blankToNull(form.elements.import_source.value);
    body.workspace = blankToNull(form.elements.import_workspace.value);
  } else {
    body.cwd = form.elements.runtime_cwd.value || ".";
    body.workspace = blankToNull(form.elements.runtime_workspace.value);
    body.source_report = blankToNull(form.elements.source_report.value);
    body.workload_kind = form.elements.workload_kind.value === "module" ? "module" : "script";
    body.workload = blankToNull(form.elements.workload.value);
    body.arguments = form.elements.arguments.value === "" ? [] : form.elements.arguments.value.split("\n");
    body.timeout = Number(form.elements.timeout.value);
    body.repeat = Number(form.elements.repeat.value);
    body.expected_status = form.elements.expected_status.value.trim() || "SUCCESS";
    body.allow_execution = Boolean(form.elements.allow_execution.checked);
  }
  return body;
}

function setMode(form, operation) {
  const staticBox = form.querySelector('[data-mode="static"]');
  const runtimeBox = form.querySelector('[data-mode="runtime"]');
  const isRuntime = operation === "profile";
  staticBox.hidden = !STATIC_OPS.has(operation);
  runtimeBox.hidden = !isRuntime;
  form.querySelector('[data-mode="import"]').hidden = operation !== "import-profile";
  form.querySelector("#operation-help").textContent = OPERATIONS[operation] || "";
  if (!isRuntime) form.elements.allow_execution.checked = false;
}

function renderPreviewDetails(container, preview) {
  container.replaceChildren();
  if (!preview) return;
  const required = document.createElement("p");
  required.className = "hint";
  required.textContent = preview.required_inputs?.length
    ? `Required inputs: ${preview.required_inputs.join(", ")}`
    : "Required inputs: none reported";
  container.append(required);
  for (const item of preview.limitations || []) {
    const line = document.createElement("p");
    line.className = "hint";
    line.textContent = item;
    container.append(line);
  }
  const note = document.createElement("p");
  note.className = "hint";
  note.textContent = `Operation: ${preview.operation} · execution_available: ${preview.execution_available}`;
  container.append(note);
}

function clearPreview(root, statusMessage) {
  const command = root.querySelector("#collection-command");
  const copy = root.querySelector("#copy-command");
  const details = root.querySelector("#preview-details");
  const status = root.querySelector("#preview-status");
  command.value = "";
  copy.disabled = true;
  details.replaceChildren();
  status.textContent = statusMessage || "";
}

const RETAINED_FIELDS = Object.freeze([
  'operation', 'interpreter_id', 'target', 'language', 'strictness', 'exclude', 'cwd', 'workspace',
  'source_report', 'workload_kind', 'workload', 'arguments', 'runtime_cwd', 'runtime_workspace',
  'timeout', 'repeat', 'expected_status', 'profiles', 'import_metadata', 'import_source', 'import_workspace',
]);

function advancedSettings(form) {
  for (const [mode, fields] of [
    ['static', ['strictness', 'exclude', 'cwd', 'workspace']],
    ['runtime', ['runtime_cwd', 'runtime_workspace', 'timeout', 'repeat', 'expected_status']],
  ]) {
    const contents = element('div', {className: 'collection-advanced-fields'});
    for (const name of fields) {
      const input = form.elements[name];
      contents.append(input.closest('.collection-path-field') || input.closest('label'));
    }
    const summary = element('p', {className: 'hint', 'data-settings-summary': mode});
    const box = form.querySelector(`[data-mode="${mode}"]`);
    box.append(summary, disclosure({title: 'Advanced settings', content: contents}));
  }
  const descriptions = {
    target: 'Source path within the collection root. Static analysis does not execute target code.',
    language: 'Auto discovers supported source languages; choose one to restrict collection.',
    interpreter_id: 'Only startup-trusted Python interpreters can collect evidence.',
    source_report: 'Saved static evidence binds runtime frames to known source identities.',
    workload: 'Script path or Python module to execute only after explicit authorization and confirmation.',
    strictness: 'Controls analysis thresholds, not runtime execution or permissions.',
    exclude: 'Exact directory names, not filename patterns or globs.',
    cwd: 'Command working directory; distinct from the report target and source-preview root.',
    runtime_cwd: 'The workload runs in this directory inside the collection root.',
    workspace: 'Result storage, not source. A new workspace leaf under an existing confined parent is created only after confirmed collection.',
    runtime_workspace: 'Result storage, not source. A new workspace leaf is created only after confirmed collection; this does not change which threads are profiled.',
  };
  for (const [name, text] of Object.entries(descriptions)) {
    const control = form.elements[name];
    const existing = form.querySelector(`#collection-help-${name}`);
    const hint = existing || element('p', {id: `collection-help-${name}`, className: 'hint', text});
    control.setAttribute('aria-describedby', hint.id);
    if (existing) continue;
    const pathField = control.closest('.collection-path-field');
    if (pathField) pathField.append(hint);
    else control.closest('label').after(hint);
  }
}

export async function collectionView(content, api, post, escapeHtml, signal, session = {}) {
  const status = await api("/api/collection", signal);
  if (signal.aborted) return;

  if (!status.enabled) {
    content.replaceChildren();
    const wrap = document.createElement("div");
    wrap.className = "empty";
    const heading = document.createElement("h2");
    heading.textContent = "Collection planning unavailable";
    const message = document.createElement("p");
    message.textContent =
      "Relaunch with code-analysis ui --collection-root PATH to enable confined collection browsing and command preview.";
    const host = document.createElement("p");
    host.className = "hint";
    host.textContent = `Host: ${status.host} · interpreter: ${status.interpreter}`;
    wrap.append(heading, message, host);
    content.append(wrap);
    return;
  }

  content.innerHTML = shell();
  const root = content.querySelector(".collection-view");
  const form = root.querySelector("#collection-form");
  const errorBox = root.querySelector("#collection-error");
  const dialog = root.querySelector("#collection-browser");
  const browserList = root.querySelector("#browser-list");
  const browserPath = root.querySelector("#browser-path");
  const browserBoundary = root.querySelector("#browser-boundary");
  const browserStats = root.querySelector("#browser-stats");
  const chooseEntry = root.querySelector("#browser-choose");
  const chooseDir = root.querySelector("#browser-choose-dir");
  const openDir = root.querySelector("#browser-open");
  const upButton = root.querySelector("#browser-up");
  const commandBox = root.querySelector("#collection-command");
  const copyButton = root.querySelector("#copy-command");
  const previewStatus = root.querySelector("#preview-status");
  const previewDetails = root.querySelector("#preview-details");
  const contextContents = element('div', {}, root.querySelector('.collection-meta'), root.querySelector('#collection-continuity'));
  const context = disclosure({title: 'Execution host and exact collection paths', content: contextContents});
  context.classList.add('collection-context');
  root.insertBefore(context, form);
  root.querySelector('.collection-preview h2').after(root.querySelector('.collection-banner'));

  for (const item of status.interpreters || []) {
    const option = document.createElement("option");
    option.value = item.id;
    option.textContent = item.path;
    form.elements.interpreter_id.append(option);
  }
  if (![...form.elements.interpreter_id.options].some((item) => item.value === "current")) {
    const option = document.createElement("option");
    option.value = "current";
    option.textContent = status.interpreter;
    form.elements.interpreter_id.append(option);
  }
  form.elements.interpreter_id.value = "current";

  let previewController = null;
  let browseController = null;
  let browseField = "target";
  let browsePath = ".";
  let selectedEntry = null;
  let previewGeneration = 0;
  const jobs = status.execution_available ? collectionJobs(root, api, post, escapeHtml, signal,
    () => buildBody(form).workspace || '') : null;
  if (jobs) {
    form.elements.workspace.value = '.code-analysis';
    form.elements.runtime_workspace.value = '.code-analysis';
    form.elements.import_workspace.value = '.code-analysis';
    root.querySelector('.collection-banner').textContent = 'Review the command below, then explicitly confirm collection. Profiling runs target code; static analysis does not.';
  }
  const scope = `${status.host}\n${status.root}`;
  if (session.scope === scope && session.values) {
    for (const name of RETAINED_FIELDS) {
      const control = form.elements[name];
      const value = session.values[name];
      if (value === undefined) continue;
      if (control instanceof HTMLSelectElement && ![...control.options].some(item => item.value === value)) continue;
      control.value = value;
    }
  }
  session.scope = scope;
  if (Object.hasOwn(OPERATIONS, session.requestedOperation)) {
    form.elements.operation.value = session.requestedOperation;
  }
  delete session.requestedOperation;
  form.elements.allow_execution.checked = false;
  advancedSettings(form);
  const remember = () => {
    session.values = Object.fromEntries(RETAINED_FIELDS.map(name => [name, form.elements[name].value]));
  };
  form.dataset.mode = "static";

  const abortChildren = () => {
    remember();
    previewController?.abort();
    browseController?.abort();
  };
  signal.addEventListener("abort", abortChildren, {once: true});

  const showError = (message) => {
    if (!message) {
      errorBox.hidden = true;
      errorBox.textContent = "";
      return;
    }
    errorBox.hidden = false;
    errorBox.textContent = message;
  };

  const invalidate = (reason = "Command preview cleared — rebuild after edits.") => {
    jobs?.setPreview(null);
    previewGeneration += 1;
    previewController?.abort();
    clearPreview(root, reason);
  };

  const syncMeta = () => {
    const cwd = form.elements.operation.value === "import-profile" ? "." : form.elements.operation.value === "profile"
      ? form.elements.runtime_cwd.value || "."
      : form.elements.cwd.value || ".";
    const choice = (status.interpreters || []).find(
      (item) => item.id === form.elements.interpreter_id.value,
    );
    applyMeta(root, status, cwd, choice?.path);
    root.querySelector('[data-settings-summary="static"]').textContent =
      `Strictness: ${form.elements.strictness.value}; exclusions: ${lines(form.elements.exclude.value).length}; cwd: ${form.elements.cwd.value || '.'}; workspace: ${form.elements.workspace.value || '(default)'}.`;
    root.querySelector('[data-settings-summary="runtime"]').textContent =
      `cwd: ${form.elements.runtime_cwd.value || '.'}; workspace: ${form.elements.runtime_workspace.value || '(default)'}; timeout: ${form.elements.timeout.value}s; repetitions: ${form.elements.repeat.value}; expected: ${form.elements.expected_status.value}.`;
    remember();
  };

  setMode(form, form.elements.operation.value);
  syncMeta();

  form.elements.operation.addEventListener("change", () => {
    const previous = form.dataset.mode || "static";
    const next = form.elements.operation.value === "profile" ? "runtime" : "static";
    setMode(form, form.elements.operation.value);
    if (previous !== next) form.elements.allow_execution.checked = false;
    form.dataset.mode = next;
    invalidate();
    syncMeta();
  });

  form.addEventListener("input", () => {
    invalidate();
    syncMeta();
  });
  form.addEventListener("change", () => {
    invalidate();
    syncMeta();
  });

  async function loadBrowser(path) {
    browseController?.abort();
    browseController = new AbortController();
    if (signal.aborted) return;
    const current = browseController.signal;
    browserList.replaceChildren();
    chooseEntry.disabled = true;
    chooseDir.disabled = true;
    upButton.disabled = true;
    openDir.disabled = true;
    selectedEntry = null;
    browserPath.textContent = "Loading…";
    browserBoundary.textContent = "";
    browserStats.textContent = "";
    try {
      const payload = await api(
        `/api/collection/files?path=${encodeURIComponent(path)}`,
        current,
      );
      current.throwIfAborted();
      browsePath = payload.path || path;
      chooseDir.disabled = ["source_report", "workload", "profiles", "import_metadata", "import_source"].includes(browseField);
      const displayPath = browsePath === '.' ? status.root : `${status.root.replace(/\/$/, '')}/${browsePath}`;
      browserPath.textContent = `Current directory: ${displayPath}`;
      browserStats.textContent = `Omitted: ${payload.omitted} · skipped: ${payload.skipped}`;
      upButton.disabled = payload.parent == null;
      upButton.dataset.parent = payload.parent == null ? "" : payload.parent;
      browserBoundary.textContent = payload.parent == null
        ? 'At collection root. Up stops here. To browse a parent or sibling project, start the UI with --collection-root PATH set to a parent folder.'
        : 'Up returns to the parent directory within the collection root.';

      for (const entry of payload.entries || []) {
        const button = document.createElement("button");
        button.type = "button";
        button.className = "detail-button collection-entry";
        button.setAttribute("role", "option");
        button.dataset.path = entry.path;
        button.dataset.kind = entry.kind;
        button.dataset.name = entry.name;
        button.textContent = `${entry.name} (${entry.kind})`;
        button.addEventListener("click", () => {
          browserList.querySelectorAll("[aria-selected='true']").forEach((node) => {
            node.removeAttribute("aria-selected");
          });
          button.setAttribute("aria-selected", "true");
          selectedEntry = entry;
          chooseEntry.disabled = false;
          openDir.disabled = entry.kind !== "directory";
        });
        button.addEventListener("dblclick", () => {
          if (entry.kind === "directory") loadBrowser(entry.path).catch(browseError);
          else applyBrowse(entry.path);
        });
        button.addEventListener("keydown", (event) => {
          if (event.key === "Enter" && entry.kind === "directory") {
            event.preventDefault();
            loadBrowser(entry.path).catch(browseError);
          }
        });
        browserList.append(button);
      }
      if (!(payload.entries || []).length) {
        const empty = document.createElement("p");
        empty.className = "hint";
        empty.textContent = "No browsable entries in this directory.";
        browserList.append(empty);
      }
    } catch (error) {
      if (error.name === "AbortError") return;
      browserPath.textContent = path;
      browserStats.textContent = error.message;
    }
  }

  function browseError(error) {
    if (error.name !== "AbortError") browserStats.textContent = error.message;
  }

  function fieldInput(name) {
    if (name === "workspace" && form.elements.operation.value === "profile") return form.elements.runtime_workspace;
    if (name === "cwd") {
      return form.elements.operation.value === "profile"
        ? form.elements.runtime_cwd
        : form.elements.cwd;
    }
    return form.elements[name];
  }

  function applyBrowse(pathValue) {
    const input = fieldInput(browseField);
    if (!input) return;
    input.value = browseField === "profiles"
      ? [...new Set([...lines(input.value), pathValue])].join("\n") : pathValue;
    input.dispatchEvent(new Event("input", {bubbles: true}));
    dialog.close();
  }

  root.querySelectorAll("[data-browse]").forEach((button) => {
    button.addEventListener("click", () => {
      browseField = button.dataset.browse;
      const purposes = {
        target: ['Choose existing source', 'Choose an existing source file or folder to analyze. Browsing does not run code or create folders.'],
        cwd: ['Choose working directory', 'Choose an existing directory for the command. This is distinct from its source target and result storage.'],
        workspace: ['Choose result storage', 'Choose an existing directory, or close this picker and enter a new workspace leaf in the field. Creation occurs only after confirmed collection, never while browsing.'],
        source_report: ['Choose saved static report', 'Choose an existing saved JSON report to bind runtime evidence to its recorded source identities.'],
        workload: ['Choose Python workload', 'Choose an existing Python script. Choosing it does not grant execution authorization. Enter a dotted module name directly in the field for Module mode.'],
        profiles: ['Choose saved profile', 'Choose an existing cProfile or pstats artifact. Each Choose adds it to the import list without changing the original.'],
        import_metadata: ['Choose capture metadata', 'Choose existing capture metadata. Missing historical provenance is not manufactured.'],
        import_source: ['Choose saved static report', 'Choose an existing static report for optional source correlation.'],
        import_workspace: ['Choose existing owned workspace', 'Managed profile import requires an existing owned Code Analysis workspace. Browsing does not create one.'],
      };
      // Workspace browsing follows the current operation, not a generic mkdir API.
      const purpose = purposes[browseField === 'workspace' && form.elements.operation.value === 'import-profile' ? 'import_workspace' : browseField];
      root.querySelector('#collection-browser-title').textContent = purpose[0];
      root.querySelector('#collection-browser-purpose').textContent = purpose[1] + ' Use Choose to apply the selection; folder navigation alone does not change the field.';
      const current = fieldInput(browseField)?.value;
      const start =
        browseField === "workload" && form.elements.workload_kind.value === "module"
          ? "."
          : current || ".";
      dialog.showModal();
      const directory = /\.(py|js|mjs|cjs|jsx|rb|json)$/i.test(start)
        ? start.split('/').slice(0, -1).join('/') || '.' : start;
      loadBrowser(directory || '.').catch(browseError);
    });
  });

  upButton.addEventListener("click", () => {
    const parent = upButton.dataset.parent;
    if (parent) loadBrowser(parent).catch(browseError);
  });
  root.querySelector('#browser-root').addEventListener('click', () => loadBrowser('.').catch(browseError));
  dialog.addEventListener('close', () => browseController?.abort());
  openDir.addEventListener("click", () => {
    if (selectedEntry?.kind === "directory") loadBrowser(selectedEntry.path).catch(browseError);
  });
  chooseDir.addEventListener("click", () => applyBrowse(browsePath));
  chooseEntry.addEventListener("click", () => {
    if (selectedEntry) applyBrowse(selectedEntry.path);
  });

  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    showError("");
    const body = buildBody(form);
    previewController?.abort();
    previewController = new AbortController();
    const generation = ++previewGeneration;
    const current = previewController.signal;
    clearPreview(root, "Building command…");
    jobs?.setPreview(null);
    syncMeta();
    try {
      const preview = await post("/api/collection/preview", body, current);
      current.throwIfAborted();
      if (generation !== previewGeneration || signal.aborted) return;
      commandBox.value = preview.command || "";
      copyButton.disabled = !commandBox.value;
      root.querySelector("[data-preview-cwd]").textContent = preview.cwd || body.cwd;
      root.querySelector("[data-preview-host]").textContent = preview.host || status.host;
      root.querySelector("[data-preview-interpreter]").textContent =
        preview.interpreter || status.interpreter;
      renderPreviewDetails(previewDetails, preview);
      jobs?.setPreview(preview, body);
      previewStatus.textContent = jobs ? 'Preview ready. Review and confirm in Run and monitor, or copy to your terminal.' : "Preview ready. Copy and run only in your own terminal.";
    } catch (error) {
      if (error.name === "AbortError" || generation !== previewGeneration) return;
      clearPreview(root, "");
      showError(error.message || "Preview failed");
      previewStatus.textContent = "Preview failed; form values were preserved.";
    }
  });

  copyButton.addEventListener("click", async () => {
    const text = commandBox.value;
    if (!text) return;
    try {
      await navigator.clipboard.writeText(text);
      previewStatus.textContent = "Command copied.";
    } catch {
      commandBox.focus();
      commandBox.select();
      previewStatus.textContent = "Select and copy the command manually.";
    }
  });
}
