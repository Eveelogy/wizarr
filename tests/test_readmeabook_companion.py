"""ReadMeABook companion: accounts are created through its own sign-in routes.

ReadMeABook's API tokens cannot reach user management, so the client signs in
as a local admin, registers the invitee through the public route, and approves
the account when the instance requires approval. In Plex mode nothing is
created, because everyone signs in with Plex.
"""

from contextlib import contextmanager
from unittest.mock import Mock, patch

import pytest
import requests

from app.models import Connection, MediaServer
from app.services.companions import get_companion_client
from app.services.companions.readmeabook import ReadMeABookClient

BASE = "https://rmab.local"


@contextmanager
def _requests():
    """Stand in for requests, keeping its real exception classes."""
    with patch("app.services.companions.readmeabook.requests") as req:
        req.exceptions = requests.exceptions
        yield req


def _connection():
    conn = Mock(spec=["url", "api_key", "admin_username", "name"])
    conn.url = BASE + "/"
    conn.admin_username = "admin"
    conn.api_key = "admin-password"
    conn.name = "ReadMeABook"
    return conn


def _resp(status=200, payload=None):
    resp = Mock(spec=["status_code", "text", "json"])
    resp.status_code = status
    resp.text = str(payload)
    resp.json = Mock(return_value=payload if payload is not None else {})
    return resp


LOGIN = _resp(200, {"accessToken": "jwt"})


def _settings(mode="audiobookshelf", registration=True):
    return _resp(
        200,
        {"backendMode": mode, "registration": {"enabled": registration}},
    )


def test_registered():
    assert get_companion_client("readmeabook") is ReadMeABookClient


def test_invite_registers_account():
    with _requests() as req:
        req.post.side_effect = [LOGIN, _resp(200, {"success": True})]
        req.get.return_value = _settings()

        result = ReadMeABookClient().invite_user(
            "Alice", "a@example.com", _connection(), "s3cretpass"
        )

    assert result["status"] == "success"
    login_call, register_call = req.post.call_args_list
    assert login_call.args[0] == f"{BASE}/api/auth/admin/login"
    assert login_call.kwargs["json"] == {
        "username": "admin",
        "password": "admin-password",
    }
    assert register_call.args[0] == f"{BASE}/api/auth/register"
    assert register_call.kwargs["json"] == {
        "username": "Alice",
        "password": "s3cretpass",
    }


def test_invite_approves_pending_account():
    pending = _resp(
        200,
        {"users": [{"id": "u1", "plexUsername": "alice", "authProvider": "local"}]},
    )
    with _requests() as req:
        req.post.side_effect = [
            LOGIN,
            _resp(200, {"success": False, "pendingApproval": True}),
            _resp(200, {"success": True}),
        ]
        req.get.side_effect = [_settings(), pending]

        result = ReadMeABookClient().invite_user(
            "Alice", "", _connection(), "s3cretpass"
        )

    assert result["status"] == "success"
    approve_call = req.post.call_args_list[2]
    assert approve_call.args[0] == f"{BASE}/api/admin/users/u1/approve"
    assert approve_call.kwargs["json"] == {"approve": True}


def test_invite_in_plex_mode_creates_nothing():
    with _requests() as req:
        req.post.return_value = LOGIN
        req.get.return_value = _settings(mode="plex")

        result = ReadMeABookClient().invite_user("alice", "", _connection(), "pw")

    assert result["status"] == "info"
    assert req.post.call_count == 1  # only the admin login


def test_invite_reports_disabled_registration():
    with _requests() as req:
        req.post.return_value = LOGIN
        req.get.return_value = _settings(registration=False)

        result = ReadMeABookClient().invite_user("alice", "", _connection(), "pw")

    assert result["status"] == "error"
    assert "registration" in result["message"]


def test_invite_without_password_is_an_error():
    with _requests() as req:
        req.post.return_value = LOGIN
        req.get.return_value = _settings()

        result = ReadMeABookClient().invite_user("alice", "", _connection(), "")

    assert result["status"] == "error"
    assert req.post.call_count == 1


def test_bad_admin_credentials():
    with _requests() as req:
        req.post.return_value = _resp(401, {"error": "AuthenticationError"})

        result = ReadMeABookClient().test_connection(_connection())

    assert result["status"] == "error"
    assert "admin username or password" in result["message"]


def test_delete_finds_local_user_by_username():
    users = _resp(
        200,
        {
            "users": [
                {"id": "p1", "plexUsername": "alice", "authProvider": "plex"},
                {"id": "l1", "plexUsername": "alice", "authProvider": "local"},
            ]
        },
    )
    with _requests() as req:
        req.post.return_value = LOGIN
        req.get.return_value = users
        req.delete.return_value = _resp(200, {"success": True})

        result = ReadMeABookClient().delete_user("Alice", _connection())

    assert result["status"] == "success"
    assert req.delete.call_args.args[0] == f"{BASE}/api/admin/users/l1"


def test_delete_missing_user_is_a_warning():
    with _requests() as req:
        req.post.return_value = LOGIN
        req.get.return_value = _resp(200, {"users": []})

        result = ReadMeABookClient().delete_user("alice", _connection())

    assert result["status"] == "warning"
    req.delete.assert_not_called()


@pytest.fixture
def media_server(session):
    server = MediaServer(
        name="ABS",
        server_type="audiobookshelf",
        url="http://abs:13378",
        api_key="test-key",
    )
    session.add(server)
    session.commit()
    yield server.id
    session.rollback()
    session.query(Connection).delete()
    session.commit()


def test_form_shows_admin_fields(admin_client, media_server):
    resp = admin_client.get(
        "/settings/connections/create?connection_type=readmeabook",
        headers={"HX-Request": "true"},
    )
    html = resp.get_data(as_text=True)
    assert resp.status_code == 200
    assert 'name="admin_username"' in html
    assert "Admin Password" in html
    assert "ReadMeABook Information" in html


def test_create_connection_saves_admin_username(admin_client, media_server):
    with patch.object(
        ReadMeABookClient,
        "test_connection",
        return_value={"status": "success", "message": "ok"},
    ):
        resp = admin_client.post(
            "/settings/connections/create",
            data={
                "connection_type": "readmeabook",
                "name": "Books",
                "media_server_id": str(media_server),
                "url": "https://rmab.example.com",
                "admin_username": "admin",
                "api_key": "admin-password",
            },
        )
    assert resp.status_code == 302

    conn = Connection.query.filter_by(name="Books").one()
    assert conn.connection_type == "readmeabook"
    assert conn.admin_username == "admin"
    assert conn.api_key == "admin-password"
