from pathlib import Path

import pytest

from paper_pipeline.convert.contract import ConversionRequest, Converter
from tests.fakes import FakeConverter


def request(tmp_path: Path) -> ConversionRequest:
    tmp_path.mkdir(parents=True, exist_ok=True)
    pdf_path = tmp_path / "source.pdf"
    pdf_path.write_bytes(b"not read by the fake")
    staging_dir = tmp_path / "staging"
    staging_dir.mkdir()
    return ConversionRequest(
        pdf_path=pdf_path,
        staging_dir=staging_dir,
        timeout_seconds=1,
    )


def assert_inside(path: Path, directory: Path) -> None:
    path.resolve().relative_to(directory.resolve())


def test_success_satisfies_converter_contract(tmp_path: Path) -> None:
    converter: Converter = FakeConverter(figure_count=2)
    conversion_request = request(tmp_path)

    result = converter.convert(conversion_request)

    assert result.ok is True
    assert result.backend == "fake"
    assert result.backend_version == "1.0"
    assert result.duration_seconds >= 0
    assert result.error is None
    assert result.transcription_path is not None
    assert result.transcription_path.read_text(encoding="utf-8") == (
        "# Fake transcription\n\nDeterministic converter output.\n"
    )
    assert len(result.figure_paths) == 2
    assert result.page_paths == [conversion_request.staging_dir / "pages" / "page1.png"]
    for output_path in [result.transcription_path, *result.figure_paths, *result.page_paths]:
        assert_inside(output_path, conversion_request.staging_dir)
        assert output_path.is_file()


def test_success_is_deterministic(tmp_path: Path) -> None:
    first = request(tmp_path / "first")
    second = request(tmp_path / "second")

    first_result = FakeConverter(figure_count=1).convert(first)
    second_result = FakeConverter(figure_count=1).convert(second)

    assert first_result.transcription_path is not None
    assert second_result.transcription_path is not None
    assert (
        first_result.transcription_path.read_bytes()
        == second_result.transcription_path.read_bytes()
    )
    assert first_result.figure_paths[0].read_bytes() == second_result.figure_paths[0].read_bytes()


def test_ordinary_failure_returns_failed_result(tmp_path: Path) -> None:
    conversion_request = request(tmp_path)

    result = FakeConverter(mode="failure").convert(conversion_request)

    assert result.ok is False
    assert result.error == "fake converter failure"
    assert result.transcription_path is None
    assert result.figure_paths == []
    assert result.page_paths == []
    assert list(conversion_request.staging_dir.iterdir()) == []


def test_empty_output_is_not_reported_as_success(tmp_path: Path) -> None:
    conversion_request = request(tmp_path)

    result = FakeConverter(mode="empty").convert(conversion_request)

    assert result.ok is False
    assert result.error == "fake converter produced an empty transcription"
    assert result.transcription_path is not None
    assert_inside(result.transcription_path, conversion_request.staging_dir)
    assert result.transcription_path.read_bytes() == b""


def test_crash_raises(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError, match="fake converter crash"):
        FakeConverter(mode="crash").convert(request(tmp_path))


def test_hang_sleeps_past_timeout(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    sleeps: list[float] = []
    monkeypatch.setattr("tests.fakes.time.sleep", sleeps.append)
    conversion_request = request(tmp_path)

    result = FakeConverter(mode="hang").convert(conversion_request)

    assert sleeps == [conversion_request.timeout_seconds + 1.0]
    assert result.ok is True
    assert result.transcription_path is not None
    assert result.transcription_path.stat().st_size > 0


def test_explicit_hang_duration_is_supported(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    sleeps: list[float] = []
    monkeypatch.setattr("tests.fakes.time.sleep", sleeps.append)

    FakeConverter(mode="hang", hang_seconds=0.25).convert(request(tmp_path))

    assert sleeps == [0.25]


def test_negative_figure_count_is_rejected() -> None:
    with pytest.raises(ValueError, match="figure_count must not be negative"):
        FakeConverter(figure_count=-1)


def test_negative_page_count_is_rejected() -> None:
    with pytest.raises(ValueError, match="page_count must not be negative"):
        FakeConverter(page_count=-1)
