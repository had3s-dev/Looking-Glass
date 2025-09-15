import time
from typing import Any, Dict, List, Optional


class LibraryCache:
    def __init__(self, max_age_seconds: int = 900) -> None:
        self.max_age = max_age_seconds
        self._data: Optional[Dict[str, List[str]]] = None
        self._ts: float = 0.0

    def get(self) -> Optional[Dict[str, List[str]]]:
        if self._data is None:
            return None
        if (time.time() - self._ts) > self.max_age:
            return None
        return self._data

    def set(self, data: Dict[str, List[str]]) -> None:
        self._data = data
        self._ts = time.time()
