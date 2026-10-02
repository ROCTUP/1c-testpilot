"""Pictures passed between document HTML output and input arguments."""
from dataclasses import dataclass


@dataclass(frozen=True)
class Picture:
    data: bytes
