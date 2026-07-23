# VaultRAG — one image, two roles (`VAULTRAG_ROLE=api|indexer`).
#
# Shape: ../Rag/reference/reference-docker-compose.md. The role switch is the whole point
# of a single image — the same bytes run the query service and the indexer, so a laptop, a
# VPS and (eventually) K8s differ in *config*, never in code.
#
# Two tags from one build (p3-step-plan Decision 5):
#
#   docker build -t vaultrag:latest  .                        # base — no PDF support
#   docker build -t vaultrag:formats --build-arg EXTRAS=--extra=pdf .   # + Docling/OCR
#
# The split exists because the `pdf` extra is +79 packages including torch (~2.5 GB): a
# single fat image makes every vault pay for a format most do not have, and a base-only
# image silently drops PDFs for the vaults that do. The formats build *asserts the engines
# import* below, so a silently-broken extra fails the build instead of producing an image
# that skips its way to green — the same guard `.github/workflows/checks.yml`'s `formats`
# job puts on the test suite.

# ---------------------------------------------------------------------------
# builder — resolve and install the venv; nothing from here reaches the runtime
# image except /app/.venv, so compilers and caches do not ship.
# ---------------------------------------------------------------------------
FROM python:3.12-slim-bookworm AS builder

# Pinned: an image that resolves differently next month is not a reproducible deploy.
COPY --from=ghcr.io/astral-sh/uv:0.11.7 /uv /uvx /usr/local/bin/

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

WORKDIR /app

# Dependencies before source: editing a .py file must not re-resolve the whole tree.
# `--no-install-project` is what makes that layer independent of src/.
ARG EXTRAS=""
COPY pyproject.toml uv.lock README.md ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-install-project ${EXTRAS}

COPY src ./src
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev ${EXTRAS}

# ---------------------------------------------------------------------------
# runtime
# ---------------------------------------------------------------------------
FROM python:3.12-slim-bookworm AS runtime

# `git` is not optional-looking but is: write-back auto-commits to the *vault's own* repo
# (writeback/git.py) and degrades to `committed=False` without it. Shipping it means the
# audit trail works in a container instead of being quietly absent there.
RUN apt-get update \
    && apt-get install -y --no-install-recommends git ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# UID/GID are build args because /vaults is a bind mount from the host and write-back needs
# rw on it. On Linux the container uid must match the vault's owner; on Docker Desktop the
# mount is uid-mapped and this does not matter. `|| true` covers base images that already
# ship a group at this id.
ARG UID=1000
ARG GID=1000
RUN (groupadd -g ${GID} vaultrag || true) \
    && useradd -m -u ${UID} -g ${GID} -s /usr/sbin/nologin vaultrag \
    && mkdir -p /vaults /state \
    && chown -R ${UID}:${GID} /vaults /state

COPY --from=builder --chown=${UID}:${GID} /app /app

ENV PATH="/app/.venv/bin:${PATH}" \
    PYTHONUNBUFFERED=1 \
    # Container-shaped defaults for the two paths and the one service address that cannot
    # be right on both a laptop and in compose. Everything else keeps the code default;
    # see docker-compose.yml for the settings that are a *decision* rather than a path fix.
    VAULTRAG_STATE_DIR=/state \
    VAULTRAG_QDRANT_URL=http://qdrant:6333

# Not set here on purpose: VAULTRAG_API_HOST. The code defaults to 127.0.0.1
# (reference-security-model: "setting 0.0.0.0 is a decision, so it must be typed out"), and
# inside a container that default makes the port unreachable even from the compose network.
# The decision is therefore typed out in docker-compose.yml, right next to the loopback-only
# port publish that is what actually keeps the API off the network. A bare `docker run` must
# pass `-e VAULTRAG_API_HOST=0.0.0.0` — see README.

# The vault is the source of truth; /state is a derived cache (CLAUDE.md §6). Declaring it
# a volume keeps a container rebuild from silently discarding an index that took 15 minutes
# to build, while `reindex` remains the recovery path if it is discarded anyway.
VOLUME ["/state"]

USER vaultrag

# A bind-mounted vault is usually owned by a different uid than this container's, and git
# refuses to operate on a repo it considers foreign. The container only ever touches vaults
# an operator explicitly mounted, so the broad form is the honest one here — the narrow
# alternative would need the vault paths baked into the image.
RUN git config --global --add safe.directory '*'

# Assert the extra actually installed, in the image that claims it (Decision 5's verify).
# Mirrors the `formats` CI job: both suites gate their real-conversion tests on
# `skipif(not available())`, so a silently-unresolved extra would ship an image whose PDF
# support is absent and whose tests skip rather than fail.
ARG EXTRAS=""
RUN if [ -n "${EXTRAS}" ]; then \
        python -c "\
from vaultrag.parsing.pdf import docling_available; \
from vaultrag.parsing.image import rapidocr_available, tesseract_available; \
assert docling_available(), 'docling missing — the pdf extra did not install'; \
assert rapidocr_available() or tesseract_available(), 'no OCR engine available'; \
print('formats image: docling + OCR present')"; \
    else \
        python -c "import vaultrag; print('base image: ' + vaultrag.__version__)"; \
    fi

EXPOSE 8000 9464

ENTRYPOINT ["vaultrag"]
CMD ["serve"]
