"""Isolated local page-renderer contract tests."""

from __future__ import annotations

import os
import time
from pathlib import Path

from paper_pipeline.pages.contract import PageRenderRequest, PageRenderResult
from paper_pipeline.pages.runner import PageRendererSpec, run_page_render
from tests.fakes import FakePageRenderer

FAKE_RENDERER = "tests.fakes:FakePageRenderer"


class ProcessIdentityPageRenderer(FakePageRenderer):
    def render(self, request: PageRenderRequest) -> PageRenderResult:
        result = super().render(request)
        return PageRenderResult(
            **{
                **result.__dict__,
                "diagnostics": {"renderer_pid": str(os.getpid())},
            }
        )


def _request(tmp_path: Path, *, timeout: int = 5) -> PageRenderRequest:
    tmp_path.mkdir(parents=True, exist_ok=True)
    source = tmp_path / "source.pdf"
    source.write_bytes(b"%PDF fake")
    staging = tmp_path / "staging"
    staging.mkdir()
    return PageRenderRequest(source, staging, timeout_seconds=timeout)


def test_success_runs_in_spawned_child_and_preserves_only_page_images(tmp_path: Path) -> None:
    request = _request(tmp_path)

    result = run_page_render(
        PageRendererSpec(
            "tests.pages.test_runner:ProcessIdentityPageRenderer",
            {"page_count": 2},
        ),
        request,
    )

    assert result.ok
    assert int(result.diagnostics["renderer_pid"]) != os.getpid()
    assert [path.name for path in result.page_paths] == ["page1.png", "page2.png"]
    assert all(path.parent == request.staging_dir / "pages" for path in result.page_paths)


def test_failure_and_invalid_success_clean_staging(tmp_path: Path) -> None:
    failed_request = _request(tmp_path / "failed")
    failed = run_page_render(
        PageRendererSpec(FAKE_RENDERER, {"mode": "failure"}),
        failed_request,
    )
    assert not failed.ok
    assert list(failed_request.staging_dir.iterdir()) == []

    empty_request = _request(tmp_path / "empty")
    empty = run_page_render(
        PageRendererSpec(FAKE_RENDERER, {"mode": "empty"}),
        empty_request,
    )
    assert not empty.ok
    assert "empty" in (empty.error or "")
    assert list(empty_request.staging_dir.iterdir()) == []


def test_timeout_terminates_child_and_cleans_staging(tmp_path: Path) -> None:
    request = _request(tmp_path, timeout=1)
    started = time.monotonic()

    result = run_page_render(
        PageRendererSpec(FAKE_RENDERER, {"mode": "hang", "hang_seconds": 30}),
        request,
    )

    assert not result.ok
    assert "timed out" in (result.error or "")
    assert time.monotonic() - started < 10
    assert list(request.staging_dir.iterdir()) == []
