import datetime
import enum
import pathlib
import re
from collections.abc import Iterable

import pytest

from mactv.serialize import (
    DATA_KEY,
    TYPE_KEY,
    _Registry,
    deserialize,
    register,
    serializable,
    serialize,
)


def test_standard_decorated_object_round_trips():
    @serializable("SerializeTestObject")
    class TestObject:
        def __init__(self):
            self.value = 42

    result = deserialize(serialize(TestObject()))

    assert isinstance(result, TestObject)
    assert result.value == 42


def test_custom_serializer_and_deserializer_round_trip():
    @serializable("SerializeTestCustom")
    class TestCustom:
        def __init__(self, value=0):
            self.value = value

        def _serialize(self):
            return {"value": self.value * 2}

        @classmethod
        def _deserialize(cls, data):
            return cls(data["value"] // 2)

    result = deserialize(serialize(TestCustom(21)))

    assert isinstance(result, TestCustom)
    assert result.value == 21


def test_registered_subclass_takes_priority_over_registered_base():
    registry = _Registry()

    class Base:
        pass

    class Child(Base):
        pass

    registry.register(Base, lambda obj: "base", lambda cls, data: data, "Base")
    registry.register(Child, lambda obj: "child", lambda cls, data: data, "Child")

    assert registry.serializer_for(Child())[0] == "Child"


def test_registered_mro_base_is_used_for_unregistered_subclass():
    registry = _Registry()

    class Base:
        pass

    class Child(Base):
        pass

    registry.register(Base, lambda obj: "base", lambda cls, data: data, "Base")

    assert registry.serializer_for(Child())[0] == "Base"


def test_virtual_abc_registration_is_used_as_fallback():
    registry = _Registry()

    class IterableObject:
        def __iter__(self):
            return iter(())

    registry.register(
        Iterable,
        lambda obj: "iterable",
        lambda cls, data: data,
        "Iterable",
    )

    assert registry.serializer_for(IterableObject())[0] == "Iterable"


def test_duplicate_class_and_json_name_registration_is_rejected():
    registry = _Registry()

    class First:
        pass

    class Second:
        pass

    serializer = lambda obj: None
    deserializer = lambda cls, data: None
    registry.register(First, serializer, deserializer, "First")

    with pytest.raises(ValueError, match="already registered"):
        registry.register(First, serializer, deserializer, "Other")
    with pytest.raises(ValueError, match="already registered"):
        registry.register(Second, serializer, deserializer, "First")


@pytest.mark.parametrize(
    "value",
    [
        pathlib.Path("a/b"),
        datetime.datetime(2024, 1, 2, 3, 4),
        datetime.date(2024, 1, 2),
        datetime.time(3, 4),
    ],
)
def test_builtin_value_round_trips(value):
    result = deserialize(serialize(value))

    assert type(result) is type(value)
    assert result == value


@pytest.mark.parametrize("value", [b"", b"\x00\xff\x80binary", b"utf-8 \xe2\x98\x83"])
def test_bytes_round_trip(value):
    result = deserialize(serialize(value))

    assert result == value


def test_string_regex_pattern_and_flags_round_trip():
    value = re.compile("a", re.IGNORECASE | re.MULTILINE)

    result = deserialize(serialize(value))

    assert result.pattern == value.pattern
    assert result.flags == value.flags


def test_bytes_regex_pattern_and_flags_round_trip():
    value = re.compile(b"a\\xff", re.IGNORECASE)

    result = deserialize(serialize(value))

    assert result.pattern == value.pattern
    assert result.flags == value.flags


def test_int_enum_round_trips_when_registered():
    @serializable("SerializeTestIntEnum")
    class TestIntEnum(enum.IntEnum):
        ONE = 1

    result = deserialize(serialize(TestIntEnum.ONE))

    assert result is TestIntEnum.ONE


def test_string_enum_round_trips_when_registered():
    @serializable("SerializeTestStringEnum")
    class TestStringEnum(str, enum.Enum):
        RED = "red"

    result = deserialize(serialize(TestStringEnum.RED))

    assert result is TestStringEnum.RED


def test_regex_flag_round_trips():
    result = deserialize(serialize(re.RegexFlag.IGNORECASE))

    assert result is re.RegexFlag.IGNORECASE


def test_nested_registered_values_round_trip():
    @serializable("SerializeTestNested")
    class TestNested:
        def __init__(self, value=0):
            self.value = value

    value = {
        "items": [
            TestNested(1),
            pathlib.Path("nested"),
            re.RegexFlag.MULTILINE,
        ]
    }

    result = deserialize(serialize(value))

    assert isinstance(result["items"][0], TestNested)
    assert result["items"][0].value == 1
    assert result["items"][1] == pathlib.Path("nested")
    assert result["items"][2] is re.RegexFlag.MULTILINE


def test_none_data_round_trips():
    @serializable("SerializeTestNone")
    class TestNone:
        def _serialize(self):
            return None

        @classmethod
        def _deserialize(cls, data):
            assert data is None
            return cls

    assert deserialize(serialize(TestNone())) is TestNone


def test_reserved_metadata_requires_data_for_known_type():
    @serializable("SerializeTestMissingData")
    class TestMissingData:
        pass

    payload = f'{{"{TYPE_KEY}": "SerializeTestMissingData"}}'

    with pytest.raises(TypeError, match=DATA_KEY):
        deserialize(payload)


def test_unknown_serialized_type_is_rejected():
    payload = f'{{"{TYPE_KEY}": "Unknown", "{DATA_KEY}": {{}}}}'

    with pytest.raises(TypeError, match="No deserializer"):
        deserialize(payload)


def test_circular_container_is_rejected():
    value = []
    value.append(value)

    with pytest.raises(ValueError, match="Circular reference"):
        serialize(value)


def test_registered_objects_cannot_be_dictionary_keys():
    @serializable("SerializeTestKey")
    class TestKey:
        pass

    with pytest.raises(TypeError, match="dictionary keys"):
        serialize({TestKey(): "value"})
