"""Built-in secret guard — the non-overridable half of the ingest secrets barrier.

A RAG that memorizes API keys hands them to every agent, so blocking secrets is a
first-class threat model, not an edge case (repo ``CLAUDE.md`` §6;
``../Rag/reference/reference-security-model.md``). This module is enforced *in code*
at the scan boundary (Step 3) and again at parse time (Step 4): unlike ``.ragignore``,
it cannot be widened away. ``.ragignore`` can only narrow indexing further.

Two independent gates:

* :func:`is_secret_filename` — blocks whole files by name (``.env*``, ``id_rsa*``,
  ``*.pem``, ``*credentials*``, and other common key material). Cheap; runs on every
  path during the walk.
* :func:`scan_for_secrets` — a high-precision content scan for embedded key material
  (a note that pastes an AWS key, a private-key block, a service token). Precision is
  chosen over recall so ordinary dev notes full of hashes/UUIDs are not blocked.

**Never returns or logs the secret itself** — only the *name* of the rule that matched
(``CLAUDE.md`` §5: never log secrets). Callers log the path + rule, never the value.
"""

from __future__ import annotations

import math
import re
from fnmatch import fnmatch

# ---------------------------------------------------------------------------
# Gate 1 — filename deny patterns (matched case-insensitively against the
# POSIX basename). The four called out in CLAUDE.md §6 (.env*, id_rsa*, *.pem,
# *credentials*) plus the common remainder of the key-material family. This set
# may only ever grow; nothing a user configures can shrink it.
# ---------------------------------------------------------------------------
_SECRET_FILENAME_GLOBS: tuple[str, ...] = (
    # dotenv / env dumps
    ".env",
    ".env.*",
    "*.env",
    # ssh / signing keys
    "id_rsa*",
    "id_dsa*",
    "id_ecdsa*",
    "id_ed25519*",
    "*.pem",
    "*.key",
    "*.ppk",
    "*.p12",
    "*.pfx",
    "*.pkcs12",
    "*.keystore",
    "*.jks",
    "*.kdbx",
    # credential / secret files and token stores
    "*credentials*",
    "*secret*",
    ".npmrc",
    ".pypirc",
    ".netrc",
    ".htpasswd",
    "*.pgpass",
)


def is_secret_filename(rel_path: str) -> bool:
    """True if ``rel_path`` looks like key material by name (non-overridable).

    Matches the POSIX basename case-insensitively against :data:`_SECRET_FILENAME_GLOBS`.
    """
    name = rel_path.rsplit("/", 1)[-1].lower()
    return any(fnmatch(name, glob) for glob in _SECRET_FILENAME_GLOBS)


# ---------------------------------------------------------------------------
# Gate 2 — content secret detection.
#
# Two tiers, both high-precision:
#   (a) self-identifying tokens — a fixed vendor prefix + a body of the right
#       shape (AKIA…, ghp_…, xox?-…, AIza…, sk_live_…, PEM blocks). Safe to flag
#       on sight; nothing else in a dev note produces these shapes by accident.
#   (b) keyword-anchored high-entropy assignment — `api_key = "<random>"`. Here we
#       require BOTH a secret-ish key name AND that the value clears an entropy
#       floor, so 40-char git SHAs, UUIDs, and base64 diagrams don't trip it.
#
# The high-entropy key regex named in CLAUDE.md §6 is tier (b); the specific
# vendor rules are the precise front line.
# ---------------------------------------------------------------------------

# (a) self-identifying vendor tokens.
_TOKEN_RULES: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("private-key-block", re.compile(r"-----BEGIN (?:[A-Z0-9 ]+ )?PRIVATE KEY-----")),
    ("aws-access-key-id", re.compile(r"\b(?:AKIA|ASIA|AGPA|AIDA|AROA|ANPA|ANVA)[0-9A-Z]{16}\b")),
    ("github-token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36,255}\b")),
    ("github-fine-grained-pat", re.compile(r"\bgithub_pat_[0-9A-Za-z_]{60,}\b")),
    ("slack-token", re.compile(r"\bxox[baprs]-[0-9A-Za-z-]{10,}\b")),
    ("google-api-key", re.compile(r"\bAIza[0-9A-Za-z_\-]{35}\b")),
    ("stripe-secret-key", re.compile(r"\b[rs]k_live_[0-9A-Za-z]{20,}\b")),
    ("openai-key", re.compile(r"\bsk-[A-Za-z0-9]{20}T3BlbkFJ[A-Za-z0-9]{20}\b")),
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b")),
)

# (b) keyword-anchored high-entropy secret assignment.
_SECRET_KEYWORD = (
    r"(?:api[_-]?key|secret[_-]?key|secret|token|passwd|password|"
    r"access[_-]?key|private[_-]?key|client[_-]?secret|auth[_-]?token)"
)
# The opening boundary is `(?<![A-Za-z0-9])`, NOT `\b`. `_` is a word character, so a
# leading `\b` does not match inside `AWS_SECRET_ACCESS_KEY` / `GITHUB_TOKEN` /
# `DB_PASSWORD` — i.e. it missed the single most common way a real credential is
# written, while catching the bare `password=` form nobody pastes. Underscore and hyphen
# are name separators here, so they must read as boundaries; letters and digits still
# must not, which is what keeps `not_a_token_value` from anchoring a match.
_ASSIGNMENT_RE = re.compile(
    rf"""(?ix)
    (?<![A-Za-z0-9]){_SECRET_KEYWORD}\b   # a secret-ish name, `_`/`-` count as boundaries
    \s*[:=]\s*                   # : or =
    ['"]?                        # optional opening quote
    (?P<val>[A-Za-z0-9+/_\-]{{20,}})   # the candidate value
    ['"]?
    """
)
# Below this Shannon entropy (bits/char) a keyword-anchored value is treated as a
# placeholder / prose (e.g. "your_api_key_here"), not a live secret.
_ENTROPY_FLOOR = 3.2


def _shannon_entropy(s: str) -> float:
    """Shannon entropy in bits per character (0.0 for the empty string)."""
    if not s:
        return 0.0
    n = len(s)
    counts: dict[str, int] = {}
    for ch in s:
        counts[ch] = counts.get(ch, 0) + 1
    return -sum((c / n) * math.log2(c / n) for c in counts.values())


def scan_for_secrets(text: str) -> str | None:
    """Return the name of the first secret rule that matches ``text``, else ``None``.

    Precision-first: self-identifying vendor tokens match on sight; a generic
    ``key = value`` only counts when the name is secret-ish *and* the value clears
    :data:`_ENTROPY_FLOOR`. Returns only the rule name — never the matched secret.
    """
    for name, pattern in _TOKEN_RULES:
        if pattern.search(text):
            return name
    for m in _ASSIGNMENT_RE.finditer(text):
        val = m.group("val")
        # Ignore obvious placeholders even if the entropy floor is met.
        if val.lower() in {"your_api_key_here", "changeme", "xxxxxxxxxxxx"}:
            continue
        if _shannon_entropy(val) >= _ENTROPY_FLOOR:
            return "high-entropy-secret-assignment"
    return None
