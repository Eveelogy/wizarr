"""Homarr companion: accounts are created and deleted through Homarr's REST API.

The API key is sent in the ``ApiKey`` header and acts as the admin who made it.
Deletion only ever touches the credentials account with the user's name, never
an OIDC or LDAP user that happens to share it.
"""

from contextlib import contextmanager
from unittest.mock import Mock, patch

import pytest
import requests

from app.models import Connection, MediaServer
from app.services.companions import get_companion_client, list_companion_types
from app.services.companions.homarr import HomarrClient

BASE = "https://homarr.local"


@contextmanager
def _requests():
    """Stand in for requests, keeping its real exception classes."""
    with patch("app.services.companions.homarr.requests") as req:
        req.exceptions = requests.exceptions
        yield req


def _connection():
    conn = Mock(spec=["url", "api_key", "name"])
    conn.url = BASE + "/"
    conn.api_key = "keyid.secret"
    conn.name = "Homarr"
    return conn


def _resp(status=200, payload=None):
    resp = Mock(spec=["status_code", "text", "json"])
    resp.status_code = status
    resp.text = str(payload)
    resp.json = Mock(return_value=payload if payload is not None else {})
    return resp


def test_registered():
    assert get_companion_client("homarr") is HomarrClient
    assert ("homarr", "Homarr") in list_companion_types()


def test_invite_creates_credentials_user():
    with _requests() as req:
        req.post.return_value = _resp(200, None)

        result = HomarrClient().invite_user(
            "Alice", "a@example.com", _connection(), "S3cret!pass"
        )

    assert result["status"] == "success"
    call = req.post.call_args
    assert call.args[0] == f"{BASE}/api/users"
    assert call.kwargs["headers"]["ApiKey"] == "keyid.secret"
    assert call.kwargs["json"] == {
        "username": "Alice",
        "password": "S3cret!pass",
        "confirmPassword": "S3cret!pass",
        "groupIds": [],
        "email": "a@example.com",
    }


def test_invite_omits_blank_email():
    with _requests() as req:
        req.post.return_value = _resp(200, None)
        HomarrClient().invite_user("alice", "", _connection(), "S3cret!pass")

    assert "email" not in req.post.call_args.kwargs["json"]


def test_invite_without_password_creates_nothing():
    with _requests() as req:
        result = HomarrClient().invite_user("alice", "", _connection(), "")

    assert result["status"] == "error"
    req.post.assert_not_called()


def test_invite_reports_existing_user():
    with _requests() as req:
        req.post.return_value = _resp(409, {"message": "Username already taken"})
        result = HomarrClient().invite_user("alice", "", _connection(), "S3cret!pass")

    assert result["status"] == "error"
    assert "already exists" in result["message"]


def test_invite_passes_on_homarr_message():
    with _requests() as req:
        req.post.return_value = _resp(
            403, {"message": "Credentials provider is disabled"}
        )
        result = HomarrClient().invite_user("alice", "", _connection(), "S3cret!pass")

    assert result["status"] == "error"
    assert "Credentials provider is disabled" in result["message"]


def test_delete_skips_same_name_from_other_provider():
    users = [
        {"id": "oidc1", "name": "alice"},
        {"id": "cred1", "name": "alice"},
        {"id": "bob1", "name": "bob"},
    ]
    with _requests() as req:
        req.get.side_effect = [
            _resp(200, users),
            _resp(200, {"id": "oidc1", "provider": "oidc"}),
            _resp(200, {"id": "cred1", "provider": "credentials"}),
        ]
        req.delete.return_value = _resp(200, None)

        result = HomarrClient().delete_user("Alice", _connection())

    assert result["status"] == "success"
    assert req.delete.call_args.args[0] == f"{BASE}/api/users/cred1"


def test_delete_missing_user_warns():
    with _requests() as req:
        req.get.return_value = _resp(200, [{"id": "bob1", "name": "bob"}])
        result = HomarrClient().delete_user("alice", _connection())

    assert result["status"] == "warning"
    req.delete.assert_not_called()


def test_test_connection_success_reports_version():
    with _requests() as req:
        req.get.side_effect = [_resp(200, []), _resp(200, {"version": "1.40.0"})]
        result = HomarrClient().test_connection(_connection())

    assert result["status"] == "success"
    assert "1.40.0" in result["message"]


def test_test_connection_rejects_non_admin_key():
    with _requests() as req:
        req.get.return_value = _resp(403, {"message": "Permission denied"})
        result = HomarrClient().test_connection(_connection())

    assert result["status"] == "error"
    assert "not a Homarr admin" in result["message"]


def test_test_connection_unreachable():
    with _requests() as req:
        req.get.side_effect = requests.exceptions.ConnectionError()
        result = HomarrClient().test_connection(_connection())

    assert result["status"] == "error"
    assert "Cannot connect" in result["message"]


@pytest.fixture
def media_server(session):
    server = MediaServer(
        name="Jellyfin",
        server_type="jellyfin",
        url="http://jellyfin:8096",
        api_key="test-key",
    )
    session.add(server)
    session.commit()
    yield server.id
    session.rollback()
    session.query(Connection).delete()
    session.commit()


@pytest.mark.parametrize("htmx", [False, True], ids=["page", "modal"])
def test_form_explains_homarr_setup(admin_client, media_server, htmx):
    headers = {"HX-Request": "true"} if htmx else {}
    resp = admin_client.get(
        "/settings/connections/create?connection_type=homarr", headers=headers
    )
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    assert "Homarr Information" in html
    assert 'name="api_key"' in html
