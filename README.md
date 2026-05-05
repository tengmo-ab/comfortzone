# Comfortzone Heat Pump - Home Assistant Integration

This custom component integrates the Comfortzone exhaust air heat pump into Home Assistant via the Loggamera API. It allows you to monitor and control your heat pump seamlessly, managing hot water and climate heating dynamically.

## Features
- **Climate Entity:** Control indoor temperature, monitor current temperatures.
- **Water Heater & Climate Control:** Advanced monitoring and tweaking of your Comfortzone heat pump.
- **Sensors:** Comprehensive set of sensors including indoor/outdoor temperatures, compressor power, hot water temp, etc.
- **Binary Sensors:** Alarms and active components like compressor and exchange valves.
- **Smart Queuing:** Built-in API request queuing handles delays automatically for stable communication when performing multiple changes.
- **Model Selection:** Set up and change your specific heat pump model (e.g. RX95) seamlessly via the Home Assistant UI.

## Installation

### HACS (Recommended)
1. Open HACS in your Home Assistant instance.
2. Go to "Integrations" -> Click the three dots in the top right corner -> "Custom repositories".
3. Add `https://github.com/tenganmade/comfortzone` and select "Integration" as the category.
4. Click "Download" to download the integration.
5. Restart Home Assistant.

### Manual Installation
1. Download the `custom_components/comfortzone` directory from this repository.
2. Place it in your Home Assistant's `config/custom_components` directory.
3. Restart Home Assistant.

## Configuration
This integration supports setup via the Home Assistant UI.

1. Go to **Settings** -> **Devices & Services**.
2. Click **Add Integration** and search for "Comfortzone Heat Pump".
3. Select your model and enter your **Loggamera API Key** and **Device ID**.
4. Submit to complete the setup.

Enjoy complete control over your Comfortzone!
