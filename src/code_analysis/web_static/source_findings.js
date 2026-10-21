/** Application-owned finding meaning over saved ranges; no source analysis. */
import {disclosure, element} from './localops/index.js';

export function findingsForLine(page, line) {
  return (page?.records || []).filter(item => item.start_line <= line && item.end_line >= line);
}

export function renderSourceFindings(host, page, selected, onReveal) {
  const section = element('section', {className: 'source-findings', 'aria-label': 'Recorded source findings'});
  section.append(element('h4', {text: 'Recorded findings at this source'}));
  const matches = page.records.filter(item => !selected || selected.spans.some(span =>
    span.path === page.path && span.start_line <= item.end_line && span.end_line >= item.start_line));
  section.append(element('p', {className: 'hint', role: 'status',
    text: `${matches.length} retained range${matches.length === 1 ? '' : 's'} for this selection · ${page.completeness}. ` +
      'Markers describe recorded evidence, not an exact faulty statement or a newly analyzed draft.'}));
  if (!matches.length) section.append(element('p', {text: 'No retained findings match this location; this is not a guarantee of problem-free code.'}));
  const list = element('ul', {className: 'source-finding-list'});
  for (const item of matches.slice(0, 5)) {
    const row = element('li', {'data-severity': item.severity});
    const label = element('button', {type: 'button', className: 'detail-button',
      text: `${item.severity} · ${item.rule_id} · ${item.label} · lines ${item.start_line}–${item.end_line}`});
    label.addEventListener('click', () => onReveal(item));
    row.append(label, element('p', {className: 'hint', text: item.description})); list.append(row);
  }
  section.append(list);
  if (matches.length > 5) section.append(element('p', {className: 'hint',
    text: `${matches.length - 5} additional retained ranges remain marked in the source; use Findings for the full investigation list.`}));
  if (page.omitted) section.append(element('p', {className: 'hint',
    text: `${page.omitted} ranges omitted by the display bound; finding annotations are partial.`}));
  if (page.limitations.length) section.append(disclosure({title: 'Finding evidence scope',
    content: element('div', {}, page.limitations.map(text => element('p', {className: 'hint', text})))}));
  host.append(section);
  return section;
}
