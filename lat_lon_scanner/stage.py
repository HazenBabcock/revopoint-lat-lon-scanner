"""
Control the lat/lon stage through its Marlin board over USB serial.

Positions are in degrees. X is latitude (the cradle tilt; +X tips the top of
the stage toward the scanner) and Y is longitude (the turntable; +Y turns it
counterclockwise seen from above with the cradle upright). There are no limit
switches, so zero is set by hand: energize the motors, put the cradle upright,
then call set_zero().

Run as a script for quick manual control:

    python -m lat_lon_scanner.stage position
    python -m lat_lon_scanner.stage zero
    python -m lat_lon_scanner.stage move <latitude> <longitude> [speed]
    python -m lat_lon_scanner.stage release
"""
import argparse
import glob
import re
import time

import serial

# The longitude motor's cable winds onto the apparatus beyond +-90 degrees of tilt.
latitude_hard_limit_degrees = 90.0

# The firmware's maximum feedrate is 45 degrees/s; full turns at 30 degrees/s
# lost no steps.
maximum_speed_degrees_per_second = 45.0
default_speed_degrees_per_second = 15.0

port_pattern = "/dev/serial/by-id/usb-STMicroelectronics_MARLIN_*"


class StageError(Exception):
    pass


def find_port():
    """Return the board's /dev/serial/by-id path, which survives re-plugging."""
    ports = sorted(glob.glob(port_pattern))
    if len(ports) != 1:
        raise StageError(f"Expected one Marlin board matching {port_pattern}, found {ports}")
    return ports[0]


class Stage(object):
    """
    The two-axis stage. Use as a context manager so the serial port is closed;
    closing the port does not release the motors.
    """
    def __init__(self, port_path = None, reply_timeout_seconds = 120.0):
        self.port_path = port_path if port_path is not None else find_port()
        self.reply_timeout_seconds = reply_timeout_seconds
        self.connection = serial.Serial(self.port_path, 115200, timeout = 0.5)

        # Opening the port does not reset the board; just drop anything stale.
        time.sleep(0.5)
        self.connection.reset_input_buffer()
        self.send("G90")

    def __enter__(self):
        return self

    def __exit__(self, exception_type, exception_value, traceback):
        self.close()

    def close(self):
        self.connection.close()

    def driver_prewarnings(self):
        """
        Return {"X": bool, "Y": bool}, True where a driver has an over-temperature
        pre-warning. The firmware lowers that motor's current while one persists.
        """
        prewarnings = {}
        for line in self.send("M911"):
            match = re.match(r"([XY]) temperature prewarn triggered: (true|false)", line)
            if match:
                prewarnings[match.group(1)] = (match.group(2) == "true")
        return prewarnings

    def enable(self):
        """Energize both motors so they hold position."""
        self.send("M17")

    def move_to(self, latitude, longitude, speed = default_speed_degrees_per_second):
        """
        Move to (latitude, longitude) in degrees and wait until the move is done.
        The speed (degrees/s) applies to the combined move.
        """
        if abs(latitude) > latitude_hard_limit_degrees:
            raise StageError(f"Latitude {latitude} is outside +-{latitude_hard_limit_degrees} degrees")
        if not (0 < speed <= maximum_speed_degrees_per_second):
            raise StageError(f"Speed {speed} is outside (0, {maximum_speed_degrees_per_second}] degrees/s")
        self.send(f"G1 X{latitude:.4f} Y{longitude:.4f} F{60.0 * speed:.1f}")
        self.send("M400")

    def position(self):
        """Return (latitude, longitude) in degrees as the firmware reports them."""
        for line in self.send("M114"):
            match = re.match(r"X:(-?[\d.]+) Y:(-?[\d.]+)", line)
            if match:
                return float(match.group(1)), float(match.group(2))
        raise StageError("No position in the M114 reply")

    def release(self):
        """De-energize both motors. The cradle may then drift, so re-zero before scanning."""
        self.send("M18")

    def send(self, command):
        """Send one G-code line and return its reply lines, up to and including 'ok'."""
        self.connection.write((command + "\n").encode("ascii"))
        reply_lines = []
        deadline = time.monotonic() + self.reply_timeout_seconds
        while time.monotonic() < deadline:
            line = self.connection.readline().decode("ascii", errors = "replace").strip()
            if not line:
                continue
            reply_lines.append(line)
            if line.startswith("Error"):
                raise StageError(f"'{command}' failed: {line}")
            if line.startswith("ok"):
                return reply_lines
        raise StageError(f"No 'ok' for '{command}' within {self.reply_timeout_seconds} s")

    def set_zero(self):
        """Declare the current pose (which should be upright) as latitude 0, longitude 0."""
        self.send("G92 X0 Y0")


def main():
    parser = argparse.ArgumentParser(description = "Manual control of the lat/lon stage.")
    subparsers = parser.add_subparsers(dest = "action", required = True)
    subparsers.add_parser("position", help = "Print the current latitude and longitude.")
    subparsers.add_parser("zero", help = "Energize the motors and declare the current pose as 0, 0.")
    subparsers.add_parser("release", help = "De-energize both motors.")
    move_parser = subparsers.add_parser("move", help = "Move to a latitude and longitude (degrees).")
    move_parser.add_argument("latitude", type = float)
    move_parser.add_argument("longitude", type = float)
    move_parser.add_argument("speed", type = float, nargs = "?", default = default_speed_degrees_per_second,
                             help = "Degrees/s (default %(default)s).")
    arguments = parser.parse_args()

    with Stage() as stage:
        if arguments.action == "zero":
            stage.enable()
            stage.set_zero()
        elif arguments.action == "move":
            stage.move_to(arguments.latitude, arguments.longitude, arguments.speed)
        elif arguments.action == "release":
            stage.release()
        latitude, longitude = stage.position()
        print(f"latitude {latitude:.2f}, longitude {longitude:.2f}")


if __name__ == "__main__":
    main()
