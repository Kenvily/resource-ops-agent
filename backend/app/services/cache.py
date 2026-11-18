"""Bounded in-process cache; keys contain only application-selected scopes."""

import asyncio
import time
from collections import OrderedDict
from typing import Generic, TypeVar

T = TypeVar("T")


class MemoryCache(Generic[T]):
    def __init__(self, max_entries: int = 64):
        self.entries: OrderedDict[tuple, tuple[float, T]] = OrderedDict()
        self.max_entries = max_entries
        # Fixed striped locks avoid an unbounded lock for every requested GPU label.
        self.locks = [asyncio.Lock() for _ in range(16)]

    def lock(self, key: tuple) -> asyncio.Lock:
        return self.locks[hash(key) % len(self.locks)]

    def get(self, key: tuple) -> tuple[float, T] | None:
        item = self.entries.get(key)
        if item:
            self.entries.move_to_end(key)
            return time.monotonic() - item[0], item[1]
        return None

    def put(self, key: tuple, value: T):
        self.entries[key] = (time.monotonic(), value)
        self.entries.move_to_end(key)
        while len(self.entries) > self.max_entries:
            self.entries.popitem(last=False)
