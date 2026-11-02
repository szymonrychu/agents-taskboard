"""Chart render: web, service and notifier Deployments, the secrets split and required values."""

import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest
import yaml

CHART = Path(__file__).resolve().parent.parent / "chart" / "agents-taskboard"

DB_PASS = "".join(["db", "-", "fixture", "-", "value"])
ORCH_TOKEN = "".join(["orch", "-", "fixture", "-", "value"])
RELAY_TOKEN = "".join(["relay", "-", "fixture", "-", "value"])

BASE = {
    "image": {"tag": "1.2.3"},
    "publicUrl": "https://lab.szymonrichert.pl",
    "postgres": {"url": "postgresql://" + "u:" + DB_PASS + "@db:5432/s"},
    "orchestrator": {"url": "http://orch.agents.svc", "token": ORCH_TOKEN},
    "relayToken": RELAY_TOKEN,
}
COMPONENTS = ("web", "service", "notifier")


def _merge(a: dict, b: dict) -> dict:
    out = dict(a)
    for k, v in b.items():
        out[k] = _merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else v
    return out


def _run(values: dict) -> subprocess.CompletedProcess:
    helm = shutil.which("helm")
    assert helm, "helm binary not found on PATH; the chart tests require it"
    with tempfile.TemporaryDirectory() as tmp:
        vf = Path(tmp) / "values.yaml"
        vf.write_text(yaml.safe_dump(values))
        return subprocess.run(
            [helm, "template", "tb", str(CHART), "-n", "agents", "-f", str(vf)],
            capture_output=True,
            text=True,
        )


def render(overrides: dict | None = None) -> list[dict]:
    proc = _run(_merge(BASE, overrides or {}))
    assert proc.returncode == 0, proc.stderr
    return [d for d in yaml.safe_load_all(proc.stdout) if d]


def of_kind(docs: list[dict], kind: str) -> list[dict]:
    return [d for d in docs if d["kind"] == kind]


def deployments(docs: list[dict]) -> dict[str, dict]:
    return {
        d["metadata"]["labels"]["app.kubernetes.io/component"]: d
        for d in of_kind(docs, "Deployment")
    }


def env_of(container: dict) -> dict:
    return {e["name"]: e for e in container.get("env", [])}


def test_chart_renders_three_components():
    docs = render()
    deps = deployments(docs)
    assert set(deps) == set(COMPONENTS)
    args = {c: deps[c]["spec"]["template"]["spec"]["containers"][0] for c in COMPONENTS}
    assert args["web"]["args"] == ["serve-web"]
    assert args["service"]["args"] == ["serve-service"]
    assert args["notifier"]["args"] == ["notifier"]
    for dep in deps.values():
        spec = dep["spec"]["template"]["spec"]
        assert spec["containers"][0]["command"] == ["taskboard"]
        assert spec["containers"][0]["image"].endswith("agents-taskboard:1.2.3")
        init = spec["initContainers"][0]
        assert init["name"] == "migrate" and init["args"] == ["migrate"]
    assert deps["notifier"]["spec"]["replicas"] == 1
    assert deps["notifier"]["spec"]["strategy"] == {"type": "Recreate"}
    ports = {c: args[c].get("ports", [{}])[0].get("containerPort") for c in COMPONENTS}
    assert ports == {"web": 8000, "service": 8002, "notifier": None}
    services = {
        s["metadata"]["labels"]["app.kubernetes.io/component"]: s for s in of_kind(docs, "Service")
    }
    assert set(services) == {"web", "service"}
    assert services["service"]["spec"]["ports"][0]["port"] == 8002
    assert services["web"]["spec"]["ports"][0]["targetPort"] == "http"


def test_settings_and_secrets_wiring():
    docs = render()
    cm = of_kind(docs, "ConfigMap")[0]["data"]
    assert cm["TASKBOARD_PUBLIC_BASE_URL"] == "https://lab.szymonrichert.pl"
    assert cm["TASKBOARD_ORCHESTRATOR_URL"] == "http://orch.agents.svc"
    assert cm["TASKBOARD_HTTP_PORT"] == "8000"
    assert cm["TASKBOARD_SERVICE_PORT"] == "8002"
    assert cm["TASKBOARD_ALLOWED_CIDRS"] == "127.0.0.1/32"
    assert DB_PASS not in str(cm) and RELAY_TOKEN not in str(cm) and ORCH_TOKEN not in str(cm)
    secret_names = {s["metadata"]["name"] for s in of_kind(docs, "Secret")}
    for dep in deployments(docs).values():
        env = env_of(dep["spec"]["template"]["spec"]["containers"][0])
        for name in (
            "TASKBOARD_DATABASE_URL",
            "TASKBOARD_ORCHESTRATOR_TOKEN",
            "TASKBOARD_RELAY_TOKEN",
        ):
            ref = env[name]["valueFrom"]["secretKeyRef"]
            assert ref["name"] in secret_names
        assert "value" not in env["TASKBOARD_RELAY_TOKEN"]


def test_existing_secrets_are_referenced_not_rendered():
    docs = render(
        {
            "postgres": {"url": "", "existingSecret": "tb-db", "existingSecretKey": "uri"},
            "orchestrator": {"token": "", "existingSecret": "orch", "existingSecretKey": "tok"},
            "relayToken": "",
            "relayExistingSecret": "relay",
            "relayExistingSecretKey": "r",
        }
    )
    assert of_kind(docs, "Secret") == []
    env = env_of(deployments(docs)["web"]["spec"]["template"]["spec"]["containers"][0])
    refs = {k: env[k]["valueFrom"]["secretKeyRef"] for k in env if "valueFrom" in env[k]}
    assert refs["TASKBOARD_DATABASE_URL"] == {"name": "tb-db", "key": "uri"}
    assert refs["TASKBOARD_ORCHESTRATOR_TOKEN"] == {"name": "orch", "key": "tok"}
    assert refs["TASKBOARD_RELAY_TOKEN"] == {"name": "relay", "key": "r"}


def test_chart_fails_without_public_url():
    proc = _run(_merge(BASE, {"publicUrl": ""}))
    assert proc.returncode != 0
    assert "publicUrl is required" in proc.stderr


@pytest.mark.parametrize(
    ("override", "message"),
    [
        ({"postgres": {"url": ""}}, "a Postgres connection is required"),
        ({"postgres": {"existingSecret": "x"}}, "only one of postgres.url"),
        ({"orchestrator": {"url": ""}}, "orchestrator.url is required"),
        ({"orchestrator": {"token": ""}}, "orchestrator token is required"),
        ({"relayToken": ""}, "relay token is required"),
        ({"env": {"OTHER_X": "1"}}, "must start with TASKBOARD_"),
    ],
)
def test_required_values(override, message):
    proc = _run(_merge(BASE, override))
    assert proc.returncode != 0
    assert message in proc.stderr


def test_web_pod_annotations_and_allowed_cidrs():
    anno = {"oauth2-proxy/inject": "true"}
    docs = render(
        {
            "components": {"web": {"podAnnotations": anno}},
            "allowedCidrs": ["10.0.0.0/8", "127.0.0.1/32"],
        }
    )
    deps = deployments(docs)
    web = deps["web"]["spec"]["template"]["metadata"]["annotations"]
    assert web["oauth2-proxy/inject"] == "true"
    for other in ("service", "notifier"):
        assert (
            "oauth2-proxy/inject" not in deps[other]["spec"]["template"]["metadata"]["annotations"]
        )
    cm = of_kind(docs, "ConfigMap")[0]["data"]
    assert cm["TASKBOARD_ALLOWED_CIDRS"] == "10.0.0.0/8,127.0.0.1/32"


def test_ingress_routes_web_only():
    docs = render({"ingress": {"enabled": True}})
    ing = of_kind(docs, "Ingress")[0]
    rule = ing["spec"]["rules"][0]
    assert rule["host"] == "lab.szymonrichert.pl"
    path = rule["http"]["paths"][0]
    assert path["path"] == "/taskboard" and path["pathType"] == "Prefix"
    assert path["backend"]["service"]["name"].endswith("-web")
    assert of_kind(render(), "Ingress") == []


def test_pods_are_locked_down():
    for dep in deployments(render()).values():
        spec = dep["spec"]["template"]["spec"]
        assert spec["securityContext"]["runAsNonRoot"] is True
        for c in [*spec["containers"], *spec["initContainers"]]:
            assert c["securityContext"]["readOnlyRootFilesystem"] is True
