// Candidate decisions and estimates are supplied by the validated backend report.
export function candidatePacket(packet, escape) {
  const list = values => values.length ? `<ul>${values.map(value => `<li>${escape(value)}</li>`).join('')}</ul>` : '<p>No records on this page.</p>';
  const range = value => value.lower === null ? 'Unresolved' : `${value.lower} / ${value.likely} / ${value.upper}`;
  const section = (title, body) => `<section class="panel"><h3>${escape(title)}</h3>${body}</section>`;
  const omissions = packet.omitted.filter(item => item.value).map(item => `${item.value} ${item.label} outside this page; available through packet continuation`);
  const cost = packet.cost;
  const costRows = cost ? [
    ['Expected deleted LOC', cost.expected_deleted_loc],
    ['Added owner LOC', cost.added_owner_loc],
    ['Migration/adaptor LOC', cost.migration_adapter_loc],
    ['Net production LOC', cost.net_production_loc],
    ['New files', cost.new_files], ['New types', cost.new_types],
    ['New callbacks', cost.new_callbacks], ['New parameters', cost.new_parameters],
    ['New options', cost.new_options], ['New imports', cost.new_imports],
    ['Graph-edge change', cost.graph_edge_change],
  ].map(([label, value]) => `<dt>${escape(label)}</dt><dd>${escape(range(value))}</dd>`).join('') : '';
  const owners = packet.owners.map(item => section(item.logical_owner,
    `<p>${escape(item.owner_class)} · ${item.viable ? 'Viable alternative' : 'Rejected alternative'}</p>${list(item.rejection_reasons)}${list(item.warnings)}`)).join('');
  const proposals = packet.proposals.map(item => section(item.problem_statement,
    `<p><strong>${escape(item.state)}</strong>: ${escape(item.primary_decision_reason)}</p><p>Proposed owner: ${escape(item.proposed_logical_ownership)}</p>` +
    `<h4>Gate decisions</h4>${list(item.gate_decisions.map(gate => `${gate.gate}: ${gate.outcome} — ${gate.reason}`))}` +
    `<h4>Compatibility</h4>${list(item.compatibility_concerns)}<h4>Rollback criteria</h4>${list(item.rollback_criteria)}` +
    `<h4>Validation needs</h4>${list(item.validation_needs)}<h4>Maintainer decisions</h4>${list(item.unresolved_maintainer_decisions)}`)).join('');
  const differences = packet.differences.map(item => `${item.kind}: ${item.left_subject_id} [${item.left_identities.join(', ')}] versus ${item.right_subject_id} [${item.right_identities.join(', ')}]${item.blocking ? ' — blocking' : ''}`);
  const qualification = packet.qualification.map(item => `${item.kind}: ${item.state} — ${item.reason}`);
  return '<p class="hint">Static candidate evidence — not a live maintainer decision or authorization.</p>' +
    `<p><strong>${escape(packet.observation.state)}</strong> · Source completeness: ${escape(packet.source_completeness)} · Candidate completeness: ${escape(packet.observation.completeness)}</p>` +
    `<p>Section offset: ${packet.offset}. ${packet.totals.map(item => `${escape(item.label)}: ${packet[item.label].length}/${item.value}`).join('; ')}</p>` +
    `${packet.offset ? '<button id="packet-previous" class="detail-button">Previous packet page</button>' : ''}${packet.next_offset !== null ? '<button id="packet-next" class="detail-button">Next packet page</button>' : ''}` +
    (omissions.length ? section('Bounded packet', list(omissions)) : '') +
    section('Affected subjects', list(packet.subjects.map(item => `${item.module}: ${item.logical_name} (${item.kind})`))) +
    section('Abstraction cost', cost ? `<p>Lower / likely / upper. Negative net LOC means a predicted reduction; these are estimates.</p><dl>${costRows}</dl><p>Unresolved test requirement categories: ${cost.expected_test_requirement_categories}</p>${list(cost.derivation)}${list(cost.limitations)}` : '<p>No cost estimate recorded.</p>') +
    section('Owner alternatives', owners || '<p>No alternatives on this page; see section totals.</p>') +
    section('Proposal decisions', proposals || '<p>No proposal on this page; see section totals. This candidate remains analytical evidence.</p>') +
    section('Behavior differences', list(differences)) +
    section('Qualification requirements', list(qualification)) +
    section('Counter-evidence', list(packet.observation.counter_evidence.map(item => item.observation))) +
    section('Limitations', list(packet.observation.limitations));
}
