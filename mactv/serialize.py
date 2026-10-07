
import json
import pathlib
import datetime
from typing import Any, Callable

class JSONMapping:
    def __init__(self):
        self._cls_to_json_name: dict[type, str] = {}
        self._json_name_to_cls: dict[str, type] = {}

        self._external_serialize: dict[type, Callable[[object], Any]] = {}
        self._external_cls_to__json_name: dict[type, str] = {}
        self._external_deserialize: dict[str, Callable[[dict], object]] = {}

    def register(self, cls: type, json_name: str | None = None):
        if json_name is None:
            json_name = cls.__name__
        self._json_name_to_cls[json_name] = cls
        self._cls_to_json_name[cls] = json_name

json_mapping = JSONMapping()

custom_serialization = {pathlib.Path, datetime.datetime, datetime.date, datetime.time}


def serializable(cls_or_json_name: type | str, *args, **kwargs):
    '''Decorator to register a class as serializable.

    Can be used as @serializable or @serializable("CustomName").
    '''
    if isinstance(cls_or_json_name, str):
        # Decorator was called with a custom JSON name e.g. @serializable("CustomName")
        # We have to return a wrapper function that will be called with the class itself
        json_name = cls_or_json_name
        def wrapper(cls: type) -> type:
            json_mapping.register(cls, json_name)
            # We leave the underlying class unchanged
            return cls
        return wrapper
    else:
        # Decorator was used without arguments e.g. @serializable
        # So this function itself does the decoratorating
        cls = cls_or_json_name
        json_mapping.register(cls)
        return cls


def serialize(obj: object) -> str:
    return json.dumps(obj, indent=4, ensure_ascii=False, default=_serialize_handler)

def _serialize_handler(obj: object):
    new_obj = None
    json_name = json_mapping._cls_to_json_name.get(type(obj), None)
    if json_name is not None:
        obj_serialize = getattr(obj, "_serialize", None)
        if obj_serialize is not None:
            if callable(obj_serialize):
                new_obj = obj_serialize()
            else:
                raise TypeError(f"_serialize attribute of object of type {type(obj).__name__} is not callable")
        else:
            new_obj = getattr(obj, "__dict__", None)
            if new_obj is None:
                raise TypeError(f"Object of type {type(obj).__name__} needs a _serialize method or " +
                                f"a __dict__ attribute")
    else:
        has_external = False
        for ext_cls, serializer in json_mapping._external_serialize.items():
            if isinstance(obj, ext_cls):
                json_name = json_mapping._external_cls_to__json_name.get(ext_cls)
                new_obj = serializer(obj)
                has_external = True
                break
        if not has_external:
            raise TypeError(f"Object of type {type(obj).__name__} is not registered as serializable")

    if new_obj is None:
        raise TypeError(f"Failed to serialize object of type {type(obj).__name__}")
    return {
        "__type__": json_name,
        "__data__": new_obj
    }    

def deserialize(json_str: str) -> object:
    return json.loads(json_str, object_hook=_deserialize_handler)
    

def _deserialize_handler(input_dict: dict) -> object:
    json_name = input_dict.get("__type__", None)
    if json_name is None:
        # It's just a normal dictionary, not a serialized object
        return input_dict
    # From this point we have a json_name
    data = input_dict.get("__data__", None)
    if data is None:
        raise ValueError(f"JSON string for class {json_name} does not contain a __data__ field")
    # From this point we have valid data

    cls = json_mapping._json_name_to_cls.get(json_name, None)
    if cls is not None:
        cls_deserialize = getattr(cls, "_deserialize", None)
        if cls_deserialize is not None:
            instance = cls_deserialize(data)
            return instance
        else:
            instance = cls()
            instance.__dict__.update(data)
            return instance
    else:
        for json_name_ext, deserializer in json_mapping._external_deserialize.items():
            if json_name == json_name_ext:
                instance = deserializer(data)
                return instance
        raise ValueError(f"External class {json_name} is not registered as serializable")
    