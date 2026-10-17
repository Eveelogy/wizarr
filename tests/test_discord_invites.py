"""Single-use Discord invites for invited users.

The wizard widget renders a button only; the invite is created when the user
clicks it, works once, and is reused from the session if they click again.
"""

from unittest.mock import Mock, patch

import pytest
import requests

from app.extensions import db
from app.models import Settings
from app.services.discord_invites import (
    INVITE_MAX_AGE,
    DiscordInviteError,
    create_invite,
)
from app.services.wizard_presets import create_step_from_preset
from app.services.wizard_widgets import process_widget_placeholders

POST = "app.services.discord_invites.requests.post"


def _resp(status=200, payload=None):
    resp = Mock(spec=["status_code", "json"])
    resp.status_code = status
    resp.json = Mock(return_value=payload or {})
    return resp


@pytest.fixture
def configured(session):
    session.add_all(
        [
            Settings(key="discord_bot_token", value="bot-token"),
            Settings(key="discord_invite_channel_id", value="12345"),
        ]
    )
    session.commit()


@pytest.fixture
def invited_client(client):
    with client.session_transaction() as sess:
        sess["wizard_access"] = "INVITE1"
    return client


def test_create_invite_asks_for_single_use(app, configured):
    with (
        app.app_context(),
        patch(POST, return_value=_resp(200, {"code": "abc"})) as post,
    ):
        url, _expires = create_invite()

    assert url == "https://discord.gg/abc"
    assert (
        post.call_args.args[0] == "https://discord.com/api/v10/channels/12345/invites"
    )
    assert post.call_args.kwargs["headers"] == {"Authorization": "Bot bot-token"}
    assert post.call_args.kwargs["json"] == {
        "max_age": INVITE_MAX_AGE,
        "max_uses": 1,
        "unique": True,
    }


@pytest.mark.parametrize(
    ("status", "message"),
    [(401, "bot token"), (403, "Create Invite"), (404, "invite channel")],
)
def test_create_invite_explains_discord_errors(app, configured, status, message):
    with (
        app.app_context(),
        patch(POST, return_value=_resp(status)),
        pytest.raises(DiscordInviteError, match=message),
    ):
        create_invite()


def test_create_invite_wraps_network_errors(app, configured):
    with (
        app.app_context(),
        patch(POST, side_effect=requests.ConnectionError("down")),
        pytest.raises(DiscordInviteError, match="Cannot reach Discord"),
    ):
        create_invite()


def test_create_invite_requires_settings(app, session):
    with app.app_context(), pytest.raises(DiscordInviteError, match="not set"):
        create_invite()


def test_route_redirects_to_new_invite_and_reuses_it(invited_client, configured):
    with patch(POST, return_value=_resp(200, {"code": "abc"})) as post:
        first = invited_client.get("/wizard/discord-invite")
        second = invited_client.get("/wizard/discord-invite")

    assert first.status_code == 302
    assert first.headers["Location"] == "https://discord.gg/abc"
    assert second.headers["Location"] == "https://discord.gg/abc"
    assert post.call_count == 1


def test_route_refuses_visitors_without_an_invitation(client, configured):
    with patch(POST) as post:
        resp = client.get("/wizard/discord-invite")

    assert resp.status_code == 302
    assert "/wizard/help" in resp.headers["Location"]
    post.assert_not_called()


def test_route_refuses_visitors_even_with_public_wizard(client, configured, session):
    session.add(Settings(key="wizard_acl_enabled", value="false"))
    session.commit()
    with patch(POST) as post:
        resp = client.get("/wizard/discord-invite")

    assert "/wizard/help" in resp.headers["Location"]
    post.assert_not_called()


def test_route_shows_error_page_when_discord_fails(invited_client, configured):
    with patch(POST, return_value=_resp(403)):
        resp = invited_client.get("/wizard/discord-invite")

    assert resp.status_code == 502
    assert "Discord invite unavailable" in resp.get_data(as_text=True)


def test_widget_links_to_route_without_creating_an_invite(app, configured):
    with app.test_request_context(), patch(POST) as post:
        html = process_widget_placeholders(
            '{{ widget:discord_invite text="Come say hi" }}', "plex"
        )

    assert 'href="/wizard/discord-invite"' in html
    assert "Come say hi" in html
    post.assert_not_called()


def test_widget_hidden_until_configured(app, session):
    with app.test_request_context():
        html = process_widget_placeholders("{{ widget:discord_invite }}", "plex")

    assert html == ""


def test_preset_uses_the_widget():
    content = create_step_from_preset("discord_invite")
    assert '{{ widget:discord_invite text="Join our Discord" }}' in content


def test_general_settings_saves_discord_fields(admin_client, session):
    resp = admin_client.post(
        "/settings/general",
        data={
            "server_name": "Wizarr",
            "expiry_action": "delete",
            "cinema_posters_source": "",
            "discord_bot_token": "bot-token",
            "discord_invite_channel_id": "12345",
        },
        headers={"HX-Request": "true"},
    )

    assert resp.status_code == 200
    assert "{{ widget:discord_invite }}" in resp.get_data(as_text=True)
    saved = {s.key: s.value for s in db.session.query(Settings).all()}
    assert saved["discord_bot_token"] == "bot-token"
    assert saved["discord_invite_channel_id"] == "12345"
