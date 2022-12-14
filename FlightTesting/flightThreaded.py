import logging
import math
from threading import Thread
import time
import json
import numpy as np
import cflib.crtp
from cflib.crazyflie import Crazyflie
from cflib.crazyflie.log import LogConfig
import socket
import struct
from multiprocessing import Process, Queue, Value

from cflib.crtp.crtpstack import CRTPPacket



OPTI_PORT = '1511'
OPTI_IP = '192.168.0.99'
CLIENT_PORT = '3500'



# Specify the uri of the drone to which we want to connect (if your radio
# channel is X, the uri should be 'radio://0/X/2M/E7E7E7E7E7')
uri = 'radio://0/23/2M/E7E7E7E7E7'

prev_alpha = 0
prev_time = 0

# Specify the variables we want to log (all at 100 Hz)
variables = [
    # State
    'ae483log.o_x',
    'ae483log.o_y',
    'ae483log.o_z',
    'ae483log.psi',
    'ae483log.theta',
    'ae483log.phi',
    'ae483log.v_x',
    'ae483log.v_y',
    'ae483log.v_z',
    'ae483log.w_x',
    'ae483log.w_y',
    'ae483log.w_z',
    'ae483log.alpha',
    'ae483com.tick',
    'ae483log.alpha_dot',
    'ae483log.x',
    'ae483log.y',
    'ae483log.z',
    # Setpoint
    'ae483log.o_x_des',
    'ae483log.o_y_des',
    'ae483log.o_z_des',
    # Input
    'ae483log.tau_x',
    'ae483log.tau_y',
    'ae483log.tau_z',
    'ae483log.f_z',
    # Motor power commands
    'ae483log.m_1',
    'ae483log.m_2',
    'ae483log.m_3',
    'ae483log.m_4',
]



class SimpleClient:
    def __init__(self, uri, use_controller=False, use_observer=False):
        self.init_time = time.time()
        self.use_controller = use_controller
        self.use_observer = use_observer
        self.cf = Crazyflie(rw_cache='./cache')
        self.cf.connected.add_callback(self.connected)
        self.cf.fully_connected.add_callback(self.fully_connected)
        self.cf.connection_failed.add_callback(self.connection_failed)
        self.cf.connection_lost.add_callback(self.connection_lost)
        self.cf.disconnected.add_callback(self.disconnected)
        print(f'Connecting to {uri}')
        self.cf.open_link(uri)
        self.is_fully_connected = False
        self.data = {}

    def connected(self, uri):
        print(f'Connected to {uri}')
    
    def fully_connected(self, uri):
        print(f'Fully connected to {uri}')
        self.is_fully_connected = True

        # Reset the stock EKF
        self.cf.param.set_value('kalman.resetEstimation', 1)

        # Enable the controller (1 for stock controller, 4 for ae483 controller)
        if self.use_controller:
            self.cf.param.set_value('stabilizer.controller', 4)
            self.cf.param.set_value('powerDist.motorSetEnable', 1)
        else:
            self.cf.param.set_value('stabilizer.controller', 1)
            self.cf.param.set_value('powerDist.motorSetEnable', 0)

        # Enable the observer (0 for disable, 1 for enable)
        if self.use_observer:
            self.cf.param.set_value('ae483par.use_observer', 1)
        else:
            self.cf.param.set_value('ae483par.use_observer', 0)

        # Start logging
        self.logconfs = []
        self.logconfs.append(LogConfig(name=f'LogConf0', period_in_ms=10))
        num_variables = 0
        for v in variables:
            num_variables += 1
            if num_variables > 5: # <-- could increase if you paid attention to types / sizes (max 30 bytes per packet)
                num_variables = 0
                self.logconfs.append(LogConfig(name=f'LogConf{len(self.logconfs)}', period_in_ms=10))
            self.data[v] = {'time': [], 'data': []}
            self.logconfs[-1].add_variable(v)
        for logconf in self.logconfs:
            try:
                self.cf.log.add_config(logconf)
                logconf.data_received_cb.add_callback(self.log_data)
                logconf.error_cb.add_callback(self.log_error)
                logconf.start()
            except KeyError as e:
                print(f'Could not start {logconf.name} because {e}')
                for v in logconf.variables:
                    print(f' - {v.name}')
            except AttributeError:
                print(f'Could not start {logconf.name} because of bad configuration')
                for v in logconf.variables:
                    print(f' - {v.name}')

    def connection_failed(self, uri, msg):
        print(f'Connection to {uri} failed: {msg}')

    def connection_lost(self, uri, msg):
        print(f'Connection to {uri} lost: {msg}')

    def disconnected(self, uri):
        print(f'Disconnected from {uri}')
        self.is_fully_connected = False

    def log_data(self, timestamp, data, logconf):
        for v in logconf.variables:
            self.data[v.name]['time'].append(timestamp)
            self.data[v.name]['data'].append(data[v.name])

    def log_error(self, logconf, msg):
        print(f'Error when logging {logconf}: {msg}')

    def move(self, x, y, z, yaw, dt):
        print(f'Move to {x}, {y}, {z} with yaw {yaw} degrees for {dt} seconds')
        start_time = time.time()
        while time.time() - start_time < dt:
            self.cf.commander.send_position_setpoint(x, y, z, yaw)
            time.sleep(0.1)

    def stop(self, dt):
        print(f'Stop for {dt} seconds')
        self.cf.commander.send_stop_setpoint()
        start_time = time.time()
        while time.time() - start_time < dt:
            time.sleep(0.1)

    def disconnect(self):
        self.cf.close_link()

    def write_data(self, filename='logged_data.json'):
        with open(filename, 'w') as outfile:
            json.dump(self.data, outfile, indent=4, sort_keys=False)


def optitrack(queue: Queue, run_process: Value):
    print('Beginning optitrack socket listener')
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.bind(('0.0.0.0', int(CLIENT_PORT)))
        print(f'Starting optitrack socket listener')
        while run_process.value == 1:
            data = s.recvfrom(1024)[0]
            if not data:
                print(f'No data received')
                break
            else:
                [x1,z1,y1, x2, z2, y2, x3, z3, y3] = struct.unpack('fffffffff', data)
                
                
                # Point 1: reference
                # Point 2: Tip
                # Point 3: Pivot

                u2 = np.array([x1, z1, y1])
                u1 = np.array([x2, z2, y2])
                u3 = np.array([x3, z3, y3])

                

                angle = np.degrees(np.arctan((u2[1]-u3[1])/np.sqrt(((u2[0]-u3[0])**2) + ((u2[2]-u3[2])**2))))
            
                z_offset = np.array([0, u3[1] - u1[1], 0])

                u1 += z_offset

                v1 = u2 - u3 #Pivot to Tip
                v2 = u1 - u3 #Pivot to Reference

                comp = np.dot(v2, v1)/np.linalg.norm(v2)

                # print(angle, comp)
                # print(u1, u2, u3)
                # print(v1, v2)
                # time.sleep(4)
                # print(x2 - x3, x1 - x3)
                # print(comp)


                if (comp < 0):
                    angle = angle - 90
                else:
                    angle = 90 - angle

                # print(angle)

                if queue.empty():
                    queue.put(np.radians(angle))


def send_angle(client, queue: Queue):
    print('starting send_angle thread')
    prev_alpha = 0
    prev_time = time.time()
    while client.is_fully_connected:
        t = time.time()
        alpha = queue.get()
        # alpha = 0
        x = 1
        y = 1
        z = 1
        alpha_dot = 0
        pk = CRTPPacket()
        pk.port = 0x0A
        pk.channel = 0
        # pk.data = struct.pack('<fff', x, y, z)
        pk.data = struct.pack('<fffff', x, y, z, alpha, alpha_dot)
        # Send the data packet to the drone
        client.cf.send_packet(pk)
        prev_alpha = alpha
        prev_time = t



if __name__ == '__main__':
    # Initialize everything
    # Listen to OptiTrack
    q = Queue()
    run_process = Value('b', 1)
    optitrack_process = Process(target=optitrack, args=(q, run_process))
    logging.info('beginning OptiTrack process')
    optitrack_process.start()
    # Initialize everything
    logging.basicConfig(level=logging.error)
    cflib.crtp.init_drivers()
    # Create and start the client that will connect to the drone
    client = SimpleClient(uri, use_controller=True, use_observer=False)
    while not client.is_fully_connected:
        print(f' ... connecting ...')
        time.sleep(2.0)
    # Send position estimates from queue
    send_pose_thread = Thread(target=send_angle, args=(client, q,))
    logging.info('beginning estimate thread')
    send_pose_thread.start()


    client.stop(2.0)
    # Take off and hover (with zero yaw)
    client.move(0.0, 0.0, 1.25, 0.0, 20)
   

    client.stop(2.0)
    # Disconnect from drone
    client.disconnect()

    send_pose_thread.join()
    run_process.value = 0
    optitrack_process.join()

    # Write data from flight
    client.write_data('FlightData\Flight_4.json')
