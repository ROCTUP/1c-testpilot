"""Binary values shared by scenario formats."""
from dataclasses import dataclass

@dataclass(frozen=True)
class BinaryData:
    data: bytes
