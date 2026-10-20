// Source selection uses backend-owned comparison bindings, never paths parsed from IDs.
import {disclosure, element} from '/localops/index.js';

function comparisonContext(reports) {
  const context = element('section', {className: 'comparison-context', 'aria-label': 'Selected comparison reports'});
  const identities = element('dl');
  for (const [role, label] of [['primary', 'Baseline'], ['comparison', 'Candidate']]) {
    const report = reports.find(item => item.role === role);
    context.append(element('p', {text: `${label}: ${report.target_root} · ${report.completeness} · SHA-256 prefix ${report.report_sha256.slice(0, 12)}`}));
    identities.append(element('dt', {text: `${label} SHA-256`}), element('dd', {className: 'mono', text: report.report_sha256}));
  }
  context.append(disclosure({title: 'Exact report identities', content: identities}));
  return context;
}

export function wireComparisonDownloads({content, request, signal, reports, selection, search}) {
  const controls = document.createElement('div');
  controls.className = 'downloads';
  controls.innerHTML = '<p>Export this applied filtered page:</p>' +
    [['json-compact', 'JSON'], ['tsv', 'TSV'], ['markdown', 'Markdown']].map(([format, label]) =>
      `<button class="detail-button" data-comparison-export="${format}">${label}</button>`).join('') +
    '<p class="comparison-export-status" aria-live="polite"></p>';
  content.querySelector('#comparison-filters').after(controls);
  const buttons = controls.querySelectorAll('button');
  const status = controls.querySelector('.comparison-export-status');
  const invalidate = () => {
    buttons.forEach(button => { button.disabled = true; });
    status.textContent = 'Filters changed. Apply or refresh the selection before exporting.';
  };
  content.querySelector('#comparison-filters').addEventListener('input', invalidate);
  search.addEventListener('input', invalidate, {signal});
  buttons.forEach(button => button.addEventListener('click', async () => {
    const params = new URLSearchParams(selection);
    params.set('baseline_sha256', reports.find(report => report.role === 'primary').report_sha256);
    params.set('candidate_sha256', reports.find(report => report.role === 'comparison').report_sha256);
    params.set('format', button.dataset.comparisonExport);
    try {
      const response = await request(`/api/comparison-download?${params}`, signal);
      const blob = await response.blob();
      if (signal.aborted || button.disabled) return;
      const link = document.createElement('a');
      const url = URL.createObjectURL(blob);
      link.href = url;
      link.download = `comparison.${{'json-compact': 'json', tsv: 'tsv', markdown: 'md'}[button.dataset.comparisonExport]}`;
      link.click();
      setTimeout(() => URL.revokeObjectURL(url), 1000);
      status.textContent = `Exported ${response.headers.get('X-Code-Analysis-Completeness')} evidence; truncated: ${response.headers.get('X-Code-Analysis-Truncated')}.`;
    } catch (error) {
      if (!signal.aborted) status.textContent = `Export unavailable: ${error.message}`;
    }
  }));
}

export function wireComparisonSources({content, api, signal, reports, escape}) {
  const baseline = reports.find(item => item.role === 'primary').report_sha256;
  const candidate = reports.find(item => item.role === 'comparison').report_sha256;
  let revision = 0, pending, origin;
  const panel = content.querySelector('#comparison-source');
  content.querySelector('#comparison-filters').before(comparisonContext(reports));
  const reset = () => {
    revision++; pending?.abort(); panel.hidden = true;
    content.querySelectorAll('[data-comparison-position]').forEach(button => button.setAttribute('aria-expanded', 'false'));
  };
  const rows = new MutationObserver(() => {
    if (panel.hidden || !origin) return;
    content.querySelectorAll('[data-comparison-position]').forEach(button =>
      button.setAttribute('aria-expanded', String(button.dataset.comparisonPosition === origin.position)));
  });
  rows.observe(content, {childList: true, subtree: true});
  signal.addEventListener('abort', () => { rows.disconnect(); reset(); }, {once: true});
  content.querySelector('#comparison-filters').addEventListener('input', reset, {signal});
  content.querySelector('#comparison-filters').addEventListener('change', reset, {signal});
  document.querySelector('#filter').addEventListener('input', reset, {signal});
  content.addEventListener('click', async event => {
      if (event.target.closest('[data-comparison-return]')) {
        reset();
        if (origin) {
          // Toolkit column changes rebuild row DOM, but not comparison positions.
          const row = [...content.querySelectorAll('[data-comparison-position]')]
            .find(button => button.dataset.comparisonPosition === origin.position);
          const target = row || content.querySelector('#comparison-filters button');
          target?.focus({preventScroll: true});
          if (origin.scroller) origin.scroller.scrollTop = origin.scrollTop;
          const table = row?.closest('.lui-data-table__scroll') || content.querySelector('.lui-data-table__scroll');
          if (table) table.scrollLeft = origin.scrollLeft;
        }
        return;
      }
      const button = event.target.closest('[data-comparison-position]');
      if (!button || !content.contains(button)) return;
      reset();
      const current = ++revision;
      pending = new AbortController();
      const currentSignal = pending.signal;
      const scroller = content.closest('.lui-shell__content');
      const table = button.closest('.lui-data-table__scroll');
      origin = {position: button.dataset.comparisonPosition, scroller,
        scrollTop: scroller?.scrollTop || 0, scrollLeft: table?.scrollLeft || 0};
      button.setAttribute('aria-expanded', 'true');
      const params = new URLSearchParams({baseline_sha256: baseline,
        candidate_sha256: candidate, position: button.dataset.comparisonPosition});
      panel.innerHTML = '<div class="comparison-source-heading"><h2 id="comparison-source-heading">Selected source evidence</h2><button type="button" class="detail-button" data-comparison-return>Return to selected row</button></div><div data-comparison-detail aria-live="polite">Loading saved source choices…</div>';
      panel.hidden = false;
      panel.focus({preventScroll: true});
      panel.scrollIntoView({block: 'start'});
      const body = panel.querySelector('[data-comparison-detail]');
      try {
        const detail = await api(`/api/comparison-detail?${params}`, currentSignal);
        if (currentSignal.aborted || revision !== current) return;
        body.innerHTML = `<h3>Source evidence: ${escape(detail.subject_id)}</h3><div class="comparison-source-sides">` +
          ['baseline', 'candidate'].map(side => {
            const value = detail[side];
            return `<section><h4>${side === 'baseline' ? 'Baseline' : 'Candidate'}</h4>` +
              `<p>${escape(value.message)} · ${value.omitted_option_count} source choices omitted</p>` +
              (value.options.length ? `<label>Recorded ${side} source<select data-source-side="${side}">` +
                value.options.map(option => `<option value="${escape(option.option_id)}">${escape(option.label)}</option>`).join('') +
                `</select></label><button class="detail-button" data-preview-side="${side}">Preview ${side}</button>` : '') +
              `<div class="source-preview" data-source-output="${side}" aria-live="polite"></div></section>`;
          }).join('') + '</div>';
        for (const side of ['baseline', 'candidate']) {
          const previewButton = panel.querySelector(`[data-preview-side="${side}"]`);
          if (!previewButton) continue;
          let requestNumber = 0;
          const select = panel.querySelector(`[data-source-side="${side}"]`);
          const output = panel.querySelector(`[data-source-output="${side}"]`);
          select.addEventListener('change', () => { requestNumber++; output.textContent = ''; });
          previewButton.addEventListener('click', async () => {
            const request = ++requestNumber;
            const query = new URLSearchParams(params);
            query.set('side', side); query.set('option_id', select.value);
            output.textContent = 'Loading hash-bound source preview…';
            try {
              const preview = await api(`/api/comparison-source?${query}`, currentSignal);
              if (currentSignal.aborted || revision !== current || request !== requestNumber) return;
              output.innerHTML = `<p>${escape(preview.state)}: ${escape(preview.message)}</p>` +
                `<p>${escape(preview.logical_path)} · SHA-256 ${escape(preview.expected_sha256)}</p>` +
                (preview.lines.length ? `<pre>${escape(preview.lines.map(line => `${line.number}: ${line.text}`).join('\n'))}</pre>` : '');
            } catch (error) {
              if (!currentSignal.aborted && revision === current && request === requestNumber)
                output.textContent = `Source unavailable: ${error.message}`;
            }
          });
        }
      } catch (error) {
        if (!currentSignal.aborted && revision === current) body.textContent = `Source choices unavailable: ${error.message}`;
      }
  }, {signal});
}
