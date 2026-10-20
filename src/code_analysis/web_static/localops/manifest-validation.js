const ID = /^[A-Za-z0-9](?:[A-Za-z0-9._:-]*[A-Za-z0-9])?$/;

function record(value, label) {
  if (!value || typeof value !== "object" || Array.isArray(value)) throw new TypeError(`${label} must be an object`);
  return value;
}

function exact(value, label, allowed, required = []) {
  const candidate = record(value, label);
  for (const key of Object.keys(candidate)) if (!allowed.includes(key)) throw new TypeError(`${label}.${key} is not allowed`);
  for (const key of required) if (!(key in candidate)) throw new TypeError(`${label}.${key} is required`);
  return candidate;
}

function identifier(value, label) {
  if (typeof value !== "string" || value.length > 128 || !ID.test(value)) throw new TypeError(`${label} must be a stable identifier`);
}

function string(value, label, optional = false) {
  if (optional && value === undefined) return;
  if (typeof value !== "string" || value.length === 0) throw new TypeError(`${label} must be a non-empty string`);
}

function boolean(value, label) {
  if (value !== undefined && typeof value !== "boolean") throw new TypeError(`${label} must be a boolean`);
}

function number(value, label) {
  if (value !== undefined && (typeof value !== "number" || !Number.isFinite(value))) throw new TypeError(`${label} must be a finite number`);
}

function json(value, label) {
  if (value === null || ["string", "boolean"].includes(typeof value)) return;
  if (typeof value === "number" && Number.isFinite(value)) return;
  if (Array.isArray(value)) return value.forEach((item, index) => json(item, `${label}[${index}]`));
  const candidate = record(value, label);
  for (const [key, item] of Object.entries(candidate)) json(item, `${label}.${key}`);
}

const NODE_FIELDS = {
  text: ["type", "id", "text", "style"],
  status_badge: ["type", "id", "label", "tone"],
  state_view: ["type", "id", "kind", "title", "message", "action_id", "action_label"],
  button: ["type", "id", "label", "command_id", "variant", "disabled", "icon"],
  dialog: ["type", "id", "kind", "trigger_label", "title", "description", "command_id", "result_key", "default_value_key", "destructive"],
  copy_control: ["type", "id", "label", "value_key"],
  number_field: ["type", "id", "label", "value_key", "minimum", "maximum", "step", "suffix"],
  slider_field: ["type", "id", "label", "value_key", "minimum", "maximum", "step", "suffix"],
  path_field: ["type", "id", "label", "value_key", "adapter_id", "kind", "allow_browse", "allow_create"],
  data_table: ["type", "id", "caption", "columns", "rows_key", "row_key", "preference_key", "sort_command_id", "client_sort", "sort_locale", "empty_message"],
  task_monitor: ["type", "id", "adapter_id", "cancel_command_id", "retry_command_id", "poll_interval_ms"],
  chart: ["type", "id", "dataset_key", "description"],
  tabs: ["type", "id", "tabs", "selected_key", "aria_label"],
  disclosure: ["type", "id", "title", "children", "open_key", "initially_open"],
  stack: ["type", "id", "children", "gap"],
  row: ["type", "id", "children", "wrap", "gap"],
  grid: ["type", "id", "children", "columns", "gap"],
  panel: ["type", "id", "title", "children"],
};

const NODE_REQUIRED = {
  text: ["text"], status_badge: ["label"], state_view: ["kind", "title"],
  button: ["label", "command_id"], dialog: ["kind", "trigger_label", "title"],
  copy_control: ["value_key"], number_field: ["label", "value_key"],
  slider_field: ["label", "value_key", "minimum", "maximum"],
  path_field: ["label", "value_key", "adapter_id"],
  data_table: ["caption", "columns", "rows_key", "row_key"],
  task_monitor: ["adapter_id"], chart: ["dataset_key"], tabs: ["tabs"],
  disclosure: ["title", "children"], stack: ["children"], row: ["children"],
  grid: ["children"], panel: ["children"],
};

function validateNode(value, label, ids) {
  const base = record(value, label);
  if (!Object.hasOwn(NODE_FIELDS, base.type)) throw new TypeError(`${label}.type is unsupported`);
  const node = exact(base, label, NODE_FIELDS[base.type], ["type", "id", ...NODE_REQUIRED[base.type]]);
  identifier(node.id, `${label}.id`);
  if (ids.has(node.id)) throw new TypeError(`duplicate public ID: ${node.id}`);
  ids.add(node.id);
  for (const key of ["command_id", "action_id", "value_key", "result_key", "default_value_key", "adapter_id", "rows_key", "row_key", "preference_key", "sort_command_id", "dataset_key", "selected_key", "open_key", "cancel_command_id", "retry_command_id"])
    if (node[key] !== undefined && node[key] !== null) identifier(node[key], `${label}.${key}`);
  for (const key of ["disabled", "destructive", "allow_browse", "allow_create", "client_sort", "initially_open", "wrap", "visible", "sortable"])
    boolean(node[key], `${label}.${key}`);
  for (const key of ["minimum", "maximum", "step", "poll_interval_ms"])
    number(node[key], `${label}.${key}`);
  for (const key of ["label", "title", "trigger_label", "caption", "aria_label", "text"])
    if (node[key] !== undefined && node[key] !== null) string(node[key], `${label}.${key}`);
  if (node.type === "grid") number(node.columns, `${label}.columns`);
  if (node.type === "data_table") {
    if (!Array.isArray(node.columns) || node.columns.length === 0) throw new TypeError(`${label}.columns must be a non-empty array`);
    const columnIds = new Set();
    node.columns.forEach((raw, index) => {
      const column = exact(raw, `${label}.columns[${index}]`, ["id", "label", "value_key", "sortable", "visible", "width", "align"], ["id", "label", "value_key"]);
      identifier(column.id, `${label}.columns[${index}].id`);
      if (columnIds.has(column.id)) throw new TypeError(`${label} column IDs must be unique`);
      columnIds.add(column.id);
      string(column.label, `${label}.columns[${index}].label`);
      identifier(column.value_key, `${label}.columns[${index}].value_key`);
      boolean(column.sortable, `${label}.columns[${index}].sortable`);
      boolean(column.visible, `${label}.columns[${index}].visible`);
      number(column.width, `${label}.columns[${index}].width`);
    });
    if (node.client_sort === true && !node.sort_locale) throw new TypeError(`${label}.client_sort requires sort_locale`);
    if (node.client_sort === true && node.sort_command_id) throw new TypeError(`${label} cannot combine client_sort and sort_command_id`);
  }
  if (node.type === "tabs") {
    if (!Array.isArray(node.tabs) || node.tabs.length === 0) throw new TypeError(`${label}.tabs must be a non-empty array`);
    node.tabs.forEach((raw, index) => {
      const tab = exact(raw, `${label}.tabs[${index}]`, ["id", "label", "children"], ["id", "label", "children"]);
      identifier(tab.id, `${label}.tabs[${index}].id`);
      if (ids.has(tab.id)) throw new TypeError(`duplicate public ID: ${tab.id}`);
      ids.add(tab.id);
      string(tab.label, `${label}.tabs[${index}].label`);
      if (!Array.isArray(tab.children)) throw new TypeError(`${label}.tabs[${index}].children must be an array`);
      tab.children.forEach((child, childIndex) => validateNode(child, `${label}.tabs[${index}].children[${childIndex}]`, ids));
    });
  } else if (["disclosure", "stack", "row", "grid", "panel"].includes(node.type)) {
    if (!Array.isArray(node.children)) throw new TypeError(`${label}.children must be an array`);
    node.children.forEach((child, index) => validateNode(child, `${label}.children[${index}]`, ids));
  }
  return node;
}

export function validateSnapshot(value) {
  const snapshot = exact(value, "UISnapshot", ["schema_version", "revision", "spec", "state"], ["schema_version", "revision", "spec", "state"]);
  if (snapshot.schema_version !== "1") throw new TypeError("UISnapshot.schema_version must equal 1");
  if (!Number.isSafeInteger(snapshot.revision) || snapshot.revision < 0) throw new TypeError("UISnapshot.revision must be a non-negative integer");
  const spec = exact(snapshot.spec, "UISnapshot.spec", ["schema_version", "id", "pages", "initial_page_id"], ["schema_version", "id", "pages"]);
  if (spec.schema_version !== "1") throw new TypeError("UISpec.schema_version must equal 1");
  identifier(spec.id, "UISpec.id");
  if (!Array.isArray(spec.pages) || spec.pages.length === 0) throw new TypeError("UISpec.pages must be a non-empty array");
  const ids = new Set();
  const pageIds = new Set();
  spec.pages.forEach((raw, index) => {
    const page = exact(raw, `UISpec.pages[${index}]`, ["id", "title", "body", "navigation"], ["id", "title", "body"]);
    identifier(page.id, `UISpec.pages[${index}].id`);
    if (ids.has(page.id)) throw new TypeError(`duplicate public ID: ${page.id}`);
    ids.add(page.id); pageIds.add(page.id); string(page.title, `UISpec.pages[${index}].title`);
    for (const [navIndex, rawItem] of (page.navigation ?? []).entries()) {
      const item = exact(rawItem, `UISpec.pages[${index}].navigation[${navIndex}]`, ["id", "label", "command_id"], ["id", "label"]);
      identifier(item.id, `navigation[${navIndex}].id`);
      if (ids.has(item.id)) throw new TypeError(`duplicate public ID: ${item.id}`);
      ids.add(item.id); string(item.label, `navigation[${navIndex}].label`);
      if (item.command_id !== undefined && item.command_id !== null) identifier(item.command_id, `navigation[${navIndex}].command_id`);
    }
    validateNode(page.body, `UISpec.pages[${index}].body`, ids);
  });
  if (spec.initial_page_id !== undefined && spec.initial_page_id !== null && !pageIds.has(spec.initial_page_id)) throw new TypeError("UISpec.initial_page_id must reference a page");
  json(record(snapshot.state, "UISnapshot.state"), "UISnapshot.state");
  return snapshot;
}

export function validatePatch(value) {
  const patch = exact(value, "StatePatch", ["schema_version", "base_revision", "revision", "changes", "removed"], ["schema_version", "base_revision", "revision", "changes"]);
  if (patch.schema_version !== "1") throw new TypeError("StatePatch.schema_version must equal 1");
  if (!Number.isSafeInteger(patch.base_revision) || patch.base_revision < 0 || !Number.isSafeInteger(patch.revision) || patch.revision <= patch.base_revision) throw new TypeError("StatePatch revision must advance from a non-negative base");
  json(record(patch.changes, "StatePatch.changes"), "StatePatch.changes");
  if (patch.removed !== undefined && (!Array.isArray(patch.removed) || patch.removed.some((key) => typeof key !== "string"))) throw new TypeError("StatePatch.removed must be a string array");
  return patch;
}

export function validateUpdate(value) {
  return record(value, "UI update").spec === undefined ? validatePatch(value) : validateSnapshot(value);
}

export function validateResponse(value) {
  const response = exact(value, "UITransportResponse", ["schema_version", "result", "update"], ["schema_version", "update"]);
  if (response.schema_version !== "1") throw new TypeError("UITransportResponse.schema_version must equal 1");
  if ("result" in response) json(response.result, "UITransportResponse.result");
  validateUpdate(response.update);
  return response;
}
