"""Config overlays — how ``vaultrag eval --config a.yaml --config b.yaml`` does A/B.

``../Rag/projects/vaultrag/evaluation-framework.md`` specifies the A/B harness as "run the
same golden set against two configs side by side". An overlay is a small file of
:class:`~vaultrag.config.Settings` field overrides applied on top of the ambient
environment-derived settings, so a comparison file says only what differs::

    # rerank-on.yaml
    rerank_enabled: true
    rerank_threshold: 0.1

Anything not named keeps its normal value. Both YAML and JSON are accepted — the extension
decides — because the documented interface says ``.yaml`` and JSON costs nothing to allow.

Unknown keys are **rejected**, not ignored. A typo like ``rerank_enable: true`` would
otherwise produce an overlay that silently changes nothing and an A/B that reports two
identical configs as a real comparison.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import yaml

from vaultrag.config import Settings


class OverlayError(ValueError):
    """An overlay file is missing, unparseable, or names a setting that does not exist."""


def load_overlay(path: Path) -> dict[str, Any]:
    """Read one overlay file into a dict of ``Settings`` field overrides."""
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise OverlayError(f"config overlay not found: {path}") from exc

    try:
        data = json.loads(text) if path.suffix == ".json" else yaml.safe_load(text)
    except (json.JSONDecodeError, yaml.YAMLError) as exc:
        raise OverlayError(f"cannot parse config overlay {path}: {exc}") from exc

    if data is None:
        return {}
    if not isinstance(data, dict):
        raise OverlayError(f"config overlay {path} must be a mapping of setting -> value")

    known = set(Settings.model_fields)
    unknown = sorted(set(data) - known)
    if unknown:
        raise OverlayError(
            f"config overlay {path} sets unknown setting(s): {', '.join(unknown)}. "
            f"Valid settings: {', '.join(sorted(known))}"
        )
    return dict(data)


def apply_overlay(base: Settings, overrides: dict[str, Any]) -> Settings:
    """Return a copy of ``base`` with ``overrides`` applied, revalidated.

    Goes back through ``Settings`` validation rather than mutating, so an overlay setting
    ``embed_dims: "banana"`` fails here with a readable pydantic error instead of surfacing
    as a strange failure deep inside the vector store.
    """
    merged = base.model_dump()
    merged.update(overrides)
    try:
        return Settings(**merged)
    except Exception as exc:  # pydantic ValidationError, kept broad on purpose
        raise OverlayError(f"invalid config overlay values: {exc}") from exc


def overlay_label(path: Path, overrides: dict[str, Any]) -> str:
    """Short column heading for the A/B table: the file stem, or what it changed."""
    if not overrides:
        return f"{path.stem} (no changes)"
    return path.stem
