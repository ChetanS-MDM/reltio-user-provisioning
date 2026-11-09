"""Tests for app.matrix (DoU parsing and roles-matrix resolution)."""
import pytest

from app.matrix import ResolveResult, normalize_country, parse_dou, resolve, _matrix_cache


@pytest.fixture(autouse=True)
def _clear_matrix_cache():
    """Ensure each test gets a fresh matrix load."""
    _matrix_cache["index"] = None
    _matrix_cache["version"] = None
    yield
    _matrix_cache["index"] = None
    _matrix_cache["version"] = None


# ---------------------------------------------------------------------------
# parse_dou()
# ---------------------------------------------------------------------------
class TestParseDou:

    def test_strips_full_prefix_and_version(self):
        raw = "~2578 Enterprise Profile-DOU-Global EPH IT Analyst Read Only V18"
        assert parse_dou(raw) == "Global EPH IT Analyst Read Only"

    def test_strips_prefix_without_tilde(self):
        raw = "2578 Enterprise Profile-DOU-Basic Search and Find V03"
        assert parse_dou(raw) == "Basic Search and Find"

    def test_handles_raw_name(self):
        assert parse_dou("Consent consumer (GCCP)") == "Consent consumer (GCCP)"

    def test_strips_version_suffix(self):
        assert parse_dou("Some Use Case V99") == "Some Use Case"

    def test_preserves_descriptor(self):
        """'Global EPH IT Analyst' and 'Global EPH IT Analyst Read Only'
        are different use cases and must not conflate."""
        assert parse_dou("Global EPH IT Analyst") == "Global EPH IT Analyst"
        assert parse_dou("Global EPH IT Analyst Read Only") == "Global EPH IT Analyst Read Only"


# ---------------------------------------------------------------------------
# normalize_country()
# ---------------------------------------------------------------------------
class TestNormalizeCountry:

    def test_valid_two_letter(self, settings):
        assert normalize_country("us", settings) == "US"

    def test_global(self, settings):
        assert normalize_country("global", settings) == "GLOBAL"

    def test_extra_country_codes(self):
        """Extra country codes configured in settings are accepted."""
        from app.config import Settings
        s = Settings(
            extra_country_codes="APAC,EMEA",
            entra_tenant_id="t", entra_api_audience="a",
        )
        assert normalize_country("APAC", s) == "APAC"
        assert normalize_country("emea", s) == "EMEA"

    def test_invalid(self, settings):
        assert normalize_country("XYZ", settings) is None
        assert normalize_country("", settings) is None
        assert normalize_country(None, settings) is None


# ---------------------------------------------------------------------------
# resolve()
# ---------------------------------------------------------------------------
class TestResolve:

    @pytest.mark.asyncio
    async def test_matched_use_case(self, settings):
        """'Consent consumer (GCCP)' is non-country-scoped and not retired."""
        valid_roles = {
            "ROLE_LILLY_EPH_GLOBAL_GCCP",
            "ROLE_API_EXPORT",
        }
        result = await resolve(settings, "Consent consumer (GCCP)", valid_roles)
        assert result.matched is True
        assert result.retired is False
        assert "ROLE_LILLY_EPH_GLOBAL_GCCP" in result.reltio_roles
        assert "ROLE_API_EXPORT" in result.reltio_roles
        assert result.unknown_roles == []
        assert result.grantable is True

    @pytest.mark.asyncio
    async def test_unmatched_dou(self, settings):
        result = await resolve(settings, "Totally Made Up Use Case", set())
        assert result.matched is False
        assert result.error is not None
        assert result.grantable is False

    @pytest.mark.asyncio
    async def test_retired_use_case(self, settings):
        """'Commercial Customer Search and Find' is marked as retired."""
        valid_roles = {
            "ROLE_LILLY_EPH_GLOBAL_COMSEARCH",
            "ROLE_API_NO_EXPORT",
        }
        result = await resolve(settings, "Commercial Customer Search and Find", valid_roles)
        assert result.matched is True
        assert result.retired is True
        assert result.grantable is False

    @pytest.mark.asyncio
    async def test_country_placeholder_substitution(self, settings):
        """'Basic Search and Find' has a country placeholder in its roles."""
        valid_roles = {
            "ROLE_LILLY_EPH_US_BASICSEARCH",
            "ROLE_API_NO_EXPORT",
        }
        result = await resolve(settings, "Basic Search and Find", valid_roles, country_code="US")
        assert result.matched is True
        assert result.country_scoped is True
        assert "ROLE_LILLY_EPH_US_BASICSEARCH" in result.reltio_roles
        assert result.grantable is True

    @pytest.mark.asyncio
    async def test_country_scoped_without_country_code(self, settings):
        """Country-scoped use case without a country code should error."""
        result = await resolve(settings, "Basic Search and Find", set())
        assert result.matched is True
        assert result.country_scoped is True
        assert result.error is not None
        assert "country" in result.error.lower()

    @pytest.mark.asyncio
    async def test_unknown_roles_detected(self, settings):
        """Roles in the matrix but not in Reltio's valid set are flagged."""
        valid_roles = {"ROLE_API_EXPORT"}
        result = await resolve(settings, "Consent consumer (GCCP)", valid_roles)
        assert "ROLE_LILLY_EPH_GLOBAL_GCCP" in result.unknown_roles
        assert "ROLE_API_EXPORT" in result.reltio_roles
        assert result.grantable is True  # still grantable: valid roles remain  # unknown_roles present
