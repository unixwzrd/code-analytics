import { api, fetchJson } from "./lib/dom.js";

export async function createJob(jobId, name) {
  return fetch(`/api/jobs/${jobId}`, {
    method: "POST",
    body: JSON.stringify({ name: name, force: null, extra: true }),
  });
}

export function readJob(jobId) {
  return fetchJson(`/api/jobs/${jobId}?include=summary`);
}

export function readJobThroughApi(jobId) {
  return api(`/api/jobs/${jobId}?include=details`);
}

export function runAction({ path }) {
  return api(path);
}

export function startJob(jobId) {
  return runAction({ path: `/api/jobs/${jobId}/start` });
}

export function loadPlan(jobId, hasPlan, force) {
  const planUrl = hasPlan
    ? `/api/jobs/${jobId}/plan`
    : `/api/jobs/${jobId}/fallback`;
  const requestUrl = planUrl + `?force=${force}`;
  return api(requestUrl);
}

export function followStats(path, since) {
  const url =
    `/api/events/${path}?since=${encodeURIComponent(since)}` +
    `&max_rows=500`;
  return new EventSource(url);
}

export function connectEvents(clientId) {
  const socket = new WebSocket(`/api/events/${clientId}`);
  socket.addEventListener("message", () => {});
  return socket;
}
