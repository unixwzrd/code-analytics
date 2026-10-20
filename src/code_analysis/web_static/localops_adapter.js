import {operatorShell, DataTable} from "/localops/index.js";

/** @type {HTMLElement & { closeNavigation?: (options?: { focusTarget?: HTMLElement }) => boolean }} */
let shellRoot;

function syncDocumentTheme() {
  const dark = window.matchMedia("(prefers-color-scheme: dark)").matches;
  document.documentElement.dataset.theme = dark ? "dark" : "light";
}

function mountOperatorShell() {
  const bootstrap = document.querySelector("#localops-bootstrap");
  if (!bootstrap) throw new Error("LocalOps bootstrap markup is missing");
  const sourceNav = bootstrap.querySelector('nav[aria-label="Report views"]');
  const appView = bootstrap.querySelector(".app-view");
  const reportState = document.querySelector("#report-state");
  if (!sourceNav || !appView || !reportState) {
    throw new Error("LocalOps bootstrap navigation or report state is missing");
  }

  const shell = operatorShell({
    title: "Code Analysis",
    navigationLabel: "Report views",
    navigation: [],
    content: appView,
    topBarActions: [reportState],
  });

  const titleEl = shell.querySelector(".lui-shell__title");
  if (titleEl) {
    titleEl.replaceChildren();
    const brand = document.createElement("div");
    brand.className = "app-brand";
    brand.innerHTML = '<img class="mark" src="/icon.svg" alt=""><strong>Code Analysis</strong>';
    titleEl.append(brand);
  }

  const shellNav = shell.querySelector('nav[aria-label="Report views"]');
  if (!shellNav) throw new Error("LocalOps shell navigation region is missing");
  while (sourceNav.firstChild) shellNav.append(sourceNav.firstChild);

  document.body.insertBefore(shell, bootstrap);
  bootstrap.remove();
  shellRoot = shell;
  shell.addEventListener('keydown', event => {
    const toggle = shell.querySelector('.lui-shell__menu');
    if (event.key === 'Escape' && toggle.getAttribute('aria-expanded') === 'true' &&
        getComputedStyle(toggle).display !== 'none') {
      event.preventDefault();
      shell.closeNavigation();
      toggle.focus();
    }
  });
  document.body.dataset.localopsShell = "mounted";
}

syncDocumentTheme();
window.matchMedia("(prefers-color-scheme: dark)").addEventListener("change", syncDocumentTheme);
mountOperatorShell();

/** Close the mobile navigation drawer after the user accepts a view change. */
export function finalizeAcceptedNavigation(target = document.querySelector("#title")) {
  const title = target;
  if (!title || !shellRoot?.closeNavigation) return;
  shellRoot.closeNavigation({focusTarget: title});
}

export function getOperatorShellRoot() {
  return shellRoot;
}

/** Present the backend's bounded page without changing its order or metrics. */
export async function reportTable(columns, page, caption) {
  const positions = new Map(page.records.map((record, index) => [record, page.offset + index]));
  const table = new DataTable({
    caption,
    rows: page.records,
    rowKey: row => `${caption}:${positions.get(row)}`,
    columns: columns.map(([label, key, render, width], index) => ({
      id: `column-${index}-${key}`, label, sortable: false, width,
      value: row => row[key],
      render: (value, row) => {
        if (!render) return String(value ?? '');
        const rendered = render(value, row);
        // Consumer-created toolkit DOM is already trusted presentation, not target markup.
        if (rendered instanceof Node) return rendered;
        // App renderers already escape all report values. Keep their literal
        // badges/disclosures/evidence controls; no target HTML is supplied.
        const template = document.createElement('template');
        template.innerHTML = rendered;
        return template.content;
      },
    })),
  });
  await table.initialize();
  const headerLabels = new Map(table.options.columns.map(column => [column.id, column.label]));
  const labelHeaders = () => table.table.querySelectorAll('th[data-column-id]').forEach(header => {
    header.setAttribute('aria-label', headerLabels.get(header.dataset.columnId));
  });
  labelHeaders();
  // Column visibility/reordering rebuilds the head. Preserve concise header
  // names while leaving the toolkit's resize and sort control labels intact.
  new MutationObserver(labelHeaders).observe(table.table, {childList: true});
  const columnsPanel = table.root.querySelector('.lui-data-table__columns');
  const labelColumns = () => columnsPanel.querySelectorAll('label').forEach(label => {
    label.querySelector('input[type="checkbox"]').setAttribute(
      'aria-label', `Show ${label.querySelector('span').textContent} column`);
  });
  labelColumns();
  new MutationObserver(labelColumns).observe(columnsPanel, {childList: true});
  table.root.dataset.localopsReportTable = caption;
  return table.root;
}
