from hermes_feishu_card.card_limits import (
    FEISHU_MAX_ELEMENTS,
    FEISHU_MAX_TABLES,
    SAFE_CARD_JSON_BYTES,
    inspect_card_limits,
    serialize_card_json,
)


def test_inspector_uses_exact_utf8_serialization_and_recursive_tag_counts():
    card = {
        "schema": "2.0",
        "header": {"title": {"tag": "plain_text", "content": "中文"}},
        "body": {
            "elements": [
                {
                    "tag": "markdown",
                    "content": "| A |\n| --- |\n| 一 |",
                },
                {
                    "tag": "column_set",
                    "columns": [
                        {
                            "tag": "column",
                            "elements": [{"tag": "markdown", "content": "nested"}],
                        }
                    ],
                },
            ]
        },
    }

    serialized = serialize_card_json(card)
    inspection = inspect_card_limits(card)

    assert "中文" in serialized
    assert "\\u4e2d" not in serialized
    assert inspection.json_bytes == len(serialized.encode("utf-8"))
    assert inspection.element_count == 5
    assert inspection.table_count == 1
    assert inspection.violations == ()
    assert inspection.safe is True


def test_inspector_reports_each_exact_limit_without_content_excerpts():
    card = {
        "body": {
            "elements": [
                *({"tag": "markdown", "content": "x"} for _ in range(FEISHU_MAX_ELEMENTS + 1)),
                {
                    "tag": "markdown",
                    "content": "\n\n".join(
                        "| H |\n| --- |\n| secret-value |"
                        for _ in range(FEISHU_MAX_TABLES + 1)
                    ),
                },
                {
                    "tag": "markdown",
                    "content": "密" * SAFE_CARD_JSON_BYTES,
                },
            ]
        }
    }

    inspection = inspect_card_limits(card)

    assert inspection.safe is False
    assert inspection.violations == ("json_bytes", "elements", "tables")
    assert "secret-value" not in repr(inspection)
