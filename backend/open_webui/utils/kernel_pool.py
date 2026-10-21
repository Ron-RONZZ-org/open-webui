"""Kernel identities kept alive between code-interpreter calls of one conversation.

The Jupyter server owns the kernel lifecycle and culls idle kernels. This map is
only a cache: it lets a conversation reuse its kernel, and every reuse is
verified against the server, so a culled or restarted kernel is recreated rather
than reported as a failure.
"""

from __future__ import annotations

from collections import OrderedDict

DEFAULT_MAX_KERNELS = 6


class KernelPool:
    """LRU map of conversation key -> Jupyter kernel id."""

    def __init__(self, max_kernels: int = DEFAULT_MAX_KERNELS):
        if max_kernels < 1:
            raise ValueError('max_kernels must be at least 1')
        self._entries: OrderedDict[str, str] = OrderedDict()
        self._max = max_kernels

    def get(self, key: str) -> str | None:
        """Return the kernel for this conversation and mark it recently used."""
        kernel_id = self._entries.get(key)
        if kernel_id is not None:
            self._entries.move_to_end(key)
        return kernel_id

    def put(self, key: str, kernel_id: str) -> str | None:
        """Store a kernel id, returning the id evicted to stay within the cap."""
        self._entries[key] = kernel_id
        self._entries.move_to_end(key)
        if len(self._entries) > self._max:
            _, evicted = self._entries.popitem(last=False)
            return evicted
        return None

    def pop(self, key: str) -> str | None:
        return self._entries.pop(key, None)

    def __len__(self) -> int:
        return len(self._entries)
