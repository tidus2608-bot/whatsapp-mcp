"""Calls from the MCP server to the bridge's REST API (no bridge needed: requests.post is faked)."""
import os

import pytest

import whatsapp


class FakeResponse:
    def __init__(self, status_code, body):
        self.status_code = status_code
        self._body = body
        self.text = str(body)

    def json(self):
        if not isinstance(self._body, dict):
            raise ValueError("not JSON")
        return self._body


@pytest.fixture
def posts(monkeypatch):
    """Record every request to the bridge and answer with success."""
    calls = []

    def fake_post(url, json=None, headers=None, **kwargs):
        calls.append({"url": url, "json": json, "headers": headers})
        return FakeResponse(200, {"success": True, "message": "ok", "path": "/x/file.jpg"})

    monkeypatch.setattr(whatsapp.requests, "post", fake_post)
    return calls


def test_requests_carry_token_from_bridge_file(bridge_files, posts):
    assert whatsapp.send_message("447700900001", "hi") == (True, "ok")
    assert posts[0]["headers"] == {"Authorization": "Bearer file-token"}
    assert posts[0]["url"].startswith("http://127.0.0.1:8080/")


def test_token_environment_variable_takes_precedence(bridge_files, posts, monkeypatch):
    monkeypatch.setenv("WHATSAPP_API_TOKEN", "env-token")
    whatsapp.send_message("447700900001", "hi")
    assert posts[0]["headers"] == {"Authorization": "Bearer env-token"}


def test_missing_token_is_reported_without_calling_bridge(bridge_files, posts):
    token_path, _ = bridge_files
    token_path.unlink()
    success, message = whatsapp.send_message("447700900001", "hi")
    assert not success
    assert "API token not found" in message
    assert posts == []


def test_audio_is_converted_into_outbox_and_cleaned_up(bridge_files, posts, tmp_path, monkeypatch):
    _, outbox = bridge_files
    source = tmp_path / "note.mp3"
    source.write_bytes(b"mp3")

    def fake_convert(input_file, output_dir=None, **kwargs):
        converted = os.path.join(output_dir, "converted.ogg")
        with open(converted, "wb") as f:
            f.write(b"ogg")
        return converted

    monkeypatch.setattr(whatsapp.audio, "convert_to_opus_ogg_temp", fake_convert)

    assert whatsapp.send_audio_message("447700900001", str(source)) == (True, "ok")
    sent_path = posts[0]["json"]["media_path"]
    assert os.path.dirname(sent_path) == str(outbox)
    assert not os.path.exists(sent_path), "converted copy should be removed after sending"


def test_bridge_refusal_is_passed_on(bridge_files, monkeypatch, tmp_path):
    media = tmp_path / "secret.txt"
    media.write_text("x")
    monkeypatch.setattr(
        whatsapp.requests, "post",
        lambda *a, **k: FakeResponse(403, {"success": False, "message": "media file must be inside one of these folders: /outbox"}),
    )
    success, message = whatsapp.send_file("447700900001", str(media))
    assert not success
    assert "must be inside one of these folders" in message


def test_download_media_returns_path(bridge_files, posts):
    assert whatsapp.download_media("m1", "447700900001@s.whatsapp.net") == (True, "ok", "/x/file.jpg")


def test_download_media_reports_bridge_reason(bridge_files, monkeypatch):
    monkeypatch.setattr(
        whatsapp.requests, "post",
        lambda *a, **k: FakeResponse(500, {"success": False, "message": "Failed to download media: not a media message"}),
    )
    assert whatsapp.download_media("m1", "chat") == (False, "Failed to download media: not a media message", None)


def test_download_media_reports_plain_text_errors(bridge_files, monkeypatch):
    monkeypatch.setattr(whatsapp.requests, "post", lambda *a, **k: FakeResponse(401, "Unauthorized"))
    success, message, path = whatsapp.download_media("m1", "chat")
    assert not success and "401" in message and "Unauthorized" in message and path is None
