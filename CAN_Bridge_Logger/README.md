# CAN Bridge + Logger

A Windows desktop app (Python/tkinter) that bridges two independent CAN
adapters together and logs traffic to a PCAN-Explorer-compatible `.trc`
trace file.

## What it does

- **Bridge**: forwards every CAN frame received on either adapter straight
  to the other, in both directions, while the bridge is running. No per-ID
  filtering - it's a plain pass-through.
- **Trace logging**: records traffic to a `.trc` file in PCAN-Explorer v2.1
  format (readable by PCAN-View / PCAN-Explorer and compatible tooling).
  Choose which channel(s) to log (CAN 1 / CAN 2 / both) and which direction
  (RX only, TX only, or both).
- **Live data view**: one list per adapter showing the latest frame seen
  for each CAN ID (direction, ID, sender node if a DBC is loaded, extended-
  ID flag, DLC, data bytes, frame count, and period), each with its own
  Clear button.
- **DBC sender-node lookup**: load one or more `.dbc` files and each live
  list's Node column will show the transmitting node name (e.g. `VCM`,
  `LBC`) for IDs found in them. This is a lightweight lookup of each
  message's declared sender - it does not decode signal values.
- **App log**: a running log of connects/disconnects, bridge/logging state
  changes, and errors.

## Hardware supported

Each of the two channels (CAN 1, CAN 2) is configured independently and can
be either:

- a **PEAK PCAN-USB** adapter (`pcan` backend), or
- a **CANable-style USB-to-CAN adapter running SLCAN firmware** (`slcan`
  backend, connects over a virtual COM port).

The two channels don't have to match - e.g. bridge a PCAN adapter on CAN 1
to an SLCAN adapter on CAN 2.

## Requirements

- Windows, Python 3.10+
- `pip install python-can`
- PCAN adapters additionally need PEAK's Windows driver installed
- SLCAN/CANable adapters additionally need `pip install pyserial`

See `INSTALL.md` for full step-by-step setup instructions.

## Running

```
py can_bridge_logger.py
```

The window opens even with nothing connected; each channel's Connect button
will report a clear error until a matching adapter is plugged in and set
up per `INSTALL.md`.

## Project structure

- `can_bridge_logger.py` - the entire app (single file).
- `INSTALL.md` - setup guide (general Windows/Python steps, then PCAN or
  SLCAN-specific steps).
- `Logs/Captures/` - default save location offered for new trace logs
  (created automatically the first time you start logging).

## Revision tracking

The top of `can_bridge_logger.py` carries a `# REVISION: N` line and an
in-file changelog (in its module docstring) describing what changed in
each revision and why - check that before making further changes.
