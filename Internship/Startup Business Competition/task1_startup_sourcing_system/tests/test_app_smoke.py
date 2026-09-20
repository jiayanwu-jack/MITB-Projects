from pathlib import Path

from streamlit.testing.v1 import AppTest


APP_PATH = Path(__file__).resolve().parents[1] / "app.py"


def test_sources_table_renders_and_filters_without_errors():
    app = AppTest.from_file(str(APP_PATH), default_timeout=30)
    app.run()

    assert not app.exception
    search = next(field for field in app.text_input if field.label == "Search table")
    search.set_value("SMU")
    app.run()

    assert not app.exception
    assert any("Showing" in caption.value for caption in app.caption)
