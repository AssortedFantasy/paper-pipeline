from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable
from pathlib import Path

from ..models import PaperRecord, StepEstimate, StepResult


class ProcessingStep(ABC):
    """Base class for paper processing steps.

    Each step runs as a fresh subprocess to avoid memory leaks.
    """

    @property
    @abstractmethod
    def name(self) -> str:
        """Short identifier like 'nougat' or 'summarize'."""

    @abstractmethod
    def is_completed(self, paper_dir: Path) -> bool:
        """Return True if this step already ran successfully for the paper."""

    @abstractmethod
    def estimate(self, record: PaperRecord, paper_dir: Path) -> StepEstimate:
        """Check whether the step should be skipped and estimate work."""

    @abstractmethod
    def run(
        self,
        record: PaperRecord,
        paper_dir: Path,
        config: dict,
        on_log: Callable[[str], None] | None = None,
    ) -> StepResult:
        """Execute the step. Each invocation should be a fresh subprocess."""
