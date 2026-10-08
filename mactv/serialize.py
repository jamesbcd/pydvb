
import enum
import base64
import json
import os
import pathlib
import datetime
import re
from typing import Any, Callable, cast


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
        if registered is not None:
            return registered
        for base_type in obj_type.__mro__[1:]:
            registered = self._serializers.get(base_type)
            if registered is not None:
                return registered
        for registered_cls, registered in self._serializers.items():
            if isinstance(obj, registered_cls):
                return registered
        return None

    def register(self, cls: type,
                 serializer: Serializer,
                 deserializer: Deserializer,
                 json_name: str | None = None):
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
    
    For classes you '''
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
    '''Prepare registered objects and nested containers for JSON encoding.
    
    This is necessary to handle subtypes of basic Python types like dict, list, and tuple,
    and in particular Enums such as IntEnum.
    '''
    if active_ids is None:
        active_ids = set()

    registered = _registry.serializer_for(obj)
    if registered is not None:
        object_id = id(obj)
        if object_id in active_ids:
            raise ValueError("Circular reference during serialization")
        active_ids.add(object_id)
        try:
            serialized = _serialize_handler(obj)
            return _prepare_for_json(serialized, active_ids)
        finally:
            active_ids.remove(object_id)

    if isinstance(obj, dict):
        object_id = id(obj)
        if object_id in active_ids:
            raise ValueError("Circular reference during serialization")
        active_ids.add(object_id)
        try:
            prepared: dict[object, object] = {}
            for key, value in obj.items():
                if _registry.serializer_for(key) is not None:
                    raise TypeError(
                        "Registered serializable objects cannot be dictionary keys"
                    )
                prepared[key] = _prepare_for_json(value, active_ids)
            return prepared
        finally:
            active_ids.remove(object_id)

    if isinstance(obj, (list, tuple)):
        object_id = id(obj)
        if object_id in active_ids:
            raise ValueError("Circular reference during serialization")
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
    '''Called by json.dumps() as the default serialization handler for registered objects.'''
    registered = _registry.serializer_for(obj)
    if registered is None:
        raise TypeError(f"Object of type {type(obj).__name__} is not registered as serializable")
    json_name, serializer = registered
    return {
        TYPE_KEY: json_name,
        DATA_KEY: serializer(obj)
    }

def _deserialize_handler(input_dict: dict) -> object:
    '''Called by json.loads() as the object hook for deserializing registered objects.'''
    if TYPE_KEY not in input_dict:
        return input_dict
    json_name = input_dict[TYPE_KEY]
    (registered_cls, deserializer) = _registry._deserializers.get(json_name, (object, None))
    if deserializer is None:
        raise TypeError(f"No deserializer registered for JSON type {json_name}")
    if DATA_KEY not in input_dict:
        raise TypeError(f"No {DATA_KEY} key found for JSON type {json_name}")
    data = input_dict[DATA_KEY]
    return deserializer(registered_cls, data)

#
# Decorator for easiest way to register serializable classes
#
def serializable(cls_or_json_name: type | str, *args, **kwargs):
    '''Decorator to register a class as serializable.

    Can be used as @serializable or @serializable("CustomName"). Class can define custom serialization
    behavior, by adding the following methods:

        def _serialize(self) -> dict:
            # Should return a dictionary representation of the object.
    
        def _deserialize(self, registered_cls: type, data: dict) -> object`:
            # Should return an instance of the class from the given dictionary.    
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
    
    If no custom _serialize() or _deserialize() methods are defined, the standard serializers will be used.
    '''
    serializer = getattr(cls, "_serialize", None)
    if serializer is not None and not callable(serializer):
        raise TypeError(f"_serialize() method for object of type {cls.__name__} is not callable")
    if serializer is None:
        if issubclass(cls, enum.Enum):
            serializer = enum_serializer
        else:
            serializer = standard_serializer
    deserializer = getattr(cls, "_deserialize", None)
    if deserializer is not None and not callable(deserializer):
        raise TypeError(f"_deserialize() method for object of type {cls.__name__} is not callable")
    if deserializer is None:
        if issubclass(cls, enum.Enum):
            deserializer = enum_deserializer
        else:
            deserializer = standard_deserializer
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

def standard_deserializer(registered_cls: type, data: dict) -> object:
    '''Standard deserializer for objects that do not define a custom _deserialize() method.'''
    try:
        instance = registered_cls()
        instance.__dict__.update(data)
    except Exception as e:
        raise TypeError(f"Failed to deserialize object of type {registered_cls.__name__}: {e}")
    return instance

def enum_serializer(obj: object) -> dict:
    '''Serializer for enum.Enum objects.'''
    obj = cast(enum.Enum, obj)
    return {
        "value": obj.value
    }

def enum_deserializer(registered_cls: type, data: Any) -> enum.Enum:
    '''Deserializer for enum.Enum objects.'''
    return registered_cls(data["value"])

#
# Custom serializers and deserializers for common types
#

# Path serializers and deserializers

def _path_serializer(obj: object) -> str:
    obj = cast(pathlib.Path, obj)
    return obj.as_posix()

def _path_deserializer(registered_cls: type, data: Any) -> pathlib.Path:
    # When we don't know whether a path string was created on Windows or POSIX, and whether it
    # was converted to a string in POSIX form or not, the most reliable conversion method is
    # to create a Windows path, then convert to POSIX string form, then convert to the current
    # platform.
    return pathlib.Path(pathlib.PureWindowsPath(str(data)).as_posix())

_registry.register(pathlib.Path, _path_serializer, _path_deserializer)

# Datetime serializers and deserializers

def _datetime_serializer(obj: object) -> str:
    obj = cast(datetime.datetime, obj)
    return obj.isoformat()

def _datetime_deserializer(registered_cls: type, data: Any) -> datetime.datetime:
    return datetime.datetime.fromisoformat(str(data))

_registry.register(datetime.datetime, _datetime_serializer, _datetime_deserializer)

def _date_serializer(obj: object) -> str:
        obj = cast(datetime.date, obj)
        return obj.isoformat()

def _date_deserializer(registered_cls: type, data: Any) -> datetime.date:
        return datetime.date.fromisoformat(str(data))

_registry.register(datetime.date, _date_serializer, _date_deserializer)

def _time_serializer(obj: object) -> str:
        obj = cast(datetime.time, obj)
        return obj.isoformat()

def _time_deserializer(registered_cls: type, data: Any) -> datetime.time:
    return datetime.time.fromisoformat(str(data))

_registry.register(datetime.time, _time_serializer, _time_deserializer)

# Regex serializers and deserializers

def _regex_serializer(obj: object) -> dict:
    obj = cast(re.Pattern, obj)
    pattern = obj.pattern
    if isinstance(pattern, bytes):
        pattern = {
            "encoding": "base64",
            "value": base64.b64encode(pattern).decode("ascii"),
        }
    return {
        "pattern": pattern,
        "flags": obj.flags
    }

def _regex_deserializer(registered_cls: type, data: Any) -> re.Pattern:
    data = cast(dict, data)
    pattern = data["pattern"]
    if isinstance(pattern, dict):
        if pattern.get("encoding") != "base64":
            raise ValueError("Unsupported regex pattern encoding")
        pattern = base64.b64decode(pattern["value"], validate=True)
    flags = data["flags"]
    return re.compile(pattern, flags)

_registry.register(re.Pattern, _regex_serializer, _regex_deserializer)
_registry.register(re.RegexFlag, enum_serializer, enum_deserializer)