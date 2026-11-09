"""Tests for app.provisioning (Reltio user creation / role merging)."""
import pytest
from unittest.mock import AsyncMock, patch

from app.provisioning import build_new_user, merge_roles, provision


# ---------------------------------------------------------------------------
# build_new_user (pure helper, no mocks needed)
# ---------------------------------------------------------------------------
class TestBuildNewUser:

    def test_structure(self):
        user = build_new_user(
            customer="EliLilly",
            username="L123456",
            email="user@lilly.com",
            roles=["ROLE_A", "ROLE_B"],
            tenants=["t1", "t2"],
        )
        assert user["customer"] == "EliLilly"
        assert user["username"] == "L123456"
        assert user["email"] == "user@lilly.com"
        assert user["enabled"] is True
        assert user["externalUser"] is True
        assert user["userPermissions"]["roles"] == {
            "ROLE_A": ["t1", "t2"],
            "ROLE_B": ["t1", "t2"],
        }


# ---------------------------------------------------------------------------
# merge_roles (pure helper)
# ---------------------------------------------------------------------------
class TestMergeRoles:

    def test_adds_new_roles(self):
        existing = {
            "username": "L123456",
            "userPermissions": {"roles": {"ROLE_OLD": ["t1"]}},
        }
        body, added, already = merge_roles(existing, ["ROLE_NEW"], ["t1"])
        assert "ROLE_NEW" in added
        assert added["ROLE_NEW"] == ["t1"]
        assert "ROLE_NEW" in body["userPermissions"]["roles"]

    def test_adds_new_tenants_to_existing_role(self):
        existing = {
            "username": "L123456",
            "userPermissions": {"roles": {"ROLE_A": ["t1"]}},
        }
        body, added, already = merge_roles(existing, ["ROLE_A"], ["t1", "t2"])
        assert added == {"ROLE_A": ["t2"]}
        assert already == {"ROLE_A": ["t1"]}
        assert set(body["userPermissions"]["roles"]["ROLE_A"]) == {"t1", "t2"}

    def test_already_has_everything(self):
        existing = {
            "username": "L123456",
            "userPermissions": {"roles": {"ROLE_A": ["t1"]}},
        }
        body, added, already = merge_roles(existing, ["ROLE_A"], ["t1"])
        assert added == {}
        assert already == {"ROLE_A": ["t1"]}

    def test_strips_server_managed_fields(self):
        existing = {
            "username": "L123456",
            "createdDate": "2024-01-01",
            "updatedDate": "2024-06-01",
            "roles": ["old_system_field"],
            "userPermissions": {"roles": {}},
        }
        body, _, _ = merge_roles(existing, ["ROLE_A"], ["t1"])
        assert "createdDate" not in body
        assert "updatedDate" not in body
        assert "roles" not in body  # the top-level 'roles' key is server-managed
        assert "username" in body


# ---------------------------------------------------------------------------
# provision() -- async, with mocked reltio calls
# ---------------------------------------------------------------------------
class TestProvision:

    @pytest.fixture()
    def item(self):
        return {
            "global_id": "L999999",
            "email": "new@lilly.com",
            "reltio_roles": ["ROLE_A", "ROLE_B"],
            "tenants": ["t1"],
        }

    @pytest.mark.asyncio
    async def test_new_user_path(self, settings, item):
        """When Reltio has no user, provision should create one."""
        with (
            patch("app.reltio.get_valid_role_names", new_callable=AsyncMock,
                  return_value={"ROLE_A", "ROLE_B"}),
            patch("app.reltio.get_user", new_callable=AsyncMock, return_value=None) as mock_get,
            patch("app.reltio.create_user", new_callable=AsyncMock, return_value={}) as mock_create,
        ):
            result = await provision(settings, item)

        assert result["action"] == "created"
        assert "ROLE_A" in result["added"]
        mock_get.assert_awaited_once_with(settings, "L999999")
        mock_create.assert_awaited_once()
        created_body = mock_create.call_args[0][1]
        assert created_body["username"] == "L999999"
        assert created_body["email"] == "new@lilly.com"
        assert set(created_body["userPermissions"]["roles"].keys()) == {"ROLE_A", "ROLE_B"}

    @pytest.mark.asyncio
    async def test_existing_user_merge_path(self, settings, item):
        """When the user exists, provision should merge roles."""
        existing_user = {
            "username": "L999999",
            "customer": "EliLilly",
            "email": "new@lilly.com",
            "enabled": True,
            "userPermissions": {"roles": {"ROLE_OLD": ["t1"]}},
        }
        with (
            patch("app.reltio.get_valid_role_names", new_callable=AsyncMock,
                  return_value={"ROLE_A", "ROLE_B", "ROLE_OLD"}),
            patch("app.reltio.get_user", new_callable=AsyncMock, return_value=existing_user),
            patch("app.reltio.update_user", new_callable=AsyncMock, return_value={}) as mock_update,
        ):
            result = await provision(settings, item)

        assert result["action"] == "updated"
        assert "ROLE_A" in result["added"]
        assert "ROLE_B" in result["added"]
        mock_update.assert_awaited_once()
        updated_body = mock_update.call_args[0][2]
        assert "ROLE_A" in updated_body["userPermissions"]["roles"]
        assert "ROLE_B" in updated_body["userPermissions"]["roles"]
        assert "ROLE_OLD" in updated_body["userPermissions"]["roles"]

    @pytest.mark.asyncio
    async def test_idempotent_no_change(self, settings, item):
        """If the user already has every requested role+tenant, nothing changes."""
        existing_user = {
            "username": "L999999",
            "customer": "EliLilly",
            "email": "new@lilly.com",
            "enabled": True,
            "userPermissions": {"roles": {"ROLE_A": ["t1"], "ROLE_B": ["t1"]}},
        }
        with (
            patch("app.reltio.get_valid_role_names", new_callable=AsyncMock,
                  return_value={"ROLE_A", "ROLE_B"}),
            patch("app.reltio.get_user", new_callable=AsyncMock, return_value=existing_user),
            patch("app.reltio.update_user", new_callable=AsyncMock) as mock_update,
        ):
            result = await provision(settings, item)

        assert result["action"] == "no_change"
        assert result["added"] == {}
        mock_update.assert_not_awaited()
