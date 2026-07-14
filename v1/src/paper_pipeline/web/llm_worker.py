from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from queue import Empty, Queue
from typing import TYPE_CHECKING, Any, TypeVar

from ..formatting import write_text
from ..models import PaperRecord
from ..state import (
    WORKFLOW_OUTPUTS,
    mark_workflow_completed,
    mark_workflow_failed,
    mark_workflow_running,
)

_PROMPT_FILES = {
    "intro": "prompt_intro.md",
    "method": "prompt_method.md",
}
_VALID_WORKFLOWS = frozenset(_PROMPT_FILES) | {"both"}

if TYPE_CHECKING:
    pass

ConfigDict = dict[str, object]
WorkflowInfo = dict[str, str]
WorkflowJob = tuple[str, PaperRecord, ConfigDict]
T = TypeVar("T")


@dataclass
class JobEvent:
    kind: str
    citekey: str = ""
    workflow: str = ""
    message: str = ""
    timestamp: str = ""

    def to_sse(self) -> str:
        data = json.dumps(
            {
                "kind": self.kind,
                "citekey": self.citekey,
                "workflow": self.workflow,
                "message": self.message,
                "timestamp": self.timestamp,
            }
        )
        return f"event: job\ndata: {data}\n\n"


class LlmWorkflowWorker:
    def __init__(self, papers_dir: Path, workspace_root: Path) -> None:
        self.papers_dir = papers_dir
        self.workspace_root = workspace_root
        self._queue: Queue[WorkflowJob] = Queue()
        self._subscribers: list[Queue[JobEvent]] = []
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._cancel = threading.Event()
        self._current: tuple[str, str] | None = None
        self._batch_items: list[WorkflowInfo] = []

    @property
    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    @property
    def current_job(self) -> WorkflowInfo | None:
        if self._current is None:
            return None
        workflow, citekey = self._current
        return {"workflow": workflow, "citekey": citekey}

    @property
    def queued_jobs(self) -> list[WorkflowInfo]:
        return list(self._batch_items)

    def subscribe(self) -> Queue[JobEvent]:
        q: Queue[JobEvent] = Queue()
        with self._lock:
            self._subscribers.append(q)
        return q

    def unsubscribe(self, q: Queue[JobEvent]) -> None:
        with self._lock:
            try:
                self._subscribers.remove(q)
            except ValueError:
                pass

    def _publish(self, event: JobEvent) -> None:
        if not event.timestamp:
            event.timestamp = datetime.now(timezone.utc).isoformat()
        dead: list[Queue[JobEvent]] = []
        with self._lock:
            for q in self._subscribers:
                try:
                    q.put_nowait(event)
                except Exception:
                    dead.append(q)
            for q in dead:
                try:
                    self._subscribers.remove(q)
                except ValueError:
                    pass

    def enqueue(
        self, workflow_name: str, records: list[PaperRecord], config: ConfigDict
    ) -> int:
        if self.is_running:
            return 0
        if workflow_name not in _VALID_WORKFLOWS:
            raise RuntimeError(f"Unknown workflow: {workflow_name}")

        self._cancel.clear()
        self._batch_items = []
        count = 0
        for record in records:
            self._queue.put((workflow_name, record, config))
            self._batch_items.append(
                {"workflow": workflow_name, "citekey": record.citation_key}
            )
            count += 1
        if count > 0:
            self._thread = threading.Thread(target=self._worker_loop, daemon=True)
            self._thread.start()
        return count

    def request_stop(self) -> None:
        self._cancel.set()

    def _client(self) -> Any:
        if not os.getenv("OPENAI_API_KEY"):
            raise RuntimeError("OPENAI_API_KEY is not set.")
        try:
            from openai import OpenAI
        except ImportError as exc:
            raise RuntimeError(
                "The 'openai' package is not installed. Run 'uv sync' to install dependencies."
            ) from exc
        return OpenAI()

    def _prompt_text(self, workflow_name: str) -> str:
        prompt_path = self.workspace_root / _PROMPT_FILES[workflow_name]
        if not prompt_path.exists():
            raise RuntimeError(f"Prompt file not found: {prompt_path}")
        return prompt_path.read_text(encoding="utf-8")

    def _cache_key(self, record: PaperRecord) -> str:
        if record.local_pdf is None or not record.local_pdf.exists():
            raw_key = f"paper:{record.citation_key}"
        else:
            stat = record.local_pdf.stat()
            raw_key = (
                f"paper:{record.citation_key}:"
                f"size:{stat.st_size}:mtime:{stat.st_mtime_ns}"
            )
        digest = hashlib.sha256(raw_key.encode("utf-8")).hexdigest()
        return f"paper:{digest[:32]}"

    def _retry_delay_seconds(self, attempt: int) -> float:
        return min(30.0, 2.0**attempt)

    def _is_retryable_error(self, exc: Exception) -> bool:
        status_code = getattr(exc, "status_code", None)
        if status_code in {408, 409, 429, 500, 502, 503, 504}:
            return True
        return type(exc).__name__ in {
            "APIConnectionError",
            "APITimeoutError",
            "InternalServerError",
            "RateLimitError",
        }

    def _call_with_backoff(
        self,
        action_name: str,
        func: Callable[[], T],
        citekey: str,
        workflow_name: str,
    ) -> T:
        attempts = 5
        for attempt in range(attempts):
            try:
                return func()
            except Exception as exc:
                if attempt >= attempts - 1 or not self._is_retryable_error(exc):
                    raise
                delay = self._retry_delay_seconds(attempt)
                self._publish(
                    JobEvent(
                        kind="log_line",
                        citekey=citekey,
                        workflow=workflow_name,
                        message=(
                            f"{action_name} retry {attempt + 1}/{attempts - 1} in "
                            f"{delay:.0f}s after {type(exc).__name__}: {exc}"
                        ),
                    )
                )
                time.sleep(delay)
        raise RuntimeError(f"{action_name} failed without producing a result")

    def _upload_pdf(self, client: Any, record: PaperRecord) -> Any:
        pdf_path = record.local_pdf
        if pdf_path is None or not pdf_path.exists():
            raise RuntimeError("Paper PDF is missing.")

        def upload_file() -> Any:
            with pdf_path.open("rb") as handle:
                return client.files.create(file=handle, purpose="user_data")

        uploaded = self._call_with_backoff(
            action_name="file upload",
            func=upload_file,
            citekey=record.citation_key,
            workflow_name="upload",
        )
        if uploaded is None:
            raise RuntimeError("File upload returned no file id.")
        return uploaded

    @staticmethod
    def _usage_summary(response: Any) -> str:
        usage = getattr(response, "usage", None)
        if usage is None:
            return ""
        input_tokens = getattr(usage, "input_tokens", 0) or 0
        output_tokens = getattr(usage, "output_tokens", 0) or 0
        details = getattr(usage, "input_tokens_details", None)
        cached = getattr(details, "cached_tokens", 0) or 0 if details else 0
        parts = [f"in={input_tokens}"]
        if cached:
            pct = round(100 * cached / input_tokens) if input_tokens else 0
            parts.append(f"cached={cached} ({pct}%)")
        parts.append(f"out={output_tokens}")
        return " | ".join(parts)

    def _call_llm(
        self,
        client: Any,
        workflow_name: str,
        record: PaperRecord,
        config: ConfigDict,
        file_id: str,
    ) -> tuple[str, str]:
        prompt_text = self._prompt_text(workflow_name)
        response: Any = self._call_with_backoff(
            action_name="response generation",
            func=lambda: client.responses.create(
                model=str(config.get("model") or "gpt-5-mini-2025-08-07"),
                instructions=(
                    "You process one academic paper PDF at a time. "
                    "Follow the user prompt exactly and return only the requested markdown content. "
                    "Do not wrap the answer in code fences."
                ),
                input=[
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "input_file",
                                "file_id": file_id,
                            },
                            {
                                "type": "input_text",
                                "text": prompt_text,
                            },
                        ],
                    }
                ],
                prompt_cache_key=self._cache_key(record),
                text={"verbosity": "low"},
                truncation="auto",
            ),
            citekey=record.citation_key,
            workflow_name=workflow_name,
        )
        output_text = (getattr(response, "output_text", "") or "").strip()
        if not output_text:
            raise RuntimeError("Model returned an empty response.")
        return output_text.rstrip() + "\n", self._usage_summary(response)

    def _delete_file(self, client: Any, file_id: str) -> None:
        try:
            client.files.delete(file_id)
        except Exception:
            pass

    def _process_paper(
        self,
        client: Any,
        workflow_name: str,
        record: PaperRecord,
        config: ConfigDict,
    ) -> None:
        """Upload the PDF once and run one or both workflows against it."""
        if workflow_name == "both":
            sub_workflows = list(_PROMPT_FILES)
        else:
            sub_workflows = [workflow_name]

        paper_dir = self.papers_dir / record.citation_key
        uploaded = self._upload_pdf(client, record)
        try:
            for wf in sub_workflows:
                if self._cancel.is_set():
                    break
                self._current = (wf, record.citation_key)
                self._publish(
                    JobEvent(
                        kind="workflow_started",
                        citekey=record.citation_key,
                        workflow=wf,
                    )
                )
                mark_workflow_running(paper_dir, record.citation_key, wf)
                self._publish(
                    JobEvent(
                        kind="log_line",
                        citekey=record.citation_key,
                        workflow=wf,
                        message=f"starting {wf} workflow",
                    )
                )
                try:
                    content, usage = self._call_llm(
                        client, wf, record, config, uploaded.id
                    )
                    output_path = paper_dir / WORKFLOW_OUTPUTS[wf]
                    write_text(output_path, content, overwrite=True)
                except Exception as exc:
                    error_message = str(exc)
                    mark_workflow_failed(
                        paper_dir, record.citation_key, wf, error_message
                    )
                    self._publish(
                        JobEvent(
                            kind="workflow_failed",
                            citekey=record.citation_key,
                            workflow=wf,
                            message=error_message,
                        )
                    )
                else:
                    done_msg = WORKFLOW_OUTPUTS[wf]
                    if usage:
                        done_msg += f" ({usage})"
                    mark_workflow_completed(paper_dir, record.citation_key, wf)
                    self._publish(
                        JobEvent(
                            kind="workflow_completed",
                            citekey=record.citation_key,
                            workflow=wf,
                            message=done_msg,
                        )
                    )
        finally:
            self._delete_file(client, uploaded.id)

    def _worker_loop(self) -> None:
        try:
            client = self._client()
            while not self._cancel.is_set():
                try:
                    workflow_name, record, config = self._queue.get_nowait()
                except Empty:
                    break

                self._current = (workflow_name, record.citation_key)

                try:
                    self._process_paper(client, workflow_name, record, config)
                except Exception as exc:
                    # Upload-level failure: mark all sub-workflows as failed.
                    sub = (
                        list(_PROMPT_FILES)
                        if workflow_name == "both"
                        else [workflow_name]
                    )
                    paper_dir = self.papers_dir / record.citation_key
                    for wf in sub:
                        mark_workflow_failed(
                            paper_dir,
                            record.citation_key,
                            wf,
                            str(exc),
                        )
                        self._publish(
                            JobEvent(
                                kind="workflow_failed",
                                citekey=record.citation_key,
                                workflow=wf,
                                message=str(exc),
                            )
                        )

                try:
                    self._batch_items.remove(
                        {
                            "workflow": workflow_name,
                            "citekey": record.citation_key,
                        }
                    )
                except ValueError:
                    pass

            if self._cancel.is_set():
                self._publish(JobEvent(kind="workflow_batch_cancelled"))
                while True:
                    try:
                        self._queue.get_nowait()
                    except Empty:
                        break
                self._batch_items.clear()
            else:
                self._publish(JobEvent(kind="workflow_batch_done"))
        except Exception as exc:
            self._publish(JobEvent(kind="workflow_batch_failed", message=str(exc)))
            while True:
                try:
                    workflow_name, record, _config = self._queue.get_nowait()
                except Empty:
                    break
                sub = (
                    list(_PROMPT_FILES) if workflow_name == "both" else [workflow_name]
                )
                paper_dir = self.papers_dir / record.citation_key
                for wf in sub:
                    mark_workflow_failed(
                        paper_dir,
                        record.citation_key,
                        wf,
                        str(exc),
                    )
        finally:
            self._current = None
            self._batch_items.clear()
