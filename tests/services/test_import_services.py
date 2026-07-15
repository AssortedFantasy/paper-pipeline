from __future__ import annotations

import asyncio
import hashlib
from pathlib import Path

import pytest

from paper_pipeline.ingest.plan import ImportPlan, PlannedImport
from paper_pipeline.jobs.model import Job, JobKind, JobScope, JobState
from paper_pipeline.jobs.queue import CancellationToken
from paper_pipeline.library.model import (
    ConversionRecord,
    PaperMetadata,
    PaperRecord,
    RecipeRecord,
)
from paper_pipeline.library.storage import sha256_file
from paper_pipeline.library.validation import validate_library
from paper_pipeline.services.import_ops import apply_import, preview_import
from paper_pipeline.services.runtime import PaperSession, RuntimeRegistry

FIXTURES = Path(__file__).parents[1] / "fixtures" / "zotero"


def planned(tmp_path: Path, citekey: str, *, title: str = "Title", body: bytes = b"pdf"):
    attachment = tmp_path / f"{citekey}.pdf"
    attachment.write_bytes(body)
    return PlannedImport(
        metadata=PaperMetadata(citekey=citekey, title=title),
        attachment_path=attachment,
        attachment_sha256=hashlib.sha256(body).hexdigest(),
    )


async def read(runtime, citekey: str) -> PaperRecord:  # type: ignore[no-untyped-def]
    records: list[PaperRecord] = []

    async def worker(session, job, token):  # type: ignore[no-untyped-def]
        del job, token
        records.append(session.read_paper(citekey))

    job = await runtime.enqueue_library_read(JobKind.MAINTENANCE, "read", worker)
    await runtime.queue.wait(job.id)
    return records[0]


async def seed(runtime, record: PaperRecord) -> None:  # type: ignore[no-untyped-def]
    async def worker(session: PaperSession, job: Job, token: CancellationToken) -> None:
        del job, token
        session.write_record(record)

    job = await runtime.enqueue_paper(record.metadata.citekey, JobKind.IMPORT, "seed", worker)
    assert (await runtime.queue.wait(job.id)).state is JobState.SUCCEEDED


async def test_preview_parses_rdf_and_plans_through_library_read(tmp_path: Path) -> None:
    runtime = RuntimeRegistry().create(tmp_path / "library")

    plan = await preview_import(runtime, FIXTURES / "clean")

    assert len(plan.additions) == 5
    assert plan.problems == []
    assert runtime.queue.list_jobs()[-1].scope is JobScope.LIBRARY_READ


async def test_first_import_and_additive_reimport(tmp_path: Path) -> None:
    runtime = RuntimeRegistry().create(tmp_path / "library")
    first = planned(tmp_path, "First2024", body=b"first")

    report = await apply_import(runtime, ImportPlan(additions=[first]))

    assert report.added == ["First2024"]
    record = await read(runtime, "First2024")
    assert record.source_sha256 == first.attachment_sha256
    assert record.source_pdf is not None
    assert (runtime.root / record.source_pdf).read_bytes() == b"first"

    second = planned(tmp_path, "Second2024", body=b"second")
    additive = await apply_import(runtime, ImportPlan(additions=[second]))

    assert additive.added == ["Second2024"]
    assert (await read(runtime, "First2024")).metadata.title == "Title"
    assert (await read(runtime, "Second2024")).source_sha256 == second.attachment_sha256


async def test_metadata_refresh_preserves_artifact_provenance(tmp_path: Path) -> None:
    runtime = RuntimeRegistry().create(tmp_path / "library")
    item = planned(tmp_path, "Refresh2024", title="Old", body=b"same")
    await apply_import(runtime, ImportPlan(additions=[item]))
    record = await read(runtime, "Refresh2024")
    record.conversion = ConversionRecord(
        source_sha256=record.source_sha256,
        transcription_sha256="transcription-hash",
        backend="fake",
    )
    record.recipes["summary"] = RecipeRecord(
        input_artifact="papers/Refresh2024/transcription.md",
        input_sha256="transcription-hash",
        output_artifact="papers/Refresh2024/generated/summary.md",
        output_sha256="summary-hash",
    )
    await seed(runtime, record)
    refreshed = item.model_copy(deep=True)
    refreshed.metadata.title = "Corrected"

    report = await apply_import(runtime, ImportPlan(refreshes=[refreshed]))

    current = await read(runtime, "Refresh2024")
    assert report.refreshed == ["Refresh2024"]
    assert current.metadata.title == "Corrected"
    assert current.conversion == record.conversion
    assert current.recipes == record.recipes


async def test_explicit_replacement_makes_outputs_stale_without_deleting_them(
    tmp_path: Path,
) -> None:
    runtime = RuntimeRegistry().create(tmp_path / "library")
    old = planned(tmp_path, "Replace2024", body=b"old")
    await apply_import(runtime, ImportPlan(additions=[old]))
    record = await read(runtime, "Replace2024")
    old_source = runtime.root / (record.source_pdf or "")
    transcription = runtime.root / "papers" / "Replace2024" / "transcription.md"
    transcription.write_text("existing transcription", encoding="utf-8")
    record.conversion = ConversionRecord(
        source_sha256=record.source_sha256,
        transcription_sha256=sha256_file(transcription),
    )
    await seed(runtime, record)
    replacement = planned(tmp_path, "Replace2024", body=b"new")

    report = await apply_import(runtime, ImportPlan(source_replacements=[replacement]))

    current = await read(runtime, "Replace2024")
    assert report.replaced == ["Replace2024"]
    assert current.source_sha256 == replacement.attachment_sha256
    assert current.conversion.source_sha256 == old.attachment_sha256
    assert transcription.read_text(encoding="utf-8") == "existing transcription"
    assert old_source.read_bytes() == b"old"


async def test_missing_attachment_is_skipped_without_a_job(tmp_path: Path) -> None:
    runtime = RuntimeRegistry().create(tmp_path / "library")
    item = PlannedImport(
        metadata=PaperMetadata(citekey="Missing2024", title="Missing"),
        attachment_path=None,
        attachment_sha256=None,
    )

    report = await apply_import(runtime, ImportPlan(additions=[item]))

    assert report.jobs == []
    assert report.skipped == ["Missing2024: missing PDF attachment"]
    assert not (runtime.root / "papers" / "Missing2024").exists()


async def test_reapplying_same_plan_is_idempotent(tmp_path: Path) -> None:
    runtime = RuntimeRegistry().create(tmp_path / "library")
    item = planned(tmp_path, "Same2024", body=b"same")
    plan = ImportPlan(additions=[item])
    await apply_import(runtime, plan)
    before = (runtime.root / "papers" / "Same2024" / "paper.json").read_bytes()

    second = await apply_import(runtime, plan)

    after = (runtime.root / "papers" / "Same2024" / "paper.json").read_bytes()
    assert second.added == ["Same2024"]
    assert before == after
    assert len(list((runtime.root / "papers" / "Same2024" / "source").glob("*.pdf"))) == 1


async def test_failed_copy_leaves_no_paper_directory(tmp_path: Path) -> None:
    runtime = RuntimeRegistry().create(tmp_path / "library")
    item = planned(tmp_path, "Broken2024")
    assert item.attachment_path is not None
    item.attachment_path.unlink()

    report = await apply_import(runtime, ImportPlan(additions=[item]))

    assert "Broken2024" in report.failed
    assert not (runtime.root / "papers" / "Broken2024").exists()


async def test_interruption_after_source_install_leaves_library_valid(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime = RuntimeRegistry().create(tmp_path / "library")
    item = planned(tmp_path, "Interrupted2024")
    original = PaperSession.write_record
    writes = 0

    def fail_second_write(self: PaperSession, record: PaperRecord) -> None:
        nonlocal writes
        writes += 1
        if writes == 2:
            raise OSError("simulated interruption")
        original(self, record)

    monkeypatch.setattr(PaperSession, "write_record", fail_second_write)

    report = await apply_import(runtime, ImportPlan(additions=[item]))

    assert "Interrupted2024" in report.failed
    assert validate_library(runtime.root).ok is True
    record = await read(runtime, "Interrupted2024")
    assert record.source_pdf is None
    assert record.source_sha256 is None


async def test_import_cannot_overlap_conversion_for_same_citekey(tmp_path: Path) -> None:
    runtime = RuntimeRegistry().create(tmp_path / "library")
    initial = planned(tmp_path, "Lane2024", title="Old")
    await apply_import(runtime, ImportPlan(additions=[initial]))
    started = asyncio.Event()
    release = asyncio.Event()

    async def conversion(session: PaperSession, job: Job, token: CancellationToken) -> None:
        del session, job, token
        started.set()
        await release.wait()

    await runtime.enqueue_paper("Lane2024", JobKind.CONVERSION, "convert", conversion)
    await started.wait()
    refresh = initial.model_copy(deep=True)
    refresh.metadata.title = "New"
    applying = asyncio.create_task(apply_import(runtime, ImportPlan(refreshes=[refresh])))
    await asyncio.sleep(0)

    assert (await read(runtime, "Lane2024")).metadata.title == "Old"
    release.set()
    report = await applying

    assert report.refreshed == ["Lane2024"]
    assert (await read(runtime, "Lane2024")).metadata.title == "New"
