"""Behavioural tests for the Comfortzone API client.

Covers the parts the user is most likely to break by accident:
- That a normal RawData response decodes cleanly.
- That ``"Result":"busy"`` and HTML maintenance pages don't crash polling.
- That ``async_set_property`` retries transient 5xx / timeout failures
  exactly once and never retries 4xx errors.
- That issuing several writes back-to-back never overlaps the underlying
  HTTP calls (the integration's main protection against overloading the
  Loggamera API).
"""
from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock

import aiohttp
import pytest

from custom_components.comfortzone.api import (
    ComfortzoneApiAuthError,
    ComfortzoneApiClient,
    ComfortzoneApiCommunicationError,
    MIN_WRITE_SPACING_SEC,
)


# --- Fixtures --------------------------------------------------------------


def _mock_response(
    status: int = 200,
    body: str = '{"Data":{"Result":true}}',
    content_type: str = "application/json",
) -> MagicMock:
    """Construct a fake aiohttp response object."""
    resp = MagicMock()
    resp.status = status
    resp.content_type = content_type
    resp.text = AsyncMock(return_value=body)
    if content_type == "application/json":
        try:
            payload = json.loads(body)
        except (ValueError, TypeError):
            resp.json = AsyncMock(side_effect=ValueError("bad JSON"))
        else:
            resp.json = AsyncMock(return_value=payload)
    else:
        async def _raise(*args, **kwargs):
            raise ValueError("not JSON")
        resp.json = AsyncMock(side_effect=_raise)
    if not (200 <= status < 300):
        request_info = MagicMock()
        history = ()
        resp.raise_for_status = MagicMock(
            side_effect=aiohttp.ClientResponseError(
                request_info=request_info,
                history=history,
                status=status,
                message=f"HTTP {status}",
            )
        )
    else:
        resp.raise_for_status = MagicMock()
    return resp


@pytest.fixture
def client_and_session():
    """Yield an API client wired up to a brand-new AsyncMock session."""
    session = MagicMock()
    session.post = AsyncMock()
    client = ComfortzoneApiClient(api_key="dummy", device_id=1, session=session)
    return client, session


@pytest.fixture
def instant_sleep(monkeypatch):
    """Skip real waits in retry / spacing logic so tests don't drag."""
    async def _instant(_delay, *args, **kwargs):
        return None
    monkeypatch.setattr(asyncio, "sleep", _instant)


@pytest.fixture
def auto_advance_time(monkeypatch):
    """Make ``time.time()`` jump 10 s every call so write spacing is satisfied."""
    state = {"now": 1_000_000.0}
    def fake_time():
        state["now"] += 10.0
        return state["now"]
    import custom_components.comfortzone.api as api_mod
    monkeypatch.setattr(api_mod.time, "time", fake_time)
    return state


# --- async_get_data --------------------------------------------------------


@pytest.mark.asyncio
async def test_get_data_parses_raw_data_response(client_and_session):
    """A normal Loggamera RawData response decodes into a dict the coordinator can use."""
    client, session = client_and_session
    body = json.dumps(
        {
            "Data": {
                "LogDateTimeUtc": "2026-05-07T12:00:00Z",
                "Values": [{"ClearTextName": "Indoor temp (TE3)", "Value": "21.7"}],
            }
        }
    )
    session.post.return_value = _mock_response(body=body)

    result = await client.async_get_data()

    assert result is not None
    values = result["Data"]["Values"]
    assert values[0]["Value"] == "21.7"
    session.post.assert_awaited_once()


@pytest.mark.asyncio
async def test_get_data_handles_busy_html_response(client_and_session):
    """HTML-wrapped 'busy' replies are non-fatal — the coordinator keeps the old data."""
    client, session = client_and_session
    session.post.return_value = _mock_response(
        body='<html>"Result":"busy"</html>',
        content_type="text/html",
    )

    result = await client.async_get_data()

    assert result is None  # caller treats this as 'use last cached value'


@pytest.mark.asyncio
async def test_get_data_raises_auth_error_on_authentication_failure(client_and_session):
    """A JSON 'Error' mentioning authentication must surface as ComfortzoneApiAuthError."""
    client, session = client_and_session
    body = json.dumps({"Error": "Authentication failed"})
    session.post.return_value = _mock_response(body=body)

    with pytest.raises(ComfortzoneApiAuthError):
        await client.async_get_data()


# --- async_set_property: success / no-retry paths -------------------------


@pytest.mark.asyncio
async def test_set_property_succeeds_on_2xx(
    client_and_session, instant_sleep, auto_advance_time
):
    """A 2xx with Data.Result=true returns True immediately."""
    client, session = client_and_session
    session.post.return_value = _mock_response(
        status=200, body='{"Data":{"Result":true}}'
    )

    ok = await client.async_set_property("SetIndoorTemp", 22.0)

    assert ok is True
    assert session.post.await_count == 1


@pytest.mark.asyncio
async def test_set_property_returns_false_on_explicit_result_false(
    client_and_session, instant_sleep, auto_advance_time
):
    """If the API answers with ``Data.Result = false`` we must NOT retry."""
    client, session = client_and_session
    session.post.return_value = _mock_response(
        status=200, body='{"Data":{"Result":false}}'
    )

    ok = await client.async_set_property("SetIndoorTemp", 22.0)

    assert ok is False
    assert session.post.await_count == 1


@pytest.mark.asyncio
async def test_set_property_does_not_retry_on_4xx(
    client_and_session, instant_sleep, auto_advance_time
):
    """4xx is treated as a permanent client error; no retry should happen."""
    client, session = client_and_session
    session.post.return_value = _mock_response(status=400, body="bad request")

    ok = await client.async_set_property("SetIndoorTemp", 22.0)

    assert ok is False
    assert session.post.await_count == 1


# --- async_set_property: retry paths --------------------------------------


@pytest.mark.asyncio
async def test_set_property_retries_once_on_5xx(
    client_and_session, instant_sleep, auto_advance_time
):
    """A 5xx error must retry exactly once before giving up."""
    client, session = client_and_session
    session.post.side_effect = [
        _mock_response(status=503, body=""),
        _mock_response(status=200, body='{"Data":{"Result":true}}'),
    ]

    ok = await client.async_set_property("SetIndoorTemp", 22.0)

    assert ok is True
    assert session.post.await_count == 2


@pytest.mark.asyncio
async def test_set_property_retries_on_timeout(
    client_and_session, instant_sleep, auto_advance_time
):
    """Network timeouts are transient — retry once and accept the second answer."""
    client, session = client_and_session

    async def post_responses(*args, **kwargs):
        if not hasattr(post_responses, "called"):
            post_responses.called = True
            raise asyncio.TimeoutError()
        return _mock_response(status=200, body='{"Data":{"Result":true}}')

    session.post.side_effect = post_responses

    ok = await client.async_set_property("SetIndoorTemp", 22.0)

    assert ok is True
    assert session.post.await_count == 2


# --- Concurrency / spacing ------------------------------------------------


@pytest.mark.asyncio
async def test_multiple_consecutive_writes_serialise(
    client_and_session, instant_sleep, auto_advance_time
):
    """Three concurrent writes must all complete without overlapping HTTP calls.

    The internal write lock plus 5 s spacing should keep every POST sequential
    no matter how many tasks the integration / controller fires off at once.
    """
    client, session = client_and_session
    session.post.return_value = _mock_response(
        status=200, body='{"Data":{"Result":true}}'
    )

    # Track concurrent in-flight POSTs to verify mutual exclusion
    in_flight = 0
    max_concurrent = 0

    original_post = session.post.return_value

    async def tracking_post(*args, **kwargs):
        nonlocal in_flight, max_concurrent
        in_flight += 1
        max_concurrent = max(max_concurrent, in_flight)
        try:
            await asyncio.sleep(0)  # yield to the event loop
            return original_post
        finally:
            in_flight -= 1

    session.post.side_effect = tracking_post

    results = await asyncio.gather(
        client.async_set_property("SetIndoorTemp", 21.0),
        client.async_set_property("SetIndoorTemp", 21.5),
        client.async_set_property("SetIndoorTemp", 22.0),
    )

    assert results == [True, True, True]
    assert session.post.await_count == 3
    assert max_concurrent == 1  # the lock kept POSTs serialised


@pytest.mark.asyncio
async def test_write_spacing_inserts_sleep_when_calls_too_close(
    client_and_session, monkeypatch
):
    """When two writes happen back-to-back the spacing logic must call asyncio.sleep
    with a delay close to ``MIN_WRITE_SPACING_SEC``."""
    client, session = client_and_session
    session.post.return_value = _mock_response(
        status=200, body='{"Data":{"Result":true}}'
    )

    # Time freezes so the second write sees zero elapsed time since the first.
    import custom_components.comfortzone.api as api_mod
    monkeypatch.setattr(api_mod.time, "time", lambda: 1_000_000.0)

    sleep_calls: list[float] = []
    async def recording_sleep(delay, *args, **kwargs):
        sleep_calls.append(delay)
    monkeypatch.setattr(asyncio, "sleep", recording_sleep)

    await client.async_set_property("SetIndoorTemp", 21.0)
    await client.async_set_property("SetIndoorTemp", 21.5)

    # The first call sees elapsed > MIN_WRITE_SPACING_SEC (because
    # _last_write_time is 0). The second call sees elapsed == 0 → must sleep.
    spacing_sleeps = [d for d in sleep_calls if abs(d - MIN_WRITE_SPACING_SEC) < 0.01]
    assert spacing_sleeps, (
        f"Expected a sleep close to {MIN_WRITE_SPACING_SEC}s but got {sleep_calls}"
    )
