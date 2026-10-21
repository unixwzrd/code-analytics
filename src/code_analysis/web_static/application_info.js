/** Running application identity and explicit, privacy-reviewed GitHub handoff. */
import {ContentPanel, button, element} from '/localops/index.js';

const ISSUE_URL = 'https://github.com/unixwzrd/code-analytics/issues/new?template=bug_report.yml';
const VIEWS = new Set(['overview', 'collection', 'runs', 'providers', 'diagnostics',
  'modules', 'browse', 'architecture', 'contracts', 'dictionary-shapes', 'findings',
  'clones', 'state-effects', 'runtime', 'explore', 'candidates', 'test-impact', 'comparison']);

export function diagnosticSummary(info, view) {
  return `Application version: ${info.application_version}\nOS family: ${info.os_family}\n` +
    `Python version: ${info.python_version}\nCurrent view: ${VIEWS.has(view) ? view : 'unknown'}`;
}

export function issueUrl(summary) {
  const url = new URL(ISSUE_URL);
  url.searchParams.set('environment', summary);
  return url.href.length <= 2000 ? url.href : ISSUE_URL;
}

export function mountApplicationInfo({api, currentView}) {
  const footer = element('footer', {className: 'application-info', 'aria-label': 'Application information'});
  const identity = element('span', {role: 'status', text: 'Code Analysis version loading…'});
  const root = element('section');
  const warning = element('p', {id: 'issue-privacy-warning', text:
    'GitHub issues are public. Review the summary before opening GitHub. Remove private paths, tokens, source, logs and identifying details from screenshots. Nothing is uploaded or attached automatically.'});
  const summary = element('textarea', {id: 'issue-environment-summary', rows: 7,
    'aria-describedby': 'issue-privacy-warning', spellcheck: false});
  const link = element('a', {className: 'detail-button', href: ISSUE_URL, target: '_blank',
    rel: 'noopener noreferrer', text: 'Open GitHub bug report'});
  const state = element('p', {role: 'status'});
  root.append(warning, element('label', {htmlFor: summary.id, text: 'Review or edit troubleshooting summary'}),
    summary, state, link, element('p', {className: 'hint', text:
      'GitHub asks for a title, reproduction steps, and expected and actual behavior. You can select and copy this summary manually; no clipboard access is automatic.'}));
  const panel = new ContentPanel({id: 'application-issue-report', title: 'Report an issue',
    content: root, closeLabel: 'Close issue reporting', fallbackFocus: document.querySelector('#title')});
  let info;
  let failure = false;
  function updateLink() {
    link.href = issueUrl(summary.value);
    state.textContent = link.href === ISSUE_URL && summary.value ?
      'The summary is too long for a safe issue link. Copy it manually into the GitHub environment field.' : '';
  }
  summary.addEventListener('input', updateLink);
  function openIssue(opener) {
    summary.value = info ? diagnosticSummary(info, currentView()) :
      'Application version: unavailable\nOS family: not supplied\nPython version: not supplied';
    updateLink();
    if (failure) state.textContent = 'Running application details could not be loaded. You can still describe the issue manually.';
    panel.open({opener: opener instanceof HTMLElement ? opener : report});
  }
  const report = button({label: 'Report an issue', onClick: event => openIssue(event.currentTarget)});
  footer.append(identity, report);
  document.body.append(footer);
  const ready = api('/api/application-info').then(value => {
    info = value;
    identity.textContent = `Code Analysis ${value.application_version}`;
  }).catch(() => {
    failure = true;
    identity.textContent = 'Code Analysis version unavailable';
  });
  return {openIssue, ready};
}
