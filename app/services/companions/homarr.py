"""
Homarr companion client implementation.

Homarr's REST API authenticates with an API key sent in the ``ApiKey`` header.
A key acts as the admin who created it, so it can create and delete users as
long as that admin has the "admin" permission.

Only credentials accounts can be created this way, so Homarr must have the
credentials provider enabled (the default). Users signing in through OIDC or
LDAP get their accounts from those providers and are never touched here.
"""

import logging

import requests
import requests.exceptions

from app.models import Connection

from .base import CompanionClient

TIMEOUT = 10


class HomarrError(Exception):
    """Raised when Homarr refuses or cannot complete a request."""


class HomarrClient(CompanionClient):
    """Client for integrating with Homarr."""

    @property
    def requires_api_call(self) -> bool:
        return True

    @property
    def display_name(self) -> str:
        return "Homarr"

    # ------------------------------------------------------------------
    # HTTP helpers
    # ------------------------------------------------------------------
    @staticmethod
    def _base_url(connection: Connection) -> str:
        return (connection.url or "").rstrip("/")

    @staticmethod
    def _headers(connection: Connection) -> dict[str, str]:
        return {"ApiKey": connection.api_key or "", "Content-Type": "application/json"}

    @staticmethod
    def _error_text(resp: requests.Response) -> str:
        try:
            data = resp.json()
        except ValueError:
            return f"HTTP {resp.status_code}"
        if isinstance(data, dict):
            return (
                data.get("message") or data.get("error") or f"HTTP {resp.status_code}"
            )
        return f"HTTP {resp.status_code}"

    def _get(self, connection: Connection, path: str):
        resp = requests.get(
            f"{self._base_url(connection)}{path}",
            headers=self._headers(connection),
            timeout=TIMEOUT,
        )
        if resp.status_code == 401:
            raise HomarrError("Invalid API key")
        if resp.status_code == 403:
            raise HomarrError("The API key's owner is not a Homarr admin")
        if resp.status_code != 200:
            raise HomarrError(f"GET {path} failed: {self._error_text(resp)}")
        return resp.json()

    def _find_credentials_user(
        self, connection: Connection, username: str
    ) -> str | None:
        """Return the id of the credentials user with this name, if any.

        Homarr stores usernames in lowercase and only keeps them unique per
        provider, so an OIDC or LDAP user can share the name. Each match is
        checked so only the account Wizarr created is ever returned.
        """
        name = username.strip().lower()
        for user in self._get(connection, "/api/users"):
            if (user.get("name") or "").lower() != name:
                continue
            details = self._get(connection, f"/api/users/{user['id']}")
            if details.get("provider") == "credentials":
                return user["id"]
        return None

    # ------------------------------------------------------------------
    # CompanionClient interface
    # ------------------------------------------------------------------
    def invite_user(
        self,
        username: str,
        email: str,
        connection: Connection,
        password: str = "",
    ) -> dict[str, str]:
        """
        Create a Homarr credentials account with the invitee's credentials.

        Args:
            username: Username to create
            email: Email address (sent when present)
            connection: Connection object with URL and API key
            password: The password the invitee chose during the invite

        Returns:
            Dict with 'status' and 'message' keys
        """
        if not connection.url or not connection.api_key:
            return {"status": "error", "message": "Connection missing URL or API key"}
        if not password:
            return {
                "status": "error",
                "message": (
                    f"No password was set during the invite, so no "
                    f"{connection.name} account was created"
                ),
            }

        payload = {
            "username": username,
            "password": password,
            "confirmPassword": password,
            "groupIds": [],
        }
        if email:
            payload["email"] = email

        try:
            resp = requests.post(
                f"{self._base_url(connection)}/api/users",
                headers=self._headers(connection),
                json=payload,
                timeout=TIMEOUT,
            )
            if resp.status_code in (200, 201, 204):
                logging.info("Homarr user %s created on %s", username, connection.name)
                return {
                    "status": "success",
                    "message": f"User invited to {connection.name}",
                }

            logging.warning(
                "Homarr user creation failed for %s → %s: %s",
                username,
                resp.status_code,
                resp.text,
            )
            # A 403 means the key's owner is not an admin or credentials
            # sign-in is disabled; Homarr's message says which.
            reason = (
                f"user {username} already exists"
                if resp.status_code == 409
                else self._error_text(resp)
            )
            return {
                "status": "error",
                "message": f"Failed to invite user to {connection.name}: {reason}",
            }
        except Exception as exc:
            logging.error("Homarr user creation error for %s: %s", username, exc)
            return {
                "status": "error",
                "message": f"Error inviting user to {connection.name}: {exc!s}",
            }

    def delete_user(self, username: str, connection: Connection) -> dict[str, str]:
        """
        Delete a credentials user from Homarr.

        Args:
            username: Username to delete
            connection: Connection object with URL and API key

        Returns:
            Dict with 'status' and 'message' keys
        """
        if not connection.url or not connection.api_key:
            return {"status": "error", "message": "Connection missing URL or API key"}

        try:
            user_id = self._find_credentials_user(connection, username)
            if not user_id:
                return {
                    "status": "warning",
                    "message": f"User {username} not found in {connection.name}",
                }

            resp = requests.delete(
                f"{self._base_url(connection)}/api/users/{user_id}",
                headers=self._headers(connection),
                timeout=TIMEOUT,
            )
            if resp.status_code in (200, 204):
                logging.info(
                    "Homarr deleted user %s from %s", username, connection.name
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
                "Homarr delete user error for %s from %s: %s",
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
        Check the API key works and belongs to an admin who can manage users.

        Args:
            connection: Connection object with URL and API key

        Returns:
            Dict with 'status' and 'message' keys
        """
        if not connection.url or not connection.api_key:
            return {"status": "error", "message": "URL and API key are required"}

        try:
            # Listing users needs the admin permission, the same as creating them.
            self._get(connection, "/api/users")
            try:
                version = self._get(connection, "/api/info").get("version")
            except HomarrError:
                version = None
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

        suffix = f" (Homarr {version})" if version else ""
        return {
            "status": "success",
            "message": (
                f"Connection successful{suffix}. Invited users get a Homarr account "
                "with the username and password they choose."
            ),
        }
