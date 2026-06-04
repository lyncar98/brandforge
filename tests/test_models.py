import pytest

from brandforge.errors import InvalidRequestError
from brandforge.models import (
    GenerationRequest,
    GenType,
    ImageRef,
    Model,
    OutputFormat,
    State,
    Style,
)


def test_minimal_body_omits_defaults():
    body = GenerationRequest(prompt="a cat").validate().to_body()
    assert body == {"prompt": "a cat"}


def test_full_body_serialization():
    req = GenerationRequest(
        prompt="a cat",
        model=Model.UNI_1_MAX,
        aspect_ratio="16:9",
        output_format=OutputFormat.JPEG,
        web_search=True,
        image_ref=[ImageRef(url="https://example.com/a.jpg")],
    ).validate()
    body = req.to_body()
    assert body["model"] == "uni-1-max"
    assert body["aspect_ratio"] == "16:9"
    assert body["output_format"] == "jpeg"
    assert body["web_search"] is True
    assert body["image_ref"] == [{"url": "https://example.com/a.jpg"}]


def test_prompt_length_bounds():
    with pytest.raises(InvalidRequestError):
        GenerationRequest(prompt="").validate()
    with pytest.raises(InvalidRequestError):
        GenerationRequest(prompt="x" * 6001).validate()


def test_bad_aspect_ratio_rejected():
    with pytest.raises(InvalidRequestError):
        GenerationRequest(prompt="x", aspect_ratio="4:3").validate()


def test_manga_requires_portrait_on_image():
    with pytest.raises(InvalidRequestError):
        GenerationRequest(prompt="x", style=Style.MANGA, aspect_ratio="16:9").validate()
    # portrait is fine
    GenerationRequest(prompt="x", style=Style.MANGA, aspect_ratio="9:16").validate()


def test_manga_constraint_ignored_on_edit():
    GenerationRequest(
        prompt="x",
        type=GenType.IMAGE_EDIT,
        style=Style.MANGA,
        aspect_ratio="16:9",
        source=ImageRef(url="https://example.com/s.jpg"),
    ).validate()


def test_source_required_for_edit():
    with pytest.raises(InvalidRequestError):
        GenerationRequest(prompt="x", type=GenType.IMAGE_EDIT).validate()


def test_source_rejected_for_image():
    with pytest.raises(InvalidRequestError):
        GenerationRequest(prompt="x", source=ImageRef(url="https://example.com/s.jpg")).validate()


def test_image_ref_count_limits():
    refs = [ImageRef(url=f"https://example.com/{i}.jpg") for i in range(10)]
    with pytest.raises(InvalidRequestError):
        GenerationRequest(prompt="x", image_ref=refs).validate()


def test_image_ref_shape_validation():
    with pytest.raises(InvalidRequestError):
        GenerationRequest(prompt="x", image_ref=[ImageRef()]).validate()
    with pytest.raises(InvalidRequestError):
        GenerationRequest(
            prompt="x", image_ref=[ImageRef(url="u", data="d")]
        ).validate()
    with pytest.raises(InvalidRequestError):
        GenerationRequest(prompt="x", image_ref=[ImageRef(data="d")]).validate()


def test_state_terminal():
    assert State.COMPLETED.is_terminal
    assert State.FAILED.is_terminal
    assert not State.QUEUED.is_terminal
    assert not State.PROCESSING.is_terminal
