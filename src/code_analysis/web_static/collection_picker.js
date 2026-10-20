/** Confined collection-root path picker shared by collection and source-connection UI. */

function pickerMarkup() {
  return `<form method="dialog" class="collection-browser-head">
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
    </div>`;
}

/**
 * @param {ParentNode} host connected consumer DOM that owns the dialog
 * @param {(url: string, signal: AbortSignal) => Promise<any>} api
 * @param {AbortSignal} signal parent abort; closes the picker and cancels loads
 * @param {{root: string}} options display collection root string
 * @returns {{open: Function, close: Function}}
 */
export function createCollectionPicker(host, api, signal, {root}) {
  const dialog = document.createElement("dialog");
  dialog.id = "collection-browser";
  dialog.className = "collection-browser";
  dialog.setAttribute("aria-labelledby", "collection-browser-title");
  dialog.innerHTML = pickerMarkup();
  host.append(dialog);

  const browserList = dialog.querySelector("#browser-list");
  const browserPath = dialog.querySelector("#browser-path");
  const browserBoundary = dialog.querySelector("#browser-boundary");
  const browserStats = dialog.querySelector("#browser-stats");
  const chooseEntry = dialog.querySelector("#browser-choose");
  const chooseDir = dialog.querySelector("#browser-choose-dir");
  const openDir = dialog.querySelector("#browser-open");
  const upButton = dialog.querySelector("#browser-up");
  const titleNode = dialog.querySelector("#collection-browser-title");
  const purposeNode = dialog.querySelector("#collection-browser-purpose");

  let browseController = null;
  let browsePath = ".";
  let selectedEntry = null;
  let directoryOnly = false;
  let filesOnly = false;
  let onChoose = null;
  let returnFocus = null;

  const restoreFocus = () => {
    const target = returnFocus;
    returnFocus = null;
    if (target?.isConnected) target.focus();
  };

  const abortPending = () => {
    browseController?.abort();
    browseController = null;
  };

  const close = () => {
    abortPending();
    if (dialog.open) dialog.close();
    else restoreFocus();
  };

  dialog.addEventListener("close", () => {
    abortPending();
    restoreFocus();
  });
  signal.addEventListener(
    "abort",
    () => {
      close();
    },
    {once: true},
  );

  function browseError(error) {
    if (error.name !== "AbortError") browserStats.textContent = error.message;
  }

  function applyChoice(pathValue) {
    const callback = onChoose;
    abortPending();
    if (dialog.open) dialog.close();
    callback?.(pathValue);
  }

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
      chooseDir.disabled = filesOnly;
      const displayPath =
        browsePath === "." ? root : `${String(root).replace(/\/$/, "")}/${browsePath}`;
      browserPath.textContent = `Current directory: ${displayPath}`;
      browserStats.textContent = `Omitted: ${payload.omitted} · skipped: ${payload.skipped}`;
      upButton.disabled = payload.parent == null;
      upButton.dataset.parent = payload.parent == null ? "" : payload.parent;
      browserBoundary.textContent =
        payload.parent == null
          ? "At collection root. Up stops here. To browse a parent or sibling project, start the UI with --collection-root PATH set to a parent folder."
          : "Up returns to the parent directory within the collection root.";

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
          const fileBlocked = directoryOnly && entry.kind !== "directory";
          chooseEntry.disabled = fileBlocked;
          openDir.disabled = entry.kind !== "directory";
        });
        button.addEventListener("dblclick", () => {
          if (entry.kind === "directory") loadBrowser(entry.path).catch(browseError);
          else if (!directoryOnly) applyChoice(entry.path);
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

  upButton.addEventListener("click", () => {
    const parent = upButton.dataset.parent;
    if (parent) loadBrowser(parent).catch(browseError);
  });
  dialog.querySelector("#browser-root").addEventListener("click", () => {
    loadBrowser(".").catch(browseError);
  });
  openDir.addEventListener("click", () => {
    if (selectedEntry?.kind === "directory") loadBrowser(selectedEntry.path).catch(browseError);
  });
  chooseDir.addEventListener("click", () => {
    if (!filesOnly) applyChoice(browsePath);
  });
  chooseEntry.addEventListener("click", () => {
    if (!selectedEntry) return;
    if (directoryOnly && selectedEntry.kind !== "directory") return;
    applyChoice(selectedEntry.path);
  });

  function open({
    title,
    purpose,
    start,
    directoryOnly: dirOnly = false,
    filesOnly: fileOnly = false,
    onChoose: choose,
  }) {
    directoryOnly = Boolean(dirOnly);
    filesOnly = Boolean(fileOnly);
    onChoose = choose;
    titleNode.textContent = title;
    purposeNode.textContent = purpose;
    returnFocus =
      document.activeElement instanceof HTMLElement ? document.activeElement : null;
    if (!dialog.open) dialog.showModal();
    return loadBrowser(start || ".").catch(browseError);
  }

  return {open, close};
}
