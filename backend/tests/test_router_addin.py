"""The Word add-in manifest is rendered for this deployment, from the template
shipped with the image, so an admin can publish it without a build step."""

from types import SimpleNamespace
from unittest.mock import patch

import pytest
from app.routers import addin

pytestmark = pytest.mark.usefixtures("no_rate_limit")

TEMPLATE = """<?xml version="1.0" encoding="UTF-8"?>
<OfficeApp>
  <Id>7c1a0a6e-4d8b-4f3e-9b1a-2c5d6e7f8a90</Id>
  <IconUrl DefaultValue="https://localhost:3200/icons/icon-32.png" />
  <AppDomains><AppDomain>https://localhost:3200</AppDomain></AppDomains>
  <SourceLocation DefaultValue="https://localhost:3200/index.html" />
</OfficeApp>
"""


def _settings(**overrides):
    """The settings the router reads, as a plain object (Settings is frozen)."""
    values = {
        "frontend_url": "",
        "word_addin_origin": "",
        "word_addin_id": "",
        "word_addin_manifest_path": "",
    }
    values.update(overrides)
    return patch.object(addin, "settings", SimpleNamespace(**values))


@pytest.fixture
def template(tmp_path):
    path = tmp_path / "manifest.xml"
    path.write_text(TEMPLATE, encoding="utf-8")
    with patch.object(addin, "template_path", return_value=str(path)):
        yield path


def test_manifest_points_at_this_instance(client, template):
    with _settings(frontend_url="https://casecite.firm.test"):
        resp = client.get("/api/v1/addin/manifest.xml")
    assert resp.status_code == 200, resp.text
    assert resp.headers["content-type"].startswith("application/xml")
    body = resp.text
    assert "https://localhost:3200" not in body
    assert '<SourceLocation DefaultValue="https://casecite.firm.test/addin/index.html" />' in body
    assert '<IconUrl DefaultValue="https://casecite.firm.test/addin/icons/icon-32.png" />' in body
    assert "<AppDomain>https://casecite.firm.test/addin</AppDomain>" in body
    assert "<Id>7c1a0a6e-4d8b-4f3e-9b1a-2c5d6e7f8a90</Id>" in body


def test_manifest_uses_the_request_origin_when_frontend_url_is_unset(client, template):
    with _settings():
        resp = client.get("/api/v1/addin/manifest.xml")
    assert resp.status_code == 200
    assert "http://testserver/addin/index.html" in resp.text


def test_manifest_prefers_a_separately_hosted_pane_and_a_custom_id(client, template):
    with _settings(
        frontend_url="https://casecite.firm.test",
        word_addin_origin="https://addin.firm.test/",
        word_addin_id="11111111-2222-3333-4444-555555555555",
    ):
        resp = client.get("/api/v1/addin/manifest.xml")
    assert resp.status_code == 200
    assert "https://addin.firm.test/index.html" in resp.text
    assert "/addin/" not in resp.text
    assert "<Id>11111111-2222-3333-4444-555555555555</Id>" in resp.text


def test_a_malformed_id_is_refused_rather_than_published(client, template):
    with _settings(word_addin_id="not-a-guid"):
        resp = client.get("/api/v1/addin/manifest.xml")
    assert resp.status_code == 404
    assert "GUID" in resp.json()["detail"]


def test_missing_template_is_a_404(client):
    with patch.object(addin, "template_path", return_value=None), _settings():
        resp = client.get("/api/v1/addin/manifest.xml")
    assert resp.status_code == 404


def test_the_repo_template_is_found_in_development():
    """The committed manifest.xml serves local development."""
    with _settings():
        path = addin.template_path()
    assert path and path.endswith("manifest.xml")
    with open(path, encoding="utf-8") as f:
        assert addin.PLACEHOLDER_ORIGIN in f.read()
