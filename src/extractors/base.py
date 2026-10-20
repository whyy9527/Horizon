"""Base extractor interface."""

from abc import ABC, abstractmethod
from typing import Optional

import httpx


class BaseExtractor(ABC):
    async def extract_with_receipt(self, url: str, client: httpx.AsyncClient) -> tuple[Optional[str], dict]:
        """Text plus per-request cleaning evidence; legacy extractors need no state."""
        return await self.extract(url, client), {}

    @abstractmethod
    async def extract(self, url: str, client: httpx.AsyncClient) -> Optional[str]:
        """Fetch and extract article text from url. Returns None on failure."""
