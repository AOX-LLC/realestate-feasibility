"""Diffs RentCast's published OpenAPI field names against the response models.

    uv run python scripts/check_rentcast_docs.py

Run by hand. It needs the network but no API key and spends no budget. Exits 1 when the
docs and the models disagree, 0 when they match. Personal fields (listingAgent,
listingOffice, owner) are intentionally undeclared and ignored.
"""

import re
import sys
import types
from typing import Any, Union, get_args, get_origin

import httpx
from pydantic import BaseModel

from feasibility.sources.rentcast.models import PropertyRecord, SaleListing, ValueEstimate
from feasibility.sources.rentcast.scrub import INTENTIONALLY_UNDECLARED

SPEC_URL = "https://developers.rentcast.io/openapi/rentcast-api.json"
ENDPOINTS: tuple[tuple[str, type[BaseModel], bool], ...] = (
    ("/listings/sale", SaleListing, True),
    ("/listings/sale/{id}", SaleListing, False),
    ("/properties", PropertyRecord, True),
    ("/properties/{id}", PropertyRecord, False),
    ("/avm/value", ValueEstimate, False),
)
MAX_REF_DEPTH = 8
# The docs describe a keyed map (history by date, assessments by year) as an object whose
# properties are example keys; such an object is treated as a map ("*").
EXAMPLE_KEY = re.compile(r"^\d{4}(-\d{2}-\d{2})?$")


def resolve(spec: dict[str, Any], schema: dict[str, Any]) -> dict[str, Any]:
    for _ in range(MAX_REF_DEPTH):
        reference = schema.get("$ref")
        if reference is None:
            return schema
        node: Any = spec
        for part in reference.removeprefix("#/").split("/"):
            node = node[part]
        schema = node
    return schema


def walk_schema(spec: dict[str, Any], schema: dict[str, Any], prefix: str = "") -> set[str]:
    schema = resolve(spec, schema)
    paths: set[str] = set()
    if schema.get("type") == "array" or "items" in schema:
        item_prefix = f"{prefix}[]"
        paths.add(item_prefix)
        return paths | walk_schema(spec, schema.get("items", {}), item_prefix)
    properties = schema.get("properties")
    if properties and all(EXAMPLE_KEY.match(name) for name in properties):
        path = f"{prefix}.*" if prefix else "*"
        return {path} | walk_schema(spec, next(iter(properties.values())), path)
    if properties:
        for name, child in properties.items():
            if name in INTENTIONALLY_UNDECLARED:
                continue
            path = f"{prefix}.{name}" if prefix else name
            paths.add(path)
            paths |= walk_schema(spec, child, path)
    elif isinstance(schema.get("additionalProperties"), dict):
        path = f"{prefix}.*" if prefix else "*"
        paths.add(path)
        paths |= walk_schema(spec, schema["additionalProperties"], path)
    return paths


def _model_in(annotation: Any) -> Any:
    """Strip Optional / unions down to the one type that carries structure."""
    if get_origin(annotation) in (Union, types.UnionType):
        options = [arg for arg in get_args(annotation) if arg is not type(None)]
        return _model_in(options[0]) if options else annotation
    return annotation


def walk_model(model: type[BaseModel], prefix: str = "") -> set[str]:
    paths: set[str] = set()
    for name, info in model.model_fields.items():
        field_name = info.alias or name
        if field_name in INTENTIONALLY_UNDECLARED:
            continue
        path = f"{prefix}.{field_name}" if prefix else field_name
        paths.add(path)
        paths |= walk_annotation(_model_in(info.annotation), path)
    return paths


def walk_annotation(annotation: Any, path: str) -> set[str]:
    origin = get_origin(annotation)
    if origin is list:
        item_path = f"{path}[]"
        return {item_path} | walk_annotation(_model_in(get_args(annotation)[0]), item_path)
    if origin is dict:
        value_path = f"{path}.*"
        return {value_path} | walk_annotation(_model_in(get_args(annotation)[1]), value_path)
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        return walk_model(annotation, path)
    return set()


def response_schema(spec: dict[str, Any], path: str) -> dict[str, Any]:
    response = spec["paths"][path]["get"]["responses"]["200"]
    schema: dict[str, Any] = response["content"]["application/json"]["schema"]
    return schema


def main() -> int:
    spec = httpx.get(SPEC_URL, timeout=30.0).raise_for_status().json()
    differing = False
    for path, model, is_list in ENDPOINTS:
        documented = walk_schema(spec, response_schema(spec, path))
        declared = walk_model(model)
        if is_list:
            declared = {"[]"} | {f"[].{field}" if field else field for field in declared}
            declared = {item.replace("[].[]", "[]") for item in declared}
        only_docs = sorted(documented - declared)
        only_models = sorted(declared - documented)
        print(f"{path} -> {model.__name__}")
        for label, fields in (("in the docs only", only_docs), ("in the models only", only_models)):
            for field in fields:
                print(f"  {label}: {field}")
        differing = differing or bool(only_docs or only_models)
    if differing:
        return 1
    print("OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
