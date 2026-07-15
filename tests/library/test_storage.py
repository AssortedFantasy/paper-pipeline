from __future__ import annotations

import json
import shutil
import subprocess
import sys
import textwrap
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


def make_record(citekey: str = "Smith2024") -> PaperRecord:
    return PaperRecord(
        format_version=FORMAT_VERSION,
        metadata=PaperMetadata(citekey=citekey, title="A paper", authors=["Ada Smith"]),
        source_pdf=f"papers/{citekey}/source/paper.pdf",
        source_sha256="source-hash",
    )


def test_create_open_and_paper_round_trip(library_root: Path) -> None:
    library = create_library(library_root, name="Thesis")
    record = make_record()
    library.write_paper(record)

    reopened = open_library(library_root)
    assert reopened.info.name == "Thesis"
    assert reopened.info.format_version == 1
    assert reopened.read_paper("Smith2024") == record
    assert reopened.list_papers() == ([record], [])
    assert (library_root / "papers").is_dir()
    assert (library_root / "indexes").is_dir()
    assert (library_root / ".pp").is_dir()


def test_create_refuses_non_empty_directory(library_root: Path) -> None:
    (library_root / "keep.txt").write_text("user data", encoding="utf-8")

    with pytest.raises(ValueError, match="non-empty"):
        create_library(library_root)

    assert (library_root / "keep.txt").read_text(encoding="utf-8") == "user data"


def test_open_rejects_newer_format_with_upgrade_action(library_root: Path) -> None:
    create_library(library_root)
    info_path = library_root / "library.json"
    info = json.loads(info_path.read_text(encoding="utf-8"))
    info["format_version"] = FORMAT_VERSION + 1
    info_path.write_text(json.dumps(info), encoding="utf-8")

    with pytest.raises(ValueError, match=r"newer.*upgrade"):
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
        ("source_pdf", "C:/outside/paper.pdf"),
        ("source_pdf", "C:outside/paper.pdf"),
        ("source_pdf", r"papers\Smith2024\source\paper.pdf"),
        ("source_pdf", "papers/../outside.pdf"),
        ("input_artifact", "/outside/transcription.md"),
        ("log_path", "../secret.log"),
    ],
)
def test_all_path_typed_fields_require_relative_posix_paths(
    library_root: Path, field: str, value: str
) -> None:
    library = create_library(library_root)
    record = make_record()
    now = datetime.now(UTC)
    if field == "source_pdf":
        record.source_pdf = value
    elif field == "input_artifact":
        record.recipes["summary"] = RecipeRecord(input_artifact=value)
    else:
        record.conversion.last_attempt = AttemptRecord(
            id="attempt", state=AttemptState.FAILED, started_at=now, finished_at=now, log_path=value
        )

    with pytest.raises(ValueError, match="POSIX path relative"):
        library.write_paper(record)


def test_bibliographic_text_is_not_treated_as_a_path(library_root: Path) -> None:
    library = create_library(library_root)
    record = make_record()
    record.metadata.url = "https://example.test/a/../paper"
    record.metadata.title = r"Windows paths C:\research\ are valid title text"

    library.write_paper(record)

    assert library.read_paper("Smith2024") == record


def test_recipe_input_must_be_library_relative_and_scoped_to_its_paper(
    library_root: Path,
) -> None:
    library = create_library(library_root)
    record = make_record()
    record.recipes["summary"] = RecipeRecord(input_artifact="transcription.md")

    with pytest.raises(ValueError, match="this paper's library-relative"):
        library.write_paper(record)

    record.recipes["summary"] = RecipeRecord(input_artifact="papers/Smith2024/source")
    with pytest.raises(ValueError, match="this paper's library-relative"):
        library.write_paper(record)

    record.recipes["summary"] = RecipeRecord(
        output_artifact="papers/Other2024/generated/summary.md"
    )
    with pytest.raises(ValueError, match="this paper's generated directory"):
        library.write_paper(record)

    record.recipes["summary"] = RecipeRecord(input_artifact="papers/Other2024/transcription.md")
    with pytest.raises(ValueError, match="this paper's library-relative"):
        library.write_paper(record)


def test_recipe_output_filename_is_not_inferred_from_recipe_name(
    library_root: Path,
) -> None:
    library = create_library(library_root)
    record = make_record()
    record.recipes["custom"] = RecipeRecord(
        input_artifact="papers/Smith2024/transcription.md",
        output_artifact="papers/Smith2024/generated/different-name.md",
        output_sha256="output-hash",
    )

    library.write_paper(record)

    assert library.read_paper("Smith2024").recipes["custom"].output_artifact == (
        "papers/Smith2024/generated/different-name.md"
    )


def test_format_one_recipe_record_without_output_artifact_still_loads() -> None:
    legacy_json = (
        '{"format_version":1,"metadata":{"citekey":"Smith2024","title":"Legacy"},'
        '"recipes":{"summary":{"input_artifact":'
        '"papers/Smith2024/transcription.md","output_sha256":"old-hash"}}}'
    )

    record = PaperRecord.model_validate_json(legacy_json)

    assert record.recipes["summary"].output_artifact is None


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
    assert not list((library_root / ".pp" / "tmp").iterdir())


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


def test_process_killed_between_temp_write_and_rename_leaves_no_partial_record(
    library_root: Path,
) -> None:
    library = create_library(library_root)
    original = make_record()
    library.write_paper(original)
    script = textwrap.dedent(
        f"""
        import os
        from pathlib import Path
        import paper_pipeline.library.storage as storage

        library = storage.open_library(Path({str(library_root)!r}))
        record = library.read_paper("Smith2024")
        record.metadata.title = "Never installed"
        storage.os.replace = lambda source, destination: os._exit(73)
        library.write_paper(record)
        """
    )

    result = subprocess.run([sys.executable, "-c", script], check=False)

    assert result.returncode == 73
    reopened = open_library(library_root)
    assert reopened.read_paper("Smith2024") == original
    assert list((library_root / ".pp" / "tmp").glob("*.json"))


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

    assert copied.root == copied_root.resolve()
    assert copied.read_paper("Smith2024") == record
    serialized = (copied_root / "papers" / "Smith2024" / "paper.json").read_text(encoding="utf-8")
    assert str(library_root) not in serialized


def test_hash_and_freshness_helpers_compare_recorded_input_hashes(tmp_path: Path) -> None:
    source = tmp_path / "source.pdf"
    source.write_bytes(b"source bytes")
    record = make_record()
    record.source_sha256 = sha256_file(source)
    record.conversion = ConversionRecord(
        source_sha256=record.source_sha256,
        transcription_sha256="transcription-hash",
    )
    record.recipes = {
        "pdf-summary": RecipeRecord(
            input_artifact="papers/Smith2024/source/paper.pdf",
            input_sha256=record.source_sha256,
        ),
        "text-summary": RecipeRecord(
            input_artifact="papers/Smith2024/transcription.md",
            input_sha256="transcription-hash",
        ),
    }

    assert sha256_file(source) == (
        "4d4823794cbed3c4ee0bbc684c8f66e1dfd5afa6f078d494ce254ec5a4671753"
    )
    assert conversion_is_fresh(record)
    assert recipe_is_fresh(record, "pdf-summary")
    assert recipe_is_fresh(record, "text-summary")

    record.source_sha256 = "replacement-hash"
    record.conversion.transcription_sha256 = "replacement-transcription-hash"
    assert not conversion_is_fresh(record)
    assert not recipe_is_fresh(record, "pdf-summary")
    assert not recipe_is_fresh(record, "text-summary")
