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
DELEGATE_TOKENS = "".join(["dlg", "-", "fixture", "-", "a", ",", "dlg", "-", "fixture", "-", "b"])

BASE = {
    "image": {"tag": "1.2.3"},
    "publicUrl": "https://lab.szymonrichert.pl",
    "postgres": {"url": "postgresql://" + "u:" + DB_PASS + "@db:5432/s"},
    "orchestrator": {"url": "http://orch.agents.svc", "token": ORCH_TOKEN},
    "relayToken": RELAY_TOKEN,
}
COMPONENTS = ("web", "service", "notifier")
WITH_DELEGATE = {"delegateTokens": DELEGATE_TOKENS}


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


def test_chart_renders_three_components_without_delegate():
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


def test_chart_renders_four_components_with_delegate():
    docs = render(WITH_DELEGATE)
    deps = deployments(docs)
    assert set(deps) == {*COMPONENTS, "delegate"}
    delegate = deps["delegate"]["spec"]["template"]["spec"]
    container = delegate["containers"][0]
    assert container["args"] == ["serve-delegate"]
    assert container["command"] == ["taskboard"]
    assert container["ports"][0]["containerPort"] == 8003
    assert delegate["initContainers"][0]["args"] == ["migrate"]
    assert deps["delegate"]["spec"]["replicas"] == 1
    services = {
        s["metadata"]["labels"]["app.kubernetes.io/component"]: s for s in of_kind(docs, "Service")
    }
    assert set(services) == {"web", "service", "delegate"}
    assert services["delegate"]["metadata"]["name"] == "tb-agents-taskboard-delegate"
    assert services["delegate"]["spec"]["ports"][0]["port"] == 8003
    assert services["delegate"]["spec"]["ports"][0]["targetPort"] == "http"
    assert env_of(container)["TASKBOARD_DELEGATE_PORT" if False else "TASKBOARD_DELEGATE_TOKENS"]
    cm = of_kind(docs, "ConfigMap")[0]["data"]
    assert cm["TASKBOARD_DELEGATE_PORT"] == "8003"


def test_delegate_absent_without_token():
    docs = render()
    assert "delegate" not in deployments(docs)
    assert all(
        s["metadata"]["labels"]["app.kubernetes.io/component"] != "delegate"
        for s in of_kind(docs, "Service")
    )
    for dep in deployments(docs).values():
        env = env_of(dep["spec"]["template"]["spec"]["containers"][0])
        assert "TASKBOARD_DELEGATE_TOKENS" not in env


def test_delegate_token_is_secretref_not_rendered():
    docs = render(WITH_DELEGATE)
    cm = of_kind(docs, "ConfigMap")[0]["data"]
    assert "dlg-fixture" not in str(cm)
    secrets = {s["metadata"]["name"]: s for s in of_kind(docs, "Secret")}
    assert secrets["tb-agents-taskboard"]["stringData"]["delegate-tokens"] == DELEGATE_TOKENS
    for dep in deployments(docs).values():
        env = env_of(dep["spec"]["template"]["spec"]["containers"][0])
        ref = env["TASKBOARD_DELEGATE_TOKENS"]["valueFrom"]["secretKeyRef"]
        assert ref == {"name": "tb-agents-taskboard", "key": "delegate-tokens"}
        assert "value" not in env["TASKBOARD_DELEGATE_TOKENS"]


def test_delegate_existing_secret_referenced():
    docs = render({"delegateExistingSecret": "dlg", "delegateExistingSecretKey": "t"})
    assert "delegate" in deployments(docs)
    assert all(
        "delegate-tokens" not in (s.get("stringData") or {}) for s in of_kind(docs, "Secret")
    )
    env = env_of(deployments(docs)["delegate"]["spec"]["template"]["spec"]["containers"][0])
    ref = env["TASKBOARD_DELEGATE_TOKENS"]["valueFrom"]["secretKeyRef"]
    assert ref == {"name": "dlg", "key": "t"}


def test_delegate_setting_both_or_neither_value_fails_render():
    proc = _run(_merge(BASE, {**WITH_DELEGATE, "delegateExistingSecret": "dlg"}))
    assert proc.returncode != 0
    assert "set only one of the delegate tokens value" in proc.stderr
    # neither value is the supported "no delegate" case and renders
    assert _run(BASE).returncode == 0


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
            **WITH_DELEGATE,
            "components": {"web": {"podAnnotations": anno}},
            "allowedCidrs": ["10.0.0.0/8", "127.0.0.1/32"],
        }
    )
    deps = deployments(docs)
    web = deps["web"]["spec"]["template"]["metadata"]["annotations"]
    assert web["oauth2-proxy/inject"] == "true"
    for other in ("service", "notifier", "delegate"):
        assert (
            "oauth2-proxy/inject" not in deps[other]["spec"]["template"]["metadata"]["annotations"]
        )
    cm = of_kind(docs, "ConfigMap")[0]["data"]
    assert cm["TASKBOARD_ALLOWED_CIDRS"] == "10.0.0.0/8,127.0.0.1/32"


def test_ingress_still_routes_web_only():
    docs = render({**WITH_DELEGATE, "ingress": {"enabled": True}})
    ing = of_kind(docs, "Ingress")[0]
    rule = ing["spec"]["rules"][0]
    assert rule["host"] == "lab.szymonrichert.pl"
    path = rule["http"]["paths"][0]
    assert path["path"] == "/taskboard" and path["pathType"] == "Prefix"
    assert path["backend"]["service"]["name"].endswith("-web")
    assert len(rule["http"]["paths"]) == 1
    assert "delegate" not in str(of_kind(docs, "Ingress"))
    assert of_kind(render(), "Ingress") == []


def test_pods_are_locked_down():
    for dep in deployments(render()).values():
        _assert_locked_down(dep)


def test_delegate_pod_locked_down():
    deps = deployments(render(WITH_DELEGATE))
    assert "delegate" in deps
    for dep in deps.values():
        _assert_locked_down(dep)


def _assert_locked_down(dep: dict) -> None:
    if True:
        spec = dep["spec"]["template"]["spec"]
        assert spec["securityContext"]["runAsNonRoot"] is True
        for c in [*spec["containers"], *spec["initContainers"]]:
            assert c["securityContext"]["readOnlyRootFilesystem"] is True


MINDMAP_TOKENS = "".join(["mm", "-", "fixture", "-", "a"])


def test_chart_mindmap_tokens_from_secret():
    docs = render({**WITH_DELEGATE, "mindmapTokens": MINDMAP_TOKENS})
    assert MINDMAP_TOKENS not in str(of_kind(docs, "ConfigMap"))
    secrets = {s["metadata"]["name"]: s for s in of_kind(docs, "Secret")}
    assert secrets["tb-agents-taskboard"]["stringData"]["mindmap-tokens"] == MINDMAP_TOKENS
    for name, dep in deployments(docs).items():
        env = env_of(dep["spec"]["template"]["spec"]["containers"][0])
        if name != "delegate":
            assert "TASKBOARD_MINDMAP_TOKENS" not in env
            continue
        item = env["TASKBOARD_MINDMAP_TOKENS"]
        assert item["valueFrom"]["secretKeyRef"] == {
            "name": "tb-agents-taskboard",
            "key": "mindmap-tokens",
        }
        assert "value" not in item


def test_chart_mindmap_existing_secret():
    docs = render({**WITH_DELEGATE, "mindmapExistingSecret": "mm", "mindmapExistingSecretKey": "k"})
    env = env_of(deployments(docs)["delegate"]["spec"]["template"]["spec"]["containers"][0])
    assert env["TASKBOARD_MINDMAP_TOKENS"]["valueFrom"]["secretKeyRef"] == {
        "name": "mm",
        "key": "k",
    }
    assert all("mindmap-tokens" not in (s.get("stringData") or {}) for s in of_kind(docs, "Secret"))


def test_chart_mindmap_absent_by_default_and_both_fails():
    env = env_of(
        deployments(render(WITH_DELEGATE))["delegate"]["spec"]["template"]["spec"]["containers"][0]
    )
    assert "TASKBOARD_MINDMAP_TOKENS" not in env
    proc = _run(_merge(BASE, {**WITH_DELEGATE, "mindmapTokens": "a", "mindmapExistingSecret": "m"}))
    assert proc.returncode != 0
    assert "set only one of the mindmap tokens value" in proc.stderr
