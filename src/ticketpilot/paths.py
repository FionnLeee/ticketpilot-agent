from pathlib import Path


def find_ancestor_path(module_path: Path, *relative_parts: str) -> Path:
    for parent in module_path.resolve().parents:
        candidate = parent.joinpath(*relative_parts)
        if candidate.exists():
            return candidate
    return module_path.resolve().parent.joinpath(*relative_parts)
