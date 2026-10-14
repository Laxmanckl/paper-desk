"""Venue adapter interface. A real venue (e.g. Kuru on Monad) implements this
in a later step; the state engine never talks to a venue directly."""
from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterator

from ..types import BlockInput


class Feed(ABC):
    name = "feed"       # short id shown on the dashboard
    label = ""           # human description of the venue

    @abstractmethod
    def blocks(self) -> Iterator[BlockInput]:
        """Yield one BlockInput per block, in time order."""
