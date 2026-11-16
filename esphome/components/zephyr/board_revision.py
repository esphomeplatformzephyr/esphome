"""Parse `board@revision` strings and resolve revisions from `board.yml` the way
Zephyr's `parse_board_components()` and `board_check_revision()` do."""

from dataclasses import dataclass
from pathlib import Path
import re

import yaml

_BOARD_RE = re.compile(r"^(?P<name>[^@/]+)(@(?P<revision>[^/]+))?(?P<qualifiers>/.*)?$")


@dataclass(frozen=True)
class BoardParts:
    name: str
    revision: str | None
    qualifiers: str | None  # Includes the leading "/", or None.


def parse_board_string(board: str) -> BoardParts:
    """Split a `<board>[@<revision>][/<qualifiers>]` string into its parts."""
    m = _BOARD_RE.match(board)
    if m is None:
        # Empty, or starting with "@" or "/"
        return BoardParts(board, None, None)
    return BoardParts(m.group("name"), m.group("revision"), m.group("qualifiers"))


def _revision_key(fmt: str, value: str) -> tuple[int, int, int] | int | str:
    """Return a comparable sort key for `value` in Zephyr revision format `fmt`."""
    if fmt == "number":
        return int(value)
    if fmt == "letter":
        return value
    # "major.minor.patch" -- loose typing allowed (e.g. "1" == "1.0.0"), missing
    # trailing parts default to 0.
    parts = [int(p) for p in value.split(".")]
    if len(parts) > 3:
        raise ValueError(f"Too many components in revision {value!r}")
    parts += [0] * (3 - len(parts))
    return (parts[0], parts[1], parts[2])


def _read_revision_block(board_dir: Path) -> dict | None:
    """Return `board_dir/board.yml`'s `board: revision:` block, or None if absent/unreadable."""
    board_yml = board_dir / "board.yml"
    try:
        doc = yaml.safe_load(board_yml.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError):
        return None
    if not isinstance(doc, dict):
        return None
    # Older files have one "board:", newer ones a "boards:" list; with several boards
    # the folder doesn't say which one, so no revision rather than a guess
    if "board" in doc:
        entry = doc["board"]
    else:
        boards = doc.get("boards", [])
        entry = boards[0] if len(boards) == 1 else {}
    revision = entry.get("revision") if isinstance(entry, dict) else None
    return revision if isinstance(revision, dict) else None


def declared_revisions(board_dir: Path) -> list[str]:
    """Return the revision names `board_dir/board.yml` declares (empty if none)."""
    revision = _read_revision_block(board_dir)
    if revision is None:
        return []
    return [
        str(r["name"])
        for r in revision.get("revisions", [])
        if isinstance(r, dict) and "name" in r
    ]


def default_revision(board_dir: Path) -> str | None:
    """Return `board_dir/board.yml`'s `revision: default:`, which Zephyr builds when
    no `@<revision>` is given."""
    revision = _read_revision_block(board_dir)
    default = revision.get("default") if revision is not None else None
    return str(default) if default is not None else None


def resolve_revision(board_dir: Path, requested: str) -> tuple[str | None, bool]:
    """Resolve `requested` against `board_dir/board.yml`'s `revision:` block.

    Returns `(resolved, declares_revisions)`:
    - `declares_revisions` is False when the board has no `revision:` block at all --
      nothing to validate or apply an overlay for.
    - When True, `resolved` is the matched revision string, or None when `requested`
      doesn't resolve: an exact miss under `exact: true`, or no declared revision at
      or below `requested` when `exact` is false/absent -- Zephyr's own
      `board_check_revision()` picks the closest *lower* revision in that case, never
      a higher one.
    """
    revision = _read_revision_block(board_dir)
    if revision is None:
        return None, False

    revisions = declared_revisions(board_dir)
    if not revisions:
        return None, False

    fmt = str(revision.get("format", "")).strip().lower()
    exact = bool(revision.get("exact", False))

    if requested in revisions:
        return requested, True
    if exact:
        return None, True

    try:
        requested_key = _revision_key(fmt, requested)
    except (ValueError, TypeError):
        return None, True

    best: str | None = None
    best_key = None
    for candidate in revisions:
        try:
            candidate_key = _revision_key(fmt, candidate)
        except (ValueError, TypeError):
            continue
        if candidate_key <= requested_key and (
            best_key is None or candidate_key > best_key
        ):
            best, best_key = candidate, candidate_key
    return best, True
