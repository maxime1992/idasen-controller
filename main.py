#!python3
import os
import sys
import struct
import argparse
import yaml
import asyncio
import traceback 
from signal import SIGINT, SIGTERM
from bleak import BleakClient, BleakError, BleakScanner
import atexit
import pickle
from pickle import UnpicklingError

IS_LINUX = os.name == 'posix'
IS_WINDOWS = os.name == 'nt'

# HELPER FUNCTIONS

def mmToRaw(mm):
    return (mm - BASE_HEIGHT) * 10

def rawToMM(raw):
    return (raw / 10) + BASE_HEIGHT

def rawToSpeed(raw):
    return (raw / 100)

# GATT CHARACTERISTIC AND COMMAND DEFINITIONS

UUID_HEIGHT = '99fa0021-338a-1024-8a49-009c0215f78a'
UUID_COMMAND = '99fa0002-338a-1024-8a49-009c0215f78a'
UUID_REFERENCE_INPUT = '99fa0031-338a-1024-8a49-009c0215f78a'

COMMAND_UP = bytearray(struct.pack("<H", 71))
COMMAND_DOWN = bytearray(struct.pack("<H", 70))
COMMAND_STOP = bytearray(struct.pack("<H", 255))

COMMAND_REFERENCE_INPUT_STOP = bytearray(struct.pack("<H", 32769))
COMMAND_REFERENCE_INPUT_UP = bytearray(struct.pack("<H", 32768))
COMMAND_REFERENCE_INPUT_DOWN = bytearray(struct.pack("<H", 32767))

# CONFIGURATION SETUP

# Height of the desk at it's lowest (in mm)
# I assume this is the same for all Idasen desks
BASE_HEIGHT = 620
MAX_HEIGHT = 1270  # 6500

# Default config
config = {
    "mac_address": None,
    "stand_height": BASE_HEIGHT + 420,
    "sit_height": BASE_HEIGHT + 63,
    "height_tolerance": 2.0,
    "adapter_name": 'hci0',
    "scan_timeout": 5,
    "connection_timeout": 10,
    "movement_timeout": 30,
    "sit": False,
    "stand": False,
    "monitor": False,
    "move_to": None
}

# Overwrite from config.yaml
config_file = {}
config_file_path = os.path.join(os.path.dirname(
    os.path.realpath(__file__)), 'config.yaml')
if (config_file_path):
    with open(config_file_path, 'r') as stream:
        try:
            config_file = yaml.safe_load(stream)
        except yaml.YAMLError as exc:
            print("Reading config.yaml failed")
            exit(1)
config.update(config_file)

# Overwrite from command line args
parser = argparse.ArgumentParser(description='')
parser.add_argument('--mac-address', dest='mac_address',
                    type=str, help="Mac address of the Idasen desk")
parser.add_argument('--stand-height', dest='stand_height', type=int,
                    help="The height the desk should be at when standing (mm)")
parser.add_argument('--sit-height', dest='sit_height', type=int,
                    help="The height the desk should be at when sitting (mm)")
parser.add_argument('--height-tolerance', dest='height_tolerance', type=float,
                    help="Distance between reported height and target height before ceasing move commands (mm)")
parser.add_argument('--adapter', dest='adapter_name', type=str,
                    help="The bluetooth adapter device name")
parser.add_argument('--scan-timeout', dest='scan', type=int,
                    help="The timeout for bluetooth scan (seconds)")
parser.add_argument('--connection-timeout', dest='connection_timeout', type=int,
                    help="The timeout for bluetooth connection (seconds)")
parser.add_argument('--movement-timeout', dest='movement_timeout', type=int,
                    help="The timeout for waiting for the desk to reach the specified height (seconds)")
cmd = parser.add_mutually_exclusive_group()
cmd.add_argument('--sit', dest='sit', action='store_true',
                 help="Move the desk to sitting height")
cmd.add_argument('--stand', dest='stand', action='store_true',
                 help="Move the desk to standing height")
cmd.add_argument('--monitor', dest='monitor', action='store_true',
                 help="Monitor desk height and speed")
cmd.add_argument('--move-to',dest='move_to', type=int,
                 help="Move desk to specified height (mm)")
cmd.add_argument('--scan', dest='scan_adapter', action='store_true',
                 help="Scan for devices using the configured adapter")

args = {k: v for k, v in vars(parser.parse_args()).items() if v is not None}
config.update(args)

if not config['mac_address']:
    parser.error("Mac address must be provided")

if config['sit_height'] >= config['stand_height']:
    parser.error("Sit height must be less than stand height")

if config['sit_height'] < BASE_HEIGHT:
    parser.error("Sit height must be greater than {}".format(BASE_HEIGHT))

if config['stand_height'] > MAX_HEIGHT:
    parser.error("Stand height must be less than {}".format(MAX_HEIGHT))

config['stand_height_raw'] = mmToRaw(config['stand_height'])
config['sit_height_raw'] = mmToRaw(config['sit_height'])
config['height_tolerance_raw'] = 10 * config['height_tolerance']
if config['move_to']:
    config['move_to_raw'] = mmToRaw(config['move_to'])

if IS_WINDOWS:
    # Windows doesn't use this paramter so rename it so it looks nice for the logs
    config['adapter_name'] = 'default adapter'

# MAIN PROGRAM

def print_height_data(sender, data):
    height, speed = struct.unpack("<Hh", data)
    print("Height: {:4.0f}mm Speed: {:2.0f}mm/s".format(rawToMM(height), rawToSpeed(speed)))

def has_reached_target(height, target):
    # The notified height values seem a bit behind so try to stop before
    # reaching the target value to prevent overshooting
    return (abs(height - target) <= config['height_tolerance_raw'])

async def move_up(client):
    await client.write_gatt_char(UUID_COMMAND, COMMAND_UP)

async def move_down(client):
    await client.write_gatt_char(UUID_COMMAND, COMMAND_DOWN)

async def stop(client):
    # This emulates the behaviour of the app. Stop commands are sent to both
    # Reference Input and Command characteristics.
    await client.write_gatt_char(UUID_COMMAND, COMMAND_STOP)
    if IS_LINUX:
        # It doesn't like this on windows
        await client.write_gatt_char(UUID_REFERENCE_INPUT, COMMAND_REFERENCE_INPUT_STOP)

stop_flag = False

def asked_to_stop():
    global stop_flag
    return stop_flag

def ask_to_stop():
    global stop_flag
    stop_flag = True


async def subscribe(client, uuid, callback):
    """Listen for notifications on a characteristic"""
    await client.start_notify(uuid, callback)

    while not asked_to_stop():
        await asyncio.sleep(0.1)

    await client.stop_notify(uuid)

async def move_to(client, target):
    """Move the desk to a specified height"""

    initial_height, speed = struct.unpack("<Hh", await client.read_gatt_char(UUID_HEIGHT))

    # Initialise by setting the movement direction
    direction = "UP" if target > initial_height else "DOWN"
    
    # Set up callback to run when the desk height changes. It will resend
    # movement commands until the desk has reached the target height.
    global count
    count = 0
    def _move_to(sender, data):
        global count
        height, speed = struct.unpack("<Hh", data)
        count = count + 1
        print("Height: {:4.0f}mm Target: {:4.0f}mm Speed: {:2.0f}mm/s".format(rawToMM(height), rawToMM(target), rawToSpeed(speed)))

       
        # Stop if we have reached the target OR
        # If you touch desk control while the script is running then movement
        # callbacks stop. The final call will have speed 0 so detect that 
        # and stop.
        if  has_reached_target(height, target):
            asyncio.create_task(stop(client))
            ask_to_stop()
        # Or resend the movement command if we have not yet reached the
        # target.
        # Each movement command seems to run the desk motors for about 1
        # second if uninterrupted and the height value is updated about 16
        # times.
        # Resending the command on the 6th update seems a good balance
        # between helping to avoid overshoots and preventing stutterinhg
        # (the motor seems to slow if no new move command has been sent)
        elif direction == "UP" and count == 6:
            asyncio.create_task(move_up(client))
            count = 0
        elif direction == "DOWN" and count == 6:
            asyncio.create_task(move_down(client))
            count = 0

    # Listen for changes to desk height and send first move command (if we are 
    # not already at the target height).
    if not has_reached_target(initial_height, target):
        tasks = [ subscribe(client, UUID_HEIGHT, _move_to) ]
        if direction == "UP":
            tasks.append(move_up(client))
        elif direction == "DOWN":
            tasks.append(move_down(client))
        try:
            await asyncio.wait_for(asyncio.gather(*[task for task in tasks]), timeout=config['movement_timeout'])
        except asyncio.TimeoutError as e:
            print('Timed out while waiting for desk')
            await client.stop_notify(UUID_HEIGHT)

def unpickle_desk():
    """Load a Bleak device config from a pickle file and check that it is the correct device"""
    try:
        if not IS_WINDOWS:
            with open("desk.pickle",'rb') as f:
                desk = pickle.load(f)
                if desk.address == config['mac_address']:
                    return desk
    except Exception:
        pass
    return None

def pickle_desk(desk):
    """Attempt to pickle the desk"""
    if not IS_WINDOWS:
        with open('desk.pickle', 'wb') as f: 
            pickle.dump(desk, f)

async def scan(mac_address = None):
    """Scan for a bluetooth device with the configured address and return it or return all devices if no address specified"""
    print('Scanning\r', end ="")
    scanner = BleakScanner()
    devices = await scanner.discover(device=config['adapter_name'], timeout=config['scan_timeout'])
    if not mac_address:
        return devices
    for device in devices:
        if (device.address == mac_address):
            print('Scanning - Desk Found')
            return device
    print('Scanning - Desk {} Not Found'.format(mac_address))
    return None

async def connect(desk):
    """Attempt to connect to the desk"""
    try:
        print('Connecting\r', end ="")
        client = BleakClient(desk, device=config['adapter_name'])
        await client.connect(timeout=config['connection_timeout'])
        return client 
    except BleakError as e:
        print('Connecting failed')
        os._exit(1)
        raise e

client = None

async def run():
    """Begin the action specified by command line arguments and config"""
    global client
    try:
        # Scanning doesn't require a connection so do it first and exit
        if config['scan_adapter']:
            devices = await scan()
            print('Found {} devices using {}'.format(len(devices), config['adapter_name']))
            for device in devices:
                print(device)
            os._exit(0)

        # Attempt to load and connect to the pickled desk
        desk = unpickle_desk()
        if not desk:
            # If that fails then rescan for the desk
            desk = await scan(config['mac_address'])
        if not desk:
            print('Could not find desk {}'.format(config['mac_address']))
            os._exit(1)
        
        client = await connect(desk)
            
        # Cache the Bleak device config to connect more quickly in future
        pickle_desk(desk)

        def disconnect_callback(client):
            if not asked_to_stop():
                print("Lost connection with {}".format(client.address))
            ask_to_stop()
        client.set_disconnected_callback(disconnect_callback)

        print("Connected {}".format(config['mac_address']))
        # Always print current height
        initial_height, speed = struct.unpack("<Hh", await client.read_gatt_char(UUID_HEIGHT))
        print("Height: {:4.0f}mm".format(rawToMM(initial_height)))
        target = None
        if config['monitor']:
            # Print changes to height data
            await subscribe(client, UUID_HEIGHT, print_height_data)
        elif config['sit']:
            # Move to configured sit height
            target = config['sit_height_raw']
            await move_to(client, target)
        elif config['stand']:
            # Move to configured stand height
            target = config['stand_height_raw']
            await move_to(client, target)
        elif config['move_to']:
            # Move to custom height
            target = config['move_to_raw']
            await move_to(client, target)
        if target:
            # If we were moving to a target height, wait, then print the actual final height
            await asyncio.sleep(1)
            final_height, speed = struct.unpack("<Hh", await client.read_gatt_char(UUID_HEIGHT))
            print("Final height: {:4.0f}mm Target: {:4.0f}mm)".format(rawToMM(final_height), rawToMM(target)))
    except BleakError as e:
        print(e)
    except Exception as e:
        traceback.print_exc() 

def main():
    """Set up the async event loop and signal handlers"""
    loop = asyncio.get_event_loop()

    if IS_LINUX:
        for sig in (SIGINT, SIGTERM):
            # We must run client.disconnect() so attempt to exit gracefully
            # Windows seems to care a lot less about this
            loop.add_signal_handler(sig, ask_to_stop)

    loop.run_until_complete(run())

    if client:
        print('\rDisconnecting\r', end="")
        ask_to_stop()
        loop = asyncio.get_event_loop()
        loop.run_until_complete(client.disconnect())
        print('Disconnected         ')

    loop.stop()
    loop.close()

if __name__ == "__main__":
    main()
