"""
Paste a JSON array of commands below (COMMANDS_JSON) and run this script
to get the final pose.

Usage:
    python3 get_final_pose.py

Just edit COMMANDS and START_X / START_Y / START_HEADING_DEG below,
then re-run.
"""

import json
import math
from dataclasses import dataclass

# =======================================================================
# EDIT THESE TWO THINGS
# =======================================================================

# Paste your commands array here as a plain Python list of dicts.
# (Note: Python uses True/False, not JSON's True/False - if you're pasting
# straight from a JSON source, just find/replace true->True, False->False.)

# START_X = 725.2811245041205
# START_Y = 300.1997593008729
# START_HEADING_DEG = 77.96358631621061
# Robot
# COMMANDS = [
#     {"kind": "arc", "radius": 11.805722215875214, "degrees": 111.51558471170722},
#     {"kind": "spin", "degrees": 64.43725810786809},
#     {"kind": "arc", "radius": 11.291554100214363, "degrees": 119.61070953262612},
#     {"kind": "spin", "degrees": 122.55331355673886},
#     {"kind": "line", "distance": 19.0355114318276, "penDown": False},
#     {"kind": "spin", "degrees": -6.372755803462858},
#     {"kind": "arc", "radius": 90.3982160205218, "degrees": 9.657591867633348},
#     {"kind": "spin", "degrees": 31.918792971222175},
#     {"kind": "line", "distance": 23.701030578806364, "penDown": False},
#     {"kind": "spin", "degrees": 143.78774131137686},
#     {"kind": "line", "distance": 19.343400274346894, "penDown": True},
#     {"kind": "spin", "degrees": 90.90443881225355},
#     {"kind": "line", "distance": 29.052500379787865, "penDown": True},
#     {"kind": "spin", "degrees": 88.4179973695437},
#     {"kind": "line", "distance": 19.826623146199346, "penDown": True},
#     {"kind": "spin", "degrees": -34.160815573392135},
#     {"kind": "line", "distance": 22.716791031744133, "penDown": False},
#     {"kind": "spin", "degrees": -43.19991048911258},
#     {"kind": "line", "distance": 4.150559719814167, "penDown": True},
#     {"kind": "spin", "degrees": 109.51145817231804},
#     {"kind": "arc", "radius": 41.379170854459666, "degrees": 119.28248835962039},
#     {"kind": "spin", "degrees": 125.39915259872822},
#     {"kind": "arc", "radius": 338.67734727522526, "degrees": -11.410622993500999},
#     {"kind": "spin", "degrees": -87.02105841657627},
#     {"kind": "line", "distance": 18.275402063391276, "penDown": True},
#     {"kind": "spin", "degrees": -85.88323301528884},
#     {"kind": "line", "distance": 52.47341334993302, "penDown": True},
#     {"kind": "spin", "degrees": -85.39616785496696},
#     {"kind": "arc", "radius": 79.22278466639118, "degrees": -14.984662972095142},
#     {"kind": "spin", "degrees": -29.798933224139294},
#     {"kind": "line", "distance": 24.785447316926298, "penDown": False},
#     {"kind": "spin", "degrees": 4.122790965154877},
#     {"kind": "arc", "radius": 19.464745012511358, "degrees": -69.46124791307359},
#     {"kind": "spin", "degrees": 80.35813798338886},
#     {"kind": "arc", "radius": 43.070936580258355, "degrees": -166.53280139138025},
#     {"kind": "spin", "degrees": -102.68454753351392},
#     {"kind": "line", "distance": 19.302136832555185, "penDown": False},
#     {"kind": "spin", "degrees": -143.52411015090786},
#     {"kind": "arc", "radius": 11.53625694540858, "degrees": 40.890566839734355},
#     {"kind": "spin", "degrees": -17.449492159920233},
#     {"kind": "arc", "radius": 2.944673176871454, "degrees": 196.68447585177842},
#     {"kind": "spin", "degrees": 19.88729898806042},
#     {"kind": "line", "distance": 8.969003923750874, "penDown": True},
#     {"kind": "spin", "degrees": 29.576192088051084},
#     {"kind": "line", "distance": 38.825757960626746, "penDown": False},
#     {"kind": "spin", "degrees": 97.76690256199709},
#     {"kind": "arc", "radius": 47.08236196378397, "degrees": 163.6478744667666},
#     {"kind": "spin", "degrees": -27.381807633958918},
#     {"kind": "arc", "radius": 22.852315145327434, "degrees": -76.14900722907022},
#     {"kind": "spin", "degrees": -81.80023361465892},
#     {"kind": "line", "distance": 50.684137875291576, "penDown": False},
#     {"kind": "spin", "degrees": 161.10451246481733},
#     {"kind": "arc", "radius": 16.554187989829984, "degrees": 149.75560030585106},
#     {"kind": "spin", "degrees": -28.07910742392503},
#     {"kind": "line", "distance": 26.939122582921957, "penDown": False},
#     {"kind": "spin", "degrees": -101.93314503156496},
#     {"kind": "arc", "radius": 22.212580928404904, "degrees": 157.9563262653098},
#     {"kind": "spin", "degrees": 17.872762969095305},
#     {"kind": "line", "distance": 26.708374494911066, "penDown": False},
#     {"kind": "spin", "degrees": 9.302909425055645},
#     {"kind": "arc", "radius": 38.08243527197146, "degrees": -144.36786864892838},
#     {"kind": "spin", "degrees": -128.7555782207125},
#     {"kind": "line", "distance": 12.201815899295399, "penDown": False},
#     {"kind": "spin", "degrees": 19.55125835835395},
#     {"kind": "line", "distance": 7.633293896484513, "penDown": True},
#     {"kind": "spin", "degrees": 6.967896752706458},
#     {"kind": "arc", "radius": 3.606652640699648, "degrees": -148.6206771635904},
#     {"kind": "spin", "degrees": 18.18162970298298},
#     {"kind": "line", "distance": 0.9527153645222021, "penDown": True},
#     {"kind": "spin", "degrees": -168.3419866116687},
#     {"kind": "line", "distance": 47.9090148956439, "penDown": False},
#     {"kind": "spin", "degrees": -85.79647059336193},
#     {"kind": "arc", "radius": 15.737063832730975, "degrees": 161.5146975788066},
#     {"kind": "spin", "degrees": 17.218964566352184},
#     {"kind": "line", "distance": 30.726134567869284, "penDown": False},
#     {"kind": "spin", "degrees": 129.7691528063709},
#     {"kind": "arc", "radius": 8.951843122860366, "degrees": 161.53367267785688},
#     {"kind": "spin", "degrees": 53.18615841094423},
#     {"kind": "arc", "radius": 21.17230425595948, "degrees": 36.9094138510707},
#     {"kind": "spin", "degrees": 56.12614793205501},
#     {"kind": "line", "distance": 8.49061961624792, "penDown": True},
#     {"kind": "spin", "degrees": 9.690225700310924},
#     {"kind": "line", "distance": 20.693479928340615, "penDown": False},
#     {"kind": "spin", "degrees": -8.060826127020125},
#     {"kind": "line", "distance": 9.985605109235282, "penDown": True},
#     {"kind": "spin", "degrees": 8.535091506988591},
#     {"kind": "line", "distance": 3.0081771311184067, "penDown": True},
#     {"kind": "spin", "degrees": 57.944905931608574},
#     {"kind": "arc", "radius": 10.128106430743626, "degrees": 145.83592676286537},
#     {"kind": "spin", "degrees": 65.282726266757},
#     {"kind": "line", "distance": 13.163251043119622, "penDown": True},
#     {"kind": "spin", "degrees": -17.974304081180144},
#     {"kind": "line", "distance": 22.843518150397905, "penDown": False},
#     {"kind": "spin", "degrees": -74.5257658895344},
#     {"kind": "line", "distance": 32.37580122882373, "penDown": True},
#     {"kind": "spin", "degrees": -76.69377207175404},
#     {"kind": "arc", "radius": 267.27035237073096, "degrees": -15.850127425933833},
#     {"kind": "spin", "degrees": -85.311528513227},
#     {"kind": "line", "distance": 70.90559133260331, "penDown": True},
#     {"kind": "spin", "degrees": -77.29340083211284},
#     {"kind": "arc", "radius": 261.08694561728805, "degrees": -17.225247864283673},
#     {"kind": "spin", "degrees": -96.38733346448767},
#     {"kind": "arc", "radius": 181.98637235492575, "degrees": 12.651236773436441},
#     {"kind": "spin", "degrees": 82.34818584629994},
#     {"kind": "arc", "radius": 30.816619400005145, "degrees": 64.30396160597901},
#     {"kind": "spin", "degrees": 60.18102040522036},
#     {"kind": "line", "distance": 2.7117549126620077, "penDown": True},
#     {"kind": "spin", "degrees": 12.237040730736004},
#     {"kind": "arc", "radius": 2.874211164783719, "degrees": 57.54484872543374},
#     {"kind": "spin", "degrees": -0.41535120046414115},
#     {"kind": "arc", "radius": 4.1596817646239765, "degrees": 161.73851254527776},
# ]


# Dog
# START_X = 570.4643754087219
# START_Y = 318.4047144958466
# START_HEADING_DEG = 240.00040105674034
COMMANDS = [
    {"kind": "line", "distance": 9.752451254246827, "penDown": True},
    {"kind": "spin", "degrees": 93.45354561132082},
    {"kind": "line", "distance": 19.480963735480252, "penDown": False},
    {"kind": "spin", "degrees": -138.35633304147854},
    {"kind": "line", "distance": 9.851449153117978, "penDown": True},
    {"kind": "spin", "degrees": 68.47912726568455},
    {"kind": "line", "distance": 14.051800172392097, "penDown": False},
    {"kind": "spin", "degrees": 67.09528164205754},
    {"kind": "line", "distance": 10.171347942610506, "penDown": True},
    {"kind": "spin", "degrees": -113.03613279563238},
    {"kind": "line", "distance": 21.94964232242123, "penDown": False},
    {"kind": "spin", "degrees": -112.5134780168301},
    {"kind": "line", "distance": 10.245498605120407, "penDown": True},
    {"kind": "spin", "degrees": 93.83271189465225},
    {"kind": "line", "distance": 20.3106234200599, "penDown": False},
    {"kind": "spin", "degrees": -139.6686436962079},
    {"kind": "line", "distance": 9.892739923476347, "penDown": True},
    {"kind": "spin", "degrees": 34.64479525530097},
    {"kind": "line", "distance": 13.406898932327492, "penDown": False},
    {"kind": "spin", "degrees": 11.068724824392461},
    {"kind": "arc", "radius": 10.201475196958961, "degrees": -360.0},
    {"kind": "spin", "degrees": -30.168760515382488},
    {"kind": "line", "distance": 21.484172125099835, "penDown": False},
    {"kind": "spin", "degrees": 31.149685321654964},
    {"kind": "line", "distance": 8.868390805676135, "penDown": True},
    {"kind": "spin", "degrees": 109.44321264713035},
    {"kind": "line", "distance": 21.48555255231315, "penDown": False},
    {"kind": "spin", "degrees": 115.21414238059373},
    {"kind": "line", "distance": 10.72858273516501, "penDown": True},
    {"kind": "spin", "degrees": -71.10350576088082},
    {"kind": "line", "distance": 14.14944147588146, "penDown": False},
    {"kind": "spin", "degrees": -67.3740466375381},
    {"kind": "line", "distance": 10.065436541296446, "penDown": True},
    {"kind": "spin", "degrees": -5.047159576432641},
    {"kind": "line", "distance": 27.012349647218667, "penDown": False},
    {"kind": "spin", "degrees": 72.7965801066377},
    {"kind": "arc", "radius": 27.803354045519306, "degrees": -142.00405949190687},
    {"kind": "spin", "degrees": -56.17548575566544},
    {"kind": "arc", "radius": 9.14166014222979, "degrees": -53.41422333455898},
    {"kind": "spin", "degrees": -11.183138804083848},
    {"kind": "line", "distance": 4.971707476023569, "penDown": True},
    {"kind": "spin", "degrees": 99.08760857759071},
    {"kind": "arc", "radius": 12.992721810751663, "degrees": -173.04449635380013},
    {"kind": "spin", "degrees": 102.76501718902122},
    {"kind": "line", "distance": 6.386834286990805, "penDown": True},
    {"kind": "spin", "degrees": -25.617110690554227},
    {"kind": "arc", "radius": 13.005943169918442, "degrees": -43.29382697176386},
    {"kind": "spin", "degrees": -52.230741597371384},
    {"kind": "line", "distance": 51.07232561640854, "penDown": False},
    {"kind": "spin", "degrees": 15.47430782673121},
    {"kind": "arc", "radius": 7.032430541640974, "degrees": 173.87708221835632},
    {"kind": "spin", "degrees": 35.31603487984639},
    {"kind": "arc", "radius": 19.886789928306488, "degrees": 151.86392305890388},
    {"kind": "spin", "degrees": 62.3685112601148},
    {"kind": "arc", "radius": 90.1497177867636, "degrees": 13.825888251675348},
    {"kind": "spin", "degrees": -64.93634308113488},
    {"kind": "arc", "radius": 2656.414863723469, "degrees": -1.0942847058633065},
    {"kind": "spin", "degrees": -15.074015417843261},
    {"kind": "arc", "radius": 40.37329383354574, "degrees": -13.775615407655316},
    {"kind": "spin", "degrees": 123.005013801001},
    {"kind": "line", "distance": 10.617577532325235, "penDown": False},
    {"kind": "spin", "degrees": 63.09867587790619},
    {"kind": "line", "distance": 25.364578541286715, "penDown": True},
    {"kind": "spin", "degrees": -40.5362741818116},
    {"kind": "arc", "radius": 40.371951214254715, "degrees": -113.20753045635055},
    {"kind": "spin", "degrees": -110.84385946750199},
    {"kind": "line", "distance": 13.530488765908064, "penDown": False},
    {"kind": "spin", "degrees": -59.416231182650606},
    {"kind": "line", "distance": 28.892765474451632, "penDown": True},
    {"kind": "spin", "degrees": 26.86742930606864},
    {"kind": "line", "distance": 61.19485573072987, "penDown": False},
    {"kind": "spin", "degrees": 139.8402148405733},
    {"kind": "line", "distance": 5.614632318229879, "penDown": True},
    {"kind": "spin", "degrees": 49.57834134207003},
    {"kind": "arc", "radius": 90.1486446289276, "degrees": 54.389211989035765},
    {"kind": "spin", "degrees": 40.25834597998924},
    {"kind": "arc", "radius": 19.92219335383538, "degrees": 56.63088979407903},
    {"kind": "spin", "degrees": -4.420690431972312},
    {"kind": "arc", "radius": 19.88407209530897, "degrees": 44.45323210362163},
    {"kind": "spin", "degrees": 139.91796466235456},
    {"kind": "arc", "radius": 32.48606012482161, "degrees": -77.8533185514398},
    {"kind": "spin", "degrees": 72.32669515754668},
    {"kind": "arc", "radius": 154.32826341607495, "degrees": -18.253415233944747},
    {"kind": "spin", "degrees": 68.12443489089944},
    {"kind": "line", "distance": 19.752008476665825, "penDown": False},
    {"kind": "spin", "degrees": 176.8772117742185},
    {"kind": "arc", "radius": 181.25001605700965, "degrees": 42.921027630552786},
]

# Where the robot starts. If you don't know / don't care, leave at 0,0,0
# and you'll get the pose in local/relative coordinates.
START_X = 570.4643754087219
START_Y = 318.4047144958466
START_HEADING_DEG = 240.00040105674034

# How finely arcs get flattened into straight segments (only affects the
# drawn strokes' precision, not really the final pose much).
ARC_STEP_DEG = 6.0


# =======================================================================
# Supporting types / functions (self-contained - no external imports needed)
# =======================================================================


@dataclass
class Pose:
    x: float
    y: float
    headingDegrees: float


@dataclass
class Command:
    kind: str
    distance: float = 0.0
    penDown: bool = True
    degrees: float = 0.0
    radius: float = 0.0


def parse_commands(data) -> list:
    """Turn a list of command dicts (or a JSON string) into Command objects."""
    if isinstance(data, str):
        data = json.loads(data)
    commands = []
    for c in data:
        commands.append(
            Command(
                kind=c["kind"],
                distance=c.get("distance", 0.0),
                penDown=c.get("penDown", True),
                degrees=c.get("degrees", 0.0),
                radius=c.get("radius", 0.0),
            )
        )
    return commands


def commands_to_strokes_with_pose(commands, arc_step_deg: float = 6.0):
    """Turtle-integrate drawing commands into pen-down polylines (local mm
    frame) and also return the final local pose (x, y, headingDegrees)."""

    x, y, heading = START_X, START_Y, START_HEADING_DEG
    strokes: list = []
    current: list = []

    def extend(nx: float, ny: float) -> None:
        nonlocal current
        if not current:
            current = [(x, y)]
        current.append((nx, ny))

    def flush() -> None:
        nonlocal current
        if len(current) >= 2:
            strokes.append(current)
        current = []

    for cmd in commands:
        if cmd.kind == "line":
            rad = math.radians(heading)
            nx = x + cmd.distance * math.cos(rad)
            ny = y + cmd.distance * math.sin(rad)
            if cmd.penDown:
                extend(nx, ny)
            else:
                flush()
            x, y = nx, ny
        elif cmd.kind == "spin":
            heading += cmd.degrees
        elif cmd.kind == "arc":
            steps = max(1, int(math.ceil(abs(cmd.degrees) / arc_step_deg)))
            dtheta = cmd.degrees / steps
            seg_len = cmd.radius * math.radians(abs(dtheta))
            for _ in range(steps):
                rad = math.radians(heading)
                nx = x + seg_len * math.cos(rad)
                ny = y + seg_len * math.sin(rad)
                extend(nx, ny)
                x, y = nx, ny
                heading += dtheta
        else:
            raise ValueError(f"Unknown drawing command kind: {cmd.kind!r}")

    flush()
    return strokes, Pose(x=x, y=y, headingDegrees=heading)


def local_to_world(
    local_pose: Pose, start_x: float, start_y: float, heading_deg: float
) -> Pose:
    rad = math.radians(heading_deg)
    cos_t, sin_t = math.cos(rad), math.sin(rad)
    world_x = local_pose.x * cos_t - local_pose.y * sin_t + start_x
    world_y = local_pose.x * sin_t + local_pose.y * cos_t + start_y
    world_heading = heading_deg + local_pose.headingDegrees
    return Pose(x=world_x, y=world_y, headingDegrees=world_heading)


# =======================================================================
# Run it
# =======================================================================

if __name__ == "__main__":
    commands = parse_commands(COMMANDS)
    print(f"Parsed {len(commands)} commands.\n")

    strokes, local_pose = commands_to_strokes_with_pose(
        commands, arc_step_deg=ARC_STEP_DEG
    )
    print(
        f"Final pose:  x={local_pose.x:.6f}, y={local_pose.y:.6f}, "
        f"headingDegrees={local_pose.headingDegrees:.6f}"
    )
