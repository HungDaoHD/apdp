"""Base classes for pipeline steps."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any


class Step(ABC):
    """Abstract base class for all pipeline steps."""

    @abstractmethod
    def run(self, context: dict[str, Any]) -> dict[str, Any]:
        """
        Execute the step.

        Parameters
        ----------
        context : dict
            Shared pipeline context. Steps read inputs from and write
            outputs into this dict.

        Returns
        -------
        dict
            Updated context after this step.
        """
        ...

    def __repr__(self) -> str:
        return f"{self.__class__.__name__}()"
