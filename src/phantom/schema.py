from collections.abc import Sequence
from typing import Any
from typing import Literal

from typing_extensions import TypedDict
from typing_extensions import final


class Schema(TypedDict, total=False):
    title: str
    description: str
    type: Literal["array", "string", "float", "number"]
    format: str
    examples: Sequence[object]
    minimum: float | None
    maximum: float | None
    exclusiveMinimum: float | None
    exclusiveMaximum: float | None
    minItems: int | None
    maxItems: int | None
    minLength: int | None
    maxLength: int | None


class SchemaField:
    @classmethod
    @final
    def __modify_schema__(cls, field_schema: dict) -> None:
        """
        Legacy pydantic v1 hook that collects overrides from
        :func:`Phantom.__schema__() <phantom.Phantom.__schema__>`. Override
        :func:`__schema__() <phantom.Phantom.__schema__>` to provide custom schema
        representations for phantom types.
        """
        field_schema.update(
            {key: value for key, value in cls.__schema__().items() if value is not None}
        )

    @classmethod
    def __get_pydantic_json_schema__(
        cls, core_schema: Any, handler: Any
    ) -> dict[str, Any]:
        json_schema: dict[str, Any] = handler(core_schema)
        json_schema = handler.resolve_ref_schema(json_schema)
        json_schema.update(
            {key: value for key, value in cls.__schema__().items() if value is not None}
        )
        json_schema = cls._normalize_array_union(json_schema)
        if json_schema.get("type") == "array" and "items" not in json_schema:
            json_schema["items"] = {}
        return json_schema

    @classmethod
    def __schema__(cls) -> Schema:
        """
        Hook for providing schema metadata. Override in subclasses to customize a types
        schema representation. This hook differs to pydantic's schema hooks and expects
        subclasses to instantiate new dicts instead of mutating a given one.

        Example:

        .. code-block:: python

            class Name(str, Phantom, predicate=...):
                @classmethod
                def __schema__(cls):
                    return {**super().__schema__(), "description": "A name type"}
        """
        return {"title": cls.__name__}

    @staticmethod
    def _normalize_array_union(schema: dict[str, Any]) -> dict[str, Any]:
        options = SchemaField._array_union_options(schema)
        if options is None:
            return schema

        shared = SchemaField._shared_array_constraints(options)
        if shared is None:
            return schema
        shared_extras, items = shared

        collapsed = {key: value for key, value in schema.items() if key != "anyOf"}
        collapsed["type"] = "array"
        if shared_extras:
            collapsed.update(shared_extras)
        if items is not None:
            collapsed["items"] = items
        return collapsed

    @staticmethod
    def _array_union_options(
        schema: dict[str, Any],
    ) -> list[dict[str, Any]] | None:
        any_of = schema.get("anyOf")
        if not isinstance(any_of, list) or not any_of:
            return None
        options: list[dict[str, Any]] = []
        for option in any_of:
            if not isinstance(option, dict) or option.get("type") != "array":
                return None
            options.append(option)
        return options

    @staticmethod
    def _shared_array_constraints(
        options: list[dict[str, Any]],
    ) -> tuple[dict[str, Any] | None, Any | None] | None:
        shared_extras: dict[str, Any] | None = None
        items = None
        for option in options:
            option_extras = {
                key: value
                for key, value in option.items()
                if key not in {"type", "items"}
            }
            if shared_extras is None:
                shared_extras = option_extras
            elif shared_extras != option_extras:
                return None
            option_items = option.get("items")
            if option_items is not None:
                if items is None:
                    items = option_items
                elif items != option_items:
                    return None
        return shared_extras, items
