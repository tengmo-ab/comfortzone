"""API Client for Comfortzone Heat Pump."""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Optional

import aiohttp

# Re-export the pure helper so existing imports keep working.
from .calculations import find_value_from_raw_data  # noqa: F401
from .const import API_ENDPOINT, API_ENDPOINT_SET

_LOGGER = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 30
DEFAULT_POLL_TIMEOUT = 20
DEFAULT_HEADERS = {"Content-Type": "application/json"}
MIN_WRITE_SPACING_SEC = 5.0
RETRY_DELAY_SEC = 60
MAX_WRITE_ATTEMPTS = 2


class ComfortzoneApiClientError(Exception):
    """Base exception for the Comfortzone API client."""


class ComfortzoneApiCommunicationError(ComfortzoneApiClientError):
    """Network / transport-level error."""


class ComfortzoneApiAuthError(ComfortzoneApiClientError):
    """Authentication error from the API."""


class ComfortzoneApiCommandError(ComfortzoneApiClientError):
    """The API explicitly rejected the command."""


class ComfortzoneApiClient:
    """API Client to interact with the Loggamera platform for Comfortzone."""

    def __init__(
        self,
        api_key: str,
        device_id: int,
        session: aiohttp.ClientSession,
    ) -> None:
        """Initialize the API client."""
        self._api_key = api_key
        self._device_id = device_id
        self._session = session
        self._write_lock = asyncio.Lock()
        self._last_write_time = 0.0

    async def async_get_data(self) -> Optional[dict[str, Any]]:
        """Fetch data from the RawData endpoint. Returns None when API is busy."""
        payload = {"ApiKey": self._api_key, "DeviceId": self._device_id}
        url = API_ENDPOINT
        _LOGGER.debug("[GetData] Requesting data from %s", url)
        try:
            async with asyncio.timeout(DEFAULT_POLL_TIMEOUT):
                response = await self._session.post(url, headers=DEFAULT_HEADERS, json=payload)

            response_text = await response.text()
            _LOGGER.debug(
                "[GetData] HTTP %s, content-type=%s", response.status, response.content_type
            )

            if not (200 <= response.status < 300):
                _LOGGER.warning(
                    "[GetData] HTTP %s. Response: %s", response.status, response_text[:500]
                )
                response.raise_for_status()

            if response.content_type == "text/html":
                if '"Result":"busy"' in response_text:
                    _LOGGER.warning("[GetData] API busy (HTML-wrapped). Skipping update.")
                    return None
                _LOGGER.warning(
                    "[GetData] API returned HTML (maintenance?). First 500 chars: %s",
                    response_text[:500],
                )
                raise ComfortzoneApiCommunicationError(
                    "API returned HTML instead of JSON (likely maintenance)."
                )

            try:
                json_data = await response.json(content_type=None)
            except (aiohttp.ContentTypeError, ValueError) as json_err:
                _LOGGER.error(
                    "[GetData] Failed to decode JSON. Status=%s, ct=%s, body=%s",
                    response.status,
                    response.content_type,
                    response_text[:500],
                )
                raise ComfortzoneApiCommunicationError(
                    f"Failed to decode API JSON response: {json_err}"
                ) from json_err

            if not isinstance(json_data, dict):
                raise ComfortzoneApiCommunicationError(
                    f"Unexpected JSON top-level type: {type(json_data).__name__}"
                )

            if json_data.get("Error"):
                error_msg = json_data["Error"]
                if "authentication" in str(error_msg).lower():
                    raise ComfortzoneApiAuthError(f"Authentication failed: {error_msg}")
                raise ComfortzoneApiCommunicationError(
                    f"API returned an error message: {error_msg}"
                )

            data_block = json_data.get("Data")
            if not isinstance(data_block, dict) or not isinstance(data_block.get("Values"), list):
                raise ComfortzoneApiCommunicationError(
                    f"Unexpected JSON format: missing Data.Values list. Response: {response_text[:500]}"
                )

            return json_data

        except asyncio.TimeoutError as err:
            _LOGGER.warning("[GetData] Timeout: %s", err)
            raise ComfortzoneApiCommunicationError("Timeout contacting API for status") from err
        except aiohttp.ClientError as err:
            _LOGGER.warning("[GetData] Client error: %s", err)
            raise ComfortzoneApiCommunicationError(
                f"Error communicating with API: {err}"
            ) from err
        except (ComfortzoneApiAuthError, ComfortzoneApiCommunicationError):
            raise
        except Exception as err:
            _LOGGER.exception("[GetData] Unexpected error: %s", err)
            raise ComfortzoneApiClientError(
                f"An unexpected error occurred fetching status: {err}"
            ) from err

    async def async_set_property(self, property_name: str, value: Any) -> bool:
        """Send a SetProperty command. Retries once after 60s on transient errors.

        Writes are queued (min 5s spacing) to avoid overloading the API.
        Returns True on success, False otherwise.
        """
        async with self._write_lock:
            elapsed = time.time() - self._last_write_time
            if elapsed < MIN_WRITE_SPACING_SEC:
                wait = MIN_WRITE_SPACING_SEC - elapsed
                _LOGGER.debug("Queuing write '%s'. Waiting %.1fs", property_name, wait)
                await asyncio.sleep(wait)

            payload = {
                "ApiKey": self._api_key,
                "DeviceId": self._device_id,
                "PropertyName": property_name,
                "Value": value,
            }

            try:
                for attempt in range(1, MAX_WRITE_ATTEMPTS + 1):
                    log_prefix = f"[SetProperty {attempt}/{MAX_WRITE_ATTEMPTS}]"
                    _LOGGER.debug(
                        "%s Setting '%s' to '%s'", log_prefix, property_name, value
                    )

                    should_retry = False
                    try:
                        async with asyncio.timeout(DEFAULT_TIMEOUT):
                            response = await self._session.post(
                                API_ENDPOINT_SET, headers=DEFAULT_HEADERS, json=payload
                            )

                        response_text = await response.text()
                        _LOGGER.debug(
                            "%s HTTP %s body=%s", log_prefix, response.status, response_text[:300]
                        )

                        if 200 <= response.status < 300:
                            try:
                                json_response = await response.json(content_type=None)
                            except (aiohttp.ContentTypeError, ValueError):
                                _LOGGER.info(
                                    "%s OK '%s' (non-JSON 2xx, assumed success)",
                                    log_prefix,
                                    property_name,
                                )
                                return True

                            if isinstance(json_response, dict) and json_response.get("Error"):
                                _LOGGER.error(
                                    "%s API error: %s", log_prefix, json_response["Error"]
                                )
                                return False
                            data_dict = (
                                json_response.get("Data")
                                if isinstance(json_response, dict)
                                else None
                            )
                            if isinstance(data_dict, dict):
                                result = data_dict.get("Result")
                                if result is False:
                                    _LOGGER.error("%s Data.Result = false", log_prefix)
                                    return False
                                if result is True:
                                    _LOGGER.info(
                                        "%s OK '%s' (Result: true)", log_prefix, property_name
                                    )
                                    return True
                            _LOGGER.info(
                                "%s OK '%s' (2xx, no explicit Result)", log_prefix, property_name
                            )
                            return True

                        if 400 <= response.status < 500:
                            _LOGGER.error(
                                "%s Client error HTTP %s. Check property/value.",
                                log_prefix,
                                response.status,
                            )
                            return False

                        if 500 <= response.status < 600:
                            _LOGGER.warning("%s Server HTTP %s", log_prefix, response.status)
                            should_retry = True
                        else:
                            _LOGGER.error("%s Unexpected HTTP %s", log_prefix, response.status)
                            return False

                    except asyncio.TimeoutError:
                        _LOGGER.warning(
                            "%s Timeout (%ss) setting '%s'",
                            log_prefix,
                            DEFAULT_TIMEOUT,
                            property_name,
                        )
                        should_retry = True
                    except aiohttp.ClientError as err:
                        _LOGGER.warning(
                            "%s Communication error setting '%s': %s",
                            log_prefix,
                            property_name,
                            err,
                        )
                        should_retry = True
                    except Exception as err:
                        _LOGGER.exception(
                            "%s Unexpected error setting '%s': %s",
                            log_prefix,
                            property_name,
                            err,
                        )
                        return False

                    if should_retry and attempt < MAX_WRITE_ATTEMPTS:
                        _LOGGER.info("Waiting %ss before retry...", RETRY_DELAY_SEC)
                        await asyncio.sleep(RETRY_DELAY_SEC)
                        continue
                    if should_retry:
                        _LOGGER.error("%s Final attempt failed. Giving up.", log_prefix)
                        return False
                    return False

                return False
            finally:
                self._last_write_time = time.time()
