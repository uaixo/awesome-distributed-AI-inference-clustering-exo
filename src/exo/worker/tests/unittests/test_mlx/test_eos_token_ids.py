import pytest

from exo.shared.types.common import ModelId
from exo.worker.engines.mlx.utils_mlx import get_eos_token_ids_for_model


@pytest.mark.parametrize(
    "model_id",
    [
        "mlx-community/Qwen3.5-27B-8bit",
        "mlx-community/Qwen3.6-27B-8bit",
        "mlx-community/Qwen3.8-27B-8bit",
        "mlx-community/Qwen3.8-27B-4bit",
    ],
)
def test_qwen_3_5_family_uses_both_eos_tokens(model_id: str) -> None:
    # Qwen3.5 / 3.6 / 3.8 tokenizer configs only declare <|im_end|>, but their
    # generation configs also stop on <|endoftext|>.
    assert get_eos_token_ids_for_model(ModelId(model_id)) == [248046, 248044]


def test_unknown_model_uses_tokenizer_eos() -> None:
    assert (
        get_eos_token_ids_for_model(ModelId("mlx-community/Some-Other-Model")) is None
    )
