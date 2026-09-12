import re
from typing import Any, Self
from uuid import uuid4

from pydantic import GetCoreSchemaHandler, field_validator
from pydantic_core import CoreSchema, core_schema

from exo.utils.pydantic_ext import FrozenModel


class Id(str):
    def __new__(cls, value: str | None = None) -> Self:
        return super().__new__(cls, value or str(uuid4()))

    @classmethod
    def __get_pydantic_core_schema__(
        cls, _source: type, handler: GetCoreSchemaHandler
    ) -> core_schema.CoreSchema:
        # Just use a plain string schema
        return core_schema.no_info_after_validator_function(
            cls, core_schema.str_schema()
        )


class NodeId(Id):
    pass


class SystemId(Id):
    pass


# Hugging Face repo ids: an optional owner segment then a name segment, each
# beginning with a letter or digit. Forbidding a leading dot excludes "." and
# "..", so no ModelId can normalize to a name that escapes a models directory.
MODEL_ID_PATTERN = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9._-]*(?:/[A-Za-z0-9][A-Za-z0-9._-]*)?$"
)


class ModelId(Id):
    @classmethod
    def __get_pydantic_core_schema__(
        cls, _source: type, handler: GetCoreSchemaHandler
    ) -> core_schema.CoreSchema:
        return core_schema.no_info_after_validator_function(
            cls.parse, core_schema.str_schema()
        )

    @classmethod
    def parse(cls, value: str) -> "ModelId":
        """Build a ModelId from untrusted text, rejecting anything outside MODEL_ID_PATTERN.

        `normalize` turns an id into a directory name, so an id carrying path
        syntax reaches `shutil.rmtree` as a traversal. Ids are checked here,
        where request paths and zenoh command payloads are deserialised, so no
        later caller has to.

        Raises:
            ValueError: `value` is not a valid model id.
        """
        if MODEL_ID_PATTERN.match(value) is None:
            raise ValueError(
                f"invalid model id {value!r}: expected '<name>' or '<owner>/<name>', "
                "each segment starting with a letter or digit and otherwise "
                "containing only letters, digits, '.', '_' or '-'"
            )
        return cls(value)

    def normalize(self) -> str:
        return self.replace("/", "--")

    def short(self) -> str:
        return self.split("/")[-1]


class CommandId(Id):
    pass


class TruncatingString(str):
    truncate_length: int = -1

    @classmethod
    def __get_pydantic_core_schema__(
        cls,
        source_type: Any,  # pyright: ignore[reportAny]
        handler: GetCoreSchemaHandler,
    ) -> CoreSchema:
        return core_schema.no_info_after_validator_function(cls, handler(str))

    def __repr__(self):
        tl = type(self).truncate_length
        return (
            f"<{type(self).__name__}: {self[:tl] + '...' if len(self) > tl else self}>"
        )


class SessionId(FrozenModel):
    master_node_id: NodeId
    election_clock: int


class Host(FrozenModel):
    ip: str
    port: int

    def __str__(self) -> str:
        return f"{self.ip}:{self.port}"

    @field_validator("port")
    @classmethod
    def check_port(cls, v: int) -> int:
        if not (0 <= v <= 65535):
            raise ValueError("Port must be between 0 and 65535")
        return v
