"""Tests for ``app/config.py`` and ``backend/.env.example`` (owner: A1).

CONTRACT.md section 4 pins every variable, its default, and the rule that
``DEEPSEEK_API_KEY`` "has no default that is a real key". These tests cover:

* every documented default is present and correct;
* ``llm_available`` is False with no key, False with a blank key, False when
  ``LLM_ENABLED`` is false even with a key, and True only when both hold;
* a real key never survives into a ``repr(settings)``;
* ``.env.example`` documents every variable and contains no secret-looking
  value.

The default-value tests run as ``Settings(_env_file=None)`` with the real
environment variables cleared, so they assert the *code's* defaults and cannot
be broken — or, worse, accidentally satisfied — by whatever is in the operator's
shell or ``backend/.env``.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from app.config import Settings, get_settings

#: backend/ — this file lives in backend/tests/.
BACKEND_DIR = Path(__file__).resolve().parents[1]
ENV_EXAMPLE = BACKEND_DIR / ".env.example"

#: Every variable in CONTRACT.md section 4, as the exact environment name.
DOCUMENTED_VARIABLES = (
    "SIMULATOR_BASE_URL",
    "SIMULATOR_TIMEOUT_SECONDS",
    "SIMULATOR_MAX_RETRIES",
    "CIRCUIT_FAILURE_THRESHOLD",
    "CIRCUIT_RESET_SECONDS",
    "DATABASE_URL",
    "DEEPSEEK_API_KEY",
    "DEEPSEEK_BASE_URL",
    "DEEPSEEK_MODEL",
    "DEEPSEEK_TIMEOUT_SECONDS",
    "LLM_ENABLED",
    "API_PORT",
    "LOG_LEVEL",
    "CORS_ALLOW_ORIGINS",
)

#: A real DeepSeek key has this shape. Nothing resembling one may be committed.
SECRET_PATTERN = re.compile(r"sk-[A-Za-z0-9_\-]{8,}")

#: A test double, deliberately not a real credential and never sent anywhere.
FAKE_KEY = "sk-" + "test-only-not-a-real-credential-0000"


@pytest.fixture
def clean_env(monkeypatch: pytest.MonkeyPatch) -> pytest.MonkeyPatch:
    """Remove every documented variable from the environment for one test."""
    for name in DOCUMENTED_VARIABLES:
        monkeypatch.delenv(name, raising=False)
    return monkeypatch


def _defaults(**overrides: object) -> Settings:
    """Build Settings from the code's defaults only, ignoring any .env file."""
    return Settings(_env_file=None, **overrides)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------
@pytest.mark.usefixtures("clean_env")
class TestDefaults:
    def test_simulator_defaults(self) -> None:
        settings = _defaults()
        assert settings.simulator_base_url == "http://simulator-api:8000"
        assert settings.simulator_timeout_seconds == 10
        assert settings.simulator_max_retries == 3

    def test_circuit_defaults(self) -> None:
        settings = _defaults()
        assert settings.circuit_failure_threshold == 5
        assert settings.circuit_reset_seconds == 30

    def test_storage_default(self) -> None:
        assert _defaults().database_url == "sqlite+aiosqlite:///./data/fuel.db"

    def test_llm_defaults(self) -> None:
        settings = _defaults()
        assert settings.deepseek_api_key == ""
        assert settings.deepseek_base_url == "https://api.deepseek.com"
        assert settings.deepseek_model == "deepseek-chat"
        assert settings.deepseek_timeout_seconds == 30
        assert settings.llm_enabled is True

    def test_api_defaults(self) -> None:
        settings = _defaults()
        assert settings.api_port == 9000
        assert settings.log_level == "INFO"
        assert settings.cors_allow_origins == (
            "http://localhost:8080,http://localhost:5173"
        )

    def test_no_key_has_a_real_default(self) -> None:
        """The key's default must be empty — a real-looking default would be a leak."""
        assert _defaults().deepseek_api_key == ""

    def test_environment_overrides_defaults(
        self, clean_env: pytest.MonkeyPatch
    ) -> None:
        clean_env.setenv("SIMULATOR_BASE_URL", "http://localhost:8001")
        clean_env.setenv("API_PORT", "9555")
        settings = _defaults()
        assert settings.simulator_base_url == "http://localhost:8001"
        assert settings.api_port == 9555


# ---------------------------------------------------------------------------
# llm_available
# ---------------------------------------------------------------------------
@pytest.mark.usefixtures("clean_env")
class TestLlmAvailable:
    def test_false_with_no_key(self) -> None:
        assert _defaults().llm_available is False

    def test_false_with_empty_key_explicit(self) -> None:
        assert _defaults(deepseek_api_key="").llm_available is False

    def test_false_with_whitespace_only_key(self) -> None:
        assert _defaults(deepseek_api_key="   ").llm_available is False

    def test_false_when_disabled_even_with_key(self) -> None:
        settings = _defaults(deepseek_api_key=FAKE_KEY, llm_enabled=False)
        assert settings.llm_available is False

    def test_true_with_key_and_enabled(self) -> None:
        settings = _defaults(deepseek_api_key=FAKE_KEY)
        assert settings.llm_available is True

    def test_true_when_key_supplied_via_environment(
        self, clean_env: pytest.MonkeyPatch
    ) -> None:
        clean_env.setenv("DEEPSEEK_API_KEY", FAKE_KEY)
        settings = _defaults()
        assert settings.llm_available is True

    def test_unavailable_when_environment_key_is_blank(
        self, clean_env: pytest.MonkeyPatch
    ) -> None:
        """An exported-but-empty key must degrade, not crash."""
        clean_env.setenv("DEEPSEEK_API_KEY", "")
        assert _defaults().llm_available is False


# ---------------------------------------------------------------------------
# Secret hygiene
# ---------------------------------------------------------------------------
@pytest.mark.usefixtures("clean_env")
class TestSecretHygiene:
    def test_key_absent_from_repr(self) -> None:
        """repr(settings) must be safe to log (CONTRACT section 0.2)."""
        settings = _defaults(deepseek_api_key=FAKE_KEY, llm_enabled=True)
        assert FAKE_KEY not in repr(settings)
        assert FAKE_KEY not in str(settings)

    def test_key_field_is_declared_repr_false(self) -> None:
        """The field itself must opt out of repr — the runtime guarantee behind
        the test above. This is what stops the key riding into a log line when
        someone logs the settings object."""
        assert Settings.model_fields["deepseek_api_key"].repr is False


# ---------------------------------------------------------------------------
# get_settings caching
# ---------------------------------------------------------------------------
class TestGetSettings:
    def test_returns_settings_instance(self) -> None:
        assert isinstance(get_settings(), Settings)

    def test_is_cached(self) -> None:
        assert get_settings() is get_settings()

    def test_cache_can_be_cleared(self) -> None:
        first = get_settings()
        get_settings.cache_clear()
        try:
            assert get_settings() is not first
        finally:
            get_settings.cache_clear()


# ---------------------------------------------------------------------------
# cors_origins parsing
# ---------------------------------------------------------------------------
@pytest.mark.usefixtures("clean_env")
class TestCorsOrigins:
    def test_parsed_into_list(self) -> None:
        assert _defaults().cors_origins == [
            "http://localhost:8080",
            "http://localhost:5173",
        ]

    def test_whitespace_and_blanks_are_dropped(self) -> None:
        settings = _defaults(cors_allow_origins=" http://a.test , ,http://b.test ")
        assert settings.cors_origins == ["http://a.test", "http://b.test"]

    def test_single_origin(self) -> None:
        assert _defaults(cors_allow_origins="http://only.test").cors_origins == [
            "http://only.test"
        ]

    def test_empty_yields_empty_list(self) -> None:
        assert _defaults(cors_allow_origins="").cors_origins == []


# ---------------------------------------------------------------------------
# .env.example — the committed template
# ---------------------------------------------------------------------------
class TestEnvExample:
    @pytest.fixture(scope="class")
    def text(self) -> str:
        assert ENV_EXAMPLE.is_file(), f"{ENV_EXAMPLE} must exist (CONTRACT section 4)"
        return ENV_EXAMPLE.read_text(encoding="utf-8")

    def test_exists_and_is_not_empty(self, text: str) -> None:
        assert len(text.strip()) > 0

    @pytest.mark.parametrize("name", DOCUMENTED_VARIABLES)
    def test_documents_every_variable(self, text: str, name: str) -> None:
        """Brief section 18 / CONTRACT section 0.5: every variable is documented."""
        assert re.search(rf"^{name}=", text, flags=re.MULTILINE), (
            f"{name} is missing from .env.example"
        )

    @pytest.mark.parametrize("name", DOCUMENTED_VARIABLES)
    def test_every_variable_has_a_comment(self, text: str, name: str) -> None:
        """Each assignment is preceded by at least one comment line."""
        lines = text.splitlines()
        index = next(
            i
            for i, line in enumerate(lines)
            if line.startswith(f"{name}=")
        )
        preceding = [line for line in lines[:index] if line.strip()]
        assert preceding, f"{name} has nothing before it"
        # Walk back over the blank line to the comment block above the variable.
        cursor = index - 1
        while cursor >= 0 and not lines[cursor].strip():
            cursor -= 1
        assert cursor >= 0 and lines[cursor].lstrip().startswith("#"), (
            f"{name} is not preceded by a comment"
        )

    def test_contains_no_secret_looking_value(self, text: str) -> None:
        """The committed template must never carry a real credential."""
        matches = SECRET_PATTERN.findall(text)
        assert not matches, f".env.example contains secret-looking values: {matches}"

    def test_deepseek_key_line_is_empty(self, text: str) -> None:
        key_lines = [
            line for line in text.splitlines() if line.startswith("DEEPSEEK_API_KEY=")
        ]
        assert key_lines, "DEEPSEEK_API_KEY must be listed"
        for line in key_lines:
            assert line.strip() == "DEEPSEEK_API_KEY=", (
                "DEEPSEEK_API_KEY must ship with an empty value"
            )

    def test_no_assignment_carries_a_plausible_credential(self, text: str) -> None:
        """No assignment line in the template may carry a credential-shaped value."""
        for line in text.splitlines():
            if not line or line.lstrip().startswith("#") or "=" not in line:
                continue
            name, _, value = line.partition("=")
            if name.strip() == "DEEPSEEK_API_KEY":
                assert value.strip() == "", f"{name} must be empty in .env.example"
            assert not SECRET_PATTERN.search(value), (
                f"{name.strip()} carries a secret-looking value in .env.example"
            )

    def test_explains_that_backend_env_is_the_operators_file(self, text: str) -> None:
        assert "backend/.env" in text

    def test_states_that_backend_env_is_git_ignored(self, text: str) -> None:
        lowered = text.lower()
        assert "git-ignored" in lowered or "gitignore" in lowered

    def test_environment_names_are_the_documented_ones(self, text: str) -> None:
        """No undocumented variable may appear as an assignment."""
        assigned = {
            line.partition("=")[0].strip()
            for line in text.splitlines()
            if line
            and not line.lstrip().startswith("#")
            and "=" in line
            and re.fullmatch(r"[A-Z][A-Z0-9_]*", line.partition("=")[0].strip())
        }
        undocumented = assigned - set(DOCUMENTED_VARIABLES)
        assert not undocumented, f"undocumented variables in .env.example: {undocumented}"
