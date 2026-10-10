"""The delivery settings: what live delivery needs, what mock drops, and what an error may say."""

import pytest
from pydantic import SecretStr, ValidationError

from feasibility.config import DataMode, DeliveryMode, Settings

SLACK = "xoxb-not-a-real-credential"  # no digits run of the shape the secret scanner looks for
NOTION = "ntn_" + "n" * 40
DATABASE = "a1b2c3d4e5f60718293a4b5c6d7e8f90"
CHANNEL = "C0123ABCD45"


def make(**values: object) -> Settings:
    return Settings(_env_file=None, **values)  # type: ignore[call-arg]


def live(**overrides: object) -> Settings:
    values: dict[str, object] = {
        "delivery_mode": "live",
        "notion_token": NOTION,
        "notion_database_id": DATABASE,
        "slack_bot_token": SLACK,
        "slack_channel_id": CHANNEL,
    }
    values.update(overrides)
    return make(**values)


def test_the_defaults_deliver_nothing_anywhere() -> None:
    settings = make()

    assert settings.delivery_mode is DeliveryMode.MOCK
    assert settings.targets == ["notion", "slack"]
    assert settings.notion_token is None and settings.slack_bot_token is None


def test_live_with_both_targets_needs_all_four_values_and_names_the_missing_ones() -> None:
    with pytest.raises(ValidationError) as raised:
        make(delivery_mode="live")

    message = str(raised.value)
    for variable in ("NOTION_TOKEN", "NOTION_DATABASE_ID", "SLACK_BOT_TOKEN", "SLACK_CHANNEL_ID"):
        assert variable in message


def test_live_with_one_target_needs_only_that_targets_values() -> None:
    settings = make(
        delivery_mode="live",
        delivery_targets="slack",
        slack_bot_token=SLACK,
        slack_channel_id=CHANNEL,
    )

    assert settings.targets == ["slack"]
    with pytest.raises(ValidationError, match="NOTION_TOKEN"):
        make(delivery_mode="live", delivery_targets="notion", notion_database_id=DATABASE)


def test_live_refuses_a_token_for_a_service_that_is_not_a_target_and_names_only_the_variable() -> (
    None
):
    with pytest.raises(ValidationError) as raised:
        live(delivery_targets="slack")

    assert "NOTION_TOKEN" in str(raised.value)
    assert NOTION not in str(raised.value)
    assert live(delivery_targets="slack", notion_token=None, notion_database_id=None).targets == [
        "slack"
    ]


def test_mock_delivery_drops_the_tokens_but_redaction_still_covers_them() -> None:
    settings = make(notion_token=NOTION, slack_bot_token=SLACK)

    assert settings.notion_token is None and settings.slack_bot_token is None
    assert {NOTION, SLACK} <= set(settings.secret_values())


def test_live_keeps_the_tokens_in_the_secret_values_and_out_of_the_repr() -> None:
    settings = live()

    assert {NOTION, SLACK} <= set(settings.secret_values())
    for secret in (NOTION, SLACK):
        assert secret not in repr(settings)
        assert secret not in settings.model_dump_json()


@pytest.mark.parametrize(
    ("values", "variable"),
    [
        ({"slack_bot_token": "xoxp-" + "p" * 30}, "SLACK_BOT_TOKEN"),
        ({"slack_bot_token": "short"}, "SLACK_BOT_TOKEN"),
        ({"notion_token": "bad token with spaces"}, "NOTION_TOKEN"),
        ({"notion_database_id": "not-a-database-id"}, "NOTION_DATABASE_ID"),
        ({"slack_channel_id": "general"}, "SLACK_CHANNEL_ID"),
    ],
)
def test_a_bad_value_is_refused_by_variable_name_and_never_by_value(
    values: dict[str, object], variable: str
) -> None:
    with pytest.raises(ValidationError) as raised:
        live(**values)

    assert variable in str(raised.value)
    for value in values.values():
        assert str(value) not in str(raised.value)


def test_an_unknown_target_is_refused() -> None:
    with pytest.raises(ValidationError, match="DELIVERY_TARGETS"):
        make(delivery_targets="notion,email")


def test_a_database_id_may_have_dashes_and_is_stored_compact() -> None:
    dashed = "a1b2c3d4-e5f6-0718-293a-4b5c6d7e8f90"

    assert live(notion_database_id=dashed).notion_database_id == DATABASE


@pytest.mark.parametrize("data", [DataMode.MOCK, DataMode.LIVE])
def test_the_data_mode_and_the_delivery_mode_combine_freely(data: DataMode) -> None:
    values: dict[str, object] = {"data_mode": data}
    if data is DataMode.LIVE:
        values |= {"rentcast_api_key": SecretStr("rentcast-" + "k" * 20)}

    assert live(**values).delivery_mode is DeliveryMode.LIVE
    assert make(**values).delivery_mode is DeliveryMode.MOCK


def test_blank_values_are_unset() -> None:
    settings = make(notion_token="", notion_database_id="  ", slack_channel_id="")

    assert settings.notion_database_id is None and settings.slack_channel_id is None
