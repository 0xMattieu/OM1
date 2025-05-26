import logging
import math
import threading
import time
from typing import Optional

import serial
import zenoh

try:
    # Needed for Unitree but not TurtleBot4
    from unitree.unitree_sdk2py.core.channel import ChannelSubscriber
    from unitree.unitree_sdk2py.idl.geometry_msgs.msg.dds_ import PoseStamped_
except ImportError:
    logging.warning(
        "Unitree SDK or Cyclone not found. You do not need this unless you are connecting to a Unitree robot."
    )

from zenoh_idl import nav_msgs

from .singleton import singleton

rad_to_deg = 57.2958

gOdom = None


def listenerOdom(data):
    global gOdom
    gOdom = nav_msgs.Odometry.deserialize(data.payload.to_bytes())
    logging.debug(f"Odom listener: {gOdom}")


@singleton
class NavigationProvider:
    """
    Navigation Provider.

    This class implements a singleton pattern to manage:
        * Odom and pose data using either Zenoh or CycloneDDS
        * An optional external IMU/GPS, typically an Arduino Feather

    Parameters
    ----------
    wait: bool = False
        Whether to wait for another class to init this driver, somewhere else in the codebase
    URID: str = ""
        The URID needed to connect to the right Zenoh publisher in the local network
    use_zenoh: bool = False
        Whether to get odom/pose data from Zenoh - typically used by TurtleBot4
    gps_serial_port: str = "/dev/cu.usbmodem8014"
        The name of the serial port in use by the GPS/Mag sensor.
    """

    def __init__(
        self,
        wait: bool = False,
        URID: str = "",
        use_zenoh: bool = False,
        gps_serial_port: str = None,
    ):
        """
        Robot and sensor configuration
        """

        logging.info("Booting Navigation and State Provider")

        if wait:
            # no need to reinit driver
            return

        self.use_zenoh = use_zenoh
        self.URID = URID

        self.session = None
        self.low_state = None
        self.lowstate_subscriber = None

        if self.use_zenoh:
            # typically, TurtleBot4
            if URID is None:
                logging.warning(
                    "Aborting TurtleBot4 Navigation system, no URID provided"
                )
                return
            else:
                logging.info(f"TurtleBot4 Navigation system is using URID: {URID}")

            try:
                self.session = zenoh.open(zenoh.Config())
                logging.info(f"Zenoh navigation provider opened {self.session}")
                logging.info(
                    f"TurtleBot4 navigation listeners starting with URID: {URID}"
                )
                self.session.declare_subscriber(f"{URID}/c3/odom", listenerOdom)
            except Exception as e:
                logging.error(f"Error opening Zenoh client: {e}")
        elif not self.use_zenoh:
            # we are using CycloneDDS e.g. for the Unitree Go2
            try:
                self.pose_subscriber = ChannelSubscriber(
                    "rt/utlidar/robot_pose", PoseStamped_
                )
                self.pose_subscriber.Init(self.poseMessageHandler, 10)
            except Exception as e:
                logging.error(f"Error opening CycloneDDS client: {e}")

        baudrate = 115200
        timeout = 1  # Optional: set a timeout for reading

        self.ser = None

        if gps_serial_port:
            try:
                # Open the serial port
                self.ser = serial.Serial(gps_serial_port, baudrate, timeout=timeout)
                logging.info(f"Connected to {gps_serial_port} at {baudrate} baud")
            except serial.SerialException as e:
                logging.error(f"Error: {e}")

        self.body_height_cm = 0
        self.body_attitude = None

        self.moving = None
        self.previous_x = 0
        self.previous_y = 0
        self.previous_z = 0
        self.move_history = 0

        self._position: Optional[dict] = None

        self.x = 0.0
        self.y = 0.0
        self.z = 0.0

        self.yaw_odom_0_360 = 0.0
        self.yaw_odom_m180_p180 = 0.0

        self.yaw_mag_0_360 = 0.0
        self.yaw_mag_cardinal = ""
        self.gps_lat = 0.0
        self.gps_lon = 0.0
        self.gps_alt = 0.0
        self.gps_time = ""

        self.running = False
        self._thread: Optional[threading.Thread] = None
        self.start()

    def euler_from_quaternion(self, x, y, z, w):
        """
        https://automaticaddison.com/how-to-convert-a-quaternion-into-euler-angles-in-python/
        Convert a quaternion into euler angles (roll, pitch, yaw)
        roll is rotation around x in radians (counterclockwise)
        pitch is rotation around y in radians (counterclockwise)
        yaw is rotation around z in radians (counterclockwise)
        """
        t0 = +2.0 * (w * x + y * z)
        t1 = +1.0 - 2.0 * (x * x + y * y)
        roll_x = math.atan2(t0, t1)

        t2 = +2.0 * (w * y - z * x)
        t2 = +1.0 if t2 > +1.0 else t2
        t2 = -1.0 if t2 < -1.0 else t2
        pitch_y = math.asin(t2)

        t3 = +2.0 * (w * z + x * y)
        t4 = +1.0 - 2.0 * (y * y + z * z)
        yaw_z = math.atan2(t3, t4)

        return roll_x, pitch_y, yaw_z  # in radians

    def processOdom(self, pose):

        logging.debug(f"pose: {pose}")

        x = pose.orientation.x
        y = pose.orientation.y
        z = pose.orientation.z
        w = pose.orientation.w

        dx = (pose.position.x - self.previous_x) ** 2
        dy = (pose.position.y - self.previous_y) ** 2
        dz = (pose.position.z - self.previous_z) ** 2

        self.previous_x = pose.position.x
        self.previous_y = pose.position.y
        self.previous_z = pose.position.z

        delta = math.sqrt(dx + dy + dz)

        # moving? Use a decay kernal
        self.move_history = 0.4 * delta + 0.6 * self.move_history

        if delta > 0.01 or self.move_history > 0.005:
            # we want to detect movement quickly (and flip "moving" False->True), but
            # wait for the platform to stabilize before we flip "moving" True->False
            self.moving = True
            logging.info(f"delta moving: {delta} {self.move_history}")
        else:
            self.moving = False

        angles = self.euler_from_quaternion(x, y, z, w)

        self.m180_p180 = angles[2] * rad_to_deg * -1.0
        # the * -1.0 changes the heading to sane convention
        # turn right (CW) to INCREASE your heading
        # runs from -180 to + 180, where 0 is the "nose" of the robot

        # runs from 0 to 360
        self.yaw_odom_0_360 = self.m180_p180 + 180.0

        # current position in world frame
        self.x = pose.position.x
        self.y = pose.position.y

        self._position = {
            "x": self.x,
            "y": self.y,
            "yaw_odom_0_360": self.yaw_odom_0_360,
            "yaw_odom_m180_p180": self.m180_p180,
            "yaw_mag_0_360": self.yaw_mag_0_360,
            "yaw_mag_cardinal": self.yaw_mag_cardinal,
            "body_height_cm": self.body_height_cm,
            "body_attitude": self.body_attitude,
            "moving": self.moving,
            "gps_lat": self.gps_lat,
            "gps_lon": self.gps_lon,
            "gps_alt": self.gps_alt,
            "gps_time": self.gps_time,
        }

        logging.debug(
            f"NAV x,y,yaw_odom,yaw_mag: {round(self.x,2)},{round(self.y,2)},{round(self.yaw_odom_0_360,2)},{round(self.yaw_mag_0_360,2)}, moving: {self.moving})"
        )

    def poseMessageHandler(self, msg):
        # used by CycloneDDS / Unitree Go2

        p = msg.pose
        self.body_height_cm = round(p.position.z * 100.0)

        # only relevant to dog
        if self.body_height_cm > 24:
            self.body_attitude = "standing"
        elif self.body_height_cm > 3:
            self.body_attitude = "sitting"

        self.processOdom(p)

    def zenohOdomProcessor(self):
        # used by Zenoh / TurtleBot4
        global gOdom
        if gOdom is not None:
            self.body_height_cm = 0.0
            self.body_attitude = None
            p = gOdom.pose.pose
            self.processOdom(p)

    def magGPSProcessor(self, data):
        # Used whenever there is a connected
        # nav Arduino on serial
        # Parses lines like:
        # - GPS:37.7749N,-122.4194W,KN:0.12,HEAD:84.1,ALT:30.5,SAT:7,TIME:3:14:24:546
        # - YPR: 134.57, -3.20, 1.02
        # - HDG (DEG): 225.0 SW NTC_HDG: 221.3
        try:
            if data.startswith("HDG (DEG):"):
                parts = data.split()
                if len(parts) >= 4:
                    # that's a HDG packet
                    self.yaw_mag_0_360 = float(parts[2])
                    self.yaw_mag_cardinal = parts[3]
                    logging.debug(f"MAG: {self.yaw_mag_0_360}")
                else:
                    logging.warning(f"Unable to parse heading: {data}")
            elif data.startswith("YPR:"):
                yaw, pitch, roll = map(str.strip, data[4:].split(","))
                logging.debug(
                    f"Orientation is Yaw: {yaw}°, Pitch: {pitch}°, Roll: {roll}°."
                )
            elif data.startswith("GPS:"):
                try:
                    parts = data[4:].split(",")
                    lat = parts[0]
                    lon = parts[1]
                    heading = parts[3].split(":")[1]
                    alt = parts[4].split(":")[1]
                    sats = parts[5].split(":")[1]
                    time = parts[6][5:]
                    self.gps_lat = lat
                    self.gps_lon = lon
                    self.gps_alt = alt
                    self.gps_time = time
                    logging.debug(
                        (
                            f"Current location is {lat}, {lon} at {alt} meters altitude, "
                            f"heading {heading}°, with {sats} satellites locked. The time is {time}."
                        )
                    )
                except Exception as e:
                    logging.warning(f"Failed to parse GPS: {data} ({e})")
        except Exception as e:
            logging.warning(f"Error processing serial MAG/GPSinput: {data} ({e})")

        self._position = {
            "x": self.x,
            "y": self.y,
            "yaw_odom_0_360": self.yaw_odom_0_360,
            "yaw_odom_m180_p180": self.m180_p180,
            "yaw_mag_0_360": self.yaw_mag_0_360,
            "yaw_mag_cardinal": self.yaw_mag_cardinal,
            "body_height_cm": self.body_height_cm,
            "body_attitude": self.body_attitude,
            "moving": self.moving,
            "gps_lat": self.gps_lat,
            "gps_lon": self.gps_lon,
            "gps_alt": self.gps_alt,
            "gps_time": self.gps_time,
        }

    def start(self):
        """
        Starts the Navigation Provider and processing thread
        if not already running.
        """
        if self._thread and self._thread.is_alive():
            return

        self.running = True
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self):
        """
        Main loop for the Navigation provider.
        """
        while self.running:

            if self.use_zenoh:
                # TurtleBot4
                logging.debug("zenohOdomProcessor")
                self.zenohOdomProcessor()
            else:
                # Unitree Go2 - using CycloneDDS
                logging.debug(
                    f"Cyclone Nav Provider: {self.body_height_cm} position: {self.position}"
                )

            if self.ser:
                # Read a line, decode, and remove whitespace
                data = self.ser.readline().decode("utf-8").strip()
                self.magGPSProcessor(data)
                logging.debug(f"Serial GPS: {data}")

            time.sleep(0.1)

    def stop(self):
        """
        Stop the Navigation provider.
        """
        self.running = False
        if self._thread:
            logging.info("Stopping Navigation provider")
            self._thread.join(timeout=5)

    @property
    def position(self) -> Optional[dict]:
        # """
        # Get the current robot position
        # """
        return self._position
