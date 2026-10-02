import pytest

from feasibility.sourcing.keys import (
    full_key,
    normalize_unit,
    parcel_street_key,
    parse_listing_street,
    property_key,
    stem_key,
)


def _parsed(line: str, unit: str | None = None) -> tuple[str, str, str | None]:
    result = parse_listing_street(line, unit)
    assert result is not None, line
    key, parsed_unit = result
    return full_key(key), stem_key(key), parsed_unit


def test_a_plain_address_has_a_full_key_and_a_stem_key() -> None:
    assert _parsed("2737 THISTLEWANE TRL") == (
        "2737||THISTLEWANE TRL",
        "2737||THISTLEWANE",
        None,
    )


def test_a_half_number_is_its_own_part_of_the_key() -> None:
    result = parse_listing_street("123 1/2 MAIN ST", None)

    assert result is not None
    assert (result[0].number, result[0].half) == ("123", "1/2")


def test_spelled_out_directionals_and_suffixes_match_the_abbreviations() -> None:
    assert _parsed("5521 S WEXCOMBE AVE")[0] == "5521||S WEXCOMBE AVE"
    assert _parsed("5521 South Wexcombe Avenue")[0] == "5521||S WEXCOMBE AVE"


@pytest.mark.parametrize("line", ["22 OAK TRAIL", "22 OAK TR", "22 OAK TRL", "22 Oak Trails"])
def test_suffix_aliases_give_one_key(line: str) -> None:
    assert _parsed(line)[0] == "22||OAK TRL"


def test_a_trailing_directional_keeps_the_suffix_and_the_directional() -> None:
    assert _parsed("900 ELM ST N")[:2] == ("900||ELM ST N", "900||ELM N")


def test_only_the_suffix_position_is_aliased() -> None:
    assert _parsed("410 ST MARYS DR")[0] == "410||ST MARYS DR"


def test_a_name_with_no_suffix_is_its_own_stem() -> None:
    assert _parsed("7 BROADWAY")[:2] == ("7||BROADWAY", "7||BROADWAY")


@pytest.mark.parametrize(
    ("line", "unit", "expected"),
    [
        ("12 PINE ST #4B", None, "4B"),
        ("12 PINE ST # 4B", None, "4B"),
        ("12 PINE ST UNIT 4B", None, "4B"),
        ("12 PINE ST APT 4B", None, "4B"),
        ("12 PINE ST STE 4B", None, "4B"),
        ("12 PINE ST", "Unit 4B", "4B"),
        ("12 PINE ST", None, None),
    ],
)
def test_the_unit_is_peeled_off_or_taken_as_given(
    line: str, unit: str | None, expected: str | None
) -> None:
    key, _, parsed_unit = _parsed(line, unit)

    assert key == "12||PINE ST"
    assert parsed_unit == expected


def test_normalize_unit_drops_markers_and_blank_is_none() -> None:
    assert normalize_unit("Unit 102") == normalize_unit("#102") == "102"
    assert normalize_unit("  ") is None
    assert normalize_unit(None) is None


@pytest.mark.parametrize("line", ["PO BOX 5", "", "MAIN ST"])
def test_a_line_without_a_street_number_does_not_parse(line: str) -> None:
    assert parse_listing_street(line, None) is None


def test_parcel_keys_use_the_same_normalization_as_listings() -> None:
    key = parcel_street_key("4120", "1/2", "BRINDLECOMBE ST")

    assert key is not None
    assert key.half == "1/2"
    assert full_key(key) == "4120|1/2|BRINDLECOMBE ST"
    parcel_key = parcel_street_key(" 5521 ", None, "S WEXCOMBE AVE")
    listing_parsed = parse_listing_street("5521 South Wexcombe Avenue", None)
    assert listing_parsed is not None
    assert parcel_key == listing_parsed[0]


@pytest.mark.parametrize(
    ("number", "name"), [(None, "MAIN ST"), ("", "MAIN ST"), ("1", None), ("1", "  ")]
)
def test_a_parcel_without_a_number_or_name_has_no_key(number: str | None, name: str | None) -> None:
    assert parcel_street_key(number, None, name) is None


def test_property_key_forms() -> None:
    key = parcel_street_key("2200", None, "KESTRELWYN DR")
    assert key is not None

    assert property_key("A1", "G1", "gis_group", "75218", key, None) == "gis:G1"
    assert property_key("A1", "G1", "exact", "75218", key, None) == "acct:A1"
    assert property_key(None, None, None, "75218", key, None) == "addr:75218:2200||KESTRELWYN DR"
    assert (
        property_key(None, None, None, "75218", key, "102") == "addr:75218:2200||KESTRELWYN DR:102"
    )
