#from pysolarmanv5 import PySolarmanV5, V5FrameError
#import umodbus.exceptions
#import struct
import os

import deye

stick_logger_ip = os.environ.get("DEYE_LOGGER_IP",'')
stick_logger_serial = int(os.environ.get("DEYE_LOGGER_SERIAL",''))

def main():
    deye_inverter = deye.DeyeInverter(stick_logger_ip, stick_logger_serial)

    for i in range(10):
        deye_inverter.read_registers()
        deye_inverter.decode_registers()


if __name__ == "__main__":
    main()
