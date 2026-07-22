"""Project registry + per-project settings resolution (P3 Stage 2).

Until now a project was a *string*: ``vaultrag mcp --project X --vault Y`` bound one
process to one tenant, and the ``projects`` table existed but nothing resolved through it.
This module makes a project a **first-class registered entity** so one process can serve
several — the multi-tenant model in ``../Rag/projects/vaultrag/multi-tenant-architecture.md``
and the registry row in ``../Rag/reference/reference-database-schema.md``
(``projects(id, name, vault_path, settings JSON, status, created_ts)``).

The isolation model is unchanged and already correct where it is enforced
(``index/qdrant.py:_build_filter`` makes ``project_id`` an unconditional ``must``): one
Qdrant collection partitioned by ``project_id`` payload, one SQLite db with ``project_id``
on every row. All this layer adds is *resolution* — turning a registered id into the
:class:`~vaultrag.config.Settings` a stage runs under, with the project's own vault path and
any per-project overrides applied on top of the process-wide base.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

from vaultrag.config import Settings

if TYPE_CHECKING:
    from vaultrag.store.base import MetaStore, ProjectRow

# Keys in a project's ``settings`` JSON that this layer will NOT let a project override,
# even though they are valid Settings fields: they are process/tenant identity or shared
# infrastructure, not per-project configuration. ``vault_path``/``project_id`` come from the
# row itself; ``state_dir``/``qdrant_*`` are the shared stores that make one-process-serves-N
# work — letting a project point them elsewhere would silently re-partition the deployment.
#
# ``embed_dims`` is reserved for a subtler reason than the rest: the shared collection is
# *created* with one dense vector size, so a project that overrode it would upsert vectors
# the collection cannot hold — the failure surfaces at index time, far from the setting that
# caused it. ``embed_model`` stays overridable (per-project embedding models are
# [[multi-tenant-architecture]]'s explicit design) because a different model at the *same*
# dimensionality is safe: every project's vectors are compared only against its own, the
# ``project_id`` filter being an unconditional ``must``. A model at a different width needs
# the note's documented scale path — promotion to its own collection — not a settings key.
_RESERVED_SETTINGS: frozenset[str] = frozenset(
    {
        "project_id",
        "vault_path",
        "state_dir",
        "qdrant_url",
        "qdrant_collection",
        "embed_dims",
    }
)


class UnknownProjectError(KeyError):
    """Raised when a project id is not in the registry.

    A ``KeyError`` subclass so callers that already guard ``store.get_project() is None``
    keep working, while the message names the id and the fix.
    """

    def __init__(self, project_id: str) -> None:
        self.project_id = project_id
        super().__init__(
            f"project {project_id!r} is not registered — add it with "
            f"`vaultrag project add {project_id} --vault <path>`, or pass --vault to run it "
            f"as an unregistered single project."
        )


class ProjectRegistry:
    """CRUD + settings resolution over the ``projects`` table.

    Wraps a :class:`~vaultrag.store.base.MetaStore` (the storage) and a base
    :class:`~vaultrag.config.Settings` (the process-wide defaults every project inherits).
    Stateless beyond those two — safe to construct per call or hold for the process.
    """

    def __init__(self, store: MetaStore, base: Settings) -> None:
        self._store = store
        self._base = base

    # --- CRUD -----------------------------------------------------------------
    def add(
        self,
        project_id: str,
        vault_path: Path | str,
        *,
        name: str | None = None,
        settings: dict[str, Any] | None = None,
        status: str = "active",
    ) -> ProjectRow:
        """Register (or update) a project. Idempotent — re-adding updates in place.

        The vault path must be an existing directory (a registry of dangling paths is a
        footgun that surfaces as an empty index later) and is stored resolved to an
        absolute path, so ``open`` and write-back resolve against the right root regardless
        of the process's working directory.
        """
        root = Path(vault_path).expanduser()
        if not root.is_dir():
            raise NotADirectoryError(f"vault path is not a directory: {root}")
        root = root.resolve()
        overrides = self._validate_settings(settings or {})
        self._store.upsert_project(
            project_id=project_id,
            name=name or project_id,
            vault_path=str(root),
            settings=overrides,
            status=status,
        )
        row = self._store.get_project(project_id)
        if row is None:  # pragma: no cover - just written above
            raise RuntimeError(f"project {project_id!r} vanished immediately after upsert")
        return row

    def get(self, project_id: str) -> ProjectRow | None:
        return self._store.get_project(project_id)

    def list(self) -> list[ProjectRow]:
        return self._store.list_projects()

    def remove(self, project_id: str) -> bool:
        """Delete a project and everything it owns. Returns whether it existed."""
        return self._store.delete_project(project_id)

    # --- resolution -----------------------------------------------------------
    def resolve(self, project_id: str) -> Settings:
        """The effective :class:`Settings` a registered project runs under.

        The process-wide base, overlaid with the project's ``vault_path``/``project_id`` and
        any per-project overrides in its ``settings`` JSON (embed model, ignore rules, write
        policy — [[multi-tenant-architecture]]). Shared infrastructure (``state_dir``, the
        Qdrant endpoint/collection) always stays the base's, which is what keeps N projects
        in one db + one payload-partitioned collection.
        """
        row = self._store.get_project(project_id)
        if row is None:
            raise UnknownProjectError(project_id)
        overrides = self._validate_settings(row.settings)
        data = {
            **self._base.model_dump(),
            **overrides,
            "project_id": row.id,
            "vault_path": row.vault_path,
        }
        return Settings(**data)

    def resolve_or_base(self, project_id: str | None) -> Settings:
        """Resolve a registered project, or fall back to the base settings.

        The single-project shorthand: an unregistered ``--project X --vault Y`` invocation
        keeps working (base settings already carry that ``project_id``/``vault_path``), while
        a registered id resolves through the row. ``None`` means "use the base as-is".
        """
        if project_id is None:
            return self._base
        if self._store.get_project(project_id) is None:
            return self._base
        return self.resolve(project_id)

    @staticmethod
    def _validate_settings(settings: dict[str, Any]) -> dict[str, Any]:
        """Keep only per-project-overridable Settings fields; reject reserved/unknown keys.

        A typo'd or reserved key in a project's ``settings`` JSON is a silent misconfig
        otherwise — it would be stored, never applied, and the operator would wonder why the
        override does nothing. Fail loudly instead.
        """
        valid = set(Settings.model_fields)
        bad = {k for k in settings if k not in valid or k in _RESERVED_SETTINGS}
        if bad:
            raise ValueError(
                f"per-project settings override unknown or reserved keys: {sorted(bad)}. "
                f"vault_path is set from the registry row; state_dir/qdrant_* are shared "
                f"across tenants and cannot be per-project; embed_dims is fixed by the "
                f"shared collection (embed_model itself is overridable at the same width)."
            )
        return dict(settings)
