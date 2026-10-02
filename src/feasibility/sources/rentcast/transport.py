"""How a RentCast request reaches an answer: over HTTP (live) or from the committed
synthetic snapshot (mock). Both are keyed by the same canonical request, so the client
above them is identical in both modes."""

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

import httpx
from pydantic import SecretStr

BASE_URL = "https://api.rentcast.io/v1"
TIMEOUT = httpx.Timeout(20.0, connect=5.0)


@dataclass(frozen=True)
class TransportResponse:
    status_code: int
    body: Any


class TransportError(Exception):
    def __init__(self, path: str) -> None:
        super().__init__(path)
        self.path = path


class ConnectFailedError(TransportError):
    """The request never reached RentCast, so it cannot have been billed."""


class NoResponseError(TransportError):
    """The request was sent but no response arrived; whether it was billed is unknown."""


class Transport(Protocol):
    def get(self, path: str, params: Mapping[str, str]) -> TransportResponse: ...

    def close(self) -> None: ...


def canonical_params(params: Mapping[str, str | int | None]) -> dict[str, str]:
    """Sorted, stripped, string-valued; None values dropped."""
    return {key: str(value).strip() for key, value in sorted(params.items()) if value is not None}


def request_key(path: str, params: Mapping[str, str]) -> str:
    """sha256 of the endpoint path and its canonical query parameters. The API key travels
    in a header and is never part of this."""
    canonical = json.dumps({"path": path, "params": params}, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


class HttpTransport:
    def __init__(self, api_key: SecretStr, base_url: str = BASE_URL) -> None:
        self._client = httpx.Client(
            base_url=base_url,
            # The only place the key's value is read.
            headers={"X-Api-Key": api_key.get_secret_value(), "Accept": "application/json"},
            timeout=TIMEOUT,
            follow_redirects=False,
        )

    def get(self, path: str, params: Mapping[str, str]) -> TransportResponse:
        failure: type[TransportError] | None = None
        try:
            response = self._client.get(path, params=params)
        except (httpx.ConnectError, httpx.ConnectTimeout):
            failure = ConnectFailedError
        except httpx.HTTPError:
            failure = NoResponseError
        if failure is not None:
            # Raised outside the except block so neither __cause__ nor __context__ holds
            # the httpx error, whose request carries the key header.
            raise failure(path)
        return TransportResponse(response.status_code, _json_or_none(response))

    def close(self) -> None:
        self._client.close()


def _json_or_none(response: httpx.Response) -> Any:
    if not response.content:
        return None
    try:
        return response.json()
    except ValueError:
        return None


class SnapshotTransport:
    """Serves data/snapshot/rentcast/<request key>.json. A request the snapshot does not
    hold answers 404, which RentCast uses for "no records matched"."""

    def __init__(self, directory: Path) -> None:
        self._directory = directory

    def get(self, path: str, params: Mapping[str, str]) -> TransportResponse:
        snapshot_file = self._directory / f"{request_key(path, params)}.json"
        if not snapshot_file.is_file():
            return TransportResponse(404, {"status": 404, "error": "snapshot/not-found"})
        recorded = json.loads(snapshot_file.read_text(encoding="utf-8"))
        return TransportResponse(int(recorded["status"]), recorded["body"])

    def close(self) -> None:
        """Nothing to release."""
