from __future__ import annotations

from pathlib import Path
from typing import Literal

from ._model import BoundModel, ModelError, load_document
from ._starrocks import render_query
from ._trino import render_query as render_trino_query
from .api import CompilationError, CompiledQuery, FieldFilter, MetricFilter, OutputColumn, Query
from .planning import plan_query

class Compiler:
    def __init__(self, model: BoundModel):
        self._model = model

    @classmethod
    def from_file(cls, path: str | Path) -> "Compiler":
        source = Path(path)
        format = "yaml" if source.suffix.lower() in {".yaml", ".yml"} else "json"
        return cls.from_text(source.read_text(encoding="utf-8"), format=format)

    @classmethod
    def from_text(cls, document: str, *, format: Literal["json", "yaml"]) -> "Compiler":
        try:
            return cls(load_document(document, format))
        except ModelError as error:
            raise CompilationError(error.code, error.location, str(error)) from error

    def compile(
        self,
        query: Query,
        *,
        target: Literal["starrocks", "trino"] = "starrocks",
    ) -> CompiledQuery:
        plan = plan_query(self._model, query)
        if target == "starrocks":
            return render_query(self._model, plan)
        if target == "trino":
            return render_trino_query(self._model, plan)
        raise CompilationError("INVALID_TARGET", "$.target", f"unsupported SQL target {target!r}")

__all__ = ["CompilationError", "CompiledQuery", "Compiler", "FieldFilter", "MetricFilter", "OutputColumn", "Query"]
