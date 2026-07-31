from __future__ import annotations

import json
import shutil
from datetime import UTC, datetime
from pathlib import Path

import pytest

from paper_pipeline.library.model import (
    AttemptRecord,
    AttemptState,
    ConversionRecord,
    PaperMetadata,
    PaperRecord,
    RecipeRecord,
)
from paper_pipeline.library.paths import FORMAT_VERSION
from paper_pipeline.library.storage import (
    conversion_is_fresh,
    create_library,
    open_library,
    recipe_is_fresh,
    sha256_file,
)

SOURCE_HASH = "c35b21d6ca39aa7cc3b79a705d989f1a6e88b99ab43988d74048799e3db926a3"


def make_record(citekey: str = "Smith2024") -> PaperRecord:
    return PaperRecord(
        format_version=FORMAT_VERSION,
        metadata=PaperMetadata(citekey=citekey, title="A paper", authors=["Ada Smith"]),
        source_pdf=f"papers/{citekey}/source/{SOURCE_HASH}.pdf",
        source_sha256=SOURCE_HASH,
    )


def test_create_open_and_paper_round_trip(library_root: Path) -> None:
    library = create_library(library_root, name="Thesis")
    record = make_record()
    library.write_paper(record)

    reopened = open_library(library_root)
    assert reopened.info.name == "Thesis"
    assert reopened.info.format_version == FORMAT_VERSION
    assert reopened.read_paper("Smith2024") == record
    assert reopened.list_papers() == ([record], [])
    assert (library_root / "papers").is_dir()
    assert (library_root / "indexes").is_dir()
    assert (library_root / ".pp").is_dir()


def test_format_two_records_remain_compatible_with_later_optional_recipe_fields() -> None:
    record = PaperRecord.model_validate(
        {
            "format_version": FORMAT_VERSION,
            "metadata": {"citekey": "Smith2024", "title": "Incomplete"},
            "recipes": {
                "summary": {
                    "input_artifact": "papers/Smith2024/transcription.md",
                    "output_sha256": "old-hash",
                    "prompt_tokens": 100,
                }
            },
        }
    )

    recipe = record.recipes["summary"]
    assert recipe.output_artifact is None
    assert recipe.cache_write_tokens == 0


def test_serialized_models_reject_unknown_fields() -> None:
    payload = make_record().model_dump(mode="json")
    payload["metadata"]["titel"] = "misspelled"

    with pytest.raises(ValueError, match="titel"):
        PaperRecord.model_validate(payload)


def test_create_refuses_non_empty_directory(library_root: Path) -> None:
    (library_root / "keep.txt").write_text("user data", encoding="utf-8")

    with pytest.raises(ValueError, match="non-empty"):
        create_library(library_root)

    assert (library_root / "keep.txt").read_text(encoding="utf-8") == "user data"


@pytest.mark.parametrize(
    ("version_delta", "recovery_guidance"),
    [(1, r"newer.*upgrade"), (-1, r"older.*rebuild.*Zotero RDF")],
)
def test_open_rejects_incompatible_format_with_recovery_guidance(
    library_root: Path, version_delta: int, recovery_guidance: str
) -> None:
    create_library(library_root)
    info_path = library_root / "library.json"
    info = json.loads(info_path.read_text(encoding="utf-8"))
    info["format_version"] = FORMAT_VERSION + version_delta
    info_path.write_text(json.dumps(info), encoding="utf-8")

    with pytest.raises(ValueError, match=recovery_guidance):
        open_library(library_root)


@pytest.mark.parametrize(
    "citekey",
    ["", "bad/key", "bad key", ".hidden", "trailing.", "CON", "con.txt", "LPT9"],
)
def test_invalid_citekeys_are_rejected(library_root: Path, citekey: str) -> None:
    library = create_library(library_root)

    with pytest.raises(ValueError, match=r"[Ii]nvalid citekey"):
        library.write_paper(make_record(citekey))


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("source_pdf", "/outside/paper.pdf"),
        ("conversion_log", "../secret.log"),
        ("recipe_input", r"papers\Smith2024\transcription.md"),
        ("recipe_output", "C:/outside/summary.md"),
        ("recipe_log", "C:secret.log"),
    ],
)
def test_durable_path_fields_require_relative_posix_paths(
    library_root: Path, field: str, value: str
) -> None:
    library = create_library(library_root)
    record = make_record()
    now = datetime.now(UTC)
    if field == "source_pdf":
        record.source_pdf = value
    elif field == "conversion_log":
        record.conversion.last_attempt = AttemptRecord(
            id="attempt", state=AttemptState.FAILED, started_at=now, finished_at=now, log_path=value
        )
    elif field == "recipe_input":
        record.recipes["summary"] = RecipeRecord(input_artifact=value)
    elif field == "recipe_output":
        record.recipes["summary"] = RecipeRecord(output_artifact=value)
    elif field == "recipe_log":
        record.recipes["summary"] = RecipeRecord(
            last_attempt=AttemptRecord(
                id="attempt",
                state=AttemptState.FAILED,
                started_at=now,
                finished_at=now,
                log_path=value,
            )
        )
    else:  # pragma: no cover - the parameter table is exhaustive
        raise AssertionError(f"unknown durable path field: {field}")

    with pytest.raises(ValueError, match="POSIX path relative"):
        library.write_paper(record)


@pytest.mark.parametrize(
    "source_pdf",
    [
        "papers/Other2024/source/paper.pdf",
        "papers/Smith2024/not-a-source.pdf",
        "papers/Smith2024/source/nested/paper.pdf",
    ],
)
def test_source_pdf_must_be_one_file_in_its_own_source_directory(
    library_root: Path, source_pdf: str
) -> None:
    library = create_library(library_root)
    record = make_record()
    record.source_pdf = source_pdf

    with pytest.raises(ValueError, match="inside this paper's source directory"):
        library.write_paper(record)


def test_bibliographic_text_is_not_treated_as_a_path(library_root: Path) -> None:
    library = create_library(library_root)
    record = make_record()
    record.metadata.url = "https://example.test/a/../paper"
    record.metadata.title = r"Windows paths C:\research\ are valid title text"

    library.write_paper(record)

    assert library.read_paper("Smith2024") == record


@pytest.mark.parametrize(
    ("recipe", "problem"),
    [
        (RecipeRecord(input_artifact="transcription.md"), "this paper's library-relative"),
        (
            RecipeRecord(input_artifact="papers/Smith2024/source"),
            "this paper's library-relative",
        ),
        (
            RecipeRecord(output_artifact="papers/Other2024/summary.md"),
            "this paper's directory",
        ),
        (
            RecipeRecord(output_artifact="papers/Smith2024/transcription.md"),
            "reserved paper filename",
        ),
        (
            RecipeRecord(input_artifact="papers/Other2024/transcription.md"),
            "this paper's library-relative",
        ),
    ],
)
def test_recipe_artifact_paths_are_scoped_to_their_paper(
    library_root: Path, recipe: RecipeRecord, problem: str
) -> None:
    library = create_library(library_root)
    record = make_record()
    record.recipes["summary"] = recipe

    with pytest.raises(ValueError, match=problem):
        library.write_paper(record)


def test_recipe_output_filename_is_not_inferred_from_recipe_name(
    library_root: Path,
) -> None:
    library = create_library(library_root)
    record = make_record()
    record.recipes["custom"] = RecipeRecord(
        input_artifact="papers/Smith2024/transcription.md",
        output_artifact="papers/Smith2024/different-name.md",
        output_sha256="output-hash",
    )

    library.write_paper(record)

    assert library.read_paper("Smith2024").recipes["custom"].output_artifact == (
        "papers/Smith2024/different-name.md"
    )


def test_atomic_write_failure_preserves_previous_record(
    library_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    library = create_library(library_root)
    original = make_record()
    library.write_paper(original)
    replacement = original.model_copy(deep=True)
    replacement.metadata.title = "Replacement"

    def interrupted_replace(source: Path, destination: Path) -> None:
        raise OSError("simulated process interruption before rename")

    monkeypatch.setattr("paper_pipeline.library.storage.os.replace", interrupted_replace)
    with pytest.raises(OSError, match="simulated process interruption"):
        library.write_paper(replacement)

    assert library.read_paper("Smith2024") == original


def test_write_paper_rejects_symlinked_paper_directory(library_root: Path, tmp_path: Path) -> None:
    library = create_library(library_root)
    outside = tmp_path / "outside-paper"
    outside.mkdir()
    linked = library_root / "papers" / "Smith2024"
    try:
        linked.symlink_to(outside, target_is_directory=True)
    except OSError as error:
        pytest.skip(f"directory symlinks unavailable: {error}")

    with pytest.raises(ValueError, match="must not contain symlinks"):
        library.write_paper(make_record())

    assert list(outside.iterdir()) == []


def test_list_papers_reports_invalid_directories_and_continues(library_root: Path) -> None:
    library = create_library(library_root)
    valid = make_record()
    library.write_paper(valid)
    invalid_dir = library_root / "papers" / "Broken2024"
    invalid_dir.mkdir()
    (invalid_dir / "paper.json").write_text("not json", encoding="utf-8")
    (library_root / "papers" / "not-a-directory.txt").write_text("noise", encoding="utf-8")

    records, problems = library.list_papers()

    assert records == [valid]
    assert len(problems) == 2
    assert any("Broken2024" in problem for problem in problems)
    assert any("not-a-directory.txt" in problem for problem in problems)


def test_copied_library_opens_without_absolute_location_dependency(
    library_root: Path, tmp_path: Path
) -> None:
    library = create_library(library_root)
    record = make_record()
    library.write_paper(record)
    copied_root = tmp_path / "elsewhere" / "copied-library"
    shutil.copytree(library_root, copied_root)

    copied = open_library(copied_root)

    assert copied.read_paper("Smith2024") == record
    serialized = (copied_root / "papers" / "Smith2024" / "paper.json").read_text(encoding="utf-8")
    assert str(library_root) not in serialized


def test_file_hash_identifies_content_without_location_affecting_identity(tmp_path: Path) -> None:
    first = tmp_path / "first.pdf"
    copy = tmp_path / "copy.pdf"
    changed = tmp_path / "changed.pdf"
    first.write_bytes(b"source bytes")
    copy.write_bytes(b"source bytes")
    changed.write_bytes(b"changed source bytes")

    assert sha256_file(first) == sha256_file(copy)
    assert sha256_file(first) != sha256_file(changed)


def test_freshness_tracks_each_declared_input_independently() -> None:
    record = make_record()
    record.conversion = ConversionRecord(
        source_sha256=record.source_sha256,
        transcription_sha256="transcription-hash",
    )
    record.recipes = {
        "pdf-summary": RecipeRecord(
            input_artifact=record.source_pdf,
            input_sha256=record.source_sha256,
        ),
        "text-summary": RecipeRecord(
            input_artifact="papers/Smith2024/transcription.md",
            input_sha256="transcription-hash",
        ),
    }

    assert conversion_is_fresh(record)
    assert recipe_is_fresh(record, "pdf-summary")
    assert recipe_is_fresh(record, "text-summary")

    record.source_sha256 = "replacement-hash"
    assert not conversion_is_fresh(record)
    assert not recipe_is_fresh(record, "pdf-summary")
    assert recipe_is_fresh(record, "text-summary")

    record.conversion.source_sha256 = record.source_sha256
    record.conversion.transcription_sha256 = "replacement-transcription-hash"
    assert conversion_is_fresh(record)
    assert not recipe_is_fresh(record, "pdf-summary")
    assert not recipe_is_fresh(record, "text-summary")
