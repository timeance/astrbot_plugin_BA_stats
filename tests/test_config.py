"""Runtime configuration contracts without a live host/API."""
import pytest

from settings import Settings


def test_image_send_mode_defaults_to_base64():
    assert Settings().image_send_mode == "base64"
    assert Settings.from_config({}).image_send_mode == "base64"


@pytest.mark.parametrize("mode", ["base64", "file", "url"])
def test_image_send_mode_accepts_supported_values(mode):
    assert Settings.from_config({"output": {"image_send_mode": mode}}).image_send_mode == mode


@pytest.mark.parametrize("mode", ["auto", "BASE64", "", None, False, 123, [], {}])
def test_image_send_mode_rejects_invalid_values(mode):
    with pytest.raises(ValueError, match="图片发送方式"):
        Settings.from_config({"output": {"image_send_mode": mode}})
