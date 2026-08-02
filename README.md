# Realtime Trains API for Home Assistant

This Home Assistant integration provides detailed live train departures, journey stats, delay tracking, and cancellation detection using the UK railway data system.

> **UPDATE (June 2026):** This repository is a heavily modified and modernized fork of the original `realtime_trains_api` created by [megakid](https://github.com/megakid). The code has been completely refactored (with some heavy lifting from AI!) to support the modern `data.rtt.io` v2 API token authentication, rate limit tracking, and advanced schedule logic. 

This integration is made possible by the brilliant Realtime Trains API ([data.rtt.io](https://data.rtt.io) / [realtimetrains.co.uk](https://www.realtimetrains.co.uk)), maintained by Tom Cairns under swlines Ltd ([@swlines](https://twitter.com/swlines)).

---

## 🌟 Features

Unlike the built-in `uk_transport` integration, this custom component provides deep journey details:
* **Live Status:** Tracks delays and catches hidden partial cancellations natively.
* **Journey Data:** Calculates journey time, arrival times, and tracks stops of interest.
* **Rate Limit Tracking:** Automatically pulls server-side rate limits (minute/hour/day/week) into a dedicated API counter sensor to prevent you from being blocked.
* **Quiet Hours:** Define periods (e.g., overnight) where API polling slows down to save your daily quota.
* **Time Offsets:** Ignore trains leaving in the next few minutes if you still need time to walk to the station.

---

## 📊 Example Output

The sensor state represents the minutes until the next departure. The attributes contain rich journey data:

```yaml
journey_start: CBG
journey_end: LST
next_trains:
  - origin_name: Cambridge
    destination_name: London Liverpool Street
    service_uid: P76252
    scheduled: 30-07-2026 07:38
    estimated: 30-07-2026 07:53
    delay: 15
    minutes: 7
    platform: '1'
    operator_name: Greater Anglia
    is_origin_cancelled: false
    stops_of_interest: []
    scheduled_arrival: 30-07-2026 08:45
    estimate_arrival: 30-07-2026 08:45
    arrival_delay: 0
    journey_time_mins: 52
    stops: 4
    status: Delayed
unit_of_measurement: min
icon: mdi:train
friendly_name: Next train from CBG to LST
```

*You will also get a standalone `sensor.rtt_api_lifetime_calls` entity that tracks your total hits and exposes your real-time server limits as attributes!*

---

## 🛠️ Installation & Getting your Token

1. Create an RTT unified login account and sign in to the Developer Portal at [api-portal.rtt.io](https://api-portal.rtt.io/).
2. On the **API Dashboard**, look under "Your current tokens" and click on your active token ID link. *(If you do not have one, click "Request new token" first)*.
3. On the **Token Details** page, scroll down to the "API Authorisation" section. 
4. Click the **Copy** button to grab the long token string (it begins with `eyJ...`). *This is the `api_token` you will paste into your Home Assistant configuration.*
5. In Home Assistant, navigate to **HACS > Integrations**.
6. Click the three dots in the top right and select **Custom repositories**.
7. Add `https://github.com/chrisgamblen/ha_realtime_trains_api` as an **Integration**.
8. Click **Download** on the Realtime Trains API repository.
9. Restart Home Assistant.

---

## ⚙️ Configuration

Add the following to your `configuration.yaml` file. 

```yaml
sensor:
  - platform: realtime_trains
    api_token: '[Your Long-Lived JWT Token Here]' # Highly recommended to use !secret
    scan_interval:
      seconds: 90 # Defaults to 60. Don't set too frequently to avoid hitting rate limits.
    auto_adjust_scans: true # Backs off polling to 30 mins if no departures are found
    timewindow: 120 # Search window in minutes (Default: 120)
    
    # Optional: Slow down polling during the night to save API calls
    quiet_start: '23:30:00'
    quiet_end: '06:00:00'
    quiet_interval:
      minutes: 60 

    queries:
      - origin: CBG # Start station CRS code
        destination: LST # End station CRS code
        
        # journey_data_for_next_X_trains is optional but highly recommended.
        # It queries individual train itineraries to calculate arrival times and delays.
        journey_data_for_next_X_trains: 4 
        
        stops_of_interest:
          - BIS # Adds specific timing data if the train calls at this intermediate station
          
      - origin: LST
        destination: CBG
        sensor_name: Train Home # Forces entity ID to 'sensor.train_home'
        time_offset:
          minutes: 15 # Hides departures leaving in less than 15 mins (useful for walk-time)
```
