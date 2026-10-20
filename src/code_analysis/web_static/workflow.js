/** Application-owned workflow composition; reusable controls remain LocalOps-owned. */
import {button, disclosure, element} from '/localops/index.js';

const openedGroups = new Set();
let previousView;

export function revealWorkflowView(view, accepted = false) {
  if (!accepted && view === previousView) return;
  previousView = view;
  const control = document.querySelector(`.nav[data-view="${view}"]`);
  const group = control?.closest('details[data-workflow-group]');
  if (group) group.open = true;
}

export function mountWorkflowNavigation(workflow) {
  const nav = document.querySelector('nav[aria-label="Report views"]');
  if (!nav) return;
  const controls = new Map([...nav.querySelectorAll('.nav')].map(node => [node.dataset.view, node]));
  // Move existing controls, preserving listeners and their stable view identities.
  if (!nav.dataset.workflowMounted) {
    // Start with collection, not eighteen equal-weight choices. Accepted navigation
    // opens its group; all subsequent disclosure choices remain tab-local.
    openedGroups.add('project');
    for (const group of workflow.groups) {
      const contents = element('div');
      for (const identity of group.view_ids) {
        const control = controls.get(identity);
        if (!control) throw new Error(`Workflow destination is missing: ${identity}`);
        contents.append(control);
      }
      const root = disclosure({title: group.label, content: contents, open: openedGroups.has(group.group_id)});
      root.dataset.workflowGroup = group.group_id;
      root.addEventListener('toggle', () => {
        if (root.open) openedGroups.add(group.group_id);
        else openedGroups.delete(group.group_id);
      });
      nav.append(root);
    }
    nav.dataset.workflowMounted = 'true';
  }
  for (const definition of workflow.views) {
    const control = controls.get(definition.view_id);
    if (!control) continue;
    control.disabled = false;
    const unavailable = ['missing', 'failed'].includes(definition.readiness.state);
    // These controls open an explanation when prerequisites are missing; they
    // remain operable rather than advertising a disabled action to assistive tools.
    control.removeAttribute('aria-disabled');
    control.dataset.readiness = definition.readiness.state;
    control.title = definition.purpose;
    const id = `workflow-nav-${definition.view_id}`;
    let hint = document.getElementById(id);
    if (!hint) {
      hint = element('span', {id, className: 'visually-hidden'});
      control.after(hint);
    }
    hint.textContent = definition.readiness.reason + (unavailable ? ' Activate for prerequisites and corrective actions.' : '');
    control.setAttribute('aria-describedby', id);
  }
}

export function workflowDestination(workflow, view) {
  return workflow?.views.find(item => item.view_id === view);
}

export function guideLink(definition) {
  const link = element('a', {className: 'detail-button', text: 'Read the guide',
    href: `/help/${encodeURIComponent(definition.help_document_id)}.html#${encodeURIComponent(definition.help_anchor)}`});
  link.title = 'Open the packaged guide alongside this investigation.';
  return link;
}

export function renderWorkflowGuidance(container, workflow, view, navigate) {
  container.replaceChildren();
  const definition = workflowDestination(workflow, view);
  if (!definition) return;
  const text = element('div');
  text.append(element('p', {text: definition.usage}));
  const required = element('ul');
  for (const item of definition.evidence_required) required.append(element('li', {text: item}));
  text.append(required, element('p', {text: definition.readiness.reason}));
  if (definition.next_view && definition.next_view !== view) {
    const destination = workflowDestination(workflow, definition.next_view);
    text.append(button({label: `Next: ${destination?.label || definition.next_view}`,
      onClick: () => navigate(definition.next_view)}));
  }
  text.append(guideLink(definition));
  container.append(disclosure({title: 'How to use this view', content: text}));
}

export function showWorkflowUnavailable(workflow, view, navigate) {
  const definition = workflowDestination(workflow, view);
  const notice = document.querySelector('#workflow-notice');
  if (!definition || !notice) return;
  notice.hidden = false;
  notice.replaceChildren(element('strong', {text: `${definition.label}: ${definition.readiness.state}`}),
    element('p', {text: definition.readiness.reason}));
  if (definition.next_view && definition.next_view !== view) {
    notice.append(button({label: `Go to ${workflowDestination(workflow, definition.next_view)?.label || definition.next_view}`,
      onClick: () => navigate(definition.next_view)}));
  }
  notice.tabIndex = -1;
  notice.focus();
}

function startingAction(workflow, findingCount) {
  if (workflow.reports.length) {
    if (['failed', 'missing'].includes(workflow.overall_readiness.state)) {
      return {action_id: 'inspect-diagnostics', label: 'Check collection issues', view_id: 'diagnostics',
        reason: workflow.overall_readiness.reason};
    }
    return findingCount === 0
      ? {action_id: 'inspect-modules', label: 'Explore modules', view_id: 'modules', reason: 'No findings were recorded. Explore the saved module inventory and relationships.'}
      : {action_id: 'inspect-findings', label: 'Investigate findings', view_id: 'findings', reason: 'Start with a finding, inspect its supporting evidence, then return to your investigation.'};
  }
  if (workflow.runtime.available) return {action_id: 'inspect-runtime', label: 'Explore Python profile',
    view_id: 'runtime', reason: 'Inspect saved call counts and timings within the declared capture scope.'};
  return workflow.collection.enabled
    ? {action_id: 'plan-scan', label: 'Analyze code', view_id: 'collection', operation: 'scan', reason: 'Choose an existing source folder. Static analysis reads code; it does not run your application.'}
    : {action_id: 'load-saved-evidence', label: 'Open saved runs', view_id: 'runs', reason: 'Open saved evidence, or configure a collection root to analyze code.'};
}

export function workflowOverview(workflow, navigate, findingCount) {
  const root = element('section', {className: 'workflow-overview', 'aria-label': 'Project and investigation workflow'});
  const primary = startingAction(workflow, findingCount);
  const start = element('section', {className: 'workflow-start panel', 'aria-label': 'Start here'});
  const action = button({label: primary.label, variant: 'primary', onClick: () => navigate(primary.view_id, {operation: primary.operation})});
  action.classList.add('workflow-primary');
  start.append(element('p', {className: 'eyebrow', text: 'Start here'}),
    element('h2', {text: 'What would you like to investigate?'}), element('p', {text: primary.reason}), action);
  if (['partial', 'failed'].includes(workflow.overall_readiness.state)) {
    start.append(element('p', {className: 'hint', text: `Collection ${workflow.overall_readiness.state}: ${workflow.overall_readiness.reason}`}));
  }
  root.append(start);
  const actions = element('div', {className: 'workflow-secondary workflow-actions'});
  const investigation = element('div', {className: 'workflow-secondary workflow-actions'});
  for (const item of workflow.actions.filter(item => item.action_id !== primary.action_id)) {
    const labels = {'load-saved-evidence': 'Open saved runs', 'plan-profile': 'Collect a Python profile', 'plan-import-profile': 'Import a Python profile'};
    const target = Object.hasOwn(labels, item.action_id) ? actions : investigation;
    target.append(button({label: labels[item.action_id] || item.label, onClick: () => navigate(item.view_id, {operation: item.operation})}));
  }
  if (actions.childElementCount) root.append(actions);
  if (investigation.childElementCount) {
    const other = disclosure({title: 'Other investigation options', content: investigation});
    other.classList.add('workflow-other-actions');
    root.append(other);
  }
  const context = element('dl', {className: 'workflow-context'});
  const pairs = [
    ['Execution host', workflow.collection.host], ['Collection root', workflow.collection.root || 'Not configured'],
    ['Trusted interpreter', workflow.collection.interpreter],
    ['Report target', workflow.reports[0]?.target_root || 'No static report selected'],
    ['Static report', workflow.reports[0]?.report_sha256 || 'Not loaded'],
    ['Comparison report', workflow.reports[1]?.report_sha256 || 'Not selected'],
    ['Runtime profile', workflow.runtime.profile_report_sha256 || 'Not loaded'],
    ['Runtime comparison', workflow.comparison_profile_report_sha256 || 'Not selected'],
    ['Source access', workflow.source_access === 'not-configured' ? 'Not configured; saved views still work' : 'Configured; freshness checked only on explicit source reads'],
    ['Required evidence', workflow.overall_readiness.reason],
  ];
  for (const [label, value] of pairs) context.append(element('dt', {text: label}), element('dd', {text: value}));
  const contextDetails = disclosure({title: 'Exact paths, interpreter and evidence identities', content: context});
  const summary = element('section', {className: 'workflow-summary panel', 'aria-label': 'Collection context and health'});
  summary.append(element('h2', {text: 'Current context'}),
    element('p', {className: 'hint', text: `Host: ${workflow.collection.host} · Static evidence: ${workflow.reports.length ? 'loaded' : 'not selected'} · Python profile: ${workflow.runtime.available ? 'loaded' : 'not selected'}`}), contextDetails);
  if (workflow.source_connection) {
    const source = workflow.source_connection;
    summary.append(element('p', {className: 'workflow-source-status', role: 'status',
      text: source.configured ? `Source folder: ${source.source_root}. ${source.message}` : source.message}));
    if (!source.configured && source.can_connect) summary.append(button({label: 'Choose source folder for this collection',
      onClick: () => navigate('browse')}));
  }
  root.append(summary);
  const issues = workflow.overall_readiness.optional_issues;
  if (issues.length) {
    root.append(element('p', {className: 'hint', text: workflow.overall_readiness.state === 'available'
      ? 'Core evidence available; optional gaps are listed separately from report completeness.'
      : 'Optional gaps are listed separately and do not determine core evidence readiness.'}));
    const list = element('ul');
    for (const issue of issues) list.append(element('li', {text: issue}));
    root.append(disclosure({title: `Optional evidence gaps (${issues.length})`, content: list}));
  }
  if (!workflow.reports.length) root.append(element('p', {className: 'hint',
    text: 'No static metrics are shown until a validated report is selected. This is missing evidence, not zero findings.'}));
  return root;
}
