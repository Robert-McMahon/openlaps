from google.protobuf.internal import containers as _containers
from google.protobuf.internal import enum_type_wrapper as _enum_type_wrapper
from google.protobuf import descriptor as _descriptor
from google.protobuf import message as _message
from collections.abc import Iterable as _Iterable, Mapping as _Mapping
from typing import ClassVar as _ClassVar, Optional as _Optional, Union as _Union

DESCRIPTOR: _descriptor.FileDescriptor

class ValueType(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    VALUE_TYPE_UNSPECIFIED: _ClassVar[ValueType]
    DOUBLE: _ClassVar[ValueType]
    INT64: _ClassVar[ValueType]
    BOOL: _ClassVar[ValueType]
    STRING: _ClassVar[ValueType]
VALUE_TYPE_UNSPECIFIED: ValueType
DOUBLE: ValueType
INT64: ValueType
BOOL: ValueType
STRING: ValueType

class Channel(_message.Message):
    __slots__ = ("id", "name", "source_ref", "units", "type")
    ID_FIELD_NUMBER: _ClassVar[int]
    NAME_FIELD_NUMBER: _ClassVar[int]
    SOURCE_REF_FIELD_NUMBER: _ClassVar[int]
    UNITS_FIELD_NUMBER: _ClassVar[int]
    TYPE_FIELD_NUMBER: _ClassVar[int]
    id: int
    name: str
    source_ref: str
    units: str
    type: ValueType
    def __init__(self, id: _Optional[int] = ..., name: _Optional[str] = ..., source_ref: _Optional[str] = ..., units: _Optional[str] = ..., type: _Optional[_Union[ValueType, str]] = ...) -> None: ...

class ChannelRegistry(_message.Message):
    __slots__ = ("registry_seq", "vehicle_id", "created_unix_ms", "channels")
    REGISTRY_SEQ_FIELD_NUMBER: _ClassVar[int]
    VEHICLE_ID_FIELD_NUMBER: _ClassVar[int]
    CREATED_UNIX_MS_FIELD_NUMBER: _ClassVar[int]
    CHANNELS_FIELD_NUMBER: _ClassVar[int]
    registry_seq: int
    vehicle_id: str
    created_unix_ms: int
    channels: _containers.RepeatedCompositeFieldContainer[Channel]
    def __init__(self, registry_seq: _Optional[int] = ..., vehicle_id: _Optional[str] = ..., created_unix_ms: _Optional[int] = ..., channels: _Optional[_Iterable[_Union[Channel, _Mapping]]] = ...) -> None: ...

class Sample(_message.Message):
    __slots__ = ("channel_id", "t_offset_us", "d", "i", "b", "s")
    CHANNEL_ID_FIELD_NUMBER: _ClassVar[int]
    T_OFFSET_US_FIELD_NUMBER: _ClassVar[int]
    D_FIELD_NUMBER: _ClassVar[int]
    I_FIELD_NUMBER: _ClassVar[int]
    B_FIELD_NUMBER: _ClassVar[int]
    S_FIELD_NUMBER: _ClassVar[int]
    channel_id: int
    t_offset_us: int
    d: float
    i: int
    b: bool
    s: str
    def __init__(self, channel_id: _Optional[int] = ..., t_offset_us: _Optional[int] = ..., d: _Optional[float] = ..., i: _Optional[int] = ..., b: _Optional[bool] = ..., s: _Optional[str] = ...) -> None: ...

class SampleBatch(_message.Message):
    __slots__ = ("registry_seq", "batch_epoch_unix_ms", "batch_epoch_mono_ns", "samples")
    REGISTRY_SEQ_FIELD_NUMBER: _ClassVar[int]
    BATCH_EPOCH_UNIX_MS_FIELD_NUMBER: _ClassVar[int]
    BATCH_EPOCH_MONO_NS_FIELD_NUMBER: _ClassVar[int]
    SAMPLES_FIELD_NUMBER: _ClassVar[int]
    registry_seq: int
    batch_epoch_unix_ms: int
    batch_epoch_mono_ns: int
    samples: _containers.RepeatedCompositeFieldContainer[Sample]
    def __init__(self, registry_seq: _Optional[int] = ..., batch_epoch_unix_ms: _Optional[int] = ..., batch_epoch_mono_ns: _Optional[int] = ..., samples: _Optional[_Iterable[_Union[Sample, _Mapping]]] = ...) -> None: ...
