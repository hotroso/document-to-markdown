"""Shared intermediate document model used by all input adapters."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class Block:
    """A logical unit that can be rendered to Markdown."""

    kind: str
    text: str = ""
    rows: List[List[str]] = field(default_factory=list)
    source: Dict[str, Any] = field(default_factory=dict)
    confidence: Optional[float] = None
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class Document:
    """Source-independent representation of a converted document."""

    source: str
    source_type: str
    title: str
    blocks: List[Block] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    metadata: Dict[str, Any] = field(default_factory=dict)

    def add(self, block: Block) -> None:
        self.blocks.append(block)

    def warn(self, message: str) -> None:
        self.warnings.append(message)
