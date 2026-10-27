"""Shared positional initialization support for validated transport DTOs."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from pydantic import BaseModel
from pydantic._internal import _model_construction
from typing_extensions import dataclass_transform

if TYPE_CHECKING:
    from pydantic.fields import Field as PydanticModelField
    from pydantic.fields import PrivateAttr as PydanticModelPrivateAttr

    @dataclass_transform(
        kw_only_default=False,
        field_specifiers=(PydanticModelField, PydanticModelPrivateAttr),
    )
    class _PositionalModelMetaclass(_model_construction.ModelMetaclass): ...

else:
    _PositionalModelMetaclass = _model_construction.ModelMetaclass


class PositionalModel(BaseModel, metaclass=_PositionalModelMetaclass):
    """Pydantic model that accepts positional args in field declaration order."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        if args:
            field_names = tuple(self.__class__.model_fields)
            if len(args) > len(field_names):
                raise TypeError(
                    f"{self.__class__.__name__}() takes {len(field_names)} positional arguments but more were given"
                )
            for name, value in zip(field_names, args, strict=False):
                kwargs.setdefault(name, value)
        super().__init__(**kwargs)
