"""Tests for the ModelId grammar that keeps model ids out of the filesystem."""

import pytest
from pydantic import TypeAdapter, ValidationError

from exo.shared.types.common import ModelId

adapter: TypeAdapter[ModelId] = TypeAdapter(ModelId)


@pytest.mark.parametrize(
    "value",
    [
        "gpt-4o",
        "llama-3.2-1b",
        "Qwen3",
        "mlx-community/Qwen3-30B-A3B-4bit",
        "mlx-community/DeepSeek-V3.2-8bit",
        "test_org/model.v2",
    ],
)
def test_real_model_ids_are_accepted(value: str) -> None:
    """Every id shape the model cards and tests use must keep working."""
    assert adapter.validate_python(value) == value


@pytest.mark.parametrize(
    "value",
    [
        ".",
        "..",
        "../..",
        "a/../b",
        ".hidden",
        "-leading",
        "",
        "a b",
        "a/b/c",
        "%2e%2e",
        "model\x00",
    ],
)
def test_path_syntax_and_junk_are_rejected(value: str) -> None:
    """`.` and `..` are the traversal payloads; the rest fall outside the repo-id grammar."""
    with pytest.raises(ValidationError):
        _ = adapter.validate_python(value)


def test_normalize_produces_a_plain_name() -> None:
    """The owner separator is the only character normalize rewrites."""
    assert adapter.validate_python("mlx-community/Qwen3").normalize() == (
        "mlx-community--Qwen3"
    )


def test_parse_reports_the_offending_value() -> None:
    """An operator who mistypes an id needs to see which id was refused."""
    with pytest.raises(ValueError, match="invalid model id '\\.\\.'"):
        _ = ModelId.parse("..")
