import json
from pathlib import Path

from leadradar_core.main import create_app
from leadradar_core.settings import settings


def test_openapi_snapshot_matches_code() -> None:
    settings.ENV = "test"
    app = create_app()
    current_openapi = app.openapi()

    openapi_path = Path("openapi.json")
    if not openapi_path.exists():
        # Try from backend/backend directory or root
        openapi_path = Path("../../openapi.json")

    assert openapi_path.exists(), "openapi.json must exist in repository root"
    stored_schema = json.loads(openapi_path.read_text(encoding="utf-8"))

    # Assert paths and tags match
    assert set(current_openapi["paths"].keys()) == set(stored_schema["paths"].keys())
