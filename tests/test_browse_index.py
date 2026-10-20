from __future__ import annotations

import hashlib
import importlib.util
import json
import sqlite3

import pytest
from pydantic import ValidationError

from code_analysis.browse_index import BrowseIndex
from code_analysis.browse_index_build import build_browse_index
from code_analysis.browse_index_models import BrowseIndexManifest, BrowseRequest
from code_analysis.ui_rows import BrowseTable
from code_analysis.web_service import ReportViewService


@pytest.fixture
def indexed_report(ordinary_report, tmp_path):
    service = ReportViewService(ordinary_report)
    destination = tmp_path / "index"
    manifest = build_browse_index(service, destination)
    return service, destination, manifest


def test_index_finding_continuations_equal_saved_queries(ordinary_report, tmp_path, monkeypatch):
    from code_analysis import queries
    from code_analysis.query_models import QuerySelector, QuerySelectorKind

    original = queries._entity_items
    identity = ordinary_report.findings[0].finding_id

    def expanded(report):
        entities = original(report)
        template = entities[identity]
        for number in range(101):
            key = f"synthetic-evidence-{number:03d}"
            entities[key] = template.model_copy(
                update={"item_id": key, "identity": key, "related_ids": (identity,)}
            )
        return entities

    monkeypatch.setattr(queries, "_entity_items", expanded)
    service = ReportViewService(ordinary_report)
    destination = tmp_path / "evidence-index"
    manifest = build_browse_index(service, destination)
    index = BrowseIndex.open(destination, report_sha256=manifest.report_sha256)
    after = None
    counts = []
    prepared = queries.PreparedQueries.prepare(ordinary_report)
    while True:
        page = index.finding_evidence(identity, after)
        expected = prepared.build(
            QuerySelector(kind=QuerySelectorKind.FINDING, identity=identity), after=after
        )
        assert page.model_dump_json() == expected.model_dump_json()
        counts.append(page.returned_count)
        if page.next_identity is None:
            break
        after = page.next_identity
    assert len(counts) >= 3 and counts[:2] == [50, 50]
    if importlib.util.find_spec("textual") is not None:
        import asyncio

        from code_analysis.tui_app import AnalysisTerminal
        from code_analysis.tui_evidence import FindingEvidence

        async def page_in_terminal():
            app = AnalysisTerminal(index)
            async with app.run_test(size=(80, 24)) as pilot:
                app.push_screen(FindingEvidence(index, identity, app.evidence_closed))
                await pilot.pause()
                await app.workers.wait_for_complete()
                first = app.screen.page.next_identity
                await pilot.press("ctrl+n")
                await app.workers.wait_for_complete()
                await pilot.pause()
                assert app.screen.page.after_identity == first
                assert app.screen.page.returned_count == 50
                await pilot.press("escape")
                await pilot.press("ctrl+c")

        asyncio.run(page_in_terminal())
    with pytest.raises(ValueError, match="not present"):
        index.finding_evidence(identity, "bad-cursor")
    with pytest.raises(ValueError, match="not present"):
        index.finding_evidence("' OR 1=1 --")
    with sqlite3.connect(index.database) as connection:
        connection.execute("UPDATE finding_pages SET payload = '{}' ")
    with pytest.raises(ValueError, match="changed"):
        index.finding_evidence(identity)


def test_terminal_finding_evidence_is_saved_query(indexed_report, monkeypatch):
    import asyncio

    pytest.importorskip("textual")
    from code_analysis.tui_app import AnalysisTerminal
    from code_analysis.tui_evidence import FindingEvidence

    service, destination, manifest = indexed_report
    index = BrowseIndex.open(destination, report_sha256=manifest.report_sha256)

    async def exercise():
        app = AnalysisTerminal(index)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.press("f3")
            await app.workers.wait_for_complete()
            await pilot.pause()
            identity = app.current_page.records[0].finding_id
            await pilot.press("enter")
            await app.workers.wait_for_complete()
            await pilot.pause()
            assert isinstance(app.screen, FindingEvidence)
            assert app.screen.page == service.evidence(kind="finding", identity=identity)
            await pilot.press("escape")
            await pilot.pause()
            assert app.focused.id == "records"

            def damaged(*args, **kwargs):
                raise ValueError("damaged evidence")

            monkeypatch.setattr(BrowseIndex, "finding_evidence", damaged)
            await pilot.press("enter")
            await app.workers.wait_for_complete()
            await pilot.pause()
            import errno

            assert app.exit_status == errno.EBADMSG
            await pilot.press("ctrl+c")
        assert app.return_value == errno.EBADMSG

    asyncio.run(exercise())


@pytest.fixture
def candidate_index(tmp_path):
    from conftest import FIXTURES

    from code_analysis.config import load_config_file
    from code_analysis.project import discover_context
    from code_analysis.service import scan_refactor

    report = scan_refactor(
        discover_context(
            FIXTURES / "proposal_project", load_config_file(FIXTURES / "proposal-config.toml")
        )
    )
    service = ReportViewService(report)
    destination = tmp_path / "candidate-index"
    manifest = build_browse_index(service, destination)
    return service, BrowseIndex.open(destination, report_sha256=manifest.report_sha256)


def test_index_candidate_packets_equal_live_service_and_fail_closed(candidate_index, monkeypatch):
    service, index = candidate_index
    expected = {
        item.candidate_id: service.candidate_packet(item.candidate_id)
        for item in service.primary.refactor_candidates
    }
    assert expected

    def forbidden(*args, **kwargs):
        raise AssertionError("packet reader cannot load report or invoke live service")

    monkeypatch.setattr(ReportViewService, "candidate_packet", forbidden)
    monkeypatch.setattr(ReportViewService, "load", forbidden)
    for identity, packet in expected.items():
        assert index.candidate_packet(identity).model_dump_json() == packet.model_dump_json()
    with pytest.raises(ValueError, match="not present"):
        index.candidate_packet("' OR 1=1 --")
    with sqlite3.connect(index.database) as connection:
        connection.execute("UPDATE candidate_packets SET payload = '{}' ")
    with pytest.raises(ValueError, match="changed"):
        index.candidate_packet(next(iter(expected)))
    raw = index.database.read_bytes()
    manifest = index.manifest.model_copy(
        update={"database_sha256": hashlib.sha256(raw).hexdigest(), "database_bytes": len(raw)}
    )
    (index.database.parent / "manifest.json").write_text(manifest.model_dump_json())
    refreshed = BrowseIndex.open(index.database.parent, report_sha256=manifest.report_sha256)
    with pytest.raises(ValidationError):
        refreshed.candidate_packet(next(iter(expected)))


def test_index_retains_candidate_continuations_and_terminal_navigation(
    candidate_index, tmp_path, monkeypatch
):
    import asyncio

    from code_analysis.ui_models import UICandidatePacket, UICount

    service, _index = candidate_index
    identity = service.primary.refactor_candidates[0].candidate_id
    original = ReportViewService.candidate_packet
    base = original(service, identity)
    requirements = tuple(
        base.qualification[0].model_copy(
            update={"requirement_id": f"candidate-qualification-{n:020x}"}
        )
        for n in range(61)
    )

    def synthetic_page(self, selected, *, limit=25, offset=0):
        page = original(self, selected, limit=limit, offset=offset)
        if selected != identity:
            return page
        values = requirements[offset : offset + limit]
        return UICandidatePacket.model_validate(
            {
                **page.model_dump(),
                "qualification": values,
                "totals": tuple(
                    UICount(label=x.label, value=61 if x.label == "qualification" else x.value)
                    for x in page.totals
                ),
                "omitted": tuple(
                    UICount(
                        label=x.label,
                        value=61 - len(values) if x.label == "qualification" else x.value,
                    )
                    for x in page.omitted
                ),
                "next_offset": offset + limit if offset + limit < 61 else None,
            }
        )

    monkeypatch.setattr(ReportViewService, "candidate_packet", synthetic_page)
    destination = tmp_path / "continued-index"
    manifest = build_browse_index(service, destination)
    index = BrowseIndex.open(destination, report_sha256=manifest.report_sha256)
    expected = tuple(synthetic_page(service, identity, offset=n) for n in (0, 25, 50))
    monkeypatch.setattr(
        ReportViewService,
        "candidate_packet",
        lambda *a, **kw: pytest.fail("source service used during indexed read"),
    )
    pages = tuple(index.candidate_packet(identity, offset=n) for n in (0, 25, 50))
    assert pages == expected
    assert tuple(record for page in pages for record in page.qualification) == requirements
    assert pages[-1].next_offset is None
    with pytest.raises(ValueError):
        index.candidate_packet(identity, offset=True)

    pytest.importorskip("textual")
    from code_analysis.tui_app import AnalysisTerminal
    from code_analysis.tui_candidate import CandidatePacket

    async def exercise():
        app = AnalysisTerminal(index)
        async with app.run_test(size=(100, 32)) as pilot:
            dialog = CandidatePacket(index, pages[0], app.evidence_closed)
            app.push_screen(dialog)
            await pilot.pause()
            for key, offset in (("ctrl+n", 25), ("ctrl+n", 50), ("ctrl+p", 25)):
                await pilot.press(key)
                await app.workers.wait_for_complete()
                assert dialog.page.offset == offset
            await pilot.press("escape")
            assert app.screen is not dialog

    asyncio.run(exercise())


def test_terminal_candidate_review_uses_indexed_packet(candidate_index):
    import asyncio

    pytest.importorskip("textual")
    from textual.widgets import Static

    from code_analysis.tui_app import AnalysisTerminal
    from code_analysis.tui_candidate import CandidatePacket

    _, index = candidate_index

    async def exercise():
        app = AnalysisTerminal(index)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.press("f4")
            await app.workers.wait_for_complete()
            await pilot.pause()
            await pilot.press("enter")
            await app.workers.wait_for_complete()
            await pilot.pause()
            assert isinstance(app.screen, CandidatePacket)
            assert app.screen.page == index.candidate_packet(app.screen.page.candidate.candidate_id)
            content = str(app.screen.query_one("#candidate-content", Static).render())
            for section in ("cost", "owners", "qualification", "proposals", "omitted"):
                assert section in content
            await pilot.press("escape")
            await pilot.press("ctrl+c")
        assert app.return_value == app.exit_status

    asyncio.run(exercise())


@pytest.mark.parametrize("table", tuple(BrowseTable))
def test_index_pages_match_service_and_do_not_load_source(
    indexed_report, monkeypatch, table
) -> None:
    service, destination, manifest = indexed_report

    def forbidden(*args, **kwargs):
        raise AssertionError("indexed browsing must not load a report or source")

    monkeypatch.setattr(ReportViewService, "load", forbidden)
    index = BrowseIndex.open(destination, report_sha256=manifest.report_sha256)
    assert index.bootstrap().model_dump_json() == service.bootstrap().model_dump_json()
    method = getattr(service, table.value.replace("-", "_"))
    queries = ("", "PACKAGE", "%", "_", "' OR 1=1 --", "Straße")
    for query in queries if table is not BrowseTable.PROVIDERS else ("",):
        options = {"query": query} if table is not BrowseTable.PROVIDERS else {}
        expected = method(offset=0, limit=1, **options)
        for offset in range(expected.eligible + 1):
            request = BrowseRequest(table=table, offset=offset, limit=1, query=query)
            actual = index.page(request)
            assert (
                actual.model_dump_json()
                == method(offset=offset, limit=1, **options).model_dump_json()
            )
        with pytest.raises(ValueError, match="offset"):
            index.page(BrowseRequest(table=table, offset=expected.eligible + 1, query=query))
    if table is BrowseTable.FINDINGS:
        for severity in ("error", "warning", "info"):
            assert index.page(BrowseRequest(table=table, severity=severity)).model_dump_json() == (
                service.findings(offset=0, limit=25, severity=severity).model_dump_json()
            )


def test_index_is_deterministic_and_never_overwrites(indexed_report, tmp_path) -> None:
    service, destination, manifest = indexed_report
    second = tmp_path / "second"
    assert build_browse_index(service, second) == manifest
    assert (destination / "index.sqlite3").read_bytes() == (second / "index.sqlite3").read_bytes()
    with pytest.raises(FileExistsError):
        build_browse_index(service, destination)
    assert not tuple(tmp_path.glob(".browse-index-*"))


def test_index_rejects_wrong_identity_tampering_and_symlinks(indexed_report, tmp_path) -> None:
    _, destination, manifest = indexed_report
    with pytest.raises(ValueError, match="different report"):
        BrowseIndex.open(destination, report_sha256="f" * 64)
    link = tmp_path / "linked"
    link.symlink_to(destination, target_is_directory=True)
    with pytest.raises(ValueError, match="non-symlink"):
        BrowseIndex.open(link, report_sha256=manifest.report_sha256)
    database = destination / "index.sqlite3"
    index = BrowseIndex.open(destination, report_sha256=manifest.report_sha256)
    with sqlite3.connect(database) as connection:
        connection.execute("DELETE FROM browse_rows WHERE table_name = 'modules'")
    with pytest.raises(ValueError, match="changed"):
        index.page(BrowseRequest(table=BrowseTable.MODULES))
    with pytest.raises(ValueError, match="identity mismatch"):
        BrowseIndex.open(destination, report_sha256=manifest.report_sha256)


def test_index_failed_preparation_leaves_no_published_or_temporary_cache(
    indexed_report, tmp_path, monkeypatch
) -> None:
    service, _, _ = indexed_report
    from code_analysis import browse_index_build

    def failed(*args, **kwargs):
        raise ValueError("synthetic assembly failure")

    monkeypatch.setattr(browse_index_build, "_write_tables", failed)
    with pytest.raises(ValueError, match="synthetic"):
        build_browse_index(service, tmp_path / "failed")
    assert not (tmp_path / "failed").exists()
    assert not tuple(tmp_path.glob(".browse-index-*"))


def test_index_validates_selected_rows_and_manifest_counts(indexed_report) -> None:
    _, destination, manifest = indexed_report
    database = destination / "index.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.execute("UPDATE browse_rows SET payload = '{}' WHERE table_name = 'modules'")
    # Even a matching checksum receipt cannot make malformed row models valid.
    updated = manifest.model_copy(
        update={
            "database_sha256": hashlib.sha256(database.read_bytes()).hexdigest(),
            "database_bytes": database.stat().st_size,
        }
    )
    (destination / "manifest.json").write_text(updated.model_dump_json())
    index = BrowseIndex.open(destination, report_sha256=manifest.report_sha256)
    with pytest.raises(ValidationError):
        index.page(BrowseRequest(table=BrowseTable.MODULES))
    payload = json.loads(manifest.model_dump_json())
    payload["tables"] = payload["tables"][:-1]
    with pytest.raises(ValidationError, match="every table"):
        BrowseIndexManifest.model_validate_json(json.dumps(payload))


@pytest.mark.parametrize(
    "invalid",
    [{"limit": True}, {"limit": 501}, {"offset": -1}, {"severity": "fatal"}, {"extra": 1}],
)
def test_index_selection_is_strict(invalid) -> None:
    with pytest.raises(ValidationError):
        BrowseRequest.model_validate({"table": BrowseTable.MODULES, **invalid})


def test_index_rejects_inapplicable_filters() -> None:
    with pytest.raises(ValidationError, match="severity"):
        BrowseRequest(table=BrowseTable.MODULES, severity="error")
    with pytest.raises(ValidationError, match="provider search"):
        BrowseRequest(table=BrowseTable.PROVIDERS, query="native")


def test_index_cli_is_source_free_and_preserves_errno(ordinary_report, tmp_path, capsys) -> None:
    import errno

    from code_analysis.cli import main
    from code_analysis.models import Completeness
    from code_analysis.reports.json_report import canonical_json

    report_path = tmp_path / "report.json"
    report_path.write_text(canonical_json(ordinary_report), encoding="utf-8")
    destination = tmp_path / "index"
    expected = 0 if ordinary_report.completeness is Completeness.COMPLETE else errno.ENODATA
    assert main(["index", str(report_path), "-o", str(destination)]) == expected
    output = capsys.readouterr()
    manifest = BrowseIndexManifest.model_validate_json(output.out)
    assert "incomplete" in output.err if expected else output.err == ""
    assert (destination / ".gitignore").read_text() == "*\n"
    assert main(["index", str(report_path), "-o", str(destination)]) == errno.EEXIST
    capsys.readouterr()
    assert (
        main(["index", str(report_path), "-o", str(tmp_path / "missing" / "index")]) == errno.EINVAL
    )
    report_path.unlink()
    index = BrowseIndex.open(destination, report_sha256=manifest.report_sha256)
    assert index.page(BrowseRequest(table=BrowseTable.MODULES)).retained > 0


def test_index_symlink_artifact_and_invalid_database_are_rejected(indexed_report, tmp_path) -> None:
    _, destination, manifest = indexed_report
    database = destination / "index.sqlite3"
    outside = tmp_path / "database"
    database.rename(outside)
    database.symlink_to(outside)
    with pytest.raises(ValueError, match="non-symlink"):
        BrowseIndex.open(destination, report_sha256=manifest.report_sha256)
    database.unlink()
    database.write_bytes(b"not sqlite")
    payload = json.loads(manifest.model_dump_json())
    payload.update(database_bytes=10, database_sha256=hashlib.sha256(b"not sqlite").hexdigest())
    (destination / "manifest.json").write_text(json.dumps(payload))
    with pytest.raises(sqlite3.DatabaseError):
        BrowseIndex.open(destination, report_sha256=manifest.report_sha256)


def test_index_paginates_large_tables_without_a_full_report(
    ordinary_report, tmp_path, monkeypatch
) -> None:
    from code_analysis import browse_index, browse_index_build
    from code_analysis.ui_rows import module_rows, table_rows

    service = ReportViewService(ordinary_report)
    overview = service.bootstrap()
    count = 1201
    expanded = overview.model_copy(
        update={
            "counts": tuple(
                item.model_copy(update={"value": count}) if item.label == "Modules" else item
                for item in overview.counts
            )
        }
    )
    first = next(module_rows(ordinary_report))

    def synthetic_rows(report, table):
        if table is BrowseTable.MODULES:
            for number in range(count):
                yield first.model_copy(
                    update={"module": f"large.module{number}", "path": f"large/module{number}.py"}
                )
        else:
            yield from table_rows(report, table)

    monkeypatch.setattr(ReportViewService, "bootstrap", lambda self: expanded)
    monkeypatch.setattr(browse_index_build, "table_rows", synthetic_rows)
    from code_analysis.module_navigation import ModuleNavigationPage

    def synthetic_navigation(report):
        for row in synthetic_rows(report, BrowseTable.MODULES):
            for view in ("incoming", "outgoing", "symbols"):
                yield ModuleNavigationPage.model_validate(
                    {
                        "module": row,
                        "view": view,
                        "source_completeness": report.completeness,
                        "offset": 0,
                        "limit": 25,
                        "eligible": 0,
                        "retained": 0,
                        "truncated": False,
                    }
                )

    monkeypatch.setattr(browse_index_build, "module_navigation_pages", synthetic_navigation)
    destination = tmp_path / "large"
    manifest = build_browse_index(service, destination)
    index = BrowseIndex.open(destination, report_sha256=manifest.report_sha256)
    queries = []
    connect = browse_index._connect

    def traced_connect(path):
        connection = connect(path)
        connection.set_trace_callback(queries.append)
        return connection

    monkeypatch.setattr(browse_index, "_connect", traced_connect)
    page = index.page(BrowseRequest(table=BrowseTable.MODULES, offset=1000, limit=7))
    assert page.eligible == count and page.retained == 7 and page.truncated
    assert page.records[0].module == "large.module1000"
    assert any("LIMIT 7 OFFSET 1000" in query for query in queries)


def test_index_preparation_bound_has_no_successful_empty_cache(
    ordinary_report, tmp_path, monkeypatch, capsys
) -> None:
    import errno

    from code_analysis import browse_index_build
    from code_analysis.cli import main
    from code_analysis.reports.json_report import canonical_json
    from code_analysis.ui_rows import module_rows

    first = next(module_rows(ordinary_report))

    def oversized(*args):
        yield first.model_copy(update={"path": "x" * (1024 * 1024 + 1)})

    monkeypatch.setattr(browse_index_build, "table_rows", oversized)
    report = tmp_path / "report.json"
    report.write_text(canonical_json(ordinary_report))
    destination = tmp_path / "bounded"
    assert main(["index", str(report), "-o", str(destination)]) == errno.ENODATA
    output = capsys.readouterr()
    assert output.out == "" and "preparation incomplete" in output.err
    assert not destination.exists() and not tuple(tmp_path.glob(".browse-index-*"))


@pytest.mark.parametrize(
    "field,value", [("database_bytes", True), ("database_bytes", 0), ("extra", 1)]
)
def test_index_receipt_rejects_coercion_and_extras(indexed_report, field, value) -> None:
    _, _, manifest = indexed_report
    payload = json.loads(manifest.model_dump_json())
    payload[field] = value
    with pytest.raises(ValidationError):
        BrowseIndexManifest.model_validate_json(json.dumps(payload))


def test_index_rejects_parent_symlink_and_nonregular_receipt(indexed_report, tmp_path) -> None:
    import os

    _, destination, manifest = indexed_report
    link = tmp_path / "parent-link"
    link.symlink_to(destination.parent, target_is_directory=True)
    with pytest.raises(ValueError, match="non-symlink"):
        BrowseIndex.open(link / destination.name, report_sha256=manifest.report_sha256)
    receipt = destination / "manifest.json"
    receipt.unlink()
    os.mkfifo(receipt)
    with pytest.raises(ValueError, match="regular"):
        BrowseIndex.open(destination, report_sha256=manifest.report_sha256)


def test_index_rejects_changed_builder_identity(indexed_report, monkeypatch) -> None:
    from code_analysis import browse_index

    _, destination, manifest = indexed_report
    monkeypatch.setattr(browse_index, "builder_identity", lambda: "f" * 64)
    with pytest.raises(ValueError, match="builder identity changed"):
        BrowseIndex.open(destination, report_sha256=manifest.report_sha256)


@pytest.mark.parametrize("profiled", [False, True])
def test_index_measurement_runs_without_report_in_separate_process(
    indexed_report, tmp_path, profiled
) -> None:
    import pstats
    import subprocess
    import sys
    from pathlib import Path

    _, destination, manifest = indexed_report
    output = tmp_path / "measurement.json"
    profile = tmp_path / "reader.prof"
    script = Path(__file__).resolve().parents[1] / "tools" / "measure_browse_index.py"
    command = [
        sys.executable,
        str(script),
        str(destination),
        str(output),
        "--report-sha256",
        manifest.report_sha256,
    ]
    if profiled:
        command.extend(["--profile", str(profile)])
    subprocess.run(command, cwd=tmp_path, check=True, capture_output=True, timeout=30)
    result = json.loads(output.read_text())
    assert result["profiled"] is profiled
    assert len(result["samples"]) == 16
    assert result["report_sha256"] == manifest.report_sha256
    if profiled:
        functions = {key[2] for key in pstats.Stats(str(profile)).stats}
        assert "page" in functions
        assert "load_json" not in functions and "_expand_payload" not in functions
