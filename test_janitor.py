"""Structural safety guards for the production janitor script.

Run: pytest test_janitor.py
"""

from pathlib import Path


JANITOR = Path(__file__).with_name("janitor.sh")


def test_build_cache_prune_keeps_a_20gb_floor():
    script = JANITOR.read_text(encoding="utf-8")
    assert "docker builder prune -af --keep-storage=20GB" in script


def test_janitor_never_prunes_volumes():
    script = JANITOR.read_text(encoding="utf-8")
    assert "volume prune" not in script
