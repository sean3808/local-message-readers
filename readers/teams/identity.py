"""Teams self identity configuration shared by cache and export readers."""

from readers.settings import get_setting


def resolve_self_id(data_id: str | None = None) -> str | None:
    """Prefer the source identity, then the identity from settings."""
    return (data_id or "").strip() or get_setting("teams_self_id")
