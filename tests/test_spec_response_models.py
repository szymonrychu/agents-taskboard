"""The spec routes document typed response models (no free-form objects) and their real
responses validate against them (T-116)."""

from datetime import datetime

import pytest
from test_api_spec_comments import comments_url
from test_api_specs import SPECS, doc_url, make_spec
from test_api_tasks import agent
from test_scope import ORCH, orchestrator

from agents_taskboard.api import openapi, schemas
from agents_taskboard.api.app import PREFIX
from agents_taskboard.projects import ProjectLookup

KEY = "{key}"
DOC = f"{PREFIX}/specs/{KEY}/docs/{{path}}"
CID = f"{PREFIX}/specs/{KEY}/comments/{{comment_id}}"

ROUTES = {
    ("get", f"{PREFIX}/specs", "200"): "SpecPage",
    ("post", f"{PREFIX}/specs", "201"): "SpecDetail",
    ("get", f"{PREFIX}/specs/{KEY}", "200"): "SpecDetail",
    ("patch", f"{PREFIX}/specs/{KEY}", "200"): "SpecDetail",
    ("get", f"{PREFIX}/specs/{KEY}/docs", "200"): "DocumentList",
    ("get", DOC, "200"): "DocumentBody",
    ("put", DOC, "201"): "DocumentVersion",
    ("get", f"{DOC}/versions", "200"): "VersionPage",
    ("get", f"{PREFIX}/specs/{KEY}/comments", "200"): "CommentList",
    ("post", f"{PREFIX}/specs/{KEY}/comments", "201"): "SpecComment",
    ("post", f"{CID}/resolve", "200"): "SpecComment",
    ("post", f"{CID}/accept", "201"): "SuggestionAccepted",
    ("post", f"{CID}/dismiss", "200"): "SpecComment",
}


@pytest.fixture(autouse=True)
def fake_projects(service_client, web_client, delegate_client):
    for client in (service_client, web_client, delegate_client):
        client.app.state.projects = ProjectLookup(ORCH, "t", transport=orchestrator())


def test_spec_routes_reference_named_models():
    doc = openapi._document()
    for (method, path, status), model in ROUTES.items():
        content = doc["paths"][path][method]["responses"][status]["content"]
        schema = content["application/json"]["schema"]
        assert schema == {"$ref": f"#/components/schemas/{model}"}, (method, path)
        assert model in doc["components"]["schemas"]


def test_spec_models_have_no_free_form_objects_at_top_level():
    schemas_ = openapi._document()["components"]["schemas"]
    for name in set(ROUTES.values()):
        props = schemas_[name]["properties"]
        assert props and all(p != "additionalProperties" for p in props)
        assert schemas_[name].get("additionalProperties") is False


def test_timestamps_are_date_time_strings():
    props = openapi._document()["components"]["schemas"]["SpecComment"]["properties"]
    assert props["created_at"] == {"type": "string", "format": "date-time", "title": "Created At"}


async def test_real_responses_validate_and_keep_shape(service_client, web_client):
    spec = await make_spec(service_client, labels=["plan"])
    key = spec["key"]
    schemas.SpecDetail.model_validate(spec)
    assert datetime.fromisoformat(spec["created_at"]).isoformat() == spec["created_at"]

    page = (await service_client.get(SPECS, headers=agent())).json()
    schemas.SpecPage.model_validate(page)
    assert "documents" not in page["items"][0]

    r = await service_client.get(f"{SPECS}/{key}/docs", headers=agent())
    schemas.DocumentList.model_validate(r.json())

    r = await service_client.get(doc_url(key), headers=agent())
    schemas.DocumentBody.model_validate(r.json())

    r = await service_client.put(
        doc_url(key), json={"body": "# Two\n", "base_version": 1}, headers=agent()
    )
    assert r.status_code == 201
    assert "removed" not in r.json()
    schemas.DocumentVersion.model_validate(r.json())
    r = await service_client.put(
        doc_url(key), json={"body": None, "base_version": 2}, headers=agent()
    )
    assert r.json()["sha256"] is None and r.json()["size"] is None
    schemas.DocumentVersion.model_validate(r.json())

    r = await service_client.get(f"{doc_url(key)}/versions", headers=agent())
    versions = schemas.VersionPage.model_validate(r.json())
    assert [v.removed for v in versions.items] == [True, False, False]

    r = await service_client.patch(
        f"{SPECS}/{key}", json={"version": spec["version"], "title": "New"}, headers=agent()
    )
    schemas.SpecDetail.model_validate(r.json())

    spec2 = await make_spec(service_client, name="plan-b")
    key2 = spec2["key"]
    r = await service_client.post(
        comments_url(key2),
        json={"body": "x", "path": "plan.md", "suggestion": "# S\n"},
        headers=agent(),
    )
    created = r.json()
    schemas.SpecComment.model_validate(created)
    assert created["suggestion_status"] == "open" and created["decided_by"] is None
    r = await service_client.get(comments_url(key2), headers=agent())
    schemas.CommentList.model_validate(r.json())

    r = await web_client.post(f"{comments_url(key2)}/{created['id']}/accept", json={"version": 1})
    assert r.status_code == 201, r.text
    accepted = schemas.SuggestionAccepted.model_validate(r.json())
    assert accepted.comment.suggestion_status == "accepted"

    other = (
        await service_client.post(
            comments_url(key2),
            json={"body": "y", "path": "plan.md", "suggestion": "# T\n"},
            headers=agent(),
        )
    ).json()
    r = await web_client.post(f"{comments_url(key2)}/{other['id']}/dismiss")
    schemas.SpecComment.model_validate(r.json())
    plain = (
        await service_client.post(comments_url(key2), json={"body": "z"}, headers=agent())
    ).json()
    r = await service_client.post(f"{comments_url(key2)}/{plain['id']}/resolve", headers=agent())
    assert schemas.SpecComment.model_validate(r.json()).resolved_by is not None
