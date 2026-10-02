"""Load and validate market packs; look them up by market id."""

import tomllib
from functools import lru_cache
from pathlib import Path

from feasibility.markets.schema import MarketPack

PACKS_DIR = Path(__file__).parent / "packs"


class PackError(ValueError):
    """A pack file is missing, unreadable or invalid."""


def load_pack(path: Path) -> MarketPack:
    try:
        with path.open("rb") as pack_file:
            data = tomllib.load(pack_file)
    except (OSError, tomllib.TOMLDecodeError) as error:
        raise PackError(f"{path.name}: {error}") from error

    pack = MarketPack.model_validate(data)
    if pack.market.id != path.stem:
        raise PackError(f"{path.name}: market.id {pack.market.id!r} must match the file name")
    return pack


def pack_paths(directory: Path = PACKS_DIR) -> list[Path]:
    return sorted(directory.glob("*.toml"))


@lru_cache
def load_registry(directory: Path = PACKS_DIR) -> dict[str, MarketPack]:
    return {path.stem: load_pack(path) for path in pack_paths(directory)}


def get_pack(market_id: str) -> MarketPack:
    registry = load_registry()
    if market_id not in registry:
        raise PackError(f"no market pack {market_id!r}; available: {sorted(registry)}")
    return registry[market_id]
