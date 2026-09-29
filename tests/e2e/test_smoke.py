"""Smoke test of a live MISP through its API: reachable, authenticated, configured, working.

    pytest tests/e2e/test_smoke.py --url https://misp.example.com [--key <authkey>]

Without --url the module skips; without --key only the login page is checked.
The kind test (tests/run-kind-test.sh) runs it against the base on a cluster.
"""

import pytest

from misp_container import WORKER_QUEUES
from stack import Stack


@pytest.fixture(scope="module")
def misp(options):
    if not options["url"]:
        pytest.skip("no --url given")
    s = Stack([])  # no compose files: HTTP only
    s.base_url = options["url"]
    s.key = options["key"]
    return s


@pytest.fixture(scope="module")
def authed(misp):
    if not misp.key:
        pytest.skip("no --key given")
    return misp


def test_login_page_reachable(misp):
    assert misp.http("GET", "/users/login", json_api=False, timeout=10)[0] == 200


def test_api_auth(authed):
    assert (authed.api("GET", "/users/view/me", authed.key) or {}).get("User", {}).get("email")


def test_version(authed):
    assert (authed.api("GET", "/servers/getVersion", authed.key) or {}).get("version")


def setting(misp, name):
    return (misp.api("GET", f"/servers/getSetting/{name}", misp.key) or {}).get("value")


def test_baseurl_set(authed):
    assert setting(authed, "MISP.baseurl")


def test_live(authed):
    assert setting(authed, "MISP.live") in (True, 1, "1", "true")


@pytest.mark.parametrize("path", ["/organisations", "/events/index"])
def test_index_answers(authed, path):
    assert isinstance(authed.api("GET", path, authed.key), list)


def test_users_index(authed):
    # A key without site admin rights gets no user list; that is no failure
    assert authed.http("GET", "/admin/users/index", authed.key)[0] in (200, 403)


@pytest.fixture(scope="module")
def workers(authed):
    return authed.api("GET", "/servers/getWorkers", authed.key) or {}


def test_supervisord_reachable(workers):
    assert workers.get("supervisord_status") is True


@pytest.mark.parametrize("queue", WORKER_QUEUES)
def test_worker_queue(workers, queue):
    assert len(workers.get(queue, {}).get("workers") or []) > 0


def test_enrichment_url(authed):
    assert setting(authed, "Plugin.Enrichment_services_url")


@pytest.fixture(scope="module")
def event(authed):
    created = authed.api("POST", "/events/add", authed.key, {"Event": {
        "info": "smoketest-event", "distribution": "0",
        "Attribute": [{"type": "domain", "value": "smoketest.example.com", "to_ids": False}]}}) or {}
    return created.get("Event", {}).get("id")


def test_event_created(event):
    assert event


def test_event_deleted(authed, event):
    assert authed.http("POST", f"/events/delete/{event}", authed.key, {})[0] == 200
