# Deployment and Troubleshooting Guide

**Complete guide for deploying and maintaining the Pi-Decoder on Raspberry Pi 5**

---

# PART 1: DEPLOYMENT

## Pre-Deployment Checklist

### Hardware Requirements
- [ ] Raspberry Pi 5 (4GB RAM recommended)
- [ ] MicroSD card (32GB+ Class 10/U1 minimum)
- [ ] HDMI cable
- [ ] USB-C power supply (5V 5A for Pi 5)
- [ ] Ethernet cable (recommended) or WiFi credentials
- [ ] TV/Monitor with HDMI input
- [ ] Computer for initial setup
- [ ] Keyboard (for initial Pi setup)

### Information You Need
- [ ] HLS stream URL from your encoder (e.g., `http://192.168.1.50:8080/stream.m3u8`)
- [ ] Network details (WiFi password if using WiFi)
- [ ] PCO API credentials (if using countdown overlay)
- [ ] Desired hostname for the Pi (optional)

---

## Step 1: Prepare SD Card

### Download and Flash OS
1. **Download Raspberry Pi Imager** from https://www.raspberrypi.com/software/
2. **Insert SD card** into your computer
3. **Run Pi Imager:**
   - OS: **"Raspberry Pi OS Lite (64-bit)"** (Bookworm)
   - Storage: Select your SD card
   - Settings (gear icon):
     - Enable SSH (set username: `pi`, password: your choice)
     - Configure WiFi (if not using Ethernet)
     - Set locale settings (timezone, keyboard)
4. **Write** the image to SD card
5. **Safely eject** SD card

### First Boot Setup
1. **Insert SD card** into Pi
2. **Connect HDMI, keyboard, power**
3. **Wait for boot** (2-3 minutes first time)
4. **Login** as `pi` with your password
5. **Connect to internet:**
   ```bash
   # For Ethernet: Should connect automatically
   # For WiFi: Use raspi-config if not connected
   sudo raspi-config
   # Navigate: System Options → Wireless LAN
   ```

---

## Step 2: Initial Pi Configuration

### Basic System Setup
```bash
# Update system
sudo apt update && sudo apt upgrade -y

# Configure Pi settings
sudo raspi-config
```

**In raspi-config menu:**
- **1 System Options** → **S5 Boot / Auto Login** → **B2 Console Autologin**
- **3 Interface Options** → **P2 SSH** → **Yes** (if you want remote access)
- **5 Localisation Options** → Set your timezone
- **6 Advanced Options** → **A1 Expand Filesystem**
- **Finish** → **Yes** to reboot

### Verify Network Connection
```bash
# Test internet connectivity
ping -c 3 google.com

# Get Pi's IP address (note this down)
hostname -I

# Test your stream server (replace with your server)
ping -c 3 your-encoder-ip
```

---

## Step 3: Deploy the Pi-Decoder

### Clone Repository
```bash
# Clone the repository (or copy via USB/SCP)
git clone <repository-url>
cd och-pi-decoder
```

### Run the Installation
```bash
# Navigate to deploy folder
cd deploy

# Make installer executable (if not already)
chmod +x setup.sh

# Run the installer
sudo ./setup.sh
```

**The installer will:**
- Update all system packages
- Install mpv media player and dependencies
- Install Python package with FastAPI web server
- Configure systemd service for auto-start
- Configure HDMI output and DRM display settings
- Create default configuration file

---

## Step 4: Configure Your Stream

### During Installation
The installer creates a default config at `/etc/pi-decoder/config.toml`.

### After Installation (Web Interface)
1. **Open browser** on any device on your network
2. **Go to:** `http://[PI-IP-ADDRESS]/`
3. **Stream tab:** Enter your HLS stream URL
4. **Click "Apply & Restart Video"**

### After Installation (Command Line)
```bash
# Edit configuration
sudo nano /etc/pi-decoder/config.toml

# Restart service after changes
sudo systemctl restart pi-decoder
```

### Configuration File Format
```toml
[general]
name = "Pi-Decoder"

[stream]
url = "http://192.168.1.50:8080/stream.m3u8"
network_caching = 2000

[overlay]
enabled = false
position = "bottom-right"
font_size = 56
transparency = 0.7
timer_mode = "service"
timezone = "Europe/Copenhagen"

[pco]
app_id = ""
secret = ""
folder_id = ""
poll_interval = 5

[web]
port = 80

[network]
hotspot_ssid = "Pi-Decoder"
hotspot_password = "pidecodersetup"
ethernet_timeout = 10
wifi_timeout = 40
```

---

## Step 5: Configure PCO Overlay (Optional)

### Get PCO API Credentials
1. Go to https://api.planningcenteronline.com/oauth/applications
2. Click **"New Personal Access Token"**
3. Give it a name (e.g., "Pi-Decoder")
4. Copy the **Application ID** and **Secret**

### Configure via Web Interface
1. Open `http://[PI-IP-ADDRESS]/`
2. Go to **Overlay** tab
3. Enable **"Enable PCO countdown overlay"**
4. Enter **App ID** and **Secret**
5. Click **"Test Connection"**
6. Select your **Service Type** from the dropdown
7. Click **"Save Credentials"**

### Overlay Modes
- **Service Countdown:** Shows total time remaining in the service
- **Current Item:** Shows time remaining for the current item (goes negative when overtime)

---

## Step 6: Verify Deployment

### Check Service Status
```bash
# Check if service is running
sudo systemctl status pi-decoder

# You should see: "active (running)"
```

### Test Video Output
1. **Check TV/Monitor** - Should show your HLS stream
2. **Check audio** - Should hear audio through HDMI
3. **Wait 30-60 seconds** for full startup if no video initially

### Test Web Interface
1. **Get Pi IP:** `hostname -I`
2. **Open browser:** `http://[PI-IP]/`
3. **Check status tab:** Should show green indicators

---

## Step 7: Production Configuration

### Set Static IP (Recommended)
```bash
# Edit dhcpcd configuration
sudo nano /etc/dhcpcd.conf

# Add at the end (adjust IP addresses for your network):
interface eth0
static ip_address=192.168.1.100/24
static routers=192.168.1.1
static domain_name_servers=192.168.1.1 8.8.8.8

# Restart networking
sudo systemctl restart dhcpcd
```

### Verify Auto-Start
```bash
# Enable service to start on boot (should already be enabled)
sudo systemctl enable pi-decoder

# Test auto-start
sudo reboot
# Wait 2-3 minutes, check if video appears automatically
```

---

# PART 2: TROUBLESHOOTING

## Emergency Procedures (For Volunteers)

### LEVEL 1: Basic Fixes (Try these first)

#### No Video on TV
1. **Check TV is on** and set to correct HDMI input (use "TV On" and "Livestream Source" on the Dashboard)
2. **Unplug HDMI cable** from Pi, wait 5 seconds, plug back in
3. **Reboot the Pi:** use the "Reboot" button on the Dashboard or System tab
4. **Wait 2-3 minutes** for complete startup

#### Player Not Working
1. **Restart video:** use the "Restart Video" button on the Dashboard
2. **Wait 1 minute** then check the Dashboard for status
3. **If still broken, reboot:** use the "Reboot" button on the Dashboard

#### Need to Change Stream URL
1. **Web method:** Open browser → `http://[PI-IP]/` → Stream tab
2. **File method:** Edit `/etc/pi-decoder/config.toml` → Run `sudo systemctl restart pi-decoder`

### LEVEL 2: Nuclear Option (When everything fails)
1. **Unplug Pi power cable**
2. **Wait 10 seconds**
3. **Plug power back in**
4. **Wait 3 minutes** for complete startup
5. **Check if video appears on TV**

**This fixes 90% of all problems!**

---

## Common Problems & Solutions

### Category 1: Display/TV Issues

#### Problem: Black Screen on TV
**Symptoms:** No video output, TV shows "No Signal"

**Solutions:**
```bash
# Check DRM display status
cat /sys/class/drm/card?-HDMI-A-1/status

# Check current display mode
cat /sys/class/drm/card?-HDMI-A-1/modes

# Verify video= kernel parameter
cat /proc/cmdline | tr ' ' '\n' | grep video

# Reboot to apply changes
sudo reboot
```

#### Problem: No Audio Through HDMI
**Symptoms:** Video works, but no sound

**Solutions:**
```bash
# Check audio devices
aplay -l

# Set HDMI as default audio
amixer cset numid=3 2

# Test audio
speaker-test -c 2 -t wav
```

### Category 2: Service Issues

#### Problem: Service Won't Start
**Symptoms:** `systemctl status pi-decoder` shows "failed" or "inactive"

**Diagnosis:**
```bash
sudo systemctl status pi-decoder -l
sudo journalctl -u pi-decoder -f
```

**Solutions:**
```bash
# Reset service
sudo systemctl reset-failed pi-decoder
sudo systemctl start pi-decoder

# If still fails, check configuration
cat /etc/pi-decoder/config.toml

# Check mpv is installed
which mpv
```

#### Problem: Service Starts But Video Doesn't Play
**Symptoms:** Service running but no video on screen

**Common Causes & Solutions:**
1. **Invalid Stream URL:**
   ```bash
   # Test the stream URL manually
   mpv --no-video http://your-stream-url
   ```

2. **DRM display not available:**
   ```bash
   # Check DRM device permissions
   ls -la /dev/dri/
   # Verify user is in video/render groups
   groups pi
   ```

3. **Network issue:**
   ```bash
   # Test connectivity to stream server
   ping your-encoder-ip
   curl -I http://your-stream-url
   ```

### Category 3: Network Issues

#### Problem: Cannot Connect to Stream
**Symptoms:** Service running but no video, logs show connection errors

**Diagnosis:**
```bash
# Test internet connectivity
ping -c 3 google.com

# Test stream server
ping -c 3 your-encoder-ip

# Test stream URL
curl -I http://your-encoder-ip:8080/stream.m3u8
```

**Solutions:**
1. **Network connectivity issues:**
   ```bash
   sudo systemctl restart networking
   sudo systemctl restart dhcpcd
   ```

2. **DNS issues:**
   ```bash
   echo "nameserver 8.8.8.8" | sudo tee -a /etc/resolv.conf
   ```

#### Problem: Stream Keeps Dropping
**Symptoms:** Video plays but frequently stops and restarts

**Solutions:**
- Increase network caching in web interface (try 3000-5000ms)
- Check network connection quality
- Ensure encoder is outputting stable stream
- Use Ethernet instead of WiFi

### Category 4: PCO Overlay Issues

#### Problem: Overlay Not Showing
**Symptoms:** Video plays but no countdown timer visible

**Solutions:**
1. Check overlay is enabled in web interface
2. Verify PCO credentials with "Test Connection"
3. Ensure a Live session is active in PCO Services
4. Check logs for PCO API errors

#### Problem: "Not live" Message
**Symptoms:** Overlay shows "Not live" instead of countdown

**Solution:**
- Go to PCO Services and click "Go Live" on your service

#### Problem: Wrong Countdown Time
**Solutions:**
- Check timezone setting in Overlay tab
- Verify item lengths are set correctly in PCO

---

## Web Interface Guide

### Accessing the Web Interface
1. **Find Pi's IP address:** `hostname -I` or check your router
2. **Open browser:** Type `http://[PI-IP-ADDRESS]/`

### Tabs Overview
- **Dashboard:** Live preview, CEC TV controls (power, input, volume), stream play/stop, service restart/reboot, system health stats
- **Stream:** Configure stream URL and network caching
- **Overlay:** PCO credentials, timer appearance, folder/poll settings
- **Network:** WiFi scanning and connection, hotspot controls, hostname display
- **System:** Pi-Decoder name, config backup/restore, log viewer, reboot/shutdown
- **Help:** Built-in documentation for all features

### Changing the Stream
1. Go to **Stream** tab
2. Enter new stream URL (must be HLS, usually ends in `.m3u8`)
3. Click **"Apply & Restart Video"**
4. Wait 30-60 seconds for video to appear

### CEC TV Control
The Dashboard provides direct TV control over HDMI using CEC:
- **TV On / TV Off** — power the TV on or off
- **Livestream Source** — switch the TV to the Pi's HDMI input
- **Volume Up / Volume Down / Mute** — adjust TV volume

These work with most TVs that support CEC (Samsung Anynet+, LG SimpLink, Sony Bravia Sync, etc.). CEC must be enabled in the TV's settings.

### Config Backup & Restore
In the **System** tab:
- **Download Config** — exports the current configuration as a TOML file (secrets are stripped for safety)
- **Import Config** — upload a previously exported TOML file to restore or clone settings

This is useful for deploying multiple pi-decoders with the same configuration, or recovering after an SD card failure.

### Shutdown
The **System** tab has both **Reboot** and **Shutdown** buttons:
- **Reboot** — restarts the Pi (back online in ~90 seconds)
- **Shutdown** — powers off the Pi completely. You will need physical access to unplug and re-plug the power cable to turn it back on

### Dark Mode
The web interface automatically switches to a dark theme when your device's OS is set to dark mode. No configuration needed.

---

## Log Analysis

### Viewing Logs
```bash
# Live service logs
sudo journalctl -u pi-decoder -f

# Last 100 lines
sudo journalctl -u pi-decoder -n 100

# Via web interface
# Go to Logs tab and select line count
```

### Understanding Log Messages

#### Normal Operation
```
INFO: Starting mpv: ...
INFO: Connected to mpv IPC socket
INFO: PCO: Found live service ...
```

#### Warning Logs (May be normal)
```
WARNING: mpv IPC socket did not appear within 5 s
WARNING: PCO: No live service found
```

#### Error Logs (Need attention)
```
ERROR: Stream URL not configured
ERROR: PCO authentication failed
ERROR: mpv process died — restarting
```

---

## Maintenance Tasks

### Weekly Tasks
```bash
# Check system status
sudo systemctl status pi-decoder

# Check disk space
df -h

# Check system temperature
vcgencmd measure_temp
```

### Monthly Tasks
```bash
# Update system
sudo apt update && sudo apt upgrade

# Check for throttling
vcgencmd get_throttled
```

---

## System Architecture

### Components
- **mpv:** Video player with hardware H.265 decoding
- **FastAPI:** Python web server for configuration
- **JSON IPC:** Communication with mpv via Unix socket
- **ASS Overlays:** Native mpv subtitle rendering for timer

### Key Files
- **Config:** `/etc/pi-decoder/config.toml`
- **Service:** `/etc/systemd/system/pi-decoder.service`
- **IPC Socket:** `/tmp/mpv-pi-decoder.sock`
- **Python Package:** `/opt/pi-decoder/`

### Service Control
```bash
# Start/stop/restart
sudo systemctl start pi-decoder
sudo systemctl stop pi-decoder
sudo systemctl restart pi-decoder

# Enable/disable auto-start
sudo systemctl enable pi-decoder
sudo systemctl disable pi-decoder

# View status
sudo systemctl status pi-decoder
```

---

## When to Call for Technical Help

### For Volunteers
Call someone technical if:
- You've tried unplugging/plugging power and waiting 3 minutes, still no video
- The web interface shows errors you don't understand
- You need to change network settings
- Physical hardware problems (Pi won't turn on, HDMI port broken)

### Information to Give Technical Support
1. **Take photos** of any error messages
2. **Tell them what you tried:** "I rebooted and waited, still no video"
3. **Tell them what changed:** "It was working yesterday"
4. **Share logs:** Output of `sudo journalctl -u pi-decoder -n 50`

### Support Checklist
Before contacting support, ensure you've tried:
- [ ] Reboot the Pi via the Dashboard or System tab (or `sudo reboot`)
- [ ] Restart video via the Dashboard (or `sudo systemctl restart pi-decoder`)
- [ ] Check HDMI connection (try "TV On" and "Livestream Source" on Dashboard)
- [ ] Verify stream URL is correct in the Stream tab
- [ ] Check internet connectivity in the Network tab

---

## Deployment Verification Checklist

### Technical Verification
- [ ] Service status shows "active (running)"
- [ ] Video plays on connected TV/monitor
- [ ] Audio works through HDMI
- [ ] Web interface accessible and functional
- [ ] System survives reboot and auto-starts
- [ ] HDMI disconnect/reconnect works
- [ ] Stream URL changes work via web interface
- [ ] PCO overlay displays (if configured)

### Volunteer Verification
- [ ] Web interface is accessible
- [ ] Emergency procedures tested
- [ ] Contact information provided

---

**System Ready!**

Your Pi-Decoder is now ready for operation. Remember: **"When in doubt, reboot!"**
