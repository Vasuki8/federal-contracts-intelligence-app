"""The verticals (NAICS code sets) the app covers, from `pipeline/config/verticals.yaml`."""

from pathlib import Path
from typing import Annotated

import yaml
from pydantic import BaseModel, ConfigDict, StringConstraints, field_validator

VERTICALS_FILE = Path(__file__).resolve().parent / "config" / "verticals.yaml"

NaicsCode = Annotated[str, StringConstraints(pattern=r"^\d{6}$")]


class Vertical(BaseModel):
    model_config = ConfigDict(frozen=True)

    key: str
    name: str
    naics: tuple[NaicsCode, ...]

    @field_validator("naics")
    @classmethod
    def _unique(cls, codes: tuple[str, ...]) -> tuple[str, ...]:
        if len(set(codes)) != len(codes):
            raise ValueError("NAICS codes in a vertical must be unique")
        if not codes:
            raise ValueError("a vertical needs at least one NAICS code")
        return codes


class VerticalsFile(BaseModel):
    verticals: tuple[Vertical, ...]


def load_verticals(path: Path = VERTICALS_FILE) -> tuple[Vertical, ...]:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    return VerticalsFile.model_validate(data).verticals


def vertical_naics(path: Path = VERTICALS_FILE) -> frozenset[str]:
    """Every NAICS code in any vertical."""
    return frozenset(code for vertical in load_verticals(path) for code in vertical.naics)
