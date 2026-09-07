from pathlib import Path

from ticketpilot.db import find_migrations_dir
from ticketpilot.paths import find_ancestor_path


def test_find_migrations_dir_uses_nearest_ancestor(tmp_path: Path) -> None:
    migrations_dir = tmp_path / "app" / "migrations" / "ticketpilot"
    migrations_dir.mkdir(parents=True)
    module_path = tmp_path / "app" / "ticketpilot" / "db.py"

    assert find_migrations_dir(module_path) == migrations_dir


def test_find_ancestor_path_supports_flat_container_layout(tmp_path: Path) -> None:
    resource_path = tmp_path / "app" / "data" / "ticketpilot" / "manifest.json"
    resource_path.parent.mkdir(parents=True)
    resource_path.touch()
    module_path = tmp_path / "app" / "ticketpilot" / "seed.py"

    assert find_ancestor_path(module_path, "data", "ticketpilot", "manifest.json") == resource_path
