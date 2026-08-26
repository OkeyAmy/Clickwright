"""Scoping: the agent may drive any site the operator names, and only that site."""

import pytest

from app.computer import hosts
from app.connectors.runtime import ConnectorRuntime
from tests.factories import CLAIM, vendor_connector, vendor_version


def test_scope_defaults_to_the_targets_own_host():
    # blindfold: contract — normalise() falls back to the URL's host when none is given
    assert hosts.normalise(None, "https://portal.example.com/login") == ["portal.example.com"]


def test_a_leading_dot_rule_covers_subdomains():
    # blindfold: contract — ".example.com" is the suffix form; bare hosts are exact-match
    assert hosts.matches("sso.example.com", ".example.com") is True
    assert hosts.matches("example.com", ".example.com") is True
    assert hosts.matches("notexample.com", "example.com") is False


def test_navigating_outside_scope_is_refused():
    with pytest.raises(hosts.HostRefused) as caught:
        hosts.check("https://evil.example.net/x", ["portal.example.com"])
    # blindfold: contract — the message names the host so an operator can see what was attempted
    assert "evil.example.net" in str(caught.value)


@pytest.mark.parametrize("url", ["file:///etc/passwd", "javascript:alert(1)", "ftp://x/y"])
def test_only_http_schemes_are_allowed(url):
    """A compiled playbook is data; a non-http scheme in it is an attack, not a typo."""
    with pytest.raises(hosts.HostRefused):
        hosts.check(url, [])


def test_localhost_is_recognised_as_private():
    # blindfold: contract — decides whether ADK's private-network guard may be relaxed
    assert hosts.is_private("http://localhost:8081/x") is True
    assert hosts.is_private("http://127.0.0.1:8081/x") is True
    assert hosts.is_private("https://example.com") is False


def test_global_allowlist_overrides_a_permissive_connector(monkeypatch):
    """TARGET_ALLOWED_HOSTS is a ceiling: a connector cannot widen its own scope
    past what the deployment permits."""
    monkeypatch.setattr(hosts, "GLOBAL_ALLOWLIST", ["portal.example.com"])

    hosts.check("https://portal.example.com/x", ["portal.example.com"])
    with pytest.raises(hosts.HostRefused) as caught:
        hosts.check("https://other.example.org/x", ["other.example.org"])
    # blindfold: contract — the ceiling is reported distinctly from a connector-scope refusal
    assert "TARGET_ALLOWED_HOSTS" in str(caught.value)


def test_a_target_is_identified_from_its_url_alone():
    """The operator supplies an address and a goal. Everything else — the
    connector id, the display name, the scope — is derived, because those are
    facts about the URL rather than decisions a user should have to make."""
    from app.server import ExploreRequest, _derive

    derived = _derive(
        ExploreRequest(
            start_url="https://www.Supplier-Portal.example.com/claims/new",
            goal="File a travel expense claim for invoice 4471",
        )
    )

    # blindfold: contract — "www." is dropped and non-alphanumerics collapse to single dashes
    assert derived.connector_id == "supplier-portal-example-com"
    # blindfold: contract — the display name keeps the host, minus the www prefix
    assert derived.portal == "supplier-portal.example.com"
    # blindfold: contract — the operation is the goal's first three words, kebab-cased
    assert derived.operation == "file-a-travel"
    # blindfold: contract — scope defaults to the target's own host and nothing else
    assert derived.allowed_hosts == ["www.supplier-portal.example.com"]


@pytest.mark.asyncio
async def test_runtime_refuses_a_playbook_that_navigates_off_scope(portal):
    """A healed or hand-edited playbook must not be able to send the browser
    somewhere the connector was never authorised for."""
    connector = vendor_connector(portal)
    connector.allowed_hosts = ["portal.example.com"]  # deliberately not the real target
    version = vendor_version(portal)

    run = await ConnectorRuntime().execute(connector, version, CLAIM)

    # blindfold: contract — a HostRefused is raised as a StepFailure, which fails the run
    assert run.status == "failed"
    # blindfold: contract — step 1 is the navigate; scope is checked before the browser moves
    assert run.failed_step == 1
    assert "outside this connector's scope" in run.error
