"""Run only explicitly selected tutorial workloads; retain genuine portable static reports.

Usage: python tutorials/qualify.py python/beginner [javascript/intermediate ...]
This is a fixture qualification helper, not a product acceptance test.
"""
import argparse
import errno
import hashlib
import json
from pathlib import Path
import shlex
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parent


def invoke(command, project):
    result = subprocess.run(command, cwd=project, capture_output=True, text=True)
    return {"command": shlex.join(command), "exit_code": result.returncode,
            "stdout": result.stdout.strip(), "stderr": result.stderr.strip()}


def observe(item, report):
    check = item["check"]
    symbols = [(module, symbol) for module in report["modules"] for symbol in module.get("symbols", [])]
    matching = [(module, symbol) for module, symbol in symbols
                if module["path"] == item["path"] and symbol["qualified_name"] == item["symbol"]]
    kind = check["kind"]
    if kind in {"symbol", "negative_control", "complexity"}:
        found = bool(matching)
        if kind == "complexity":
            found = any(symbol.get("complexity", {}).get("weighted_score", 0) >= check["min_score"]
                        for _, symbol in matching)
        if kind == "negative_control":
            found = found and not any(f.get("symbol") == item["symbol"] for f in report["findings"])
        return found
    subjects = {s["subject_id"]: s["logical_name"] for s in report["subjects"]}
    if kind == "clone_pair":
        return any(all(any(name.endswith(':' + symbol) for name in
                           [subjects.get(sid, '') for sid in cluster["subject_ids"]])
                       for symbol in check["symbols"]) for cluster in report["clone_clusters"])
    if kind == "cycle":
        return any(set(check["modules"]) == set(cycle["modules"]) for cycle in report["cycles"])
    if kind == "import_edges":
        edges = {(edge["source"], edge["target"]) for edge in report["import_edges"]}
        return all(tuple(edge) in edges for edge in check["edges"])
    if kind == "global":
        return any(s["logical_name"] == check["logical_name"] for s in report["state_resources"])
    if kind == "contract":
        return any(subjects.get(c["defining_subject_id"], '').endswith(':' + item["symbol"])
                   for c in report["contracts"])
    if kind == "shapes":
        return all(any(set(fields) <= {field["name"] for variant in shape["variants"] for field in variant["fields"]}
                       for shape in report["dictionary_shapes"]) for fields in check["field_sets"])
    if kind == "section":
        return len(report[check["section"]]) >= check.get("min_count", 1)
    raise ValueError(f"Unknown tutorial check: {kind}")


def qualify(relative):
    project = (ROOT / relative).resolve()
    if project.parent.parent != ROOT or project.parent.name not in {"python", "javascript", "ruby"}:
        raise ValueError("Choose one language/level directory within tutorials")
    evidence = project / 'evidence'
    expected = json.loads((evidence / 'expected-evidence.json').read_text())
    runs = [invoke(shlex.split(expected['commands'][key]), project) for key in ('demo', 'tests')]
    if any(run['exit_code'] for run in runs):
        print(json.dumps(runs, indent=2))
        raise RuntimeError(f"Fixture execution failed: {relative}")
    command = ['code-analysis', 'scan', '.', '--no-workspace', '--json-compact', '-o', 'evidence/actual-report.json']
    first = invoke(command, project)
    artifact = evidence / 'actual-report.json'
    if not artifact.exists():
        raise RuntimeError(f"No analyzer report: {first['stderr']}")
    original = artifact.read_bytes()
    report = json.loads(original)
    if b'/Users/' in original or b'/Volumes/' in original or b'/tmp/' in original:
        raise RuntimeError("Report has a host-specific path; investigate without editing the report")
    with tempfile.TemporaryDirectory(prefix='fawlty-rescan-') as directory:
        second_path = Path(directory) / 'report.json'
        second = invoke(command[:-1] + [str(second_path)], project)
        identical = second_path.exists() and second_path.read_bytes() == original
    source = 'lib' if project.parent.name == 'ruby' else 'src'
    paths = {item['logical_path'] for item in report['source_files']}
    assert all(path.startswith(source + '/') for path in paths), 'Evidence or documentation contaminated source discovery'
    assert paths == {p.relative_to(project).as_posix() for p in (project / source).glob('*') if p.is_file()}, 'Source inventory mismatch'
    for item in report['source_files']:
        assert hashlib.sha256((project / item['logical_path']).read_bytes()).hexdigest() == item['source_sha256']
    results = []
    for item in expected['observations']:
        status = 'deferred' if item['expectation'] == 'deferred' else 'observed' if observe(item, report) else 'missing'
        results.append((item, status))
    counts = report['summary']
    versions = [invoke([tool, '--version'], project) for tool in ('python', 'node', 'ruby')]
    lines = [f"# {expected['title']} — qualification", '',
             'Qualified on 2026-10-20. This is sample-application and evidence qualification, not human UI acceptance.', '',
             '## Application behavior', '']
    for run in runs:
        lines += [f"`{run['command']}` → exit {run['exit_code']}", '', '```text',
                  run['stdout'], run['stderr'], '```', '']
    lines += ['## Static collection', '', f"`{shlex.join(command)}` → exit {first['exit_code']}.", '',
              '`code-analysis scan . --no-workspace --json-compact -o <temporary-report.json>` repeated the same scan outside the source tree.', '',
              f"Repeat exit: {second['exit_code']}; byte-identical report: **{str(identical).lower()}**. Neither report was edited or normalized.", '',
              f"Analyzer: `{report['tool_version']}`; schema: `{report['schema_name']}@{report['schema_version']}`; completeness: **{report['completeness']}**.", '',
              f"Report SHA-256: `{hashlib.sha256(original).hexdigest()}`. Configuration SHA-256: `{report['configuration']['digest_sha256']}`.", '',
              'Source hashes were checked against every bundled file; source discovery contains only the configured source directory. Tests are separately discovered according to each provider; neither the evidence nor documentation is a source module.', '',
              '| Runtime | Observed version |', '| --- | --- |']
    for version in versions:
        lines.append(f"| `{version['command']}` | {version['stdout'] or version['stderr']} |")
    lines += ['', '| Count | Actual |', '| --- | ---: |']
    for key in ('modules', 'classes', 'functions', 'methods', 'findings', 'cycles', 'clone_clusters', 'dictionary_shapes', 'contracts', 'state_resources', 'side_effects', 'refactor_candidates', 'refactor_proposals', 'test_modules', 'test_cases', 'unresolved_edges'):
        lines.append(f"| {key} | {counts[key]} |")
    lines += [f"| diagnostic rows | {len(report['diagnostics'])} |", '', '| Provider | State | Completeness |', '| --- | --- | --- |']
    for provider in report['providers']:
        lines.append(f"| {provider['provider_id']} | {provider['state']} | {provider['completeness']} |")
    for frontend in report['language_frontends']:
        if frontend['discovered']:
            lines += ['', f"Frontend `{frontend['frontend_id']}`: {frontend['state']}; {frontend['discovered']} discovered, {frontend['complete']} complete, {frontend['partial']} partial, {frontend['failed']} failed."]
    lines += ['', '## Expected observations — answer key', '', 'Read the mission before this section.', '', '| Tutorial ID | Expectation | Actual | Interpretation |', '| --- | --- | --- | --- |']
    for item, status in results:
        lines.append(f"| {item['id']} | {item['expectation']} | {status} | {item['meaning']} {item['limitation']} |")
    matched_findings = {item['symbol'] for item, status in results if status == 'observed'}
    extras = [f"{f['category']}: {f.get('symbol') or f.get('title')}" for f in report['findings'] if f.get('symbol') not in matched_findings]
    lines += ['', 'Additional finding rows beyond symbol-specific expectations: ' + ('; '.join(extras) if extras else 'none') + '.', '',
              '## Limitations', ''] + ['- ' + value for value in report['limitations']]
    lines += ['', '## Browser verification', '', 'Not yet recorded for this regenerated report. Do not infer browser qualification from successful collection.', '']
    (evidence / 'qualification.md').write_text('\n'.join(lines))
    missing = [item['id'] for item, status in results if status == 'missing' and item['expectation'] == 'supported']
    valid = identical and first['exit_code'] in (0, errno.ENODATA) and first['exit_code'] == second['exit_code'] and report['completeness'] != 'failed' and counts['failed_files'] == 0 and not missing
    print(f"{relative}: demo/tests pass; scan={first['exit_code']} {report['completeness']}; deterministic={identical}; missing supported={missing}", flush=True)
    return valid


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('projects', nargs='+', help='Explicit language/level targets; executes their demo and tests')
    args = parser.parse_args()
    results = [qualify(project) for project in args.projects]
    raise SystemExit(0 if all(results) else 1)
