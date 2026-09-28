"""
Master script: run this once, leave it running in the background.
Everything else is controlled from WoW chat -- no more alt-tabbing.

In-game chat commands:
    routes              -> lists available routes from the routes/ folder
    startride <name>     -> teleports to that route's start and begins riding
    stopride             -> stops the current ride, resets trainer/speed

Combines: BLE trainer control, physics (power -> speed/grade), live-measured
grade from real position samples, and real in-game steering via simulated
key turns -- all in one process, all driven by one connected trainer.
"""

import asyncio
import json
import math
import os
import struct
import subprocess
import sys
import time
from bleak import BleakScanner, BleakClient

# --- BLE / FTMS ---
FTMS_SERVICE_UUID = "00001826-0000-1000-8000-00805f9b34fb"
INDOOR_BIKE_DATA_UUID = "00002ad2-0000-1000-8000-00805f9b34fb"
CONTROL_POINT_UUID = "00002ad9-0000-1000-8000-00805f9b34fb"

OP_REQUEST_CONTROL = 0x00
OP_RESET = 0x01
OP_START_RESUME = 0x07
OP_SET_SIM_PARAMS = 0x11

# --- Physics ---
TOTAL_MASS_KG = 90.0
CRR = 0.004
CDA = 0.4
AIR_DENSITY = 1.225
G = 9.81
MIN_SPEED_FOR_DRIVE_FORCE = 0.5
GRADE_SEND_THRESHOLD = 0.3
UPDATE_INTERVAL = 0.5

# Only affects how much virtual SPEED you build on descents -- trainer
# resistance always uses the full real grade regardless of this.
# 1.0 = no change (full realistic speed buildup); lower = caps downhill
# speed more aggressively. Tune this to taste.
DOWNHILL_SPEED_DAMPING = 0.5

WOW_BASE_RUN_SPEED_KMH = 23.04

# --- Steering ---
WOW_WINDOW_NAME = "^World of Warcraft$"
TURN_RATE_RAD_PER_SEC = 3.076
TURN_LEFT_DECREASES = False
MIN_CORRECTION_RAD = math.radians(2)
MAX_HOLD_SECONDS = 0.4
MIN_HOLD_SECONDS = 0.05
DAMPING_FACTOR = 0.5
LOOKAHEAD_DISTANCE_M = 10.0

# --- Shared bridge files ---
SPEED_FILE_PATH = "/tmp/tacx_speed_target.txt"
LIVE_POSITION_FILE_PATH = "/tmp/tacx_live_position.txt"
TELEPORT_FILE_PATH = "/tmp/tacx_teleport_target.txt"
COMMAND_FILE_PATH = "/tmp/tacx_command.txt"
RESPONSE_FILE_PATH = "/tmp/tacx_response.txt"
RECORDING_FILE_PATH = "/tmp/tacx_route_recording.txt"
HUD_DATA_FILE_PATH = "/tmp/tacx_hud_data.txt"


def write_hud_data(speed_kmh: float, power: float, distance_m: float, distance_to_go_m: float, grade_percent: float):
    try:
        with open(HUD_DATA_FILE_PATH, "w") as f:
            f.write(f"{speed_kmh:.1f},{power:.0f},{distance_m:.0f},{distance_to_go_m:.0f},{grade_percent:+.1f}")
    except OSError:
        pass

ROUTES_FOLDER = os.path.join(os.path.dirname(os.path.abspath(__file__)), "routes")


def build_route_from_recording(recording_path: str, route_name: str) -> dict:
    """Converts a raw position recording into a distance-indexed route dict."""
    raw_points = []
    with open(recording_path, "r") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            map_id, x, y, z, o = line.split(",")
            raw_points.append({"map_id": int(map_id), "x": float(x), "y": float(y), "z": float(z)})

    if len(raw_points) < 2:
        raise ValueError("Recording has fewer than 2 points -- did you walk long enough?")

    points = []
    cumulative_distance = 0.0
    first = raw_points[0]
    points.append({
        "distance_m": 0.0, "elevation_m": first["z"], "x": first["x"], "y": first["y"]
    })

    for prev, curr in zip(raw_points, raw_points[1:]):
        dx = curr["x"] - prev["x"]
        dy = curr["y"] - prev["y"]
        dz = curr["z"] - prev["z"]
        cumulative_distance += math.sqrt(dx * dx + dy * dy + dz * dz)
        points.append({
            "distance_m": round(cumulative_distance, 2),
            "elevation_m": curr["z"],
            "x": curr["x"],
            "y": curr["y"],
        })

    return {"name": route_name, "map_id": raw_points[0]["map_id"], "points": points}


# ============================== Trainer / BLE ==============================

class TrainerController:
    def __init__(self):
        self.response_event = asyncio.Event()
        self.latest_power = 0.0

    def handle_bike_data(self, _sender, data: bytes):
        flags = struct.unpack_from("<H", data, 0)[0]
        offset = 2
        if not (flags & 0x0001):
            offset += 2
        if flags & 0x0002:
            offset += 2
        if flags & 0x0004:
            offset += 2
        if flags & 0x0008:
            offset += 2
        if flags & 0x0010:
            offset += 3
        if flags & 0x0020:
            offset += 2
        if flags & 0x0040:
            power = struct.unpack_from("<h", data, offset)[0]
            self.latest_power = power

    def handle_control_point_response(self, _sender, data: bytes):
        self.response_event.set()

    async def send_command(self, client, payload: bytes):
        self.response_event.clear()
        await client.write_gatt_char(CONTROL_POINT_UUID, payload, response=True)
        try:
            await asyncio.wait_for(self.response_event.wait(), timeout=5.0)
        except asyncio.TimeoutError:
            pass

    async def set_grade(self, client, grade_percent: float):
        wind_speed_raw = 0
        grade_raw = int(grade_percent / 0.01)
        crr_raw = int(CRR / 0.0001)
        cw_raw = int(0.51 / 0.01)
        payload = bytes([OP_SET_SIM_PARAMS]) + struct.pack(
            "<hhBB", wind_speed_raw, grade_raw, crr_raw, cw_raw
        )
        await self.send_command(client, payload)


class PhysicsState:
    def __init__(self):
        self.speed_ms = 0.0
        self.distance_m = 0.0

    def reset(self):
        self.speed_ms = 0.0
        self.distance_m = 0.0

    def step(self, power_watts: float, grade_percent: float, dt: float):
        v = max(self.speed_ms, MIN_SPEED_FOR_DRIVE_FORCE)
        drive_force = power_watts / v if power_watts > 0 else 0.0
        gravity_force = TOTAL_MASS_KG * G * (grade_percent / 100.0)
        rolling_force = CRR * TOTAL_MASS_KG * G
        aero_force = 0.5 * AIR_DENSITY * CDA * self.speed_ms ** 2
        net_force = drive_force - gravity_force - rolling_force - aero_force
        acceleration = net_force / TOTAL_MASS_KG
        self.speed_ms = max(0.0, self.speed_ms + acceleration * dt)
        self.distance_m += self.speed_ms * dt


def write_speed_target(speed_kmh: float):
    rate = speed_kmh / WOW_BASE_RUN_SPEED_KMH
    try:
        with open(SPEED_FILE_PATH, "w") as f:
            f.write(f"{rate:.3f}")
    except OSError:
        pass


# ============================== Steering ==============================

def bearing_to(x1, y1, x2, y2) -> float:
    return math.atan2(y2 - y1, x2 - x1) % (2 * math.pi)


def angle_diff(a: float, b: float) -> float:
    return (a - b + math.pi) % (2 * math.pi) - math.pi


def read_live_position():
    try:
        with open(LIVE_POSITION_FILE_PATH, "r") as f:
            line = f.read().strip()
        if not line:
            return None
        map_id, x, y, z, o = line.split(",")
        return int(map_id), float(x), float(y), float(z), float(o)
    except (OSError, ValueError):
        return None


def write_teleport_target(x: float, y: float, z: float, facing: float):
    with open(TELEPORT_FILE_PATH, "w") as f:
        f.write(f"{x:.3f},{y:.3f},{z:.3f},{facing:.4f}")


def find_window_id(name_pattern: str):
    try:
        result = subprocess.run(
            ["xdotool", "search", "--name", name_pattern],
            capture_output=True, text=True, timeout=3
        )
        window_ids = result.stdout.strip().split("\n")
        if window_ids and window_ids[0]:
            return window_ids[0]
    except Exception:
        pass
    return None


SPEED_REFERENCE_MS = 8.0  # roughly 29 km/h -- damping stays normal at/below this, softens above it


def apply_turn_correction(window_id: str, signed_angle_diff: float, speed_ms: float = 0.0):
    magnitude = abs(signed_angle_diff)
    if magnitude < MIN_CORRECTION_RAD or window_id is None:
        return

    needs_increase = signed_angle_diff > 0
    if needs_increase:
        key = "Right" if TURN_LEFT_DECREASES else "Left"
    else:
        key = "Left" if TURN_LEFT_DECREASES else "Right"

    # Softer corrections at higher speed -- the same turn sweeps a much
    # larger lateral distance the faster you're covering ground, so a
    # damping factor tuned for normal pace becomes an overcorrection
    # (wide zigzag) when flying downhill.
    effective_damping = DAMPING_FACTOR * min(1.0, SPEED_REFERENCE_MS / max(speed_ms, 0.1))

    hold_duration = (magnitude * effective_damping) / TURN_RATE_RAD_PER_SEC
    hold_duration = max(MIN_HOLD_SECONDS, min(MAX_HOLD_SECONDS, hold_duration))

    subprocess.run(["xdotool", "keydown", "--window", window_id, key])
    time.sleep(hold_duration)
    subprocess.run(["xdotool", "keyup", "--window", window_id, key])


class RouteFollower:
    def __init__(self, waypoints: list):
        self.waypoints = waypoints
        self.cumulative = [0.0]
        for prev, curr in zip(waypoints, waypoints[1:]):
            d = math.hypot(curr["x"] - prev["x"], curr["y"] - prev["y"])
            self.cumulative.append(self.cumulative[-1] + d)
        self.total_length = self.cumulative[-1]
        self.last_position = None
        self.progress_m = 0.0
        self._has_progress = False
        self.recent_grades = []   # rolling window for grade smoothing
        self.last_grade = 0.0
        self.last_speed_ms = 0.0
        self.recent_facings = []  # rolling window for temporal smoothing of steering target
        self.smoothed_turn_intensity = 0.0

    def _closest_point_progress(self, x: float, y: float) -> float:
        SEARCH_BACKWARD_M = 10.0
        SEARCH_FORWARD_M = 60.0

        if self._has_progress:
            search_min = self.progress_m - SEARCH_BACKWARD_M
            search_max = self.progress_m + SEARCH_FORWARD_M
        else:
            search_min = -1
            search_max = self.total_length + 1

        best_dist = None
        best_progress = self.progress_m

        for i in range(len(self.waypoints) - 1):
            if self.cumulative[i + 1] < search_min or self.cumulative[i] > search_max:
                continue
            x1, y1 = self.waypoints[i]["x"], self.waypoints[i]["y"]
            x2, y2 = self.waypoints[i + 1]["x"], self.waypoints[i + 1]["y"]
            seg_dx, seg_dy = x2 - x1, y2 - y1
            seg_len_sq = seg_dx ** 2 + seg_dy ** 2
            t = 0.0 if seg_len_sq == 0 else max(0.0, min(1.0, (
                (x - x1) * seg_dx + (y - y1) * seg_dy) / seg_len_sq))
            proj_x, proj_y = x1 + t * seg_dx, y1 + t * seg_dy
            dist = math.hypot(x - proj_x, y - proj_y)
            if best_dist is None or dist < best_dist:
                best_dist = dist
                seg_len = math.hypot(seg_dx, seg_dy)
                best_progress = self.cumulative[i] + t * seg_len

        self._has_progress = True
        return best_progress

    def _point_at_distance(self, target_distance: float):
        target_distance = max(0.0, min(self.total_length, target_distance))
        for i in range(len(self.waypoints) - 1):
            if self.cumulative[i] <= target_distance <= self.cumulative[i + 1]:
                seg_len = self.cumulative[i + 1] - self.cumulative[i]
                t = 0.0 if seg_len == 0 else (target_distance - self.cumulative[i]) / seg_len
                x1, y1 = self.waypoints[i]["x"], self.waypoints[i]["y"]
                x2, y2 = self.waypoints[i + 1]["x"], self.waypoints[i + 1]["y"]
                return x1 + t * (x2 - x1), y1 + t * (y2 - y1)
        last = self.waypoints[-1]
        return last["x"], last["y"]

    def update(self, live_x, live_y, live_z, live_o, window_id) -> dict:
        result = {"grade_percent": 0.0, "progress_m": 0.0, "finished": False}

        # --- Smoothed live grade ---
        # Only recompute grade if we've moved enough distance that noise
        # doesn't get amplified by dividing by a tiny number. Otherwise,
        # keep using the last known-good grade.
        MIN_DIST_FOR_GRADE_M = 1.0
        MAX_GRADE_CHANGE_PER_TICK = 2.0  # percent -- caps how fast resistance can ramp

        if self.last_position is not None:
            lx, ly, lz = self.last_position
            horizontal_dist = math.hypot(live_x - lx, live_y - ly)
            self.last_speed_ms = horizontal_dist / UPDATE_INTERVAL

            if horizontal_dist > MIN_DIST_FOR_GRADE_M:
                raw_grade = ((live_z - lz) / horizontal_dist) * 100
                self.recent_grades.append(raw_grade)
                if len(self.recent_grades) > 4:
                    self.recent_grades.pop(0)
                averaged_grade = sum(self.recent_grades) / len(self.recent_grades)

                # Limit how fast the grade can change tick-to-tick, so
                # resistance ramps smoothly instead of spiking on bumps.
                delta = averaged_grade - self.last_grade
                delta = max(-MAX_GRADE_CHANGE_PER_TICK, min(MAX_GRADE_CHANGE_PER_TICK, delta))
                self.last_grade = self.last_grade + delta

        result["grade_percent"] = self.last_grade
        self.last_position = (live_x, live_y, live_z)

        progress = self._closest_point_progress(live_x, live_y)
        self.progress_m = progress
        result["progress_m"] = progress

        # Detect how sharply the path curves over the next stretch. This
        # must be computed ENTIRELY from the path's own points -- never
        # from live position. Using live position here created a feedback
        # loop: any small lateral drift off the path got misread as a
        # sharp turn (since bearing-to-a-path-point-from-an-offset-position
        # differs from the path's own direction even on a straight road),
        # which shrank the lookahead, which made further drift look even
        # sharper, escalating into runaway oscillation.
        seg1_start_x, seg1_start_y = self._point_at_distance(progress + 2.0)
        seg1_end_x, seg1_end_y = self._point_at_distance(progress + 8.0)
        seg2_start_x, seg2_start_y = self._point_at_distance(progress + 17.0)
        seg2_end_x, seg2_end_y = self._point_at_distance(progress + 25.0)
        near_bearing = bearing_to(seg1_start_x, seg1_start_y, seg1_end_x, seg1_end_y)
        far_bearing = bearing_to(seg2_start_x, seg2_start_y, seg2_end_x, seg2_end_y)
        upcoming_turn_amount = abs(angle_diff(far_bearing, near_bearing))

        # Continuous blend between "tight turn" and "normal" behavior,
        # instead of a hard on/off switch -- a binary switch caused a
        # visible snap right at the exact moment a curve eased into a
        # straight, if any heading error remained at that instant.
        # turn_intensity: 0 = straight, 1 = fully in a turn, saturating at 30deg.
        raw_turn_intensity = min(1.0, upcoming_turn_amount / math.radians(30))

        # Rise immediately when entering a turn, but decay slowly after
        # exiting -- the geometric turn can end before your heading has
        # actually caught up, and extending lookahead back out immediately
        # at that moment produces one last badly-timed large correction.
        # Lingering in "turn mode" briefly after the bend ends gives
        # steering time to fully settle first.
        TURN_INTENSITY_DECAY = 0.85
        if raw_turn_intensity > self.smoothed_turn_intensity:
            self.smoothed_turn_intensity = raw_turn_intensity
        else:
            self.smoothed_turn_intensity = (
                self.smoothed_turn_intensity * TURN_INTENSITY_DECAY
                + raw_turn_intensity * (1 - TURN_INTENSITY_DECAY)
            )
        turn_intensity = self.smoothed_turn_intensity

        speed_scaled_lookahead = max(LOOKAHEAD_DISTANCE_M, self.last_speed_ms * 2.5)
        dynamic_lookahead = speed_scaled_lookahead * (1 - turn_intensity) + 4.0 * turn_intensity

        target_x, target_y = self._point_at_distance(progress + dynamic_lookahead)
        raw_desired_facing = bearing_to(live_x, live_y, target_x, target_y)

        # Smooth the steering target over the last few ticks (circular mean,
        # handles the 0/360 wraparound correctly). A real turn drifts
        # consistently across ticks and survives this basically unchanged;
        # pure point-to-point recording noise doesn't have a consistent
        # direction and gets canceled out instead of chased.
        self.recent_facings.append(raw_desired_facing)
        if len(self.recent_facings) > 3:
            self.recent_facings.pop(0)
        sin_sum = sum(math.sin(f) for f in self.recent_facings)
        cos_sum = sum(math.cos(f) for f in self.recent_facings)
        desired_facing = math.atan2(sin_sum, cos_sum) % (2 * math.pi)

        correction = angle_diff(desired_facing, live_o)

        # Don't soften correction strength for speed while actively in a
        # turn -- that's exactly when full steering authority matters most.
        # Speed-based softening is only for keeping straight sections calm.
        # Blended continuously with turn_intensity, same reasoning as lookahead.
        damping_speed = self.last_speed_ms * (1 - turn_intensity)
        apply_turn_correction(window_id, correction, damping_speed)

        result["finished"] = progress >= self.total_length - 1.0
        result["live_x"] = live_x
        result["live_y"] = live_y
        result["live_o_deg"] = math.degrees(live_o)
        result["target_x"] = target_x
        result["target_y"] = target_y
        result["desired_facing_deg"] = math.degrees(desired_facing)
        result["correction_deg"] = math.degrees(correction)
        result["turn_intensity"] = turn_intensity
        result["dynamic_lookahead_m"] = dynamic_lookahead
        result["speed_ms"] = self.last_speed_ms
        result["damping_speed_used"] = damping_speed
        return result


# ============================== Routes ==============================

class Route:
    def __init__(self, name: str, points: list, map_id: int):
        self.name = name
        self.points = points  # list of dicts: distance_m, elevation_m, x, y
        self.map_id = map_id

    @property
    def length_m(self):
        return self.points[-1]["distance_m"]

    @property
    def elevation_gain_m(self):
        gain = 0.0
        for prev, curr in zip(self.points, self.points[1:]):
            delta = curr["elevation_m"] - prev["elevation_m"]
            if delta > 0:
                gain += delta
        return gain


def load_route_file(filepath: str) -> Route:
    with open(filepath, "r") as f:
        data = json.load(f)
    points = sorted(data["points"], key=lambda p: p["distance_m"])
    if len(points) < 2:
        raise ValueError(f"Route '{filepath}' needs at least 2 points.")
    return Route(
        name=data.get("name", os.path.basename(filepath)),
        points=points,
        map_id=data.get("map_id", 0),
    )


def list_available_routes(folder: str) -> list:
    if not os.path.isdir(folder):
        return []
    return sorted(f for f in os.listdir(folder) if f.endswith(".json"))


# ============================== Command handling ==============================

def read_pending_command():
    if not os.path.exists(COMMAND_FILE_PATH):
        return None
    try:
        with open(COMMAND_FILE_PATH, "r") as f:
            content = f.read().strip()
        os.remove(COMMAND_FILE_PATH)
        return content if content else None
    except OSError:
        return None


def write_response(text: str):
    try:
        with open(RESPONSE_FILE_PATH, "w") as f:
            f.write(text)
    except OSError:
        pass


EXPORTS_FOLDER = os.path.join(os.path.dirname(os.path.abspath(__file__)), "exports")


def capture_screenshot(window_id, output_path: str):
    """Captures a screenshot of the given window (must be focused first)."""
    if window_id is None:
        return False
    try:
        subprocess.run(["xdotool", "windowactivate", window_id], timeout=3)
        time.sleep(0.3)  # let the window actually come to front before capturing
        subprocess.run(["scrot", "-u", output_path], timeout=5)
        return os.path.exists(output_path)
    except Exception:
        return False


def write_tcx_export(route_name: str, log: list, start_time: float) -> str:
    """
    Writes a .tcx file from a ride log. log entries are
    (timestamp, distance_m, elevation_m, power, speed_ms).
    Returns the output file path.
    """
    os.makedirs(EXPORTS_FOLDER, exist_ok=True)

    start_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(start_time))
    safe_name = "".join(c if c.isalnum() or c in "-_" else "_" for c in route_name)
    filename = f"{safe_name}_{time.strftime('%Y%m%d_%H%M%S', time.gmtime(start_time))}.tcx"
    output_path = os.path.join(EXPORTS_FOLDER, filename)

    total_distance = log[-1][1] if log else 0.0
    total_time_seconds = (log[-1][0] - start_time) if log else 0.0

    trackpoints_xml = []
    for ts, distance_m, elevation_m, power, speed_ms in log:
        point_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))
        trackpoints_xml.append(f"""      <Trackpoint>
        <Time>{point_iso}</Time>
        <DistanceMeters>{distance_m:.1f}</DistanceMeters>
        <AltitudeMeters>{elevation_m:.1f}</AltitudeMeters>
        <Extensions>
          <TPX xmlns="http://www.garmin.com/xmlschemas/ActivityExtension/v2">
            <Watts>{power:.0f}</Watts>
            <Speed>{speed_ms:.2f}</Speed>
          </TPX>
        </Extensions>
      </Trackpoint>""")

    tcx_content = f"""<?xml version="1.0" encoding="UTF-8"?>
<TrainingCenterDatabase xmlns="http://www.garmin.com/xmlschemas/TrainingCenterDatabase/v2"
    xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"
    xsi:schemaLocation="http://www.garmin.com/xmlschemas/TrainingCenterDatabase/v2 http://www.garmin.com/xmlschemas/TrainingCenterDatabasev2.xsd">
  <Activities>
    <Activity Sport="Biking">
      <Id>{start_iso}</Id>
      <Lap StartTime="{start_iso}">
        <TotalTimeSeconds>{total_time_seconds:.0f}</TotalTimeSeconds>
        <DistanceMeters>{total_distance:.1f}</DistanceMeters>
        <Calories>0</Calories>
        <Intensity>Active</Intensity>
        <TriggerMethod>Manual</TriggerMethod>
        <Track>
{chr(10).join(trackpoints_xml)}
        </Track>
      </Lap>
      <Notes>Route: {route_name}</Notes>
    </Activity>
  </Activities>
</TrainingCenterDatabase>
"""

    with open(output_path, "w") as f:
        f.write(tcx_content)

    return output_path


DEBUG_LOG_FOLDER = os.path.join(os.path.dirname(os.path.abspath(__file__)), "debug_logs")

DEBUG_LOG_COLUMNS = [
    "tick_time", "live_x", "live_y", "live_o_deg", "progress_m",
    "target_x", "target_y", "desired_facing_deg", "correction_deg",
    "turn_intensity", "dynamic_lookahead_m", "speed_ms",
    "damping_speed_used", "grade_percent", "power",
]


class RideState:
    def __init__(self):
        self.active = False
        self.route = None
        self.follower = None
        self.physics = PhysicsState()
        self.last_sent_grade = None
        self.log = []          # list of (timestamp, distance_m, elevation_m, power, speed_ms)
        self.start_time = None
        self.screenshot_base_name = None
        self.debug_log_file = None
        self.debug_log_path = None

    def start(self, route: Route):
        self.route = route
        waypoints = [{"x": p["x"], "y": p["y"], "elevation_m": p["elevation_m"]} for p in route.points]
        self.follower = RouteFollower(waypoints)
        self.physics.reset()
        self.last_sent_grade = None
        self.log = []
        self.start_time = time.time()
        self.active = True

        os.makedirs(DEBUG_LOG_FOLDER, exist_ok=True)
        base_name = f"{route.name}_{time.strftime('%Y%m%d_%H%M%S', time.gmtime(self.start_time))}"
        self.debug_log_path = os.path.join(DEBUG_LOG_FOLDER, f"{base_name}_steering.csv")
        self.debug_log_file = open(self.debug_log_path, "w")
        self.debug_log_file.write(",".join(DEBUG_LOG_COLUMNS) + "\n")

    def write_debug_row(self, result: dict, grade_percent: float, power: float):
        if self.debug_log_file is None:
            return
        row = [
            f"{time.time():.2f}",
            f"{result['live_x']:.2f}", f"{result['live_y']:.2f}", f"{result['live_o_deg']:.1f}",
            f"{result['progress_m']:.1f}",
            f"{result['target_x']:.2f}", f"{result['target_y']:.2f}",
            f"{result['desired_facing_deg']:.1f}", f"{result['correction_deg']:.1f}",
            f"{result['turn_intensity']:.2f}", f"{result['dynamic_lookahead_m']:.1f}",
            f"{result['speed_ms']:.2f}", f"{result['damping_speed_used']:.2f}",
            f"{grade_percent:.1f}", f"{power:.0f}",
        ]
        self.debug_log_file.write(",".join(row) + "\n")
        self.debug_log_file.flush()

    def stop(self):
        self.active = False
        self.route = None
        self.follower = None
        if self.debug_log_file:
            self.debug_log_file.close()
            self.debug_log_file = None


async def handle_command(command: str, ride: RideState, window_id):
    if command == "LIST_ROUTES":
        files = list_available_routes(ROUTES_FOLDER)
        if not files:
            write_response(f"No routes found in {ROUTES_FOLDER}")
        else:
            lines = [f"Available routes ({len(files)}):"]
            for fname in files:
                try:
                    r = load_route_file(os.path.join(ROUTES_FOLDER, fname))
                    lines.append(
                        f"  {fname[:-5]} - \"{r.name}\" "
                        f"({r.length_m:.0f}m, +{r.elevation_gain_m:.0f}m climb)"
                    )
                except Exception as e:
                    lines.append(f"  {fname} - couldn't load: {e}")
            write_response("\n".join(lines))

    elif command.startswith("START_ROUTE:"):
        route_name = command[len("START_ROUTE:"):]
        filepath = os.path.join(ROUTES_FOLDER, route_name + ".json")
        if not os.path.exists(filepath):
            write_response(f"Route file not found: {route_name}.json")
            return
        try:
            route = load_route_file(filepath)
        except Exception as e:
            write_response(f"Couldn't load route: {e}")
            return

        start = route.points[0]
        second = route.points[1]
        initial_facing = bearing_to(start["x"], start["y"], second["x"], second["y"])
        write_teleport_target(start["x"], start["y"], start["elevation_m"], initial_facing)
        await asyncio.sleep(2.0)  # let the teleport apply before we start tracking

        ride.start(route)

        os.makedirs(EXPORTS_FOLDER, exist_ok=True)
        base_name = f"{route.name}_{time.strftime('%Y%m%d_%H%M%S', time.gmtime(ride.start_time))}"
        ride.screenshot_base_name = base_name
        capture_screenshot(window_id, os.path.join(EXPORTS_FOLDER, f"{base_name}_start.png"))

        write_response(
            f"Started route: {route.name} ({route.length_m:.0f}m)\n"
            f"Debug log: {ride.debug_log_path}"
        )

    elif command == "STOP_RIDE":
        if ride.active and len(ride.log) > 1:
            export_path = write_tcx_export(ride.route.name, ride.log, ride.start_time)
            if ride.screenshot_base_name:
                capture_screenshot(
                    window_id,
                    os.path.join(EXPORTS_FOLDER, f"{ride.screenshot_base_name}_finish.png")
                )
            ride.stop()
            write_speed_target(WOW_BASE_RUN_SPEED_KMH)
            write_response(f"Ride stopped. Saved: {export_path}")
        else:
            ride.stop()
            write_speed_target(WOW_BASE_RUN_SPEED_KMH)
            write_response("Ride stopped.")

    elif command == "SNAP":
        os.makedirs(EXPORTS_FOLDER, exist_ok=True)
        base_name = ride.screenshot_base_name or time.strftime("%Y%m%d_%H%M%S")
        snap_num = int(time.time())
        path = os.path.join(EXPORTS_FOLDER, f"{base_name}_snap_{snap_num}.png")
        if capture_screenshot(window_id, path):
            write_response(f"Screenshot saved: {path}")
        else:
            write_response("Screenshot failed -- is 'scrot' installed?")

    elif command.startswith("SAVE_ROUTE:"):
        route_name = command[len("SAVE_ROUTE:"):]
        if not os.path.exists(RECORDING_FILE_PATH):
            write_response("No recording found. Use startroute/stoproute first.")
            return
        try:
            route_data = build_route_from_recording(RECORDING_FILE_PATH, route_name)
        except Exception as e:
            write_response(f"Couldn't build route: {e}")
            return

        os.makedirs(ROUTES_FOLDER, exist_ok=True)
        output_path = os.path.join(ROUTES_FOLDER, route_name + ".json")
        with open(output_path, "w") as f:
            json.dump(route_data, f, indent=2)

        total_distance = route_data["points"][-1]["distance_m"]
        write_response(f"Saved '{route_name}' ({total_distance:.0f}m). Try: startride {route_name}")


# ============================== Main loop ==============================

async def main():
    print(f"Routes folder: {ROUTES_FOLDER}")
    print(f"Looking for window matching '{WOW_WINDOW_NAME}'...")
    window_id = find_window_id(WOW_WINDOW_NAME)
    print(f"Window ID: {window_id}\n")

    print("Scanning for FTMS-compatible trainers (10s)...")
    devices = await BleakScanner.discover(timeout=10.0, service_uuids=[FTMS_SERVICE_UUID])
    if not devices:
        print("No FTMS devices found.")
        return

    device = devices[0]
    print(f"Connecting to {device.name} ({device.address})...")

    controller = TrainerController()
    ride = RideState()

    async with BleakClient(device.address) as client:
        print("Connected.\n")
        await client.start_notify(INDOOR_BIKE_DATA_UUID, controller.handle_bike_data)
        await client.start_notify(CONTROL_POINT_UUID, controller.handle_control_point_response)
        await controller.send_command(client, bytes([OP_REQUEST_CONTROL]))
        await controller.send_command(client, bytes([OP_START_RESUME]))

        print("Ready. In-game, type: routes | startride <name> | stopride")
        print("Press Ctrl+C here to fully quit.\n")

        # Guard against a previous session's ride ending abnormally (crash,
        # Ctrl+C mid-ride) and leaving a physics-computed speed stuck. This
        # only runs once at startup -- feel free to manually .modify speed
        # afterward for fast route recording, it won't be overridden.
        write_speed_target(WOW_BASE_RUN_SPEED_KMH)

        try:
            while True:
                await asyncio.sleep(UPDATE_INTERVAL)

                command = read_pending_command()
                if command:
                    await handle_command(command, ride, window_id)

                if ride.active:
                    live = read_live_position()
                    if live is not None:
                        _, lx, ly, lz, lo = live
                        result = ride.follower.update(lx, ly, lz, lo, window_id)
                        current_grade = result["grade_percent"]
                        ride.write_debug_row(result, current_grade, controller.latest_power)

                        # Trainer resistance uses the full real grade (authentic
                        # climbing/descending feel in your legs, untouched).
                        # Speed/steering physics uses a dampened downhill grade,
                        # so long descents don't build unmanageable virtual
                        # speed that makes every subsequent turn near-impossible
                        # to negotiate -- realism preserved where it matters
                        # (resistance), capped where it caused real problems
                        # (runaway speed on descents).
                        physics_grade = current_grade
                        if current_grade < 0:
                            physics_grade = current_grade * DOWNHILL_SPEED_DAMPING

                        ride.physics.step(controller.latest_power, physics_grade, UPDATE_INTERVAL)

                        if ride.last_sent_grade is None or \
                                abs(current_grade - ride.last_sent_grade) >= GRADE_SEND_THRESHOLD:
                            await controller.set_grade(client, current_grade)
                            ride.last_sent_grade = current_grade

                        speed_kmh = ride.physics.speed_ms * 3.6
                        write_speed_target(speed_kmh)
                        ride.log.append((
                            time.time(), result["progress_m"], lz,
                            controller.latest_power, ride.physics.speed_ms
                        ))
                        write_hud_data(
                            speed_kmh=speed_kmh,
                            power=controller.latest_power,
                            distance_m=result["progress_m"],
                            distance_to_go_m=ride.route.length_m - result["progress_m"],
                            grade_percent=current_grade,
                        )

                        print(
                            f"\rPower: {controller.latest_power:>4.0f} W | "
                            f"Speed: {speed_kmh:>5.1f} km/h | "
                            f"Progress: {result['progress_m']:>6.0f}/{ride.route.length_m:.0f} m | "
                            f"Grade: {current_grade:>5.1f} %   ",
                            end="", flush=True
                        )

                        if result["finished"]:
                            print(f"\n\nRoute '{ride.route.name}' complete!")
                            export_path = write_tcx_export(ride.route.name, ride.log, ride.start_time)
                            if ride.screenshot_base_name:
                                capture_screenshot(
                                    window_id,
                                    os.path.join(EXPORTS_FOLDER, f"{ride.screenshot_base_name}_finish.png")
                                )
                            write_response(f"Route complete: {ride.route.name}\nSaved: {export_path}")
                            ride.stop()
                            await controller.set_grade(client, 0.0)
                            write_speed_target(WOW_BASE_RUN_SPEED_KMH)
                # (No forced speed reset while idle -- you may deliberately
                # set a high .modify speed manually for fast route recording,
                # and this must not fight against that.)

        except KeyboardInterrupt:
            pass

        print("\n\nShutting down...")
        await controller.send_command(client, bytes([OP_RESET]))
        write_speed_target(WOW_BASE_RUN_SPEED_KMH)
        await client.stop_notify(INDOOR_BIKE_DATA_UUID)
        await client.stop_notify(CONTROL_POINT_UUID)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n\nInterrupted, exiting.")
