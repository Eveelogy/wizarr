"""
ReadMeABook companion client implementation.

ReadMeABook has no admin endpoint that creates users, and its API tokens are
limited to an allowlist that excludes user management. So this client works the
way ReadMeABook's own UI does: it signs in as a local admin to manage users, and
creates accounts through the public registration route, approving them straight
away when the instance requires admin approval.

Accounts are only created when ReadMeABook runs in Audiobookshelf mode. In Plex
mode it signs everyone in with Plex, so an invited user only needs the Plex share
Wizarr already gave them and there is nothing to create.
"""

import logging

import requests
import requests.exceptions
from flask import has_request_context, request

from app.models import Connection

from .base import CompanionClient

TIMEOUT = 10


class ReadMeABookError(Exception):
    """Raised when ReadMeABook refuses or cannot complete a request."""


class ReadMeABookClient(CompanionClient):
    """Client for integrating with ReadMeABook."""

    @property
    def requires_api_call(self) -> bool:
        return True

    @property
    def display_name(self) -> str:
        return "ReadMeABook"

    # ------------------------------------------------------------------
    # HTTP helpers
    # ------------------------------------------------------------------
    @staticmethod
    def _base_url(connection: Connection) -> str:
        return (connection.url or "").rstrip("/")

    @staticmethod
    def _error_text(resp: requests.Response) -> str:
        try:
            data = resp.json()
        except ValueError:
            return f"HTTP {resp.status_code}"
        return data.get("message") or data.get("error") or f"HTTP {resp.status_code}"

    def _admin_token(self, connection: Connection) -> str:
        """Sign in as the configured local admin and return its access token."""
        if not connection.url:
            raise ReadMeABookError("Connection is missing its URL")
        if not connection.admin_username or not connection.api_key:
            raise ReadMeABookError(
                "Connection is missing the admin username or password"
            )

        resp = requests.post(
            f"{self._base_url(connection)}/api/auth/admin/login",
            json={
                "username": connection.admin_username,
                "password": connection.api_key,
            },
            timeout=TIMEOUT,
        )
        if resp.status_code == 401:
            raise ReadMeABookError("Invalid admin username or password")
        if resp.status_code == 403:
            raise ReadMeABookError(
                "Local login is disabled on ReadMeABook (DISABLE_LOCAL_LOGIN)"
            )
        if resp.status_code != 200:
            raise ReadMeABookError(f"Admin login failed: {self._error_text(resp)}")

        token = resp.json().get("accessToken")
        if not token:
            raise ReadMeABookError("Admin login returned no access token")
        return token

    def _get(self, connection: Connection, token: str, path: str) -> dict:
        resp = requests.get(
            f"{self._base_url(connection)}{path}",
            headers={"Authorization": f"Bearer {token}"},
            timeout=TIMEOUT,
        )
        if resp.status_code == 403:
            raise ReadMeABookError("The configured account is not an admin")
        if resp.status_code != 200:
            raise ReadMeABookError(f"GET {path} failed: {self._error_text(resp)}")
        return resp.json()

    def _settings(self, connection: Connection, token: str) -> dict:
        return self._get(connection, token, "/api/admin/settings")

    @staticmethod
    def _forwarded_for() -> dict[str, str]:
        """Pass the invitee's address on, as a reverse proxy would.

        ReadMeABook rate limits registration per X-Forwarded-For. Without the
        header every request Wizarr makes shares one bucket, so only a handful
        of invites an hour would get an account.
        """
        if has_request_context() and request.remote_addr:
            return {"X-Forwarded-For": request.remote_addr}
        return {}

    # ------------------------------------------------------------------
    # CompanionClient interface
    # ------------------------------------------------------------------
    def invite_user(
        self,
        username: str,
        email: str,  # noqa: ARG002
        connection: Connection,
        password: str = "",
    ) -> dict[str, str]:
        """
        Create a local ReadMeABook account with the invitee's credentials.

        Args:
            username: Username to create
            email: Email address (unused - ReadMeABook local accounts have none)
            connection: Connection object with URL and admin credentials
            password: The password the invitee chose during the invite

        Returns:
            Dict with 'status' and 'message' keys
        """
        try:
            token = self._admin_token(connection)
            settings = self._settings(connection, token)

            if settings.get("backendMode", "plex") == "plex":
                return {
                    "status": "info",
                    "message": (
                        f"{connection.name} signs users in with Plex, so no "
                        "account is needed"
                    ),
                }
            if not settings.get("registration", {}).get("enabled"):
                return {
                    "status": "error",
                    "message": (
                        f"Turn on registration in {connection.name}'s "
                        "authentication settings so Wizarr can create accounts"
                    ),
                }
            if not password:
                return {
                    "status": "error",
                    "message": (
                        f"No password was set during the invite, so no "
                        f"{connection.name} account was created"
                    ),
                }

            resp = requests.post(
                f"{self._base_url(connection)}/api/auth/register",
                json={"username": username, "password": password},
                headers=self._forwarded_for(),
                timeout=TIMEOUT,
            )
            if resp.status_code != 200:
                logging.warning(
                    "ReadMeABook registration failed for %s → %s: %s",
                    username,
                    resp.status_code,
                    resp.text,
                )
                return {
                    "status": "error",
                    "message": (
                        f"Failed to invite user to {connection.name}: "
                        f"{self._error_text(resp)}"
                    ),
                }

            if resp.json().get("pendingApproval"):
                self._approve(connection, token, username)

            logging.info("ReadMeABook user %s created on %s", username, connection.name)
            return {
                "status": "success",
                "message": f"User invited to {connection.name}",
            }
        except Exception as exc:
            logging.error("ReadMeABook user creation error for %s: %s", username, exc)
            return {
                "status": "error",
                "message": f"Error inviting user to {connection.name}: {exc!s}",
            }

    def _approve(self, connection: Connection, token: str, username: str) -> None:
        """Approve a pending registration, since the invite already vouches for it."""
        pending = self._get(connection, token, "/api/admin/users/pending")
        user_id = next(
            (
                u["id"]
                for u in pending.get("users", [])
                if u.get("plexUsername") == username.strip().lower()
                and u.get("authProvider") == "local"
            ),
            None,
        )
        if not user_id:
            raise ReadMeABookError(f"Pending account for {username} not found")

        resp = requests.post(
            f"{self._base_url(connection)}/api/admin/users/{user_id}/approve",
            headers={"Authorization": f"Bearer {token}"},
            json={"approve": True},
            timeout=TIMEOUT,
        )
        if resp.status_code != 200:
            raise ReadMeABookError(f"Approval failed: {self._error_text(resp)}")

    def delete_user(self, username: str, connection: Connection) -> dict[str, str]:
        """
        Delete a local user from ReadMeABook.

        ReadMeABook soft-deletes, keeping the user's requests, and refuses to
        delete Plex or OIDC users since those are managed by their provider.

        Args:
            username: Username to delete
            connection: Connection object with URL and admin credentials

        Returns:
            Dict with 'status' and 'message' keys
        """
        try:
            token = self._admin_token(connection)
            users = self._get(connection, token, "/api/admin/users").get("users", [])
            user_id = next(
                (
                    u["id"]
                    for u in users
                    if u.get("plexUsername") == username.strip().lower()
                    and u.get("authProvider") == "local"
                ),
                None,
            )
            if not user_id:
                return {
                    "status": "warning",
                    "message": f"User {username} not found in {connection.name}",
                }

            resp = requests.delete(
                f"{self._base_url(connection)}/api/admin/users/{user_id}",
                headers={"Authorization": f"Bearer {token}"},
                timeout=TIMEOUT,
            )
            if resp.status_code == 200:
                logging.info(
                    "ReadMeABook deleted user %s from %s", username, connection.name
                )
                return {
                    "status": "success",
                    "message": f"User deleted from {connection.name}",
                }
            return {
                "status": "error",
                "message": (
                    f"Failed to delete user from {connection.name}: "
                    f"{self._error_text(resp)}"
                ),
            }
        except Exception as exc:
            logging.error(
                "ReadMeABook delete user error for %s from %s: %s",
                username,
                connection.name,
                exc,
            )
            return {
                "status": "error",
                "message": f"Error deleting user from {connection.name}: {exc!s}",
            }

    def test_connection(self, connection: Connection) -> dict[str, str]:
        """
        Sign in as the admin and report what invites will do on this instance.

        Args:
            connection: Connection object with URL and admin credentials

        Returns:
            Dict with 'status' and 'message' keys
        """
        try:
            token = self._admin_token(connection)
            settings = self._settings(connection, token)
        except requests.exceptions.ConnectionError:
            return {
                "status": "error",
                "message": "Cannot connect to server. Check URL.",
            }
        except requests.exceptions.Timeout:
            return {
                "status": "error",
                "message": "Connection timeout. Server may be slow or unreachable.",
            }
        except Exception as exc:
            return {"status": "error", "message": f"Connection test failed: {exc!s}"}

        if settings.get("backendMode", "plex") == "plex":
            return {
                "status": "success",
                "message": (
                    "Connection successful. ReadMeABook is in Plex mode, so invited "
                    "users sign in with Plex and no accounts are created."
                ),
            }
        if not settings.get("registration", {}).get("enabled"):
            return {
                "status": "error",
                "message": (
                    "Connected, but registration is turned off in ReadMeABook's "
                    "authentication settings, so Wizarr cannot create accounts."
                ),
            }
        return {
            "status": "success",
            "message": (
                "Connection successful. Invited users get a ReadMeABook account "
                "with the username and password they choose."
            ),
        }
