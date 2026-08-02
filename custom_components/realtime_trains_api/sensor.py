"""Support for UK train data provided by data.rtt.io."""
from __future__ import annotations

from datetime import datetime, timedelta
import logging
import pytz

import voluptuous as vol
from typing import cast

from homeassistant.components.sensor import PLATFORM_SCHEMA, SensorEntity, RestoreSensor
from homeassistant.const import (
    UnitOfTime,
    STATE_UNKNOWN,
    CONF_SCAN_INTERVAL,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
import homeassistant.helpers.config_validation as cv
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.typing import ConfigType, DiscoveryInfoType
from homeassistant.util import Throttle
import homeassistant.util.dt as dt_util

_LOGGER = logging.getLogger(__name__)

DEFAULT_TIMEOFFSET = timedelta(minutes=0)

ATTR_ATCOCODE = "atcocode"
ATTR_LOCALITY = "locality"
ATTR_REQUEST_TIME = "request_time"
ATTR_JOURNEY_START = "journey_start"
ATTR_JOURNEY_END = "journey_end"
ATTR_NEXT_TRAINS = "next_trains"
ATTR_AGGREGATE = "aggregate_data"

CONF_API_TOKEN = "api_token"
CONF_QUERIES = "queries"
CONF_AUTOADJUSTSCANS = "auto_adjust_scans"
CONF_TIMEWINDOW = "timewindow"

# Quiet Hours Constants
CONF_QUIET_START = "quiet_start"
CONF_QUIET_END = "quiet_end"
CONF_QUIET_INTERVAL = "quiet_interval"

CONF_START = "origin"
CONF_END = "destination"
CONF_JOURNEYDATA = "journey_data_for_next_X_trains"
CONF_SENSORNAME = "sensor_name"
CONF_TIMEOFFSET = "time_offset"
CONF_STOPS_OF_INTEREST = "stops_of_interest"

TIMEZONE = pytz.timezone('Europe/London')
STRFFORMAT = "%d-%m-%Y %H:%M"

_QUERY_SCHEME = vol.Schema(
    {
        vol.Optional(CONF_SENSORNAME): cv.string,
        vol.Required(CONF_START): cv.string,
        vol.Required(CONF_END): cv.string,
        vol.Optional(CONF_JOURNEYDATA, default=0): cv.positive_int,
        vol.Optional(CONF_TIMEOFFSET, default=DEFAULT_TIMEOFFSET):
            vol.All(cv.time_period, cv.positive_timedelta),
        vol.Optional(CONF_STOPS_OF_INTEREST): [cv.string],
    }
)

PLATFORM_SCHEMA = PLATFORM_SCHEMA.extend(
    {
        vol.Optional(CONF_AUTOADJUSTSCANS, default=False): cv.boolean,
        vol.Optional(CONF_TIMEWINDOW, default=120): cv.positive_int,
        vol.Optional(CONF_QUIET_START): cv.time,
        vol.Optional(CONF_QUIET_END): cv.time,
        vol.Optional(CONF_QUIET_INTERVAL, default=timedelta(minutes=60)): vol.All(cv.time_period, cv.positive_timedelta),
        vol.Required(CONF_API_TOKEN): cv.string,
        vol.Required(CONF_QUERIES): [_QUERY_SCHEME],
    }
)

class ApiTracker:
    """Class to strictly track API calls and server rate limits."""
    def __init__(self):
        self.count = 0
        self.rate_limits = {}
        self.update_callbacks = []

    def add_call(self, headers=None):
        self.count += 1
        
        # Scrape the RTT rate limit headers if they exist in the response
        if headers:
            dimensions = ["Minute", "Hour", "Day", "Week"]
            for dim in dimensions:
                limit_key = f"X-RateLimit-Limit-{dim}"
                remain_key = f"X-RateLimit-Remaining-{dim}"
                
                # aiohttp headers are case-insensitive, but we use get() to be safe
                limit_val = headers.get(limit_key)
                remain_val = headers.get(remain_key)
                
                if limit_val is not None and remain_val is not None:
                    dim_lower = dim.lower()
                    if dim_lower not in self.rate_limits:
                        self.rate_limits[dim_lower] = {}
                    self.rate_limits[dim_lower]["limit"] = int(limit_val)
                    self.rate_limits[dim_lower]["remaining"] = int(remain_val)

        for cb in self.update_callbacks:
            cb()


async def async_setup_platform(
    hass: HomeAssistant,
    config: ConfigType,
    async_add_entities: AddEntitiesCallback,
    discovery_info: DiscoveryInfoType | None = None,
) -> None:
    """Get the realtime_train sensor."""
    sensors_to_add = []
    interval = config[CONF_SCAN_INTERVAL]
    autoadjustscans = config[CONF_AUTOADJUSTSCANS]
    timewindow = config[CONF_TIMEWINDOW]
    
    quiet_start = config.get(CONF_QUIET_START)
    quiet_end = config.get(CONF_QUIET_END)
    quiet_interval = config.get(CONF_QUIET_INTERVAL)
    
    api_token = config[CONF_API_TOKEN]
    queries = config[CONF_QUERIES]

    client = async_get_clientsession(hass)

    # Initialize the shared API tracker and add its dedicated sensor
    api_tracker = ApiTracker()
    sensors_to_add.append(RealtimeTrainApiCounterSensor(api_tracker))

    for query in queries:
        sensor_name = query.get(CONF_SENSORNAME, None)
        journey_start = query.get(CONF_START)
        journey_end = query.get(CONF_END)
        journey_data_for_next_X_trains = query.get(CONF_JOURNEYDATA)
        timeoffset = query.get(CONF_TIMEOFFSET)
        stops_of_interest = query.get(CONF_STOPS_OF_INTEREST, [])
        sensor = RealtimeTrainLiveTrainTimeSensor(
                sensor_name,
                api_token,
                journey_start,
                journey_end,
                journey_data_for_next_X_trains,
                timeoffset,
                autoadjustscans,
                timewindow,
                quiet_start,
                quiet_end,
                quiet_interval,
                stops_of_interest,
                interval,
                api_tracker,
                client
            )
        sensors_to_add.append(sensor)

    async_add_entities(sensors_to_add, True)


class RealtimeTrainApiCounterSensor(RestoreSensor):
    """Standalone sensor to track total API calls over the lifetime of the integration."""
    _attr_icon = "mdi:api"
    _attr_name = "RTT API Lifetime Calls"
    _attr_native_unit_of_measurement = "calls"
    _attr_state_class = "total_increasing"
    _attr_should_poll = False
    _attr_unique_id = "rtt_api_lifetime_calls"

    def __init__(self, tracker):
        """Initialize the counter."""
        self._tracker = tracker
        self._tracker.update_callbacks.append(self.async_write_ha_state)

    async def async_added_to_hass(self):
        """Restore previous state on startup."""
        await super().async_added_to_hass()
        last_state = await self.async_get_last_sensor_data()
        if last_state and last_state.native_value is not None:
            try:
                self._tracker.count = int(last_state.native_value)
            except (ValueError, TypeError):
                pass

    @property
    def native_value(self):
        """Return the current count."""
        return self._tracker.count
        
    @property
    def extra_state_attributes(self):
        """Expose the server-side rate limits as attributes."""
        attrs = {}
        for dim, values in self._tracker.rate_limits.items():
            attrs[f"limit_{dim}"] = values.get("limit")
            attrs[f"remaining_{dim}"] = values.get("remaining")
        return attrs


class RealtimeTrainLiveTrainTimeSensor(SensorEntity):
    """
    Sensor that reads the rtt API.
    """

    TRANSPORT_API_URL_BASE = "https://data.rtt.io/gb-nr/"
    AUTH_URL = "https://data.rtt.io/api/get_access_token"
    _attr_icon = "mdi:train"

    def __init__(self, sensor_name, api_token, journey_start, journey_end,
                journey_data_for_next_X_trains, timeoffset, autoadjustscans, timewindow, 
                quiet_start, quiet_end, quiet_interval, stops_of_interest, interval, api_tracker, client):
        """Construct a live train time sensor."""

        default_sensor_name = (
            f"Next train from {journey_start} to {journey_end} ({timeoffset})" if (timeoffset.total_seconds() > 0)
            else f"Next train from {journey_start} to {journey_end}")

        self._journey_start = journey_start
        self._journey_end = journey_end
        self._journey_data_for_next_X_trains = journey_data_for_next_X_trains
        self._next_trains = []
        self._aggregate_data = {}
        self._data = {}
        self._refresh_token = api_token
        self._access_token = None
        self._access_token_expiry = None
        self._timeoffset = timeoffset
        self._autoadjustscans = autoadjustscans
        self._timewindow = timewindow
        
        self._quiet_start = quiet_start
        self._quiet_end = quiet_end
        self._quiet_interval = quiet_interval
        self._last_full_update = None
        
        self._api_tracker = api_tracker
        
        self._stops_of_interest = stops_of_interest
        self._interval = interval
        self._client = client

        self._name = default_sensor_name if sensor_name is None else sensor_name
        self._state = None

        self.async_update = self._async_update

    @property
    def native_unit_of_measurement(self):
        """Dynamically return the unit of measurement to prevent HA log errors for string states."""
        if isinstance(self._state, (int, float)):
            return UnitOfTime.MINUTES
        return None

    async def _ensure_access_token(self):
        """Exchange the refresh token for a short-lived access token."""
        now = cast(datetime, dt_util.now())
        if self._access_token and self._access_token_expiry and now < self._access_token_expiry:
            return

        headers = {
            "Authorization": f"Bearer {self._refresh_token}",
            "accept": "application/json",
            "Version": "latest"
        }
        
        auth_url_with_version = f"{self.AUTH_URL}?version=latest"
        
        async with self._client.get(auth_url_with_version, headers=headers) as response:
            self._api_tracker.add_call(response.headers)
            if response.status == 200:
                data = await response.json()
                self._access_token = data.get("accessToken", data.get("token"))
                
                expiry_str = data.get("validUntil")
                if expiry_str:
                    try:
                        self._access_token_expiry = dt_util.parse_datetime(expiry_str)
                    except Exception:
                        self._access_token_expiry = now + timedelta(minutes=55)
                else:
                    self._access_token_expiry = now + timedelta(minutes=55)
            elif response.status == 429:
                retry_after = response.headers.get("Retry-After", "unknown")
                _LOGGER.error("RTT API Rate limit exceeded! Retry after %s seconds.", retry_after)
            else:
                error_body = await response.text()
                _LOGGER.error("Failed to fetch access token from RTT API. HTTP %s. Response: %s", response.status, error_body)

    async def _async_update(self):
        """Get the latest live departure data for the specified stop."""
        now = cast(datetime, dt_util.now()).astimezone(TIMEZONE)
        
        # --- QUIET HOURS CHECK ---
        if self._quiet_start and self._quiet_end:
            current_time = now.time()
            if self._quiet_start <= self._quiet_end:
                is_quiet = self._quiet_start <= current_time <= self._quiet_end
            else:
                is_quiet = current_time >= self._quiet_start or current_time <= self._quiet_end
                
            if is_quiet:
                if self._last_full_update and (now - self._last_full_update) < self._quiet_interval:
                    return
        
        self._last_full_update = now
        # -------------------------

        await self._getdepartures_api_request()
        self._next_trains = []
        departureCount = 0

        nextDepartureEstimatedTs : (datetime | None) = None

        departures = [] if self._data == {} or self._data.get("services") is None else self._data["services"]

        for departure in departures:
            sched_meta = departure.get("scheduleMetadata", {})
            if not sched_meta.get("inPassengerService", False):
                continue

            temp_data = departure.get("temporalData", {}).get("departure", {})
            
            # Use advertised schedule, fallback to internal
            scheduled_str = temp_data.get("scheduleAdvertised") or temp_data.get("scheduleInternal")
            if not scheduled_str:
                continue
                
            scheduledTs = _parse_time(scheduled_str)
            if not scheduledTs:
                continue

            # Prioritize actual real-time, then forecast, then fallback to scheduled
            estimated_str = temp_data.get("realtimeActual") or temp_data.get("realtimeForecast") or scheduled_str
            estimatedTs = _parse_time(estimated_str)
            if not estimatedTs:
                estimatedTs = scheduledTs

            # Compare against estimated time, not scheduled time!
            if _delta_secs(estimatedTs, now) < self._timeoffset.total_seconds():
                continue

            if nextDepartureEstimatedTs is None:
                nextDepartureEstimatedTs = estimatedTs
            else:
                nextDepartureEstimatedTs = min(nextDepartureEstimatedTs, estimatedTs)

            departureCount += 1

            # Get the best available platform data
            loc_meta = departure.get("locationMetadata", {})
            platform_data = loc_meta.get("platform", {}) if isinstance(loc_meta.get("platform"), dict) else {}
            platform = platform_data.get("actual") or platform_data.get("forecast") or platform_data.get("planned")

            # Check for cancellations and delays at the origin station
            is_origin_cancelled = (
                departure.get("isCancelled", False) or 
                sched_meta.get("isCancelled", False) or 
                temp_data.get("isCancelled", False)
            )

            train = {
                    "origin_name": departure.get("origin", [{}])[0].get("location", {}).get("description", "Unknown"),
                    "destination_name": departure.get("destination", [{}])[0].get("location", {}).get("description", "Unknown"),
                    "service_uid": sched_meta.get("identity"),
                    "departure_date": sched_meta.get("departureDate"),
                    "scheduled": scheduledTs.strftime(STRFFORMAT),
                    "estimated": estimatedTs.strftime(STRFFORMAT),
                    "delay": _delta_secs(estimatedTs, scheduledTs) // 60,
                    "minutes": _delta_secs(estimatedTs, now) // 60,
                    "platform": platform,
                    "operator_name": sched_meta.get("operator", {}).get("name", "Unknown"),
                    "is_origin_cancelled": is_origin_cancelled
                }
            if departureCount > self._journey_data_for_next_X_trains:
                break
            
            await self._add_journey_data(train, scheduledTs, estimatedTs)
            self._next_trains.append(train)
            
        self._aggregate_data = await self._calculate_aggregates()

        if nextDepartureEstimatedTs is None:
            self._state = "No Departures"
        else:
            self._state = _delta_secs(nextDepartureEstimatedTs, now) // 60

        if self._autoadjustscans:
            if nextDepartureEstimatedTs is None:
                self.async_update = Throttle(timedelta(minutes=30))(self._async_update)
            else:
                self.async_update = self._async_update


    @property
    def name(self):
        """Return the name of the sensor."""
        return self._name

    @property
    def native_value(self):
        """Return the state of the sensor."""
        return self._state

    async def _getdepartures_api_request(self):
        """Perform an API request."""
        await self._ensure_access_token()
        if not self._access_token:
            self._state = "Token Error"
            return
            
        headers = {
            "Authorization": f"Bearer {self._access_token}",
            "accept": "application/json"
        }
        
        depsUrl = f"{self.TRANSPORT_API_URL_BASE}location"
        params = {
            "code": self._journey_start,
            "filterTo": self._journey_end,
            "detailed": "false",
            "timeWindow": str(self._timewindow)
        }
        
        async with self._client.get(depsUrl, headers=headers, params=params) as response:
            self._api_tracker.add_call(response.headers)
            if response.status == 200:
                self._data = await response.json()
            elif response.status == 429:
                retry_after = response.headers.get("Retry-After", "unknown")
                _LOGGER.warning("RTT API Rate limit exceeded! Retry after %s seconds.", retry_after)
                self._state = "Rate Limited"
            elif response.status in (401, 403):
                self._state = "Credentials invalid"
                _LOGGER.warning("Invalid credentials for RTT API (HTTP code %s)", response.status)
            else:
                error_body = await response.text()
                _LOGGER.warning("Invalid response from API (HTTP code %s). Response: %s", response.status, error_body)

    async def _add_journey_data(self, train, scheduled_departure, estimated_departure):
        """Perform an API request for specific train data."""
        if not train.get('service_uid') or not train.get('departure_date'):
            return

        await self._ensure_access_token()
        if not self._access_token:
            return
            
        headers = {
            "Authorization": f"Bearer {self._access_token}",
            "accept": "application/json"
        }
        
        params = {
            "identity": train['service_uid'],
            "departureDate": train['departure_date']
        }
        
        trainUrl = f"{self.TRANSPORT_API_URL_BASE}service"
        
        async with self._client.get(trainUrl, headers=headers, params=params) as response:
            self._api_tracker.add_call(response.headers)
            if response.status == 200:
                data = await response.json()
                service_data = data.get('service', {})
                stopsOfInterest = []
                stopCount = -1 
                found = False
                
                for stop in service_data.get('locations', []):
                    location_info = stop.get('location', {})
                    crs_codes = location_info.get('shortCodes', [])
                    stop_crs = crs_codes[0] if crs_codes else None
                    stop_desc = location_info.get('description', '')
                    
                    temp_data = stop.get('temporalData', {})
                    arr_data = temp_data.get('arrival', {})
                    dep_data = temp_data.get('departure', {})
                    
                    if stop_crs == self._journey_end and temp_data.get('displayAs') != 'ORIGIN':
                        sched_arr_str = arr_data.get('scheduleAdvertised') or arr_data.get('scheduleInternal') or dep_data.get('scheduleAdvertised') or dep_data.get('scheduleInternal')
                        
                        if not sched_arr_str: 
                            continue
                            
                        scheduled_arrival = _parse_time(sched_arr_str)
                        if not scheduled_arrival:
                            continue
                        
                        est_arr_str = arr_data.get('realtimeActual') or arr_data.get('realtimeForecast') or dep_data.get('realtimeActual') or dep_data.get('realtimeForecast') or sched_arr_str
                        estimated_arrival = _parse_time(est_arr_str)
                        if not estimated_arrival:
                            estimated_arrival = scheduled_arrival

                        arrival_delay = _delta_secs(estimated_arrival, scheduled_arrival) // 60

                        # Catch cancellation flags no matter where they hide
                        is_cancelled = (
                            stop.get('isCancelled', False) or 
                            arr_data.get('isCancelled', False) or 
                            dep_data.get('isCancelled', False) or
                            train.get('is_origin_cancelled', False)
                        )
                        
                        # Catch delays at EITHER the origin or the destination
                        is_delayed = (
                            arrival_delay > 0 or 
                            train.get('delay', 0) > 0
                        )

                        status = "OK"
                        if is_cancelled:
                            status = "Cancelled"
                        elif is_delayed:
                            status = "Delayed"

                        newtrain = {
                            "stops_of_interest": stopsOfInterest,
                            "scheduled_arrival": scheduled_arrival.strftime(STRFFORMAT),
                            "estimate_arrival": estimated_arrival.strftime(STRFFORMAT),
                            "arrival_delay": arrival_delay,
                            "journey_time_mins": _delta_secs(estimated_arrival, estimated_departure) // 60,
                            "stops": stopCount,
                            "status": status
                        }
                        train.update(newtrain)
                        found = True
                        break
                        
                    elif stop_crs in self._stops_of_interest and temp_data.get('displayAs') == 'CALL':
                        sched_stop_str = arr_data.get('scheduleAdvertised') or arr_data.get('scheduleInternal') or dep_data.get('scheduleAdvertised') or dep_data.get('scheduleInternal')
                        
                        if sched_stop_str:
                            scheduled_stop = _parse_time(sched_stop_str)
                            
                            est_stop_str = arr_data.get('realtimeActual') or arr_data.get('realtimeForecast') or dep_data.get('realtimeActual') or dep_data.get('realtimeForecast') or sched_stop_str
                            estimated_stop = _parse_time(est_stop_str)
                            if not estimated_stop:
                                estimated_stop = scheduled_stop
                            
                            stopsOfInterest.append(
                                {
                                    "stop": stop_crs,
                                    "name": stop_desc,
                                    "scheduled_stop": scheduled_stop.strftime(STRFFORMAT) if scheduled_stop else "-",
                                    "estimate_stop": estimated_stop.strftime(STRFFORMAT) if estimated_stop else "-",
                                    "stop_delay":  _delta_secs(estimated_stop, scheduled_stop) // 60 if estimated_stop and scheduled_stop else 0,
                                    "journey_time_mins": _delta_secs(estimated_stop, estimated_departure) // 60 if estimated_stop else 0,
                                    "stops": stopCount
                                }
                            )
                    stopCount += 1
                if not found:
                    _LOGGER.warning("Could not find %s in stops for service %s.", self._journey_end, train['service_uid'])
            elif response.status == 429:
                retry_after = response.headers.get("Retry-After", "unknown")
                _LOGGER.warning("RTT API Rate limit exceeded! Retry after %s seconds.", retry_after)
            else:
                error_body = await response.text()
                _LOGGER.warning("Could not populate arrival times for %s. HTTP %s. Response: %s", train['service_uid'], response.status, error_body)

    async def _calculate_aggregates(self):
        """Calculate aggregate delay and duration data."""
        departure_delays = []
        arrival_delays = []
        stop_delays = []
        durations = []
        agg_delays = {}
        for train in self._next_trains:
            if 'journey_time_mins' in train:
                durations.append(train['journey_time_mins'])
            if 'delay' in train and train['delay'] > 0:
                departure_delays.append(train['delay'])
            if 'arrival_delay' in train and train['arrival_delay'] > 0:
                arrival_delays.append(train['arrival_delay'])
            if 'stop_delay' in train and train['stop_delay'] > 0:
                stop_delays.append(train['stop_delay'])

        if len(arrival_delays):
            agg_delays['arrival'] = {
                'count': len(arrival_delays),
                'min': min(arrival_delays),
                'max': max(arrival_delays),
                'average': round( sum(arrival_delays) / len(arrival_delays) )
            }
        if len(departure_delays):
            agg_delays['departure'] = {
                'count': len(departure_delays),
                'min': min(departure_delays),
                'max': max(departure_delays),
                'average': round( sum(departure_delays) / len(departure_delays) )
            }
        if len(stop_delays):
            agg_delays['stop'] = {
                'count': len(stop_delays),
                'min': min(stop_delays),
                'max': max(stop_delays),
                'average': round( sum(stop_delays) / len(stop_delays) )
            }
        durations_data = None
        if durations:
            durations_data = {
                'count': len(durations),
                'min': min(durations),
                'max': max(durations),
                'average': round(sum(durations) / len(durations)),
            }

        return {
            'delays': agg_delays,
            'durations': durations_data,
        }

    @property
    def extra_state_attributes(self):
        """Return other details about the sensor state."""
        attrs = {}
        if self._data is not None:
            attrs[ATTR_JOURNEY_START] = self._journey_start
            attrs[ATTR_JOURNEY_END] = self._journey_end
            
            # Explicitly forces next_trains to always be included, even if empty
            attrs[ATTR_NEXT_TRAINS] = self._next_trains
            attrs[ATTR_AGGREGATE] = self._aggregate_data
            return attrs

def _delta_secs(hhmm_datetime_a : datetime, hhmm_datetime_b : datetime) -> float:
    """Calculate time delta in minutes."""
    return (hhmm_datetime_a - hhmm_datetime_b).total_seconds()

def _parse_time(time_str):
    """Safely parse a datetime string that may or may not have timezone info."""
    if not time_str:
        return None
    try:
        dt = datetime.fromisoformat(time_str)
        if dt.tzinfo is None:
            dt = TIMEZONE.localize(dt)
        return dt
    except Exception:
        return None
