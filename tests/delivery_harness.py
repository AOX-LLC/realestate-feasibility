"""Pieces shared by the delivery tests: settings, clients that never wait, and mock transports
that fail the way a network does (a reply lost after the service acted, a 5xx, a 429)."""

import threading
from collections.abc import Iterable
from typing import Any

from sqlalchemy import Engine, text
from test_api import _settings

from feasibility.config import Settings
from feasibility.delivery.errors import TransportError
from feasibility.delivery.notion import MockNotionTransport, NotionClient
from feasibility.delivery.slack import MockSlackTransport, SlackClient
from feasibility.delivery.transport import Pacer, RecordedRequest, TransportResponse

DATABASE_ID = "0" * 32
CHANNEL_ID = "C0MOCKCHAN"
NO_WAIT = Pacer(0.0)


def settings(**changes: Any) -> Settings:
    """The test settings (mock data, mock delivery, both targets), with `changes` applied."""
    return _settings().model_copy(update=changes)


def notion_client(transport: Any = None, slept: list[float] | None = None) -> NotionClient:
    pauses = slept if slept is not None else []
    return NotionClient(
        transport or MockNotionTransport(), DATABASE_ID, pacer=NO_WAIT, sleep=pauses.append
    )


def slack_client(transport: Any = None, slept: list[float] | None = None) -> SlackClient:
    pauses = slept if slept is not None else []
    return SlackClient(
        transport or MockSlackTransport(), CHANNEL_ID, pacer=NO_WAIT, sleep=pauses.append
    )


def deliver(
    engine: Engine,
    run_id: int,
    notion: Any = None,
    slack: Any = None,
    *,
    config: Settings | None = None,
    **options: Any,
) -> Any:
    """`deliver_brief` over the given transports, with no waiting between requests."""
    from feasibility.delivery.deliver import Clients, deliver_brief

    clients = Clients(notion=notion_client(notion), slack=slack_client(slack))
    return deliver_brief(engine, config or settings(), run_id, clients=clients, **options)


def ledger(engine: Engine, run_id: int | None = None) -> list[Any]:
    with engine.connect() as connection:
        if run_id is None:
            found = connection.execute(text("SELECT * FROM delivery ORDER BY id"))
        else:
            found = connection.execute(
                text("SELECT * FROM delivery WHERE run_id = :r ORDER BY id"), {"r": run_id}
            )
        return list(found.mappings())


def clear_ledger(engine: Engine) -> None:
    with engine.begin() as connection:
        connection.execute(text("DELETE FROM delivery"))


def status_of(report: Any, target: str, item: str) -> str:
    return next(r.status for r in report.items if (r.target, r.item) == (target, item))


def statuses(report: Any, target: str) -> set[str]:
    return {r.status for r in report.items if r.target == target}


def paths(transport: Any, *, only: Iterable[str] | None = None) -> list[str]:
    wanted = set(only) if only is not None else None
    return [r.path for r in transport.requests if wanted is None or r.path in wanted]


class Faults:
    """What a transport should do wrong, by path. `lose_reply` lets the service act and then loses
    the answer; `statuses` answers the next requests to a path with those statuses instead (a
    `None` lets that request through), and then the path behaves again."""

    def __init__(
        self,
        *,
        lose_reply: Iterable[str] = (),
        statuses: dict[str, list[int | None]] | None = None,
    ) -> None:
        self.lose_reply = set(lose_reply)
        self.statuses = {path: list(codes) for path, codes in (statuses or {}).items()}

    def answer(self, owner: Any, method: str, path: str) -> TransportResponse | None:
        """A planned status for this request, or None to let it through."""
        queue = self.statuses.get(path)
        if queue:
            status = queue.pop(0)
            if status is None:
                return None
            owner.requests.append(RecordedRequest(method, path, None))
            return TransportResponse(status, None, {"Retry-After": "0"})
        return None


class LossyNotion(MockNotionTransport):
    def __init__(self, faults: Faults | None = None, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.faults = faults or Faults()

    def request(
        self,
        method: str,
        path: str,
        *,
        json: dict[str, Any] | None = None,
        data: dict[str, Any] | None = None,
    ) -> TransportResponse:
        planned = self.faults.answer(self, method, path)
        if planned is not None:
            return planned
        response = super().request(method, path, json=json, data=data)
        if path in self.faults.lose_reply:
            raise TransportError("network_error")
        return response


class LossySlack(MockSlackTransport):
    def __init__(self, faults: Faults | None = None, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.faults = faults or Faults()

    def request(
        self,
        method: str,
        path: str,
        *,
        json: dict[str, Any] | None = None,
        data: dict[str, Any] | None = None,
    ) -> TransportResponse:
        planned = self.faults.answer(self, method, path)
        if planned is not None:
            return planned
        response = super().request(method, path, json=json, data=data)
        if path in self.faults.lose_reply:
            raise TransportError("network_error")
        return response

    def upload(self, url: str, content: bytes) -> TransportResponse:
        response = super().upload(url, content)
        if "upload" in self.faults.lose_reply:
            raise TransportError("network_error")
        return response


class GateSlack(MockSlackTransport):
    """Holds the first `chat.postMessage` open until the test lets it go, so a second delivery
    can be started while the first is in the middle of its call."""

    def __init__(self) -> None:
        super().__init__()
        self.entered = threading.Event()
        self.release = threading.Event()

    def request(
        self,
        method: str,
        path: str,
        *,
        json: dict[str, Any] | None = None,
        data: dict[str, Any] | None = None,
    ) -> TransportResponse:
        if path == "/chat.postMessage":
            self.entered.set()
            assert self.release.wait(30), "the test never released the post"
        return super().request(method, path, json=json, data=data)


def run_two_days(engine: Engine) -> tuple[Any, Any]:
    """The snapshot's day one and day two, run with a model that quotes remarks (so there are
    signals and accepted narratives to deliver). Empties the database first."""
    from brief_support import DAY_ONE, DAY_TWO, quoting_model
    from conftest import empty_database

    from feasibility.snapshot.load import seed
    from feasibility.sourcing.run import run_sourcing

    empty_database(engine)
    seed(engine, settings())
    model = quoting_model()
    one = run_sourcing(engine, settings(), "dallas", DAY_ONE, model=model)
    two = run_sourcing(engine, settings(), "dallas", DAY_TWO, model=model)
    return one, two
