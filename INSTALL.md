# CAN Bridge + Logger — Install Guide

Do Section 1 first, then whichever of Section 2 or 3 matches your adapter.

## 1. General Windows setup

1. Install Python from python.org.
   - On the install screen, check **"Add python.exe to PATH"**.
2. Check it worked — open a terminal and run:
   ```
   py --version
   ```
3. Copy the `CAN_Bridge_Logger` folder onto this computer.
4. Install python-can:
   ```
   pip install python-can
   ```
5. Run the app:
   ```
   py can_bridge_logger.py
   ```

The window will open even with no adapters plugged in — Connect just won't
work until you finish the matching section below.

## 2. PCAN adapters (PEAK-System PCAN-USB)

1. Go to peak-system.com and install the Windows **driver** for your
   adapter.
2. Plug in the adapter.
3. In the app: Adapter type = `PCAN`, pick the channel
   (`PCAN_USBBUS1` for the first one, `PCAN_USBBUS2` for a second, etc.),
   pick the bitrate, then Connect.

## 3. SLCAN / CANable adapters

1. Install pyserial:
   ```
   pip install pyserial
   ```
2. Make sure the adapter is running **SLCAN firmware** (not
   "candleLight" firmware — that's different and won't work here).
3. Plug it in — it will show up as a COM port.
4. In the app: Adapter type = `SLCAN`, click **↻** to find the COM port
   (or type it in, e.g. `COM5`), pick the bitrate, then Connect.
