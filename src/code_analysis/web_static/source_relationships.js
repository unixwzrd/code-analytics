/** Read-only relationship rows; evidence meaning and navigation remain application-owned. */
import {button, disclosure, element} from '/localops/index.js';

const BASIS = {
  'lexical-call-occurrence': 'Lexical call site — not proven dispatch',
  'recorded-callee-name': 'Recorded callee name — not an invocation count',
  'saved-flow': 'Saved flow evidence',
  'structure-role': 'Saved structure role',
};

export function renderSourceRelationships(host, payload, onNavigate) {
  const root = element('section', {className: 'source-relationships',
    'aria-label': 'Saved callers, callees and structure relationships'});
  root.append(element('h4', {text: 'Related code'}),
    element('p', {className: 'hint', text:
      `${payload.records.length} of ${payload.eligible} saved relationships · ${payload.completeness}`,
      role: 'status'}));
  for (const [direction, title] of [
    ['caller', 'Callers → selected function'],
    ['callee', 'Selected function → callees'],
    ['structure', 'Structure relationships'],
  ]) {
    const rows = payload.records.filter(row => row.direction === direction);
    if (!rows.length) continue;
    const group = element('section', {'aria-label': title});
    group.append(element('h5', {text: title}));
    const list = element('ul', {className: 'code-browser-list'});
    for (const row of rows) {
      const item = element('li');
      if (row.target) item.append(button({label: row.label,
        onClick: () => onNavigate(row.target)}));
      else item.append(element('span', {text: row.label}));
      item.append(element('small', {className: 'hint', text:
        `${row.kind} · ${row.resolution} · ${BASIS[row.basis] || row.basis}`}));
      list.append(item);
    }
    group.append(list); root.append(group);
  }
  if (!payload.records.length) root.append(element('p', {className: 'hint', text:
    'No retained relationships match this selection. This does not prove there are no callers or data relationships.'}));
  if (payload.omitted) root.append(element('p', {className: 'hint', text:
    `${payload.omitted} relationships omitted by the display bound; this view is partial.`}));
  const limitations = element('div');
  for (const text of payload.limitations) limitations.append(element('p', {className: 'hint', text}));
  root.append(disclosure({title: 'Evidence scope and limitations', content: limitations}));
  host.replaceChildren(root);
  return root;
}
