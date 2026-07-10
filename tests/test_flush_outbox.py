import importlib.util
from pathlib import Path


def load_flush_outbox():
    script_path = Path(__file__).resolve().parents[1] / "forge" / "scripts" / "flush-outbox.py"
    spec = importlib.util.spec_from_file_location("flush_outbox", script_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


flush_outbox = load_flush_outbox()


def test_parse_entry_parses_aspect_project_and_tags():
    text = """## [decision] MEMEX delivery format
project:: TEST_PROJECT
tags:: memex,outbox,parser
recorded_at:: 2026-07-10T00:00:00Z

The entry body is delivered as-is.
"""

    assert flush_outbox.parse_entry(text) == (
        "decision",
        "TEST_PROJECT",
        ["memex", "outbox", "parser"],
    )


def test_parse_entry_uses_defaults_when_fields_are_absent():
    text = """# Plain note

No MEMEX metadata fields are present here.
"""

    assert flush_outbox.parse_entry(text) == (None, "INFINITY_FORGE", None)


def test_parse_entry_splits_tags_on_commas_and_whitespace():
    text = """## [insight] Tag splitting
tags:: alpha, beta gamma,delta   epsilon
"""

    assert flush_outbox.parse_entry(text) == (
        "insight",
        "INFINITY_FORGE",
        ["alpha", "beta", "gamma", "delta", "epsilon"],
    )
