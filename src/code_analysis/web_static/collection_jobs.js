// Backend jobs survive view changes; aborting requests never cancels a job.
import {disclosure, element} from '/localops/index.js';

export function collectionJobs(root, api, post, escapeHtml, signal, workspace) {
  const panel = document.createElement('section');
  panel.className = 'panel collection-jobs';
  panel.innerHTML = `<h2>3. Confirm, run and monitor</h2>
    <p class="hint">Runs on the displayed server. Static analysis does not execute target code. Profiling executes untrusted Python with the server user's permissions, not inside a sandbox. Closing this view does not cancel a job.</p>
    <div class="projection-controls">
      <label>Total job timeout (seconds)<input id="job-timeout" type="number" min="1" max="86400" value="300"></label>
      <label>Capture limit per stream (MiB)<input id="job-stream-limit" type="number" min="1" max="256" value="64"></label>
    </div>
    <p class="hint">These backend limits are separate from the CLI profile's per-repetition timeout. Shutdown allows up to five graceful and five forced-cleanup seconds.</p>
    <label class="collection-auth"><input id="job-confirm" type="checkbox"><span>I confirm the reviewed command and writes to the selected workspace.</span></label>
    <div class="downloads"><button id="launch-job" class="detail-button" disabled>Run reviewed command</button>
    <button id="connect-jobs" class="detail-button">Load saved jobs from selected workspace</button></div>
    <p id="job-error" class="collection-error" role="alert" hidden></p>
    <p id="job-status" class="hint" role="status">No workspace connected.</p>
    <div id="job-list"></div>`;
  const limits = panel.querySelector('.projection-controls');
  const explanation = limits.nextElementSibling;
  const contents = element('div', {}, limits, explanation);
  const advanced = disclosure({title: 'Advanced job limits (300 seconds; 64 MiB per stream)', content: contents});
  panel.querySelector('.collection-auth').before(advanced);
  for (const input of contents.querySelectorAll('input')) input.addEventListener('input', () => {
    advanced.querySelector('summary').textContent = `Advanced job limits (${panel.querySelector('#job-timeout').value} seconds; ${panel.querySelector('#job-stream-limit').value} MiB per stream)`;
  });
  root.append(panel);
  let reviewed = null;
  let timer;
  let busy = false;
  const confirm = panel.querySelector('#job-confirm');
  const launch = panel.querySelector('#launch-job');
  const error = panel.querySelector('#job-error');
  function showError(value) { error.hidden = !value; error.textContent = value || ''; }
  function sync() { launch.disabled = !reviewed || !confirm.checked || busy; }
  confirm.addEventListener('change', sync);
  for (const input of panel.querySelectorAll('input[type=number]')) {
    input.addEventListener('input', () => { confirm.checked = false; sync(); });
  }
  async function refresh() {
    clearTimeout(timer);
    if (signal.aborted) return;
    try {
      const catalog = await api('/api/collection/jobs', signal);
      signal.throwIfAborted();
      panel.querySelector('#job-status').textContent = catalog.workspace
        ? `${catalog.records.length} retained jobs · ${catalog.workspace}` : 'No workspace connected. Choose a workspace, then launch or load saved jobs.';
      const active = catalog.records.some(job => ['queued', 'running'].includes(job.state));
      panel.querySelector('#job-list').innerHTML = catalog.records.map(job => `<article class="collection-job">
        <h3>${escapeHtml(job.request.operation)} · ${escapeHtml(job.state)}</h3>
        <p class="mono">${escapeHtml(job.job_id)}</p>
        <p>${escapeHtml(job.reason)}</p>
        <p class="hint">Status: ${job.status ?? 'pending'} · PID: ${job.process?.pid ?? 'not launched'} · ${escapeHtml(job.created_at)}</p>
        ${job.receipt ? `<p class="hint">Captured stdout: ${job.receipt.stdout_bytes} bytes · stderr: ${job.receipt.stderr_bytes} bytes · truncated: ${job.receipt.stream_truncated}</p>` : ''}
        ${job.binding ? `<p class="mono">Generation: ${escapeHtml(job.binding.generation_id)}</p><button class="detail-button" data-open-job="${job.job_id}">Open verified result</button>` : ''}
        ${job.receipt ? `<button class="detail-button" data-job-log="${job.job_id}">Show CLI diagnostics</button><pre class="report-text" data-log-text="${job.job_id}" hidden></pre>` : ''}
        ${['queued','running'].includes(job.state) ? `<button class="detail-button" data-cancel-job="${job.job_id}">Cancel collection</button>` : ''}
      </article>`).join('');
      if (active) timer = setTimeout(refresh, 1000);
    } catch (failure) {
      if (failure.name !== 'AbortError') {
        showError(failure.message);
        timer = setTimeout(refresh, 3000);
      }
    }
  }
  launch.addEventListener('click', async () => {
    if (!reviewed || busy || !confirm.checked) return;
    busy = true; sync(); showError('');
    try {
      const limits = {wall_seconds: Number(panel.querySelector('#job-timeout').value),
        stream_bytes: Number(panel.querySelector('#job-stream-limit').value) * 1024 * 1024,
        graceful_seconds: 5, forced_seconds: 5};
      await post('/api/collection/launch', {...reviewed, limits, confirmed: true}, signal);
      confirm.checked = false;
      await refresh();
    } catch (failure) { if (failure.name !== 'AbortError') showError(failure.message); }
    finally { busy = false; sync(); }
  });
  panel.querySelector('#connect-jobs').addEventListener('click', async () => {
    showError('');
    try {
      await post('/api/collection/connect', {workspace: workspace()}, signal);
      await refresh();
    } catch (failure) { if (failure.name !== 'AbortError') showError(failure.message); }
  });
  panel.addEventListener('click', async event => {
    const cancel = event.target.closest('[data-cancel-job]');
    const open = event.target.closest('[data-open-job]');
    const log = event.target.closest('[data-job-log]');
    if (!cancel && !open && !log) return;
    const button = cancel || open || log;
    button.disabled = true; showError('');
    try {
      if (cancel) {
        await post('/api/collection/cancel', {job_id: cancel.dataset.cancelJob}, signal);
        await refresh();
      } else if (log) {
        const payload = await post('/api/collection/logs', {job_id: log.dataset.jobLog}, signal);
        signal.throwIfAborted();
        const output = panel.querySelector(`[data-log-text="${log.dataset.jobLog}"]`);
        if (output) {
          output.hidden = false;
          output.textContent = `CLI stderr: ${payload.shown_bytes} of ${payload.total_bytes} bytes; ${payload.omitted_bytes} omitted. Workload streams remain separate profile artifacts.\n${payload.text || '(empty)'}`;
        }
        log.disabled = false;
      } else {
        // Opening changes the server's selected report. Finish the matching UI
        // reload even if the old view was left while verification was in flight.
        const opened = await post('/api/collection/open', {job_id: open.dataset.openJob});
        location.hash = opened.kind === 'profile' ? 'runtime' : 'overview';
        location.reload();
      }
    } catch (failure) {
      if (failure.name !== 'AbortError') showError(failure.message);
      button.disabled = false;
    }
  });
  signal.addEventListener('abort', () => clearTimeout(timer), {once: true});
  refresh();
  return {
    setPreview(command, request) {
      reviewed = command ? {command: structuredClone(command), request: structuredClone(request)} : null;
      confirm.checked = false; sync();
    }
  };
}
