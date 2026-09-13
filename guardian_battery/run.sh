#!/usr/bin/with-contenv bashio

export MQTT_HOST="$(bashio::services mqtt host)"
export MQTT_PORT="$(bashio::services mqtt port)"
export MQTT_USERNAME="$(bashio::services mqtt username)"
export MQTT_PASSWORD="$(bashio::services mqtt password)"
export GUARDIAN_RESEARCH_API_TOKEN="$(bashio::config 'research_api_token')"

exec python3 /app/main.py
