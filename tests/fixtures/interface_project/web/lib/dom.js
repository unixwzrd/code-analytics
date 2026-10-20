export function fetchJson(path) {
  return fetch(path);
}

export function api(path) {
  return fetchJson(path);
}
