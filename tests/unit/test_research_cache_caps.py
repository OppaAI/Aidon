"""Research cache caps: store pruning, scan cap, match cap.

The cache must stay bounded: the store prunes oldest files
past the keep cap (never the file just written), a search
scans only the newest files, and returns only so many
entries. A cap of 0 disables that cap.
"""

import os
import time
from pathlib import Path

import pytest

from src.agentic.research_cache import ResearchCache


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for key in ("AIDON_RESEARCH_CACHE_MAX_FILES",
                "AIDON_RESEARCH_CACHE_MAX_SCAN",
                "AIDON_RESEARCH_CACHE_MAX_MATCHES"):
        monkeypatch.delenv(key, raising=False)


def _cache(tmp_path):
    return ResearchCache(tmp_path / "home")


def _store_many(cache, count):
    for index in range(count):
        cache.store(f"https://example.invalid/{index}",
                    f"needle line {index}")
        time.sleep(0.01)


def test_store_prunes_oldest_past_keep_cap(tmp_path, monkeypatch):
    monkeypatch.setenv("AIDON_RESEARCH_CACHE_MAX_FILES", "3")
    cache = _cache(tmp_path)
    _store_many(cache, 5)
    kept = list((tmp_path / "home" / "research_cache").glob("*.txt"))
    assert len(kept) == 3
    assert cache.fetch("https://example.invalid/4") is not None
    assert cache.fetch("https://example.invalid/0") is None


def test_store_never_prunes_the_file_just_written(tmp_path, monkeypatch):
    monkeypatch.setenv("AIDON_RESEARCH_CACHE_MAX_FILES", "1")
    cache = _cache(tmp_path)
    _store_many(cache, 3)
    kept = list((tmp_path / "home" / "research_cache").glob("*.txt"))
    assert len(kept) == 1
    assert cache.fetch("https://example.invalid/2") is not None


def test_search_match_cap(tmp_path, monkeypatch):
    monkeypatch.setenv("AIDON_RESEARCH_CACHE_MAX_MATCHES", "2")
    cache = _cache(tmp_path)
    _store_many(cache, 5)
    assert len(cache.search_research_cache("needle")) == 2


def test_search_scan_cap_skips_old_files(tmp_path, monkeypatch):
    monkeypatch.setenv("AIDON_RESEARCH_CACHE_MAX_SCAN", "2")
    monkeypatch.setenv("AIDON_RESEARCH_CACHE_MAX_MATCHES", "100")
    cache = _cache(tmp_path)
    _store_many(cache, 5)
    entries = cache.search_research_cache("needle")
    urls = [entry["url"] for entry in entries]
    assert urls == ["https://example.invalid/4",
                    "https://example.invalid/3"]


def test_zero_cap_means_unlimited(tmp_path, monkeypatch):
    monkeypatch.setenv("AIDON_RESEARCH_CACHE_MAX_FILES", "0")
    monkeypatch.setenv("AIDON_RESEARCH_CACHE_MAX_MATCHES", "0")
    cache = _cache(tmp_path)
    _store_many(cache, 5)
    kept = list((tmp_path / "home" / "research_cache").glob("*.txt"))
    assert len(kept) == 5
    assert len(cache.search_research_cache("needle")) == 5
