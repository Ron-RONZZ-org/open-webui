"""Unit tests for the per-conversation kernel cache."""

import pytest

from open_webui.utils.kernel_pool import KernelPool


def test_reuses_the_kernel_for_the_same_key():
    pool = KernelPool()
    pool.put('chat-a', 'kernel-1')

    assert pool.get('chat-a') == 'kernel-1'


def test_returns_none_for_an_unknown_key():
    assert KernelPool().get('missing') is None


def test_evicts_the_least_recently_used_kernel():
    pool = KernelPool(max_kernels=2)
    pool.put('chat-a', 'kernel-a')
    pool.put('chat-b', 'kernel-b')

    assert pool.put('chat-c', 'kernel-c') == 'kernel-a'
    assert pool.get('chat-a') is None
    assert pool.get('chat-b') == 'kernel-b'
    assert pool.get('chat-c') == 'kernel-c'


def test_get_refreshes_recency():
    pool = KernelPool(max_kernels=2)
    pool.put('chat-a', 'kernel-a')
    pool.put('chat-b', 'kernel-b')

    pool.get('chat-a')  # chat-b is now the least recently used

    assert pool.put('chat-c', 'kernel-c') == 'kernel-b'


def test_pop_removes_the_entry():
    pool = KernelPool()
    pool.put('chat-a', 'kernel-a')

    assert pool.pop('chat-a') == 'kernel-a'
    assert pool.pop('chat-a') is None


def test_rejects_a_zero_cap():
    with pytest.raises(ValueError, match='at least 1'):
        KernelPool(max_kernels=0)
