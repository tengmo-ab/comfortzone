"""API Client for Comfortzone Heat Pump."""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Optional, Sequence

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
        # Cache of "candidate tuple -> PropertyName the API actually accepted",
        # so a probed property is only discovered once per config entry.
        self._resolved_properties: dict[tuple[str, ...], str] = {}
        # Candidate sets where every name was rejected. Probing a set costs one
        # API write per name, so a sweep that found nothing is not repeated on
        # every subsequent user action — the user is pointed at the probe
        # script and the explicit property option instead.
        self._exhausted_probes: set[tuple[str, ...]] = set()
        # Which value form the API accepted, as an index into the per-option
        # tuple of forms (e.g. 0 = the string token "Low", 1 = the integer 1).
        # The pump takes one vocabulary for every mode, so the index resolved
        # for one option is reused for the rest.
        self._resolved_value_styles: dict[tuple[str, ...], int] = {}

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

    async def async_set_property(
        self, property_name: str, value: Any, *, attempts: Optional[int] = None
    ) -> bool:
        """Send a SetProperty command. Retries once after 60s on transient errors.

        Writes are queued (min 5s spacing) to avoid overloading the API.
        Pass ``attempts=1`` to disable the retry — used when probing whether a
        PropertyName exists at all, where waiting 60s per wrong guess would
        make the whole probe unusable.
        Returns True on success, False otherwise.
        """
        max_attempts = MAX_WRITE_ATTEMPTS if attempts is None else max(1, attempts)
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
                for attempt in range(1, max_attempts + 1):
                    log_prefix = f"[SetProperty {attempt}/{max_attempts}]"
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

                    if should_retry and attempt < max_attempts:
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

    def resolved_property(self, property_names: Sequence[str]) -> Optional[str]:
        """Return the PropertyName already proven to work for ``property_names``."""
        return self._resolved_properties.get(tuple(property_names))

    def probe_exhausted(self, property_names: Sequence[str]) -> bool:
        """True if every candidate in ``property_names`` was already rejected."""
        return tuple(property_names) in self._exhausted_probes

    def resolved_value_style(self, property_names: Sequence[str]) -> Optional[int]:
        """Return the index of the value form the API accepted, if known."""
        return self._resolved_value_styles.get(tuple(property_names))

    async def async_set_first_supported_combination(
        self, property_names: Sequence[str], values: Sequence[Any]
    ) -> Optional[tuple[str, Any]]:
        """Write using the first ``(PropertyName, value)`` pair the API accepts.

        Needed because the fan has *two* unknowns rather than one: which name
        writes it, and which value vocabulary it takes. The API answers a bad
        value with the same opaque error as a bad name, so neither can be
        inferred from a rejection — both have to be tried.

        ``values`` holds the forms for a single option, most-likely first.
        Once a pair works, both the name and the value form's index are
        remembered, so later writes go straight to the right combination.

        Returns the accepted ``(name, value)``, or ``None`` if all failed.
        """
        if not property_names or not values:
            return None

        cache_key = tuple(property_names)
        name = self._resolved_properties.get(cache_key)
        style = self._resolved_value_styles.get(cache_key)
        if name is not None and style is not None and style < len(values):
            value = values[style]
            if await self.async_set_property(name, value):
                return name, value
            return None

        if cache_key in self._exhausted_probes:
            _LOGGER.debug(
                "Skipping probe: every candidate in %s was already rejected",
                ", ".join(cache_key),
            )
            return None

        for candidate_name in property_names:
            for index, value in enumerate(values):
                if await self.async_set_property(candidate_name, value, attempts=1):
                    _LOGGER.info(
                        "Resolved Comfortzone write: property '%s' with value "
                        "form %r (index %d)",
                        candidate_name,
                        value,
                        index,
                    )
                    self._resolved_properties[cache_key] = candidate_name
                    self._resolved_value_styles[cache_key] = index
                    return candidate_name, value
                _LOGGER.debug(
                    "'%s' = %r rejected, trying next combination",
                    candidate_name,
                    value,
                )

        self._exhausted_probes.add(cache_key)
        _LOGGER.warning(
            "No combination of (%s) x (%s) was accepted by the API. Not probing "
            "again for this session. Run scripts/probe_loggamera_properties.py "
            "--probe-values to find what your pump accepts, then set the name "
            "via the integration options",
            ", ".join(cache_key),
            ", ".join(repr(v) for v in values),
        )
        return None

    async def async_set_first_supported_property(
        self, property_names: Sequence[str], value: Any
    ) -> Optional[str]:
        """Write ``value`` using the first PropertyName the API accepts.

        Loggamera publishes no list of writable properties, so for settings we
        only know from the device protocol — the fan speed in particular — we
        try a short list of plausible names once and remember which one stuck.
        Subsequent writes go straight to the resolved name.

        Returns the accepted PropertyName, or ``None`` if every candidate was
        rejected.
        """
        if not property_names:
            return None

        cache_key = tuple(property_names)
        known = self._resolved_properties.get(cache_key)
        if known is not None:
            return known if await self.async_set_property(known, value) else None

        if cache_key in self._exhausted_probes:
            _LOGGER.debug(
                "Skipping probe: every candidate in %s was already rejected",
                ", ".join(cache_key),
            )
            return None

        for name in property_names:
            # Single attempt per candidate: a name the API doesn't know comes
            # back as a 4xx straight away, and we want to move on immediately.
            if await self.async_set_property(name, value, attempts=1):
                _LOGGER.info(
                    "Resolved writable Comfortzone property '%s' (from %d candidates)",
                    name,
                    len(cache_key),
                )
                self._resolved_properties[cache_key] = name
                return name
            _LOGGER.debug("PropertyName '%s' rejected, trying next candidate", name)

        self._exhausted_probes.add(cache_key)
        _LOGGER.warning(
            "None of the candidate property names (%s) were accepted by the API. "
            "Not probing again for this session. Run "
            "scripts/probe_loggamera_properties.py --probe-write to find the "
            "name your pump uses, then set it in the integration options",
            ", ".join(cache_key),
        )
        return None
