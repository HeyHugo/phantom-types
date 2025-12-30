from __future__ import annotations

import abc
from collections.abc import Callable
from collections.abc import Iterable
from collections.abc import Iterator
from collections.abc import Mapping
from collections.abc import Sequence
from typing import Any
from typing import ClassVar
from typing import Generic
from typing import Protocol
from typing import TypeVar
from typing import get_args
from typing import get_origin
from typing import runtime_checkable

from typing_extensions import Self

from . import _hypothesis
from ._utils.misc import BoundType
from ._utils.misc import UnresolvedClassAttribute
from ._utils.misc import fully_qualified_name
from ._utils.misc import is_not_known_mutable_type
from ._utils.misc import is_subtype
from ._utils.misc import resolve_class_attr
from .bounds import Parser
from .bounds import get_bound_parser
from .errors import BoundError
from .errors import MissingDependency
from .predicates import Predicate
from .schema import SchemaField


@runtime_checkable
class InstanceCheckable(Protocol):
    @classmethod
    @abc.abstractmethod
    def __instancecheck__(cls, instance: object) -> bool: ...


class SupportsParse(Protocol):
    @classmethod
    @abc.abstractmethod
    def parse(cls, instance: object) -> Self: ...


V = TypeVar("V", bound=SupportsParse)


class PhantomMeta(abc.ABCMeta):
    """
    Metaclass that defers __instancecheck__ to derived classes and prevents actual
    instance creation.
    """

    def __instancecheck__(self, instance: object) -> bool:
        if not issubclass(self, InstanceCheckable):
            return False
        return self.__instancecheck__(instance)

    def __call__(cls: type[V], instance: object) -> V:
        return cls.parse(instance)


T = TypeVar("T", covariant=True)
U = TypeVar("U")


Derived = TypeVar("Derived", bound="PhantomBase")


class PhantomBase(SchemaField, metaclass=PhantomMeta):
    @classmethod
    def parse(cls: type[Derived], instance: object) -> Derived:
        """
        Parse an arbitrary value into a phantom type.

        :raises TypeError:
        """
        if not isinstance(instance, cls):
            raise TypeError(
                f"Could not parse {fully_qualified_name(cls)} from {instance!r}"
            )
        return instance

    @classmethod
    @abc.abstractmethod
    def __instancecheck__(cls, instance: object) -> bool: ...

    @classmethod
    def __get_validators__(cls: type[Derived]) -> Iterator[Callable[[object], Derived]]:
        """Legacy hook for pydantic v1 compatibility."""
        yield cls.parse

    @classmethod
    def __get_pydantic_core_schema__(cls, source: type[Any], handler: Any) -> Any:
        try:
            from pydantic_core import PydanticCustomError
            from pydantic_core import core_schema
        except ImportError as exc:
            raise MissingDependency(
                "pydantic needs to be installed to use phantom-types with Pydantic."
            ) from exc

        base_schema = cls._resolve_base_schema(source, handler, core_schema)

        def validate(
            value: Any, wrap_handler: core_schema.ValidatorFunctionWrapHandler
        ) -> Any:
            value = wrap_handler(value)
            try:
                parsed = cls.parse(value)
            except Exception as exc:
                raise PydanticCustomError(
                    "value_error", f"value is not a valid {cls.__name__}"
                ) from exc
            return parsed

        return core_schema.no_info_wrap_validator_function(validate, base_schema)

    @classmethod
    def _resolve_base_schema(
        cls, source: type[Any], handler: Any, core_schema: Any
    ) -> Any:
        base_schema = cls._schema_from_args(source, handler, core_schema)
        if base_schema is None:
            base_schema = cls._schema_from_bound(handler)
        if base_schema is None:
            base_schema = core_schema.any_schema()
        return base_schema

    @classmethod
    def _schema_from_args(
        cls, source: type[Any], handler: Any, core_schema: Any
    ) -> Any | None:
        try:
            from pydantic.errors import PydanticSchemaGenerationError
        except ImportError:
            return None

        args = cls._get_source_args(source)
        if not args:
            return None
        if not issubclass(cls, Iterable) or issubclass(cls, (str, bytes)):  # type: ignore[unreachable]
            return None

        return cls._schema_from_iterable_args(
            source, handler, core_schema, args, PydanticSchemaGenerationError
        )

    @classmethod
    def _schema_from_iterable_args(
        cls,
        source: type[Any],
        handler: Any,
        core_schema: Any,
        args: tuple[Any, ...],
        schema_error: type[Exception],
    ) -> Any | None:
        origin = cls._normalize_origin(cls._get_source_origin(source))
        if origin is None:
            origin = cls._infer_origin_from_cls()
        if cls._is_fixed_tuple(origin, args):
            return cls._fixed_tuple_schema(handler, core_schema, args, schema_error)
        if cls._is_mapping_origin(origin) or issubclass(cls, Mapping):
            return cls._mapping_schema(handler, core_schema, args, schema_error)

        item = args[0]
        try:
            item_schema = handler.generate_schema(item)
            from pydantic_core import SchemaValidator
        except (schema_error, TypeError):
            return None
        item_validator = SchemaValidator(item_schema)
        sequence_schema = cls._sequence_schema(core_schema, item_schema, item_validator)
        return cls._schema_for_origin(core_schema, origin, item_schema, sequence_schema)

    @staticmethod
    def _normalize_origin(origin: Any) -> Any | None:
        if origin is None:
            return None
        for candidate in (tuple, frozenset, set, list, Mapping, Sequence):
            try:
                if issubclass(origin, candidate):
                    return candidate
            except TypeError:
                continue
        return origin

    @classmethod
    def _infer_origin_from_cls(cls) -> Any | None:
        for candidate in (tuple, frozenset, set, list, Mapping, Sequence):
            try:
                if issubclass(cls, candidate):
                    return candidate
            except TypeError:
                continue
        return None

    @staticmethod
    def _is_fixed_tuple(origin: Any, args: tuple[Any, ...]) -> bool:
        return origin is tuple and len(args) > 1 and args[1] is not Ellipsis

    @staticmethod
    def _is_mapping_origin(origin: Any) -> bool:
        if origin is None:
            return False
        try:
            return issubclass(origin, Mapping)
        except TypeError:
            return False

    @staticmethod
    def _fixed_tuple_schema(
        handler: Any,
        core_schema: Any,
        args: tuple[Any, ...],
        schema_error: type[Exception],
    ) -> Any | None:
        try:
            item_schemas = [handler.generate_schema(item) for item in args]
        except (schema_error, TypeError):
            return None
        return core_schema.tuple_schema(item_schemas, strict=True)

    @staticmethod
    def _mapping_schema(
        handler: Any,
        core_schema: Any,
        args: tuple[Any, ...],
        schema_error: type[Exception],
    ) -> Any | None:
        if len(args) < 2:
            return None
        key_type, value_type = args[0], args[1]
        try:
            key_schema = handler.generate_schema(key_type)
            value_schema = handler.generate_schema(value_type)
        except (schema_error, TypeError):
            return None
        return core_schema.dict_schema(
            key_schema,
            value_schema,
            strict=True,
        )

    @classmethod
    def _sequence_schema(
        cls, core_schema: Any, item_schema: Any, item_validator: Any
    ) -> Any:
        schema_limits = cls.__schema__()
        min_items = schema_limits.get("minItems")
        max_items = schema_limits.get("maxItems")

        def validate_sequence(value: Any) -> Any:
            if not isinstance(value, Sequence) or isinstance(
                value, (str, bytes, list, tuple)
            ):
                raise TypeError("value is not a valid sequence")
            if min_items is not None and len(value) < min_items:
                raise TypeError("value has fewer items than allowed")
            if max_items is not None and len(value) > max_items:
                raise TypeError("value has more items than allowed")
            for item_value in value:
                item_validator.validate_python(item_value)
            return value

        return core_schema.no_info_plain_validator_function(
            validate_sequence,
            json_schema_input_schema=core_schema.list_schema(item_schema, strict=True),
        )

    @staticmethod
    def _schema_for_origin(
        core_schema: Any,
        origin: Any,
        item_schema: Any,
        sequence_schema: Any,
    ) -> Any:
        if origin is set:
            return core_schema.set_schema(item_schema)
        if origin is frozenset:
            return core_schema.frozenset_schema(item_schema)
        if origin is tuple:
            return core_schema.tuple_variable_schema(item_schema, strict=True)
        if origin is list:
            return core_schema.list_schema(item_schema, strict=True)
        return core_schema.union_schema(
            [
                core_schema.list_schema(item_schema, strict=True),
                core_schema.tuple_variable_schema(item_schema, strict=True),
                sequence_schema,
            ],
            mode="left_to_right",
        )

    @classmethod
    def _schema_from_bound(cls, handler: Any) -> Any | None:
        try:
            from pydantic.errors import PydanticSchemaGenerationError
        except ImportError:
            return None

        bound = getattr(cls, "__bound__", None)
        if bound is None:
            return None
        if isinstance(bound, tuple):
            # Prefer the narrowest subtype when an intersection is representable.
            for candidate in bound:
                if all(
                    is_subtype(candidate, other)
                    for other in bound
                    if other is not candidate
                ):
                    bound = candidate
                    break
            else:
                return None
        try:
            return handler.generate_schema(bound)
        except (PydanticSchemaGenerationError, TypeError):
            return None

    @staticmethod
    def _get_source_args(source: type[Any]) -> tuple[Any, ...]:
        args = get_args(source)
        if args:
            return args
        for base in getattr(source, "__orig_bases__", ()):
            base_args = get_args(base)
            if base_args:
                return base_args
        return ()

    @staticmethod
    def _get_source_origin(source: type[Any]) -> Any | None:
        origin = get_origin(source)
        if origin:
            return origin
        for base in getattr(source, "__orig_bases__", ()):
            base_origin = get_origin(base)
            if base_origin:
                return base_origin
        return None


class AbstractInstanceCheck(TypeError): ...


class MutableType(TypeError): ...


class Phantom(PhantomBase, Generic[T]):
    """
    Base class for predicate-based phantom types.

    **Class arguments**

    * ``predicate: Predicate[T] | None`` - Predicate function used for instance checks.
      Can be ``None`` if the type is abstract.
    * ``bound: type[T] | None`` - Bound used to check values before passing them to the
      type's predicate function. This will often but not always be the same as the
      runtime type that values of the phantom type are represented as. If this is not
      provided as a class argument, it's attempted to be resolved in order from an
      implicit bound (any bases of the type that come before ``Phantom``), or inherited
      from super phantom types that provide a bound. Can be ``None`` if the type is
      abstract.
    * ``abstract: bool`` - Set to ``True`` to create an abstract phantom type. This
      allows deferring definitions of ``predicate`` and ``bound`` to concrete subtypes.
    """

    __predicate__: Predicate[T]
    # The bound of a phantom type is the type that its values will have at
    # runtime, so when checking if a value is an instance of a phantom type,
    # it's first checked to be within its bounds, so that the value can be
    # safely passed as argument to the predicate function.
    #
    # When subclassing, the bound of the new type must be a subtype of the bound
    # of the super class.
    __bound__: ClassVar[type]
    __abstract__: ClassVar[bool]

    def __init_subclass__(
        cls,
        predicate: Predicate[T] | None = None,
        bound: type[T] | None = None,
        abstract: bool = False,
        **kwargs: Any,
    ) -> None:
        super().__init_subclass__(**kwargs)
        resolve_class_attr(cls, "__abstract__", abstract)
        resolve_class_attr(cls, "__predicate__", predicate)
        cls._resolve_bound(bound)

        if _hypothesis.register_type_strategy is not None and not cls.__abstract__:
            strategy = cls.__register_strategy__()
            if strategy is not None:
                _hypothesis.register_type_strategy(cls, strategy)

    @classmethod
    def _interpret_implicit_bound(cls) -> BoundType:
        def discover_bounds() -> Iterable[type]:
            for type_ in cls.__mro__:
                if type_ is cls:
                    continue
                if issubclass(type_, Phantom):
                    break
                yield type_
            else:  # pragma: no cover
                raise RuntimeError(f"{cls} is not a subclass of Phantom")

        types = tuple(discover_bounds())
        if len(types) == 1:
            return types[0]
        return types

    @classmethod
    def _resolve_bound(cls, class_arg: Any) -> None:
        inherited = getattr(cls, "__bound__", None)
        implicit = cls._interpret_implicit_bound()
        if class_arg is not None:
            bound = class_arg
        elif implicit:
            bound = implicit
        elif inherited is not None:
            bound = inherited
        elif not getattr(cls, "__abstract__", False):
            raise UnresolvedClassAttribute(
                f"Concrete phantom type {cls.__qualname__} must define class attribute "
                f"__bound__."
            )
        else:
            return

        if inherited is not None and not is_subtype(bound, inherited):
            raise BoundError(
                f"The bound of {cls.__qualname__} is not compatible with its "
                f"inherited bounds."
            )

        if not is_not_known_mutable_type(bound):
            raise MutableType(f"The bound of {cls.__qualname__} is mutable.")

        cls.__bound__ = bound

    @classmethod
    def __instancecheck__(cls, instance: object) -> bool:
        if cls.__abstract__:
            raise AbstractInstanceCheck(
                "Abstract phantom types cannot be used in instance checks"
            )
        bound_parser: Parser[T] = get_bound_parser(cls.__bound__)
        try:
            instance = bound_parser(instance)
        except BoundError:
            return False
        return cls.__predicate__(instance)

    @classmethod
    def __register_strategy__(cls) -> _hypothesis.HypothesisStrategy | None:
        return None
