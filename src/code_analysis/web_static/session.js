import {button, confirmDialog} from '/localops/index.js';

/** Application lifecycle integration; closing/reloading a tab never stops jobs. */
export async function installSessionControl(token, drafts) {
  const actions = document.querySelector('.lui-shell__actions');
  const status = document.createElement('span');
  status.setAttribute('role', 'status');
  status.className = 'description';
  async function call(path, body) {
    const response = await fetch(path, {
      method: body ? 'POST' : 'GET',
      headers: {'X-Code-Analysis-Token': token, ...(body ? {'Content-Type': 'application/json'} : {})},
      ...(body ? {body: JSON.stringify(body)} : {}),
    });
    const result = await response.json();
    if (!response.ok) throw new Error(result.error?.message || 'Unable to stop Code Analysis.');
    return result;
  }
  try {
    if (!(await call('/api/session')).shutdown_available) return;
  } catch {
    // Older running processes or a failed status request do not gate navigation.
    status.textContent = 'Server controls unavailable. Use the owning terminal or restart the UI.';
    actions.append(status);
    return;
  }
  const stop = button({label: 'Stop Code Analysis', onClick: async () => {
    if (!drafts.canNavigate()) return;
    stop.disabled = true;
    status.textContent = '';
    try {
      const state = await call('/api/session');
      const dirty = drafts.hasDirty() ? ' Session-only drafts will be lost; cancel and export them first if needed.' : '';
      const confirmed = await confirmDialog({
        title: 'Stop Code Analysis?',
        description: (state.active_jobs
          ? `Cancel ${state.active_jobs} active collection/profile job(s) and stop this server? Forced cancellation may prevent complete capture.`
          : 'Stop this server? Every tab connected to this session will disconnect. Saved evidence is retained.') + dirty,
        confirmLabel: state.active_jobs ? 'Cancel jobs and stop' : 'Stop server',
        cancelLabel: 'Keep running', destructive: true,
      });
      if (!confirmed) return;
      await call('/api/session/stop', {confirmed: true, cancel_jobs: state.active_jobs > 0});
      stop.textContent = 'Stopping…';
      status.textContent = 'Shutdown requested. Collection cleanup may take a few seconds. You can close this tab; run code-analysis to reopen later.';
      return;
    } catch (error) {
      status.textContent = error.message;
    } finally {
      if (stop.textContent !== 'Stopping…') stop.disabled = false;
    }
  }});
  stop.title = 'Explicitly stop this UI server. Closing the browser alone leaves it running.';
  actions.append(stop, status);
}
