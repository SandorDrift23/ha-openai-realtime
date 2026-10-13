#!/usr/bin/with-contenv bashio
set -e

# Everyday options (always present in options:)
OPENAI_API_KEY=$(bashio::config 'openai_api_key')
INSTRUCTIONS=$(bashio::config 'instructions')
TRANSCRIPTION_LANGUAGE=$(bashio::config 'transcription_language')
OPENAI_VOICE=$(bashio::config 'openai_voice')
FOLLOW_UP_LISTEN_SECONDS=$(bashio::config 'follow_up_listen_seconds')
ENABLE_WEB_SEARCH=$(bashio::config 'enable_web_search')

if [ -z "$OPENAI_API_KEY" ]; then
    bashio::log.error "OPENAI_API_KEY is required but not set"
    exit 1
fi

export OPENAI_API_KEY
export INSTRUCTIONS
export TRANSCRIPTION_LANGUAGE
export OPENAI_VOICE
export FOLLOW_UP_LISTEN_SECONDS
export ENABLE_WEB_SEARCH

# Optional fields have no defaults — bashio prints "null" when unset, so only
# export when the user revealed them via "Show unused optional…".
# Absent vars → main.py hardwired defaults (gpt_live, delays, planner, etc.).
_export_if_set() {
    local key="$1"
    local env_name="$2"
    if bashio::config.has_value "$key"; then
        local value
        value=$(bashio::config "$key")
        export "$env_name=$value"
    fi
}

_export_if_set 'openai_voice_custom' 'OPENAI_VOICE_CUSTOM'
_export_if_set 'voice_provider' 'VOICE_PROVIDER'
_export_if_set 'openai_model' 'OPENAI_MODEL'
_export_if_set 'openai_model_custom' 'OPENAI_MODEL_CUSTOM'
_export_if_set 'openai_speed' 'OPENAI_SPEED'
_export_if_set 'max_output_tokens' 'MAX_OUTPUT_TOKENS'
_export_if_set 'follow_up_open_delay_ms' 'FOLLOW_UP_OPEN_DELAY_MS'
_export_if_set 'wake_open_delay_ms' 'WAKE_OPEN_DELAY_MS'
_export_if_set 'vad_eagerness' 'VAD_EAGERNESS'
_export_if_set 'phase_idle_debounce_ms' 'PHASE_IDLE_DEBOUNCE_MS'
_export_if_set 'web_search_model' 'WEB_SEARCH_MODEL'
_export_if_set 'web_search_model_custom' 'WEB_SEARCH_MODEL_CUSTOM'
_export_if_set 'playback_prebuffer_ms' 'PLAYBACK_PREBUFFER_MS'
_export_if_set 'noise_reduction' 'NOISE_REDUCTION'
_export_if_set 'ha_mcp_url' 'HA_MCP_URL'
_export_if_set 'longlived_token' 'LONGLIVED_TOKEN'
_export_if_set 'mcp_tool_allowlist' 'MCP_TOOL_ALLOWLIST'
_export_if_set 'live_planner_model' 'LIVE_PLANNER_MODEL'
_export_if_set 'live_planner_model_custom' 'LIVE_PLANNER_MODEL_CUSTOM'
_export_if_set 'websocket_port' 'WEBSOCKET_PORT'
_export_if_set 'session_reuse_timeout_seconds' 'SESSION_REUSE_TIMEOUT_SECONDS'
_export_if_set 'max_context_messages' 'MAX_CONTEXT_MESSAGES'
_export_if_set 'transcription_model' 'TRANSCRIPTION_MODEL'
_export_if_set 'transcription_model_custom' 'TRANSCRIPTION_MODEL_CUSTOM'
_export_if_set 'turn_detection_type' 'TURN_DETECTION_TYPE'
_export_if_set 'vad_threshold' 'VAD_THRESHOLD'
_export_if_set 'vad_prefix_padding_ms' 'VAD_PREFIX_PADDING_MS'
_export_if_set 'vad_silence_duration_ms' 'VAD_SILENCE_DURATION_MS'
_export_if_set 'enable_recording' 'ENABLE_RECORDING'
_export_if_set 'enable_echo_probe' 'ENABLE_ECHO_PROBE'

# SUPERVISOR_TOKEN is provided by Home Assistant when homeassistant_api: true

export PYTHONUNBUFFERED=1
exec python3 -m app.main
