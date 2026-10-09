"""The metered client an eval run uses, and how a run reacts to a call that cannot be made."""

from decimal import Decimal

from aox_agent_core.errors import ConfigError, MissingCredentialsError, ProviderError, ReplayError

from feasibility.config import Settings
from feasibility.db import get_engine
from feasibility.llm.client import build_model_client
from feasibility.llm.errors import LlmBudgetError
from feasibility.llm.metered import MeteredClient
from feasibility.llm.spend import SessionSpendGuard

# Errors after which no later case in the run can succeed or should be tried: a missing
# recording, a provider that is failing, a cap that is spent, a broken configuration.
RUN_ENDING_ERRORS = (
    ReplayError,
    ProviderError,
    LlmBudgetError,
    ConfigError,
    MissingCredentialsError,
)


class EvalAbortedError(RuntimeError):
    """A case was not tried because an earlier case hit a run-ending error."""


def build_eval_client(settings: Settings, max_usd: Decimal) -> MeteredClient:
    """A client for one eval session: spend capped at `max_usd` in process and, when calls cost
    money, in the ledger too (rows go to `llm_call` with stage `eval`, and the month's cap
    applies). A replay run needs no database."""
    inner = build_model_client(settings)
    if settings.llm_mode.is_billable:
        guard = SessionSpendGuard(
            max_usd, engine=get_engine(), monthly_cap=settings.llm_monthly_budget_usd
        )
        return MeteredClient(inner, guard, get_engine(), inner.config)
    return MeteredClient(inner, SessionSpendGuard(max_usd), None, inner.config)
