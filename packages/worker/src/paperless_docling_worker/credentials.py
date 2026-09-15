from __future__ import annotations


def is_valid_credential(value: str | bytes) -> bool:
    if not value:
        return False
    if isinstance(value, bytes):
        return not any(bytes((character,)).isspace() for character in value)
    return not any(character.isspace() for character in value)
