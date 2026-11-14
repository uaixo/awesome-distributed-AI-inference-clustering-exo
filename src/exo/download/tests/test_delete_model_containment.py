"""Tests that deleting a model refuses a model id that is a path reference, not a name.

`ModelId.parse` rejects these where payloads are deserialised. This covers the
second layer: a caller inside the process that builds `ModelId` directly still
must not reach `shutil.rmtree` with `.` or `..`.
"""

from pathlib import Path
from unittest.mock import patch

import pytest

from exo.download import store
from exo.shared.types.common import ModelId


@pytest.fixture
def models_dir(tmp_path: Path) -> Path:
    """A models directory holding one model, inside a parent holding a sibling file."""
    parent = tmp_path / "exo"
    target = parent / "models"
    (target / "test-org--test-model").mkdir(parents=True)
    (target / "test-org--test-model" / "weights.safetensors").write_text("data")
    (parent / "node_zid").write_text("identity")
    return target


@pytest.mark.parametrize("value", [".", ".."])
def test_path_references_delete_nothing(value: str, models_dir: Path) -> None:
    """`..` resolved to the models directory's parent, taking node_zid with it."""
    with (
        patch.object(store, "EXO_MODELS_DIRS", (models_dir,)),
        patch.object(store, "EXO_DEFAULT_MODELS_DIR", models_dir),
        pytest.raises(ValueError, match="not a valid model id"),
    ):
        _ = store.delete(ModelId(value))

    assert (models_dir / "test-org--test-model" / "weights.safetensors").exists()
    assert (models_dir.parent / "node_zid").exists()


def test_multi_segment_traversal_stays_inside_the_models_directory(
    models_dir: Path,
) -> None:
    """`normalize` rewrites the separator, so `../..` becomes the literal name `..--..`.

    Only a single-segment `.` or `..` survives as a path reference, which is why
    the guard checks for exactly those two.
    """
    with (
        patch.object(store, "EXO_MODELS_DIRS", (models_dir,)),
        patch.object(store, "EXO_DEFAULT_MODELS_DIR", models_dir),
    ):
        assert store.delete(ModelId("../..")) is False

    assert (models_dir / "test-org--test-model" / "weights.safetensors").exists()
    assert (models_dir.parent / "node_zid").exists()


def test_a_real_model_is_still_deleted(models_dir: Path) -> None:
    """The guard must not stand between an operator and an actual delete."""
    with (
        patch.object(store, "EXO_MODELS_DIRS", (models_dir,)),
        patch.object(store, "EXO_DEFAULT_MODELS_DIR", models_dir),
    ):
        assert store.delete(ModelId("test-org/test-model")) is True

    assert not (models_dir / "test-org--test-model").exists()
    assert (models_dir.parent / "node_zid").exists()


def test_a_real_id_becomes_a_single_directory_name() -> None:
    """The owner separator becomes the single directory name the download code uses."""
    assert ModelId("mlx-community/Qwen3").normalize() == "mlx-community--Qwen3"
