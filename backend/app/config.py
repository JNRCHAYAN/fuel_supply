"""Runtime configuration for the platform (CONTRACT.md section 4).

Every field here has a row in the contract's configuration table and a matching,
commented entry in ``backend/.env.example``. Settings are read from the process
environment and from a ``.env`` file; ``backend/.env`` is git-ignored and is the
file the operator fills in.

Security note (brief section 18 / CONTRACT section 0.2): ``DEEPSEEK_API_KEY``
has an empty default and is declared ``repr=False``, so it cannot leak through a
``repr(settings)`` in a log line. Nothing in this module ever logs a value.
"""

from __future__ import annotations

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Process configuration, validated by pydantic-settings.

    Unknown environment keys are ignored (``extra="ignore"``) so that a stray
    variable in a shell cannot stop the service from booting.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # --- simulator gateway (CONTRACT section 5) ---------------------------
    # Root of the published BUP simulator. In docker compose this is the
    # service name; for a local simulator use http://localhost:8001.
    simulator_base_url: str = "http://simulator-api:8000"
    # Per-request timeout for every httpx call to the simulator.
    simulator_timeout_seconds: float = 10.0
    # Tenacity attempts on 5xx / transport errors only (never on 4xx).
    simulator_max_retries: int = 3
    # Consecutive failures before the circuit breaker opens.
    circuit_failure_threshold: int = 5
    # How long the breaker stays open before a single half-open trial.
    circuit_reset_seconds: float = 30.0
    # How long a successful read is reused before the simulator is asked again,
    # and the window inside which concurrent callers share one in-flight fetch.
    #
    # This is a load guard, not an optimisation. The published simulator's
    # database pool holds fifteen connections; a request reads eight endpoints,
    # so roughly two concurrent operators exhaust it. Measured: at fifty
    # operators the pool timed out, the container stopped answering and had to
    # be restarted. The simulator is not ours to modify, so the ceiling has to
    # be respected on this side. One second is ~8 simulator ticks at the
    # default speed -- recent enough for an operator console, old enough to
    # collapse a burst.
    simulator_cache_ttl_seconds: float = 1.0

    # --- storage (CONTRACT section 6) -------------------------------------
    # SQLAlchemy async URL. SQLite by default so there is zero infrastructure
    # to stand up; swappable to Postgres by URL alone.
    database_url: str = "sqlite+aiosqlite:///./data/fuel.db"

    # --- LLM / DeepSeek (CONTRACT section 8) ------------------------------
    # SECRET. Empty by default — there is deliberately no plausible-looking
    # default here. When empty, llm_available is False and every LLM path
    # takes its deterministic fallback (brief section 11).
    # repr=False keeps it out of repr(settings); it is never logged, never
    # returned by an endpoint and never used as a metric label.
    deepseek_api_key: str = Field(default="", repr=False)
    # DeepSeek is OpenAI-compatible; this is the API root, not the full path.
    deepseek_base_url: str = "https://api.deepseek.com"
    deepseek_model: str = "deepseek-chat"
    deepseek_timeout_seconds: float = 30.0
    # Set false to force the deterministic fallback path (used by tests and by
    # the resilience demo).
    llm_enabled: bool = True

    # --- API --------------------------------------------------------------
    api_port: int = 9000
    log_level: str = "INFO"
    # Comma-separated list of origins the console may be served from. The
    # backend is the single gateway, so CORS is only needed for a browser
    # console outside the compose network.
    cors_allow_origins: str = "http://localhost:8080,http://localhost:5173"

    # --- stockout / shortage intelligence (CONTRACT section 7.4) ----------
    # These four feed ``RiskThresholds`` in ``app.intelligence.stockout``. They
    # are settings rather than constants because the right band depends on the
    # network being watched: a dense city grid tolerates far less warning than a
    # long rural haul, and the console reads the same either way.
    #
    # How many simulated ticks ahead the inventory projection runs. A tick is
    # fifteen simulated minutes, so the default is two simulated hours -- long
    # enough for a depot-to-station run to land inside the window, short enough
    # that the demand forecast is still shaped by observed history rather than
    # by the flat tail a long horizon decays into.
    stockout_horizon_ticks: int = 8
    # Ticks-to-stockout at or below which risk is CRITICAL. One tick or less
    # means the station is dry now or dry before the next tick, so no
    # reallocation can reach it in time; CRITICAL is therefore reserved for
    # "act on the next delivery, not this one". A gentler bound would let a
    # station that is already dry read as merely HIGH.
    stockout_critical_ticks: float = 1.0
    # Ticks-to-stockout at or below which risk is HIGH. Three ticks is under an
    # hour of simulated time: still actionable, but only if the operator acts on
    # this reading rather than the next one.
    stockout_high_ticks: float = 3.0
    # Safety stock as a fraction of the horizon's expected demand. Below it, and
    # with no stockout projected inside the horizon, risk is MEDIUM: the station
    # is not going dry, but it has no cushion left, so the next demand spike or
    # missed delivery would take it there. A quarter of the horizon's demand is
    # the smallest buffer that still absorbs one ordinary bad day.
    stockout_safety_stock_fraction: float = 0.25

    @property
    def llm_available(self) -> bool:
        """True only when the LLM is enabled *and* a key is actually present.

        An empty or whitespace-only key means the deterministic fallback path,
        which is a supported degraded mode rather than an error.
        """
        return bool(self.llm_enabled and self.deepseek_api_key.strip())

    @property
    def cors_origins(self) -> list[str]:
        """``cors_allow_origins`` split into a clean list of origins."""
        return [origin.strip() for origin in self.cors_allow_origins.split(",") if origin.strip()]


@lru_cache
def get_settings() -> Settings:
    """Return the process-wide settings singleton.

    Cached with :func:`functools.lru_cache` so the environment is parsed once.
    Tests that need a different configuration construct ``Settings(...)``
    directly, or call ``get_settings.cache_clear()``.
    """
    return Settings()
