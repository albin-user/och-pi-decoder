#!/bin/bash
# Pi-Decoder — Network Fallback Script
# Runs at boot before decoder.service.
# 1. Wait for Ethernet
# 2. If no Ethernet, enable WiFi and wait for saved WiFi
# 3. If still nothing, start hotspot
set -e

CONFIG="/etc/pi-decoder/config.toml"

# Section-aware TOML reader: only matches keys within the given [section].
# Usage: toml_get <file> <section> <key>
toml_get() {
    awk -v section="$2" -v key="$3" '
        /^\[/ { in_section = ($0 == "[" section "]") }
        in_section && $0 ~ "^" key "[[:space:]]*=" {
            sub(/^[^=]*=[[:space:]]*/, "")
            gsub(/^"|"$/, "")     # strip surrounding quotes
            gsub(/[[:space:]]*#.*$/, "")  # strip inline comments
            print
            exit
        }
    ' "$1" 2>/dev/null
}

# Read timeouts from config (defaults: 10s ethernet, 40s WiFi)
ETH_TIMEOUT=10
WIFI_TIMEOUT=40

if [ -f "$CONFIG" ]; then
    val=$(toml_get "$CONFIG" network ethernet_timeout)
    [ -n "$val" ] && ETH_TIMEOUT="$val"
    val=$(toml_get "$CONFIG" network wifi_timeout)
    [ -n "$val" ] && WIFI_TIMEOUT="$val"
fi

# Read hotspot settings from config (defaults come from config.toml.example)
HOTSPOT_SSID="Pi-Decoder"
HOTSPOT_PASS="pidecodersetup"

if [ -f "$CONFIG" ]; then
    val=$(toml_get "$CONFIG" network hotspot_ssid)
    [ -n "$val" ] && HOTSPOT_SSID="$val"
    val=$(toml_get "$CONFIG" network hotspot_password)
    [ -n "$val" ] && HOTSPOT_PASS="$val"
fi

has_connection() {
    nmcli -t -f STATE general status 2>/dev/null | grep -q "^connected"
}

echo "[pi-decoder-network] Waiting ${ETH_TIMEOUT}s for Ethernet..."

# Step 1: Wait for Ethernet
for i in $(seq 1 "$ETH_TIMEOUT"); do
    if has_connection; then
        echo "[pi-decoder-network] Network connected (Ethernet)."
        # Single-interface policy: disconnect WiFi when Ethernet is active
        nmcli device disconnect wlan0 2>/dev/null || true
        exit 0
    fi
    sleep 1
done

echo "[pi-decoder-network] No Ethernet. Enabling WiFi radio..."

# Step 2: Enable WiFi and wait for saved network
nmcli radio wifi on 2>/dev/null || true

echo "[pi-decoder-network] Waiting ${WIFI_TIMEOUT}s for WiFi..."

for i in $(seq 1 "$WIFI_TIMEOUT"); do
    if has_connection; then
        echo "[pi-decoder-network] Network connected (WiFi)."
        exit 0
    fi
    sleep 1
done

echo "[pi-decoder-network] No WiFi. Starting hotspot: SSID=${HOTSPOT_SSID}"

# Step 3: Start hotspot
nmcli connection delete Hotspot 2>/dev/null || true
nmcli device wifi hotspot ifname wlan0 ssid "$HOTSPOT_SSID" password "$HOTSPOT_PASS" || {
    echo "[pi-decoder-network] Failed to start hotspot."
    exit 1
}

# Configure DNS to point to self (captive portal)
nmcli connection modify Hotspot ipv4.dns 10.42.0.1 2>/dev/null || true

echo "[pi-decoder-network] Hotspot active at 10.42.0.1"
