
import enum
import base64
import json
import pathlib
import datetime
import re
from typing import Any, Callable, cast


TYPE_KEY = "__type__"
DATA_KEY = "__data__"

Serializer = Callable[[object], Any]
Deserializer = Callable[[type, Any], object]

class Registry:
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

registry = Registry()

def register(cls: type,
             serializer: Serializer,
             deserializer: Deserializer,
             json_name: str | None = None) -> None:
    '''Register a class as serializable, with a custom serializer and deserializer.'''
    registry.register(cls, serializer, deserializer, json_name)

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

def deserialize(json_str: str) -> object:
    '''Deserialize a JSON string back into an object.

    The JSON string must represent an object type that has been previously registered as
    serializable, either using the @serializable decorator or the register() function.
    
    Note that __type__ and __data__ keys are reserved for the serialization mechanism.'''
    return json.loads(json_str, object_hook=_deserialize_handler)
    
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

    registered = registry.serializer_for(obj)
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
                if registry.serializer_for(key) is not None:
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
    registered = registry.serializer_for(obj)
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
    (registered_cls, deserializer) = registry._deserializers.get(json_name, (object, None))
    if deserializer is None:
        raise TypeError(f"No deserializer registered for JSON type {json_name}")
    if DATA_KEY not in input_dict:
        raise TypeError(f"No {DATA_KEY} key found for JSON type {json_name}")
    data = input_dict[DATA_KEY]
    return deserializer(registered_cls, data)


class StandardObjSerializer:
    '''Standard serializer for objects that do not define a custom _serialize() method.'''
    @staticmethod
    def _serialize(obj: object) -> Any:
        try:
            new_obj = dict(obj.__dict__)
        except Exception:
            raise TypeError(f"Failed to serialize object of type {type(obj).__name__}")
        return new_obj

    @staticmethod
    def _deserializer(registered_cls: type, data: dict) -> object:
        try:
            instance = registered_cls()
            instance.__dict__.update(data)
        except Exception as e:
            raise TypeError(f"Failed to deserialize object of type {registered_cls.__name__}: {e}")
        return instance

class EnumSerializer:
    '''Serializer for enum.Enum objects.'''
    @staticmethod
    def _serialize(obj: object) -> dict:
        obj = cast(enum.Enum, obj)
        return {
            "value": obj.value
        }

    @staticmethod
    def _deserialize(registered_cls: type, data: Any) -> enum.Enum:
        return registered_cls(data["value"])

def _get_serializers_for_decorated_class(cls: type) -> tuple[Serializer, Deserializer]:
    '''Obtain a serializer and deserializer for a class decorated with @serializable.
    
    If no custom _serialize() or _deserialize() methods are defined, the standard serializers will be used.
    '''
    serializer = getattr(cls, "_serialize", None)
    if serializer is not None and not callable(serializer):
        raise TypeError(f"_serialize() method for object of type {cls.__name__} is not callable")
    if serializer is None:
        if issubclass(cls, enum.Enum):
            serializer = EnumSerializer._serialize
        else:
            serializer = StandardObjSerializer._serialize
    deserializer = getattr(cls, "_deserialize", None)
    if deserializer is not None and not callable(deserializer):
        raise TypeError(f"_deserialize() method for object of type {cls.__name__} is not callable")
    if deserializer is None:
        if issubclass(cls, enum.Enum):
            deserializer = EnumSerializer._deserialize
        else:
            deserializer = StandardObjSerializer._deserializer
    return serializer, deserializer

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
            registry.register(cls, serializer, deserializer, json_name)
            # We leave the underlying class unchanged
            return cls
        return wrapper
    else:
        # Decorator was used without arguments e.g. @serializable
        # So this function itself does the decoratorating
        cls = cls_or_json_name
        serializer, deserializer = _get_serializers_for_decorated_class(cls)
        registry.register(cls, serializer, deserializer, cls.__name__)
        return cls

# Built-in custom serializers and deserializers for common types
class PathSerializer:
    @staticmethod
    def _serialize(obj: object) -> str:
        obj = cast(pathlib.Path, obj)
        return obj.as_posix()

    @staticmethod
    def _deserialize(registered_cls: type, data: Any) -> pathlib.Path:
        # When we don't know whether the path string was created on Windows or POSIX, and whether it
        # was converted to a string in POSIX form or not, the most reliable conversion method is
        # to create a Windows path, then convert to POSIX string form, then convert to the current
        # platform.
        data = str(data)
        return pathlib.Path(pathlib.PureWindowsPath(data).as_posix())
registry.register(pathlib.Path, PathSerializer._serialize, PathSerializer._deserialize)

class DateTimeSerializer:
    @staticmethod
    def _serialize(obj: object) -> str:
        obj = cast(datetime.datetime, obj)
        return obj.isoformat()

    @staticmethod
    def _deserialize(registered_cls: type, data: Any) -> datetime.datetime:
        data = str(data)
        return datetime.datetime.fromisoformat(data)
registry.register(datetime.datetime, DateTimeSerializer._serialize, DateTimeSerializer._deserialize)

class DateSerializer:
    @staticmethod
    def _serialize(obj: object) -> str:
        obj = cast(datetime.date, obj)
        return obj.isoformat()

    @staticmethod
    def _deserialize(registered_cls: type, data: Any) -> datetime.date:
        data = str(data)
        return datetime.date.fromisoformat(data)
registry.register(datetime.date, DateSerializer._serialize, DateSerializer._deserialize)

class TimeSerializer:
    @staticmethod
    def _serialize(obj: object) -> str:
        obj = cast(datetime.time, obj)
        return obj.isoformat()

    @staticmethod
    def _deserialize(registered_cls: type, data: Any) -> datetime.time:
        data = str(data)
        return datetime.time.fromisoformat(data)
registry.register(datetime.time, TimeSerializer._serialize, TimeSerializer._deserialize)

class RegexSerializer:
    @staticmethod
    def _serialize(obj: object) -> dict:
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

    @staticmethod
    def _deserialize(registered_cls: type, data: Any) -> re.Pattern:
        data = cast(dict, data)
        pattern = data["pattern"]
        if isinstance(pattern, dict):
            if pattern.get("encoding") != "base64":
                raise ValueError("Unsupported regex pattern encoding")
            pattern = base64.b64decode(pattern["value"], validate=True)
        flags = data["flags"]
        return re.compile(pattern, flags)
registry.register(re.Pattern, RegexSerializer._serialize, RegexSerializer._deserialize)
registry.register(re.RegexFlag, EnumSerializer._serialize, EnumSerializer._deserialize)