# Stream Resilience & Idle State Implementation

## Overview

This document describes the stream resilience feature implemented for the Pi-Decoder application. The feature makes the pi-decoder resilient to encoder outages by automatically retrying stream connections and exposing idle state via HTTP for Bitfocus Companion integration.

---

## Problem Solved

**Before this change:**
- If the encoder wasn't running when the pi-decoder started, mpv would sit idle forever
- If the encoder went offline mid-stream, mpv would try FFmpeg-level reconnects for ~30 seconds, then go idle permanently
- Manual intervention (restart via web UI or reboot) was required to recover
- No clean way for Bitfocus Companion to detect "stream down" state

**After this change:**
- Pi-Decoder automatically retries loading the stream every 5-60 seconds when idle
- When encoder comes back online, video resumes automatically (within 60 seconds max)
- Clean black screen during retries (no flashing or error messages)
- `/api/status` endpoint exposes `mpv.idle` boolean for Bitfocus Companion

---

## Files Modified

### 1. `/src/pi_decoder/mpv_manager.py`

#### Changes Made:

**Added import:**
```python
import time
```

**Added state variables in `__init__`:**
```python
self._stream_retry_backoff = 5.0  # Start at 5 seconds
self._last_stream_attempt = 0.0
```

**Added mpv command-line options in `start()` for clean idle appearance:**
```python
"--background=color",
"--background-color=0/0/0",  # Pure black when idle
"--osd-msg1=",  # No OSD messages
```

**Added `reset_stream_retry()` method:**
```python
def reset_stream_retry(self) -> None:
    """Reset retry backoff when stream URL is changed."""
    self._stream_retry_backoff = 5.0
    self._last_stream_attempt = 0.0
```

**Extended `_health_loop()` with stream health monitoring:**
```python
# Stream health check: auto-retry if idle but we have a URL configured
try:
    status = await self.get_status()
    if status.get("idle") and self._config.stream.url:
        # Stream not playing but we have a URL configured
        now = time.monotonic()
        if now - self._last_stream_attempt > self._stream_retry_backoff:
            log.info("Stream idle, attempting to reload: %s", self._config.stream.url)
            self._last_stream_attempt = now
            try:
                await self.load_stream(self._config.stream.url)
                # Increase backoff for next attempt (max 60s)
                self._stream_retry_backoff = min(self._stream_retry_backoff * 1.5, 60.0)
            except Exception:
                log.debug("Stream reload failed, will retry", exc_info=True)
    else:
        # Stream is playing, reset backoff
        self._stream_retry_backoff = 5.0
except Exception:
    log.debug("Stream health check failed", exc_info=True)
```

### 2. `/src/pi_decoder/web/app.py`

#### Changes Made:

**Added retry reset in `api_config_stream` endpoint (line ~130):**
```python
# Reset retry backoff when URL changes so it tries immediately
mpv.reset_stream_retry()
```

---

## Behavior Matrix

| Scenario | Previous Behavior | New Behavior |
|----------|-------------------|--------------|
| Pi-Decoder starts, encoder offline | mpv idle, black screen, stuck forever | Black screen, retries every 5→60s |
| Encoder goes offline mid-stream | FFmpeg reconnect for ~30s, then idle forever | Same FFmpeg reconnect, then app-level retry kicks in |
| Encoder comes back online | Required manual restart | Auto-reconnects within 60s max |
| Visual appearance during retry | N/A | Clean black screen, no OSD messages |
| Stream URL changed via web UI | Would wait for next health check | Immediately resets backoff, tries on next 5s cycle |

---

## Retry Backoff Algorithm

1. Initial backoff: **5 seconds**
2. After each failed attempt: multiply by **1.5**
3. Maximum backoff: **60 seconds**
4. Reset to 5 seconds when:
   - Stream starts playing successfully
   - Stream URL is changed via web API

**Example progression:**
- Attempt 1: wait 5s
- Attempt 2: wait 7.5s
- Attempt 3: wait 11.25s
- Attempt 4: wait 16.9s
- Attempt 5: wait 25.3s
- Attempt 6: wait 38s
- Attempt 7+: wait 60s (capped)

---

## API Reference

### GET `/api/status`

Returns current system status including stream state.

**Response:**
```json
{
  "mpv": {
    "alive": true,
    "paused": false,
    "idle": false,
    "playing": true,
    "stream_url": "rtmp://encoder.local/live/stream"
  },
  "overlay": {
    "enabled": true,
    "is_live": true,
    "countdown": "01:23:45",
    "message": "Service in progress"
  },
  "system": {
    "cpu_percent": 12.5,
    "memory_percent": 45.2,
    "temperature": 52.3,
    "uptime": "2d 5h"
  }
}
```

**Key fields for Bitfocus Companion:**
- `mpv.idle`: `true` = no stream playing, `false` = stream active
- `mpv.playing`: `true` = actively playing video, `false` = paused or idle
- `mpv.alive`: `true` = mpv process running, `false` = mpv crashed

---

## Bitfocus Companion Integration

### Polling the Status Endpoint

Configure an HTTP GET request to poll the pi-decoder status:

```
URL: http://<DECODER-IP>/api/status
Method: GET
Interval: 2-5 seconds recommended
```

### Example Button Logic

**Stream Status Indicator:**
- Parse JSON response
- If `mpv.idle == true`: Show RED "OFFLINE" indicator
- If `mpv.playing == true`: Show GREEN "LIVE" indicator
- If `mpv.alive == false`: Show YELLOW "MPV DOWN" indicator

### WebSocket Alternative

For real-time updates without polling, connect to:
```
ws://<DECODER-IP>/ws/status
```

Receives JSON status updates every 2 seconds automatically.

---

## Logging

The stream retry system logs its activity:

**INFO level:**
```
Stream idle, attempting to reload: rtmp://encoder.local/live/stream
```

**DEBUG level (on failure):**
```
Stream reload failed, will retry
```

To view logs:
```bash
# On the Pi
journalctl -u pi-decoder -f

# Via web API
curl http://<DECODER-IP>/api/logs?lines=100
```

---

## Verification Steps

### Test 1: Pi-Decoder starts without encoder
1. Stop encoder if running
2. Start/restart pi-decoder
3. **Expected:** Black screen, logs show retry attempts every 5-60s
4. Check `/api/status` returns `mpv.idle: true`

### Test 2: Encoder comes online
1. With pi-decoder running and idle (from Test 1)
2. Start encoder
3. **Expected:** Video appears within 60 seconds
4. Check `/api/status` returns `mpv.idle: false, mpv.playing: true`

### Test 3: Encoder goes offline
1. With pi-decoder playing video
2. Stop encoder
3. **Expected:** Video stops, FFmpeg reconnect attempts for ~30s, then app retry begins
4. Check `/api/status` transitions to `mpv.idle: true`

### Test 4: Encoder recovery
1. With pi-decoder in retry mode (from Test 3)
2. Start encoder again
3. **Expected:** Video resumes automatically within 60s
4. Check `/api/status` returns `mpv.playing: true`

### Test 5: URL change resets backoff
1. With pi-decoder in retry mode (long backoff)
2. Change stream URL via web UI
3. **Expected:** Next retry attempt happens within 5 seconds (backoff reset)

---

## Configuration

No new configuration options were added. The feature uses the existing `stream.url` from the config file:

```toml
[stream]
url = "rtmp://encoder.local/live/stream"
network_caching = 1000
```

The retry behavior is automatic when a URL is configured.

---

## Deployment Notes

1. **No config changes required** - Feature activates automatically
2. **No new dependencies** - Uses existing Python stdlib (`time` module)
3. **Backwards compatible** - Existing API responses unchanged, just uses existing `idle` field
4. **Service restart required** - After deploying new code:
   ```bash
   sudo systemctl restart pi-decoder
   ```

---

## Troubleshooting

### Stream not auto-reconnecting

1. Check logs for retry attempts:
   ```bash
   journalctl -u pi-decoder | grep "Stream idle"
   ```

2. Verify URL is configured:
   ```bash
   curl http://localhost/api/status | jq '.mpv.stream_url'
   ```

3. Check if mpv is alive:
   ```bash
   curl http://localhost/api/status | jq '.mpv.alive'
   ```

### Retries too frequent/infrequent

The backoff is hardcoded (5s initial, 1.5x multiplier, 60s max). To adjust, modify these values in `mpv_manager.py`:

```python
# In __init__
self._stream_retry_backoff = 5.0  # Change initial backoff

# In _health_loop
self._stream_retry_backoff = min(self._stream_retry_backoff * 1.5, 60.0)  # Change multiplier and max
```

### Black screen but mpv.idle is false

This could mean mpv thinks it's playing but the stream has no video frames. Check:
1. Encoder is actually outputting video
2. Network connectivity between encoder and pi-decoder
3. Stream URL is correct

---

## Architecture Diagram

```
┌─────────────────────────────────────────────────────────────────┐
│                      Pi-Decoder                           │
│                                                                 │
│  ┌─────────────┐    ┌──────────────┐    ┌─────────────────┐   │
│  │   FastAPI   │    │  MpvManager  │    │   mpv process   │   │
│  │   Web App   │───▶│              │───▶│                 │   │
│  └─────────────┘    └──────────────┘    └─────────────────┘   │
│        │                   │                     │             │
│        │                   │    _health_loop()   │             │
│        │                   │    (every 5s)       │             │
│        │                   │         │           │             │
│        │                   │         ▼           │             │
│        │                   │  ┌─────────────┐    │             │
│        │                   │  │ Check idle? │    │             │
│        │                   │  └─────────────┘    │             │
│        │                   │         │           │             │
│        │                   │    yes  │  no       │             │
│        │                   │         ▼           │             │
│        │                   │  ┌─────────────┐    │             │
│        │                   │  │ load_stream │───▶│             │
│        │                   │  └─────────────┘    │             │
│        │                   │                     │             │
│  GET /api/status           │                     │             │
│        │                   │                     │             │
│        ▼                   │                     │             │
│  { "mpv": {                │                     │             │
│      "idle": true/false,   │◀────────────────────┘             │
│      "playing": ...        │     get_status()                  │
│    }                       │                                   │
│  }                         │                                   │
│                            │                                   │
└─────────────────────────────────────────────────────────────────┘
          │
          │ HTTP/WebSocket
          ▼
┌─────────────────────┐
│ Bitfocus Companion  │
│                     │
│ Poll /api/status    │
│ Show stream state   │
└─────────────────────┘
```

---

## Summary

This implementation adds automatic stream recovery to the Pi-Decoder, making it resilient to encoder outages without requiring manual intervention. The decoder will continuously retry connecting to the configured stream URL with exponential backoff, and Bitfocus Companion can monitor the stream state via the existing `/api/status` endpoint.
