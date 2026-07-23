"""The deployment artifacts, checked against the code they claim to describe (P3 Stage 6).

These files are the one part of VaultRAG that no other test can reach: a Grafana panel
querying ``vaultrag_serches_total`` renders an empty graph forever, an alert rule on a
metric that was renamed never fires, and both look *exactly* like a healthy system. That is
the "looked checked, wasn't" failure P2 kept finding, and eyeballing a dashboard is not a
defence against it — the panel is empty either way, and only one of the two reasons is a
bug.

So the dashboard and the alert rules are parsed here and every ``vaultrag_*`` name and
every label they use is resolved against the **live registry**, and the scrape config is
resolved against the compose services and the port ``Settings`` actually binds. The
p3-step-plan's verify for this stage — *"the dashboard must render all P2 series with
data"* — is a statement about names, and names are checkable.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest
import yaml

from vaultrag.config import Settings
from vaultrag.metrics import (
    _LAG_BUCKETS,
    _LATENCY_BUCKETS,
    init_project_labels,
    record_key_request,
    registry,
    reset_metrics,
    set_build_info,
    set_index_drift,
)

REPO = Path(__file__).resolve().parents[1]
DEPLOY = REPO / "deploy"
DASHBOARD = DEPLOY / "grafana" / "dashboards" / "vaultrag.json"
ALERTS = DEPLOY / "alerts.yml"
PROMETHEUS = DEPLOY / "prometheus.yml"
ALERTMANAGER = DEPLOY / "alertmanager.yml"
COMPOSE = REPO / "docker-compose.yml"
DOCKERFILE = REPO / "Dockerfile"

# Labels Prometheus attaches itself, plus the ones this repo's scrape config adds. `role`
# is read from prometheus.yml rather than hard-coded, so deleting it there fails a
# dashboard that still groups by it.
_PROMETHEUS_OWN_LABELS = {"le", "job", "instance", "__name__"}

# Grafana's own template syntax inside a PromQL string; not label names.
_GRAFANA_VARS = re.compile(r"\$__\w+|\$\w+")


# ---------------------------------------------------------------------------
# fixtures / parsing helpers
# ---------------------------------------------------------------------------
@pytest.fixture(autouse=True)
def _materialised_registry() -> None:
    """A registry with every family present, including the deferred ones.

    Four families have no samples on a bare import because their label values are not
    knowable at build time — the ``project`` ones wait for a composition root to announce
    what it serves, ``key_requests`` for a key to authenticate, ``build_info`` for a
    version, ``index_drift`` for a startup check. A dashboard references all of them, so
    the test has to stand them up exactly as a running process does, or it would report
    the dashboard broken for querying series that are merely not-yet-labelled.
    """
    reset_metrics()
    init_project_labels("deploy-test")
    record_key_request(key_id="deploy-test-key")
    set_build_info("0.0.0")
    set_index_drift(0, project="deploy-test")


def _families() -> dict[str, set[str]]:
    """Family name → the label names its samples carry."""
    out: dict[str, set[str]] = {}
    for metric in registry().collect():
        labels: set[str] = set()
        for sample in metric.samples:
            labels |= set(sample.labels)
        out[metric.name] = labels
    return out


def _sample_names() -> set[str]:
    """Every name a scrape actually shows (``_total``/``_bucket``/``_sum``/``_count``)."""
    return {s.name for m in registry().collect() for s in m.samples}


def _dashboard() -> dict[str, Any]:
    return json.loads(DASHBOARD.read_text(encoding="utf-8"))  # type: ignore[no-any-return]


def _panels(dash: dict[str, Any]) -> list[dict[str, Any]]:
    """Flatten the panel tree; a collapsed row nests its children."""
    out: list[dict[str, Any]] = []
    for panel in dash.get("panels", []):
        out.append(panel)
        out.extend(panel.get("panels", []))
    return out


def _dashboard_exprs() -> list[tuple[str, str]]:
    """(where it lives, PromQL) for every query in the dashboard, panels + variables."""
    dash = _dashboard()
    exprs: list[tuple[str, str]] = []
    for panel in _panels(dash):
        for target in panel.get("targets", []):
            if expr := target.get("expr"):
                exprs.append((f"panel {panel.get('title', panel.get('id'))!r}", expr))
    for var in dash.get("templating", {}).get("list", []):
        if query := var.get("query"):
            exprs.append((f"variable ${var['name']}", query))
    return exprs


def _alert_rules() -> list[dict[str, Any]]:
    doc = yaml.safe_load(ALERTS.read_text(encoding="utf-8"))
    return [rule for group in doc["groups"] for rule in group["rules"]]


def _alert_exprs() -> list[tuple[str, str]]:
    return [(f"alert {r['alert']}", r["expr"]) for r in _alert_rules()]


def _all_exprs() -> list[tuple[str, str]]:
    return _dashboard_exprs() + _alert_exprs()


def _metric_names(expr: str) -> set[str]:
    return set(re.findall(r"\bvaultrag_[a-z0-9_]+", expr))


def _label_names(expr: str) -> set[str]:
    """Label names used in selectors and in ``by (...)`` / ``without (...)`` groupings."""
    found: set[str] = set()
    for selector in re.findall(r"\{([^}]*)\}", expr):
        found |= set(re.findall(r"(\w+)\s*(?:=~|!~|=|!=)", selector))
    for grouping in re.findall(r"\b(?:by|without)\s*\(([^)]*)\)", expr):
        found |= {part.strip() for part in grouping.split(",") if part.strip()}
    return found


def _legend_vars(panel: dict[str, Any]) -> set[str]:
    """``{{project}}`` in a legend is a label reference and typos there are just as silent."""
    found: set[str] = set()
    for target in panel.get("targets", []):
        found |= set(re.findall(r"\{\{\s*(\w+)\s*\}\}", target.get("legendFormat", "")))
    return found


def _compose() -> dict[str, Any]:
    return yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))  # type: ignore[no-any-return]


def _scrape_labels() -> set[str]:
    doc = yaml.safe_load(PROMETHEUS.read_text(encoding="utf-8"))
    found: set[str] = set()
    for job in doc["scrape_configs"]:
        for static in job.get("static_configs", []):
            found |= set(static.get("labels", {}))
    return found


# ---------------------------------------------------------------------------
# the point of the file: every query resolves against the real registry
# ---------------------------------------------------------------------------
def test_every_metric_the_dashboard_and_alerts_query_actually_exists() -> None:
    """The stage's verify, made checkable.

    A histogram is referenced by its ``_bucket`` sample and a counter by its ``_total``
    one, so both the family name and the scrape name are accepted — what is rejected is a
    name that matches neither, which is the typo/rename case.
    """
    families = set(_families())
    samples = _sample_names()
    for where, expr in _all_exprs():
        for name in _metric_names(expr):
            assert name in families or name in samples, (
                f"{where} queries {name!r}, which no series in vaultrag/metrics.py exports"
            )


def test_every_label_the_dashboard_and_alerts_group_by_actually_exists() -> None:
    """A typo'd label silently drops a grouping instead of failing; `{{proj}}` renders raw."""
    real = set().union(*_families().values()) | _PROMETHEUS_OWN_LABELS | _scrape_labels()
    for where, expr in _all_exprs():
        for label in _label_names(_GRAFANA_VARS.sub("", expr)):
            assert label in real, f"{where} groups/filters by {label!r}, which no series carries"
    for panel in _panels(_dashboard()):
        for label in _legend_vars(panel):
            assert label in real, (
                f"panel {panel.get('title')!r} renders {{{{{label}}}}} in its legend, "
                "which no series carries"
            )


def test_the_dashboard_covers_every_series_the_design_note_names() -> None:
    """monitoring-observability.md's dashboard is "ingest health, search latency by stage,
    zero-hit rate, top projects" — but a metric this repo pays to record and never plots is
    a metric nobody reads. Pinned as coverage of the whole registry, so a *new* series has
    to be plotted or deliberately exempted here.
    """
    queried: set[str] = set()
    for _, expr in _dashboard_exprs():
        queried |= _metric_names(expr)
    # Match on the family, since a query names `_bucket`/`_total`.
    plotted = {f for f in _families() if any(name.startswith(f) for name in queried)}
    missing = set(_families()) - plotted
    assert not missing, f"recorded but never plotted: {sorted(missing)}"


# ---------------------------------------------------------------------------
# alert rules: thresholds have to line up with the buckets they are read from
# ---------------------------------------------------------------------------
def test_the_three_alerts_the_design_note_names_exist() -> None:
    names = {r["alert"] for r in _alert_rules()}
    assert {"VaultRAGDeadJobs", "VaultRAGIngestLagHigh", "VaultRAGSearchLatencyHigh"} <= names


def test_every_alert_carries_a_severity_and_says_what_to_do() -> None:
    """An alert without a next action is a notification, and notifications get muted."""
    for rule in _alert_rules():
        assert rule["labels"]["severity"] in {"warning", "critical"}, rule["alert"]
        assert rule["annotations"]["summary"].strip(), rule["alert"]
        assert rule["annotations"]["description"].strip(), rule["alert"]


def test_histogram_alert_thresholds_sit_on_real_bucket_edges() -> None:
    """``histogram_quantile`` interpolates *within* a bucket, so a threshold that is not an
    edge is compared against a linear guess. 300 s and 1 s are edges by construction
    (``metrics._LAG_BUCKETS`` / ``_LATENCY_BUCKETS``) and ``test_metrics`` already pins the
    first of them — this is the other half of that pair, reading the number from the rule
    file rather than restating it.
    """
    by_name = {r["alert"]: r for r in _alert_rules()}
    lag = float(by_name["VaultRAGIngestLagHigh"]["expr"].rsplit(">", 1)[1])
    latency = float(by_name["VaultRAGSearchLatencyHigh"]["expr"].rsplit(">", 1)[1])
    assert lag in _LAG_BUCKETS, f"ingest-lag alert fires at {lag}s, not a bucket edge"
    assert latency in _LATENCY_BUCKETS, f"latency alert fires at {latency}s, not a bucket edge"


def test_the_ingest_lag_alert_matches_the_design_note_s_five_minutes() -> None:
    by_name = {r["alert"]: r for r in _alert_rules()}
    assert float(by_name["VaultRAGIngestLagHigh"]["expr"].rsplit(">", 1)[1]) == 300.0


# ---------------------------------------------------------------------------
# scrape config ↔ compose ↔ Settings
# ---------------------------------------------------------------------------
def test_prometheus_scrapes_the_port_settings_actually_binds() -> None:
    """The scrape config hard-codes a port; ``Settings.metrics_port`` owns it. If the
    default moves, this is what says so instead of an empty dashboard saying nothing."""
    doc = yaml.safe_load(PROMETHEUS.read_text(encoding="utf-8"))
    targets = [
        t
        for job in doc["scrape_configs"]
        if job["job_name"] == "vaultrag"
        for static in job["static_configs"]
        for t in static["targets"]
    ]
    port = Settings().metrics_port
    assert targets, "no vaultrag scrape targets"
    for target in targets:
        assert target.endswith(f":{port}"), f"{target} is not the exporter port ({port})"


def test_prometheus_scrapes_both_roles_by_their_compose_service_names() -> None:
    """A scrape of only the api role would leave every ingest panel and two of the five
    alerts permanently silent — the indexer owns those series and has no other surface."""
    doc = yaml.safe_load(PROMETHEUS.read_text(encoding="utf-8"))
    services = set(_compose()["services"])
    scraped = {
        static["labels"]["role"]: static["targets"][0].split(":")[0]
        for job in doc["scrape_configs"]
        if job["job_name"] == "vaultrag"
        for static in job["static_configs"]
    }
    assert set(scraped) == {"api", "indexer"}
    for role, host in scraped.items():
        assert host in services, f"scrape target {host!r} is not a compose service"
        assert host == role


def test_both_roles_export_metrics_and_neither_publishes_that_port() -> None:
    """The exporter is keyless by design (serve-transport-design-calls §1), so it must
    reach the compose network and stop there — a published 9464 would be an unauthenticated
    port on the host, which is the one thing the security model does not allow."""
    compose = _compose()
    port = Settings().metrics_port
    for name in ("api", "indexer"):
        service = compose["services"][name]
        env = service.get("environment", {})
        assert env.get("VAULTRAG_METRICS_ENABLED") == "true", f"{name} exports no metrics"
        assert env.get("VAULTRAG_METRICS_ADDR") == "0.0.0.0", f"{name} binds loopback only"
        for published in service.get("ports", []):
            assert str(port) not in str(published), f"{name} publishes the keyless exporter"


def test_every_published_port_is_bound_to_loopback() -> None:
    """reference-security-model's posture: nothing on this box listens on the network.
    Remote reach is Tailscale/WireGuard, not a published port (vps-deployment-checklist §3).
    """
    for name, service in _compose()["services"].items():
        for published in service.get("ports", []):
            assert str(published).startswith("127.0.0.1:"), (
                f"service {name} publishes {published} on all interfaces"
            )


def test_the_role_flag_and_the_role_env_var_agree() -> None:
    """Two ways to say the same thing is two ways to disagree; compose sets both, so pin
    that a copy-paste of one service into the other cannot leave them crossed."""
    for name in ("api", "indexer"):
        service = _compose()["services"][name]
        assert service["command"] == ["serve", "--role", name]
        assert service["environment"]["VAULTRAG_ROLE"] == name


def test_the_api_binds_wide_inside_the_container_and_narrow_outside() -> None:
    """The two halves of one decision. 127.0.0.1 inside a container is unreachable even
    from the compose network, so the bind is widened there and re-narrowed by the publish —
    and the guarantee only holds while *both* halves are present."""
    api = _compose()["services"]["api"]
    assert api["environment"]["VAULTRAG_API_HOST"] == "0.0.0.0"
    assert api["ports"] == [f"127.0.0.1:{Settings().api_port}:{Settings().api_port}"]


def test_the_observability_services_are_all_behind_the_profile() -> None:
    """`docker compose up -d` must bring up the three-service deployment and nothing else:
    Grafana and friends are opt-in, or the 3-4 GB single-box budget is not what it says."""
    core = {"qdrant", "api", "indexer"}
    for name, service in _compose()["services"].items():
        if name in core:
            assert "profiles" not in service, f"{name} is core and must not need a profile"
        else:
            assert service.get("profiles") == ["observability"], f"{name} has no profile"


# ---------------------------------------------------------------------------
# grafana provisioning
# ---------------------------------------------------------------------------
def test_every_panel_points_at_the_provisioned_datasource() -> None:
    """Provisioning fixes the uid; a panel with a different one (or a stale inline uid from
    an export) renders "datasource not found" while the dashboard still looks complete."""
    provisioned = yaml.safe_load(
        (DEPLOY / "grafana" / "provisioning" / "datasources" / "prometheus.yml").read_text(
            encoding="utf-8"
        )
    )
    uid = provisioned["datasources"][0]["uid"]
    found = set(re.findall(r'"uid"\s*:\s*"([^"]+)"', DASHBOARD.read_text(encoding="utf-8")))
    assert uid in found
    assert found - {uid, _dashboard()["uid"]} == set(), f"unexpected datasource uids: {found}"


def test_grafana_reads_dashboards_from_the_path_compose_mounts() -> None:
    provider = yaml.safe_load(
        (DEPLOY / "grafana" / "provisioning" / "dashboards" / "vaultrag.yml").read_text(
            encoding="utf-8"
        )
    )
    path = provider["providers"][0]["options"]["path"]
    mounts = _compose()["services"]["grafana"]["volumes"]
    assert any(str(m).split(":")[1] == path for m in mounts), f"nothing is mounted at {path}"


# ---------------------------------------------------------------------------
# alertmanager: the substitution compose does, and the default that must stay dead
# ---------------------------------------------------------------------------
def test_the_alert_webhook_placeholder_is_substituted_by_compose() -> None:
    """Alertmanager expands no env vars in its own config and refuses to parse a literal
    ``${VAR}``, so the placeholder is rewritten at container start. If the two spellings
    ever drift, alertmanager starts fine and posts every alert to a URL that is the literal
    placeholder — a silent no-op, which is the worst possible failure for an alert route.
    """
    config = ALERTMANAGER.read_text(encoding="utf-8")
    assert "__WEBHOOK_URL__" in config
    # Comments stripped: the file's own header explains the `${VAR}` trap by quoting it.
    body = "\n".join(line for line in config.splitlines() if not line.lstrip().startswith("#"))
    assert "${" not in body, "alertmanager cannot expand environment variables"
    entrypoint = " ".join(str(p) for p in _compose()["services"]["alertmanager"]["entrypoint"])
    assert "__WEBHOOK_URL__" in entrypoint, "nothing substitutes the placeholder"


def test_the_default_alert_route_goes_nowhere() -> None:
    """A default that shipped alerts somewhere real would publish this box's operational
    state the moment the profile came up. It has to be inert until an operator names a
    destination."""
    entrypoint = " ".join(str(p) for p in _compose()["services"]["alertmanager"]["entrypoint"])
    default = re.search(r"VAULTRAG_ALERT_WEBHOOK:-([^}]+)\}", entrypoint)
    assert default is not None, "no default for VAULTRAG_ALERT_WEBHOOK"
    assert default.group(1).startswith("http://127.0.0.1:"), default.group(1)


# ---------------------------------------------------------------------------
# the image
# ---------------------------------------------------------------------------
def test_the_image_never_bakes_the_derived_state_directory() -> None:
    """``state/`` is one box's SQLite db and embed cache. Baking it in would ship a stale
    index to every container and quietly break "the vault is the source of truth"."""
    ignored = {
        line.strip()
        for line in (REPO / ".dockerignore").read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.startswith("#")
    }
    assert "state/" in ignored
    assert ".env" in ignored, "an .env in the build context is copied into a layer"


def test_the_formats_variant_asserts_its_engines_at_build_time() -> None:
    """Decision 5's verify. The pdf extra's tests all ``skipif(not available())``, so an
    extra that failed to resolve produces an image whose PDF support is absent and whose
    suite reports green — the build has to be what fails, and only an explicit import can
    make it."""
    dockerfile = DOCKERFILE.read_text(encoding="utf-8")
    assert "EXTRAS" in dockerfile
    assert "docling_available()" in dockerfile
    assert "rapidocr_available() or tesseract_available()" in dockerfile


def test_the_image_ships_git_for_the_write_back_audit_trail() -> None:
    """Write-back auto-commits to the vault's own repo and degrades to ``committed=False``
    without git on PATH — a container that silently lost the audit trail would look fine."""
    assert re.search(r"apt-get install[^\n]*\bgit\b", DOCKERFILE.read_text(encoding="utf-8"))
