from __future__ import annotations

import json
import subprocess
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from queue import Empty, Queue

from ..locking import acquire_run_lock, release_run_lock
from ..models import PaperRecord
from ..state import mark_completed, mark_failed, mark_running
from ..steps.registry import get_step


@dataclass
class JobEvent:
    kind: str  # paper_started | paper_completed | paper_failed | log_line | batch_done | batch_cancelled
    citekey: str = ""
    message: str = ""
    timestamp: str = ""

    def to_sse(self) -> str:
        data = json.dumps(
            {
                "kind": self.kind,
                "citekey": self.citekey,
                "message": self.message,
                "timestamp": self.timestamp,
            }
        )
        return f"event: job\ndata: {data}\n\n"


class TranscriptionWorker:
    """Serial job queue for GPU-bound transcription work.

    The key design constraint in this repo is durability under long-lived Nougat
    runs. We intentionally run one paper at a time and share the same workspace
    lock as the CLI so the GUI cannot accidentally overlap a second GPU job.
    """

    def __init__(self, papers_dir: Path, workspace_root: Path) -> None:
        self.papers_dir = papers_dir
        self.workspace_root = workspace_root
        self._queue: Queue[tuple[PaperRecord, dict]] = Queue()
        self._subscribers: list[Queue[JobEvent]] = []
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._cancel = threading.Event()
        self._current_citekey: str | None = None
        self._batch_citekeys: list[str] = []
        self._run_lock_path: Path | None = None

    @property
    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    @property
    def current_citekey(self) -> str | None:
        return self._current_citekey

    @property
    def queued_citekeys(self) -> list[str]:
        return list(self._batch_citekeys)

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

    def enqueue(self, records: list[PaperRecord], config: dict) -> int:
        if self.is_running:
            return 0
        self._cancel.clear()
        count = 0
        self._batch_citekeys = []
        for record in records:
            self._queue.put((record, config))
            self._batch_citekeys.append(record.citation_key)
            count += 1
        if count > 0:
            self._run_lock_path = acquire_run_lock(self.workspace_root)
            self._thread = threading.Thread(target=self._worker_loop, daemon=True)
            self._thread.start()
        return count

    def request_stop(self) -> None:
        self._cancel.set()

    def _worker_loop(self) -> None:
        try:
            while not self._cancel.is_set():
                try:
                    record, config = self._queue.get_nowait()
                except Empty:
                    break

                self._current_citekey = record.citation_key
                paper_dir = self.papers_dir / record.citation_key

                self._publish(
                    JobEvent(kind="paper_started", citekey=record.citation_key)
                )
                mark_running(paper_dir, record.citation_key)

                step = get_step("nougat")

                def on_log(msg: str) -> None:
                    self._publish(
                        JobEvent(
                            kind="log_line", citekey=record.citation_key, message=msg
                        )
                    )

                config_with_workspace = {
                    **config,
                    "workspace_root": str(self.workspace_root),
                    "cancel_requested": self._cancel.is_set,
                }
                try:
                    result = step.run(
                        record, paper_dir, config_with_workspace, on_log=on_log
                    )
                except Exception as exc:
                    error_message = f"unexpected error: {exc}"
                    mark_failed(
                        paper_dir,
                        record.citation_key,
                        error_message,
                    )
                    self._publish(
                        JobEvent(
                            kind="paper_failed",
                            citekey=record.citation_key,
                            message=error_message,
                        )
                    )
                else:
                    if result.success:
                        mark_completed(paper_dir, record.citation_key)
                        self._publish(
                            JobEvent(
                                kind="paper_completed",
                                citekey=record.citation_key,
                                message=f"completed in {result.duration_seconds:.1f}s",
                            )
                        )
                    else:
                        mark_failed(
                            paper_dir,
                            record.citation_key,
                            result.error or "unknown error",
                        )
                        self._publish(
                            JobEvent(
                                kind="paper_failed",
                                citekey=record.citation_key,
                                message=result.error or "unknown error",
                            )
                        )

                # Remove from batch tracking
                try:
                    self._batch_citekeys.remove(record.citation_key)
                except ValueError:
                    pass

            if self._cancel.is_set():
                self._publish(
                    JobEvent(kind="batch_cancelled", message="batch stopped by user")
                )
                # Drain remaining items
                while True:
                    try:
                        self._queue.get_nowait()
                    except Empty:
                        break
                self._batch_citekeys.clear()
            else:
                self._publish(
                    JobEvent(kind="batch_done", message="all papers processed")
                )
        finally:
            self._current_citekey = None
            self._batch_citekeys.clear()
            if self._run_lock_path is not None:
                release_run_lock(self._run_lock_path)
                self._run_lock_path = None

    def get_gpu_status(self) -> dict | None:
        def parse_int(value: str) -> int:
            cleaned = value.strip()
            if not cleaned or cleaned.upper() == "N/A":
                return 0
            return int(cleaned)

        try:
            result = subprocess.run(
                [
                    "nvidia-smi",
                    "--query-gpu=index,name,memory.used,memory.total,utilization.gpu,temperature.gpu",
                    "--format=csv,noheader,nounits",
                ],
                capture_output=True,
                text=True,
                timeout=5,
            )
            if result.returncode != 0:
                return None
            gpus = []
            for line in result.stdout.strip().splitlines():
                parts = [p.strip() for p in line.split(",")]
                if len(parts) < 6:
                    continue
                gpus.append(
                    {
                        "index": parse_int(parts[0]),
                        "name": parts[1],
                        "memory_used_mb": parse_int(parts[2]),
                        "memory_total_mb": parse_int(parts[3]),
                        "utilization_pct": parse_int(parts[4]),
                        "temperature_c": parse_int(parts[5]),
                    }
                )

            if not gpus:
                return None

            selected = max(
                gpus,
                key=lambda gpu: (
                    gpu["memory_used_mb"] > 0,
                    gpu["utilization_pct"] > 0,
                    gpu["memory_used_mb"],
                    gpu["utilization_pct"],
                    -gpu["index"],
                ),
            )
            return {
                **selected,
                "device_count": len(gpus),
            }
        except Exception:
            pass
        return None
