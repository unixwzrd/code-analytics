import {candidatePacket} from './candidate.js';
import {disclosure, element} from '/localops/index.js';

// Render the same bounded evidence neighborhood returned by the CLI query service.
export function evidenceViewer(api, escapeHtml, {reportIdentity, onBrowseSource} = {}) {
  const dialog = document.querySelector('#evidence-dialog');
  const content = document.querySelector('#evidence-content');
  let controller, originCleanup;
  dialog.addEventListener('close', () => { controller?.abort(); originCleanup?.(); });

  async function open(kind, identity, after = '', packetOffset = 0, binding = null) {
    originCleanup?.();
    controller?.abort();
    controller = new AbortController();
    const current = controller;
    const originReport = binding?.report_sha256 || reportIdentity?.();
    if (binding?.signal?.aborted) { if (dialog.open) dialog.close(); return; }
    if (binding?.signal) {
      const abort = () => {
        if (controller === current) { current.abort(); if (dialog.open) dialog.close(); }
      };
      binding.signal.addEventListener('abort', abort, {once: true});
      originCleanup = () => binding.signal.removeEventListener('abort', abort);
    } else originCleanup = null;
    function bound(params) {
      if (originReport) params.set('report_sha256', originReport);
      if (binding) {
        params.set('report_sha256', binding.report_sha256);
        if (binding.decision_ledger_sha256) params.set('decision_ledger_sha256', binding.decision_ledger_sha256);
      }
      return params;
    }
    if (!dialog.open) dialog.showModal();
    document.querySelector('#evidence-title').textContent = identity;
    content.textContent = 'Loading evidence…';
    try {
      if (kind === 'test' && binding) {
        const detail = await api(`/api/test-detail?${bound(new URLSearchParams({identity}))}`, current.signal);
        current.signal.throwIfAborted();
        const row = detail.row;
        const coverage = row.detail.target_coverage;
        content.innerHTML = `<h3>Static test impact</h3><p>${escapeHtml(row.relationship_kind || row.record_kind)} · resolved: ${escapeHtml(String(row.resolved))}</p><p>Target: ${escapeHtml(row.target || 'No recorded application target')}. Static references indicate likely impact, not execution or behavioral coverage.</p>` +
          `<h4>Saved target coverage</h4>` +
          (coverage ? `<p>Collection section: ${escapeHtml(coverage.section_state)}; ${coverage.omitted} observations omitted.</p>` +
            (coverage.observations.length ? coverage.observations.map(item => `<article><p>${escapeHtml(item.binding)} · ${escapeHtml(item.completeness)} · ${item.covered_lines} covered / ${item.missing_lines} missing lines; ${item.contexts} contexts. These counts do not prove this test executed.</p><button class="detail-button" data-coverage-id="${escapeHtml(item.coverage_id)}">Coverage artifact evidence</button></article>`).join('') : '<p>No saved coverage observation for this target. This is not zero coverage.</p>') : '<p>No exact application subject is recorded; target coverage cannot be inferred.</p>') +
          `<h4>Recorded source choices</h4><p>${detail.omitted_sources} source choices omitted. File-level test previews are not application evidence.</p>` +
          detail.sources.map(choice => `<article><p>${escapeHtml(choice.role)} · ${escapeHtml(choice.path)}:${choice.start}–${choice.end}</p><button class="detail-button" data-test-choice="${escapeHtml(choice.choice_id)}">Preview ${escapeHtml(choice.role)} source</button><div class="source-preview" aria-live="polite"></div></article>`).join('');
        content.querySelectorAll('[data-coverage-id]').forEach(button => button.addEventListener('click', () => open('coverage', button.dataset.coverageId, '', 0, binding)));
        content.querySelectorAll('[data-test-choice]').forEach(button => button.addEventListener('click', async () => {
          const output = button.nextElementSibling;
          output.textContent = 'Verifying saved source hash…';
          try {
            const params = bound(new URLSearchParams({identity, choice_id: button.dataset.testChoice}));
            const preview = await api(`/api/test-source?${params}`, current.signal);
            current.signal.throwIfAborted();
            output.textContent = `${preview.state}: ${preview.message}\n${preview.expected_sha256}`;
            if (preview.lines.length) {
              const pre = document.createElement('pre');
              pre.textContent = preview.lines.map(line => `${line.number}: ${line.text}`).join('\n');
              output.append(pre);
            }
          } catch (error) { if (!current.signal.aborted) output.textContent = error.message; }
        }));
      } else if (kind === 'navigation') {
        const params = bound(new URLSearchParams({identity, offset: after || '0'}));
        const page = await api(`/api/navigation?${params}`, current.signal);
        current.signal.throwIfAborted();
        content.innerHTML = `<h3>Symbols and source</h3><p>Source completeness: ${escapeHtml(page.completeness)}. ${page.records.length} of ${page.eligible} source records.</p>${page.records.map(item => `<article class="panel"><h4>${escapeHtml(item.label)}</h4><p>${escapeHtml(item.kind)} · <code>${escapeHtml(item.span.path)}:${item.span.start_line}–${item.span.end_line}</code></p><button class="detail-button" data-source-path="${escapeHtml(item.span.path)}" data-start="${item.span.start_line}" data-end="${item.span.end_line}">Preview source</button></article>`).join('')}${page.next_offset !== null ? '<button id="navigation-next" class="detail-button">Next source records</button>' : ''}`;
        content.querySelector('#navigation-next')?.addEventListener('click', () => open(kind, identity, String(page.next_offset), 0, binding));
      } else {
      const params = bound(new URLSearchParams({kind, identity, limit: '25', after}));
      const result = await api(`/api/evidence?${params}`, current.signal);
      current.signal.throwIfAborted();
      const packet = kind === 'refactor-candidate' && !after
        ? await api(`/api/candidate?${bound(new URLSearchParams({identity, offset: String(packetOffset)}))}`, current.signal) : null;
      current.signal.throwIfAborted();
      const items = result.items.map(item => {
        const spans = item.evidence.map(evidence => {
          const span = evidence.span;
          return `<li><code>${escapeHtml(span.path)}:${span.start_line}–${span.end_line}</code> — ${escapeHtml(evidence.observation)} <span class="badge">${escapeHtml(evidence.confidence)}</span> <button class="detail-button" data-source-path="${escapeHtml(span.path)}" data-start="${span.start_line}" data-end="${span.end_line}">Preview source</button></li>`;
        }).join('');
        return `<article class="panel"><p class="eyebrow">${escapeHtml(item.kind)}</p><h3>${escapeHtml(item.identity)}</h3><p>${escapeHtml(item.statement)}</p><ul>${spans}</ul>${item.limitations.map(value => `<p class="hint">${escapeHtml(value)}</p>`).join('')}</article>`;
      }).join('');
      content.innerHTML = `${packet ? candidatePacket(packet, escapeHtml) : ''}<h3>Source evidence</h3><p>Source completeness: <strong>${escapeHtml(result.source_completeness)}</strong>. ${result.returned_count} evidence records on this page.</p>${items || '<p>No related records in this query.</p>'}${result.next_identity ? '<button id="evidence-next" class="detail-button">Next evidence page</button>' : ''}`;
      if (kind === 'finding' && !result.items.some(item => item.evidence.some(value => value.span?.path && value.span.start_line > 0))) {
        content.prepend(element('p', {className: 'hint', text: 'No usable saved source span is recorded for this finding. These Details do not infer a source location.'}));
      }
      if (result.limitations.length) content.append(disclosure({title: 'Report-wide limitations',
        content: element('ul', {}, result.limitations.map(value => element('li', {text: value})))}));
      content.querySelector('#evidence-next')?.addEventListener('click', () => open(kind, identity, result.next_identity, 0, binding));
      content.querySelector('#packet-next')?.addEventListener('click', () => open(kind, identity, '', packet.next_offset, binding));
      content.querySelector('#packet-previous')?.addEventListener('click', () => open(kind, identity, '', Math.max(0, packet.offset - packet.limit), binding));
      }
      content.querySelectorAll('[data-source-path]').forEach(button => {
        if (onBrowseSource && originReport) {
          const browse = document.createElement('button');
          browse.type = 'button'; browse.className = 'detail-button';
          browse.textContent = 'Browse code at this source';
          browse.addEventListener('click', async () => {
            browse.disabled = true;
            try {
              await onBrowseSource({report_sha256: originReport, path: button.dataset.sourcePath,
                line: Number(button.dataset.start)}, current.signal);
            } catch (error) {
              if (!current.signal.aborted) {
                browse.disabled = false;
                const message = document.createElement('p'); message.className = 'hint';
                message.textContent = `Code browser unavailable: ${error.message}`;
                browse.after(message);
              }
            }
          });
          button.after(browse);
        }
        let previewController;
        const evidenceSignal = current.signal;
        evidenceSignal.addEventListener('abort', () => previewController?.abort(), {once: true});
        const panel = document.createElement('section');
        panel.className = 'source-preview'; panel.setAttribute('aria-live', 'polite');
        button.after(panel);
        async function preview(start) {
          previewController?.abort(); previewController = new AbortController();
          if (evidenceSignal.aborted) return;
          panel.textContent = 'Verifying source…';
          try {
            const params = bound(new URLSearchParams({path: button.dataset.sourcePath, start, end: button.dataset.end}));
            const value = await api(`/api/source?${params}`, previewController.signal);
            panel.textContent = `${value.state}: ${value.message}`;
            if (value.state === 'bound') {
              const pre = document.createElement('pre'); pre.className = 'report-text';
              pre.textContent = value.lines.map(line => `${line.number}  ${line.text}`).join('\n');
              panel.append(pre);
              if (value.next_line !== null) {
                const more = document.createElement('button'); more.className = 'detail-button';
                more.textContent = 'Next source lines';
                more.addEventListener('click', () => preview(String(value.next_line)));
                panel.append(more);
              }
            }
          } catch (error) {
            if (error.name !== 'AbortError') panel.textContent = `Source preview unavailable: ${error.message}`;
          }
        }
        button.addEventListener('click', () => preview(button.dataset.start));
      });
    } catch (error) {
      if (error.name !== 'AbortError' && controller === current) content.textContent = `Unable to load evidence: ${error.message}`;
    }
  }

  return open;
}
