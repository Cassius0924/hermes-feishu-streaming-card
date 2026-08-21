from __future__ import annotations

import pytest
import yaml

from hermes_feishu_card.config import card_mentions_enabled, load_config


CONFIG_ENV_VARS = (
    "HERMES_FEISHU_CARD_HOST",
    "HERMES_FEISHU_CARD_PORT",
    "HERMES_FEISHU_CARD_ALLOW_NON_LOOPBACK",
    "HERMES_FEISHU_CARD_SERVICE_MANAGER",
    "HERMES_FEISHU_CARD_INTEGRITY_MODE",
    "FEISHU_APP_ID",
    "FEISHU_APP_SECRET",
)


@pytest.fixture(autouse=True)
def clear_config_env(monkeypatch):
    for name in CONFIG_ENV_VARS:
        monkeypatch.delenv(name, raising=False)


def test_load_config_defaults_mentions_in_cards_to_enabled(tmp_path):
    config = load_config(tmp_path / "missing.yaml")

    assert config["card"]["mentions_in_cards"] is True


def test_load_config_accepts_mentions_in_cards_false(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump({"card": {"mentions_in_cards": False}}))

    config = load_config(path)

    assert config["card"]["mentions_in_cards"] is False


def test_load_config_normalizes_string_mentions_in_cards(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump({"card": {"mentions_in_cards": "false"}}))

    config = load_config(path)

    assert config["card"]["mentions_in_cards"] is False


def test_load_config_rejects_invalid_mentions_in_cards_with_exact_path(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump({"card": {"mentions_in_cards": "maybe"}}))

    with pytest.raises(ValueError, match=r"card\.mentions_in_cards"):
        load_config(path)


def test_load_config_normalizes_profile_and_bot_mentions_in_cards(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "profiles": {"work": {"card": {"mentions_in_cards": False}}},
                "bots": {
                    "default": "default",
                    "items": {"bot-a": {"card": {"mentions_in_cards": "false"}}},
                },
            }
        )
    )

    config = load_config(path)

    assert config["profiles"]["work"]["card"]["mentions_in_cards"] is False
    assert config["bots"]["items"]["bot-a"]["card"]["mentions_in_cards"] is False


def test_card_mentions_enabled_defaults_true_for_missing_or_malformed():
    assert card_mentions_enabled(None) is True
    assert card_mentions_enabled({}) is True
    assert card_mentions_enabled({"mentions_in_cards": None}) is True
    assert card_mentions_enabled({"mentions_in_cards": "maybe"}) is True
    assert card_mentions_enabled({"mentions_in_cards": 5}) is True


def test_card_mentions_enabled_reads_explicit_boolean():
    assert card_mentions_enabled({"mentions_in_cards": True}) is True
    assert card_mentions_enabled({"mentions_in_cards": False}) is False


def test_card_mentions_enabled_parses_string_values():
    assert card_mentions_enabled({"mentions_in_cards": "true"}) is True
    assert card_mentions_enabled({"mentions_in_cards": "false"}) is False
    assert card_mentions_enabled({"mentions_in_cards": "off"}) is False
    assert card_mentions_enabled({"mentions_in_cards": "yes"}) is True
