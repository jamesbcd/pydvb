
import enum
import base64
from dataclasses import is_dataclass
import inspect
import json
import os
import pathlib
import datetime
import re
from typing import Any, Callable, cast, TypeVar, overload


# Constants for the keys used in the serialized JSON representation
TYPE_KEY = "__type__"
DATA_KEY = "__data__"


# Type aliases for serializer and deserializer callables
# Serializer takes an object and returns a JSON-serializable representation
# Deserializer takes a class and JSON data and returns an object instance
Serializer = Callable[[object], Any]
Deserializer = Callable[[type, Any], object]

class _Registry:
    '''Internal registry for managing serializers and deserializers for different types.'''
    def __init__(self):
        self._serializers: dict[type, tuple[str, Serializer]] = {}
        self._deserializers: dict[str, tuple[type, Deserializer]] = {}

    def serializer_for(self, obj: object) -> tuple[str, Serializer] | None:
        '''Find the serializer for an object, preferring the most specific registered type.'''
        obj_type = type(obj)
        registered = self._serializers.get(obj_type)
        # First, try to find a serializer registered for the exact type of the object.
        if registered is not None:
            return registered
        # If no exact match is found, check the base classes in the method resolution order (MRO).
        for base_type in obj_type.__mro__[1:]:
            registered = self._serializers.get(base_type)
            if registered is not None:
                return registered
        # Finally, as a last resort, check all registered types to see if the object is an instance
        # of any of them.
        for registered_cls, registered in self._serializers.items():
            if isinstance(obj, registered_cls):
                return registered
        return None

    def register(self, cls: type,
                 serializer: Serializer,
                 deserializer: Deserializer,
                 json_name: str | None = None):
        '''Register a class with its serializer and deserializer.'''
        if json_name is None:
            json_name = cls.__name__
        if not callable(serializer):
            raise TypeError(f"Serializer for object of type {cls.__name__} is not callable")
        if not callable(deserializer):
            raise TypeError(f"Deserializer for object of type {cls.__name__} is not callable")
        if cls in self._serializers:
            raise ValueError(f"Class {cls.__name__} is already registered")
        if json_name in self._deserializers:
            raise ValueError(f"JSON name {json_name} is already registered")
        self._serializers[cls] = (json_name, serializer)
        self._deserializers[json_name] = (cls, deserializer)

_registry = _Registry()

#
# Public API for registering and using serializers
#
def register(cls: type,
             serializer: Serializer,
             deserializer: Deserializer,
             json_name: str | None = None) -> None:
    '''Register a class as serializable, with a custom serializer and deserializer.
    
    This is useful to allowing classes you don't control to be serializable.
    For classes you do control, you may find it more convenient to use the
    @serializable decorator instead.

    Args:
      - cls: The class to register.
      - serializer: A callable that takes an instance of the class and returns a
        JSON-serializable representation.
      - deserializer: A callable that takes the class and JSON data and returns an
        instance of the class.
      - json_name: Optional custom name to use for the class in the JSON representation.
      If not provided, the class's __name__ is used.
    
    Raises:
      - TypeError: If the serializer or deserializer is not callable.
      - ValueError: If the class or JSON name is already registered.
    '''
    _registry.register(cls, serializer, deserializer, json_name)

Pathlike = str | os.PathLike | pathlib.Path

def save(obj: object, path: Pathlike) -> None:
    '''Save a serializable object to a file in JSON format.
    
    The object type must have been previously registered as serializable, either using the
    @serializable decorator or the register() function.'''
    path = pathlib.Path(path)
    pathlib.Path(path).parent.mkdir(parents=True, exist_ok=True)
    json_str = serialize(obj)
    with path.open('w', encoding='utf-8') as file:
        file.write(json_str)

def serialize(obj: object) -> str:
    '''Serialize an object to a JSON string.
     
    The object type must have been previously registered as serializable, either using the
    @serializable decorator or the register() function.
    
    Note that __type__ and __data__ keys are reserved for the serialization mechanism.'''
    return json.dumps(
        _prepare_for_json(obj),
        indent=4,
        ensure_ascii=False,
        default=_serialize_handler,
    )

def load(path: Pathlike) -> object:
    '''Load a serializable object from a JSON file.
    
    The object type must have been previously registered as serializable, either using the
    @serializable decorator or the register() function.'''
    path = pathlib.Path(path)
    with path.open('r', encoding='utf-8') as file:
        json_str = file.read()
    return deserialize(json_str)

def deserialize(json_str: str) -> object:
    '''Deserialize a JSON string back into an object.

    The JSON string must represent an object type that has been previously registered as
    serializable, either using the @serializable decorator or the register() function.
    
    Note that __type__ and __data__ keys are reserved for the serialization mechanism.'''
    return json.loads(json_str, object_hook=_deserialize_handler)

#
# Internal helper functions for working with JSON serialization and deserialization
#
def _prepare_for_json(
    obj: object,
    active_ids: set[int] | None = None,
) -> object:
    '''Recursively prepare registered objects and nested containers for JSON encoding.
    
    This is necessary to handle subtypes of basic Python types like dict, list, and tuple,
    and in particular Enums such as IntEnum. If we don't intercept them here, the JSONEncoder
    will not call our custom serialization handlers for them.
    '''
    if active_ids is None:
        active_ids = set()

    registered = _registry.serializer_for(obj)
    if registered is not None:
        object_id = id(obj)
        if object_id in active_ids:
            raise ValueError(f"Circular reference to {obj} during serialization")
        active_ids.add(object_id)
        try:
            serialized = _serialize_handler(obj)
            return _prepare_for_json(serialized, active_ids)
        finally:
            active_ids.remove(object_id)

    if isinstance(obj, dict):
        object_id = id(obj)
        if object_id in active_ids:
            raise ValueError(f"Circular reference to {obj} during serialization")
        active_ids.add(object_id)
        try:
            prepared: dict[object, object] = {}
            for key, value in obj.items():
                if _registry.serializer_for(key) is not None:
                    raise TypeError(
                        f"Registered serializable object {key} cannot be dictionary keys"
                    )
                prepared[key] = _prepare_for_json(value, active_ids)
            return prepared
        finally:
            active_ids.remove(object_id)

    if isinstance(obj, (list, tuple)):
        object_id = id(obj)
        if object_id in active_ids:
            raise ValueError(f"Circular reference to {obj} during serialization")
        active_ids.add(object_id)
        try:
            return [
                _prepare_for_json(value, active_ids)
                for value in obj
            ]
        finally:
            active_ids.remove(object_id)

    return obj

def _serialize_handler(obj: object):
    '''Called by json.dumps() as the default serialization handler for unrecognised objects.'''
    registered = _registry.serializer_for(obj)
    if registered is None:
        raise TypeError(f"Object of type {type(obj).__name__} is not registered as serializable")
    json_name, serializer = registered
    return {
        TYPE_KEY: json_name,
        DATA_KEY: serializer(obj)
    }

def _deserialize_handler(input_dict: dict) -> object:
    '''Called by json.loads() as the object hook for deserializing JSON objects.'''
    if TYPE_KEY not in input_dict:
        # This is a regular dictionary, so return it as-is.
        return input_dict
    json_name = input_dict[TYPE_KEY]
    (cls, deserializer) = _registry._deserializers.get(json_name, (object, None))
    if deserializer is None:
        raise TypeError(f"No deserializer registered for JSON type {json_name}")
    if DATA_KEY not in input_dict:
        raise TypeError(f"No {DATA_KEY} key found for JSON type {json_name}")
    data = input_dict[DATA_KEY]
    return deserializer(cls, data)

#
# Decorator for easiest way to register serializable classes
#

# We have to do some type overloading to support both usage patterns of the decorator:
# 1. Used directly as @serializable (without parentheses)
# 2. Used with parentheses and optional arguments, e.g., @serializable("CustomName")
# First, create a generic type variable that represents a class
T = TypeVar("T", bound=type)

# Case 1: Used directly as @serializable (without parentheses)
@overload
def serializable(cls_or_json_name: T) -> T: ...

# Case 2: Used with arguments, e.g., @serializable("my_name", config=True)
# This returns a factory function that takes the class and returns it unchanged.
@overload
def serializable(cls_or_json_name: str, *args: Any, **kwargs: Any) -> Callable[[T], T]: ...

def serializable(cls_or_json_name: type | str, *args, **kwargs) -> Any:
    '''Decorator to register a class as serializable.

    Can be used as @serializable or @serializable("CustomName"). The decorated class can
    customise serialization behavior by optionally implementing the following methods:

        def _serialize(self) -> dict:
            # Should return a dictionary representation of the object.
            # If you only need to modify the standard dictionary representation,
            # you may find it useful to call serialize.standard_serializer(self)
            # and then modify the resulting dictionary.
    
        @classmethod
        def _deserialize(cls, data: dict) -> object:
            # Should return an instance of the class from the given dictionary.
            # For example, you could modify the dictionary and then call
            # serialize.standard_deserializer(cls, data) to create the instance
            # as normal, then modify the instance before returning it.
    '''
    if isinstance(cls_or_json_name, str):
        # Decorator was called with a custom JSON name e.g. @serializable("CustomName")
        # We have to return a wrapper function that will be called with the class itself
        json_name = cls_or_json_name
        def wrapper(cls: type) -> type:
            serializer, deserializer = _get_serializers_for_decorated_class(cls)
            _registry.register(cls, serializer, deserializer, json_name)
            # We leave the underlying class unchanged
            return cls
        return wrapper
    else:
        # Decorator was used without arguments e.g. @serializable
        # So this function itself does the decoratorating
        cls = cls_or_json_name
        serializer, deserializer = _get_serializers_for_decorated_class(cls)
        _registry.register(cls, serializer, deserializer, cls.__name__)
        return cls

def _get_serializers_for_decorated_class(cls: type) -> tuple[Serializer, Deserializer]:
    '''Obtain a serializer and deserializer for a class decorated with @serializable.
    
    If the _serialize() or _deserialize() methods are not implemented, standard
    serializers will be used.
    '''
    serializer = getattr(cls, "_serialize", None)
    if serializer is not None and not callable(serializer):
        raise TypeError(f"_serialize() method for object of type {cls.__name__} is not callable")
    if serializer is None:
        if issubclass(cls, enum.Enum):
            serializer = enum_serializer
        elif is_dataclass(cls):
            serializer = dataclass_serializer
        else:
            serializer = standard_serializer
    deserializer = getattr(cls, "_deserialize", None)
    if deserializer is not None and not callable(deserializer):
        raise TypeError(f"_deserialize() method for object of type {cls.__name__} is not callable")
    if deserializer is None:
        if issubclass(cls, enum.Enum):
            deserializer = enum_deserializer
        elif is_dataclass(cls):
            deserializer = dataclass_deserializer
        else:
            deserializer = standard_deserializer
    else:
        # The decorated class implemented _deserialize()
        if inspect.ismethod(deserializer) and inspect.isclass(deserializer.__self__):
            # If the deserializer is a class method, we extract the underlying
            # unbound function, as it will be called with the class as the first
            # argument anyway.
            deserializer = deserializer.__func__
    return serializer, deserializer

#
# Standard serializers and deserializers
#

def standard_serializer(obj: object) -> Any:
    '''Standard serializer for objects that do not define a custom _serialize() method.'''
    try:
        new_obj = dict(obj.__dict__)
    except Exception:
        raise TypeError(f"Failed to serialize object of type {type(obj).__name__}")
    return new_obj

def standard_deserializer(cls: type, data: dict) -> object:
    '''Standard deserializer for objects that do not define a custom _deserialize() method.'''
    try:
        instance = cls()
        instance.__dict__.update(data)
    except Exception as e:
        raise TypeError(f"Failed to deserialize object of type {cls.__name__}: {e}")
    return instance

def enum_serializer(obj: object) -> dict:
    '''Serializer for enum.Enum objects.'''
    obj = cast(enum.Enum, obj)
    return {
        "value": obj.value
    }

def enum_deserializer(cls: type, data: Any) -> enum.Enum:
    '''Deserializer for enum.Enum objects.'''
    return cls(data["value"])

def dataclass_serializer(obj: object) -> dict:
    '''Serializer for dataclass objects.'''
    obj = cast(object, obj)
    if not is_dataclass(obj):
        raise TypeError(f"Object of type {type(obj).__name__} is not a dataclass")
    return {field.name: getattr(obj, field.name) for field in obj.__dataclass_fields__.values()}

def dataclass_deserializer(cls: type, data: dict) -> object:
    '''Deserializer for dataclass objects.'''
    if not is_dataclass(cls):
        raise TypeError(f"Registered class {cls.__name__} is not a dataclass")
    try:
        # In case the dataclass is frozen, it's more reliable to construct the instance
        # by passing all the data as keyword arguments to the constructor.
        instance = cls(**data)
    except Exception as e:
        raise TypeError(f"Failed to deserialize object of type {cls.__name__}: {e}")
    return instance

#
# Custom serializers and deserializers for common types
#

# Bytes serializers and deserializers

def _bytes_serializer(obj: object) -> dict[str, str]:
    obj = cast(bytes, obj)
    return {
        "encoding": "base64",
        "value": base64.b64encode(obj).decode("ascii"),
    }

def _bytes_deserializer(cls: type, data: Any) -> bytes:
    data = cast(dict, data)
    if data.get("encoding") != "base64":
        raise ValueError("Unsupported bytes encoding")
    return base64.b64decode(data["value"], validate=True)

_registry.register(bytes, _bytes_serializer, _bytes_deserializer)

# Path serializers and deserializers

def _path_serializer(obj: object) -> str:
    obj = cast(pathlib.Path, obj)
    return obj.as_posix()

def _path_deserializer(cls: type, data: Any) -> pathlib.Path:
    # When we don't know whether a path string was created on Windows or POSIX, and whether it
    # was converted to a string in POSIX form or not, the most reliable conversion method is
    # to create a Windows path, then convert to POSIX string form, then convert to the current
    # platform.
    return pathlib.Path(pathlib.PureWindowsPath(str(data)).as_posix())

_registry.register(pathlib.Path, _path_serializer, _path_deserializer)

# Datetime serializers and deserializers

def _isoformat_serializer(obj: datetime.datetime | datetime.date | datetime.time) -> str:
    return obj.isoformat()

def _isoformat_deserializer(cls: datetime.datetime | datetime.date | datetime.time, data: Any) -> Any:
    return cls.fromisoformat(str(data))

_registry.register(datetime.datetime, cast(Serializer, _isoformat_serializer),
                                      cast(Deserializer, _isoformat_deserializer))
_registry.register(datetime.date,     cast(Serializer, _isoformat_serializer),
                                      cast(Deserializer, _isoformat_deserializer))
_registry.register(datetime.time,     cast(Serializer, _isoformat_serializer),
                                      cast(Deserializer, _isoformat_deserializer))

def _timedelta_serializer(obj: object) -> float:
    obj = cast(datetime.timedelta, obj)
    return obj.total_seconds()

def _timedelta_deserializer(cls: type, data: Any) -> datetime.timedelta:
    return datetime.timedelta(seconds=float(data))

_registry.register(datetime.timedelta, _timedelta_serializer, _timedelta_deserializer)

# Regex serializers and deserializers

def _regex_serializer(obj: object) -> dict:
    obj = cast(re.Pattern, obj)
    pattern = obj.pattern
    if isinstance(pattern, bytes):
        pattern = _bytes_serializer(pattern)
    return {
        "pattern": pattern,
        "flags": obj.flags
    }

def _regex_deserializer(cls: type, data: Any) -> re.Pattern:
    data = cast(dict, data)
    pattern = data["pattern"]
    if isinstance(pattern, dict):
        pattern = _bytes_deserializer(bytes, pattern)
    flags = data["flags"]
    return re.compile(pattern, flags)

_registry.register(re.Pattern, _regex_serializer, _regex_deserializer)
_registry.register(re.RegexFlag, enum_serializer, enum_deserializer)