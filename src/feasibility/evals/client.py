"""The metered client an eval run uses, and how a run reacts to a call that cannot be made."""

from dataclasses import dataclass
from decimal import Decimal

from aox_agent_core import Mode
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


class RunEnd:
    """Remembers the case that ended a run, so that later cases are not tried."""

    def __init__(self) -> None:
        self.ended_by: str | None = None

    def refuse_if_ended(self) -> None:
        if self.ended_by is not None:
            raise EvalAbortedError(f"not tried: case {self.ended_by} ended the run")

    def mark(self, case_id: str) -> None:
        self.ended_by = case_id


@dataclass(frozen=True)
class EvalSession:
    """The client of one eval run and the mode it serves calls in (the scorecard records it)."""

    client: MeteredClient
    mode: Mode


def build_eval_client(settings: Settings, max_usd: Decimal) -> EvalSession:
    """A client for one eval session: spend capped at `max_usd` in process and, when calls cost
    money, in the ledger too (rows go to `llm_call` with stage `eval`, and the month's cap
    applies). A replay run needs no database."""
    inner = build_model_client(settings)
    mode = inner.config.mode
    if settings.llm_mode.is_billable:
        engine = get_engine()
        guard = SessionSpendGuard(
            max_usd, engine=engine, monthly_cap=settings.llm_monthly_budget_usd
        )
        return EvalSession(MeteredClient(inner, guard, engine, inner.config), mode)
    return EvalSession(MeteredClient(inner, SessionSpendGuard(max_usd), None, inner.config), mode)


def ended_message(case_id: str, error: str | None) -> str:
    """Why a run stopped, in words. A missing recording says so, and that nothing was called."""
    reason = error or "unknown error"
    if reason.startswith("ReplayMissError"):
        return (
            f"ReplayMissError on case {case_id}: no recording exists for it, so the eval stopped. "
            "Replay never falls back to a live call; recordings are made once, in record mode."
        )
    return f"the eval stopped at case {case_id}: {reason}"
