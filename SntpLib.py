import datetime
import socket
import struct
import time
import select
import sys
import logging
import logging.handlers
import argparse
import netifaces
import random
import queue

logger = logging.getLogger(__name__)

class TimeToHighLow:
    """Use descriptor rather than having to repeat a bunch of properties"""
    _SYSTEM_EPOCH = datetime.date(*time.gmtime(0)[0:3])
    _NTP_EPOCH = datetime.date(1900, 1, 1)
    NTP_DELTA = (_SYSTEM_EPOCH - _NTP_EPOCH).days * 24 * 3600
    """delta between system and NTP time"""
    def __set_name__(self, owner, name):
        self.name = name

    def __get__(self, instance, owner):
        return self._to_time(
                getattr(instance, self.name+'_high'),
                getattr(instance, self.name+'_low')) - self.NTP_DELTA

    def __set__(self, instance, value):
        ntp_value = value + self.NTP_DELTA
        high, low = self._to_high_low(ntp_value)
        setattr(instance, self.name+'_high', high)
        setattr(instance, self.name+'_low', low)


    def _to_time(self, integ, frac, n=32):
        """Return a timestamp from an integral and fractional part.

        Parameters:
        integ -- integral part
        frac  -- fractional part
        n     -- number of bits of the fractional part

        Returns:
        timestamp
        """
        return integ + float(frac)/2**n

    def _to_high_low(self, timestamp):
        """Return the high and low components of a timestamp

        Parameters:
            timestamp -- full floating point timestamp

        Returns:
            high,low
        """
        high = int(timestamp)
        return high, int((timestamp-high)*(2**32))

def get_network_addresses():
    ifaces = netifaces.interfaces()
    bcast_addresses = []
    interface_addresses = []
    for iface in ifaces:
        details = netifaces.ifaddresses(iface)
        for k, vals in details.items():
            if k == netifaces.AF_INET:
                for addr in vals:
                    bcast_addresses.append(addr['broadcast'])
                    interface_addresses.append(addr['addr'])
    return interface_addresses, bcast_addresses

def setup_logger(logger, level=logging.INFO, file_path=None):
    logger.setLevel(level)
    console_formatter = logging.Formatter(
            fmt='%(asctime)s - %(levelname)s - %(module)s - %(lineno)d - %(message)s')
    console_handler = logging.StreamHandler()
    console_handler.setFormatter(console_formatter)
    logger.addHandler(console_handler)

    if file_path:
        #also add a file handler
        file_formatter = logging.Formatter(
            fmt='%(asctime)s - %(levelname)s - %(funcName)s - %(lineno)d - %(message)s')
        file_handler = logging.handlers.TimedRotatingFileHandler(
                filename=file_path,
                when='D',
                backupCount=7 #allow for a week of logs
                )
        file_handler.setFormatter(file_formatter)
        logger.addHandler(file_handler)

def _to_frac(timestamp, n=32):
    """Return the fractional part of a timestamp.

    Parameters:
    timestamp -- NTP timestamp
    n         -- number of bits of the fractional part

    Retuns:
    fractional part
    """
    return int(abs(timestamp - int(timestamp)) * 2**n)


class NTPException(Exception):
    """Exception raised by this module."""
    pass


class NTP:
    """Helper class defining constants."""

    """reference identifier table"""
    REF_ID_TABLE = {
            'DNC': "DNC routing protocol",
            'NIST': "NIST public modem",
            'TSP': "TSP time protocol",
            'DTS': "Digital Time Service",
            'ATOM': "Atomic clock (calibrated)",
            'VLF': "VLF radio (OMEGA, etc)",
            'callsign': "Generic radio",
            'LORC': "LORAN-C radionavidation",
            'GOES': "GOES UHF environment satellite",
            'GPS': "GPS UHF satellite positioning",
    }

    """stratum table"""
    STRATUM_TABLE = {
        0: "unspecified",
        1: "primary reference",
    }

    """mode table"""
    MODE_TABLE = {
        0: "unspecified",
        1: "symmetric active",
        2: "symmetric passive",
        3: "client",
        4: "server",
        5: "broadcast",
        6: "reserved for NTP control messages",
        7: "reserved for private use",
    }

    """leap indicator table"""
    LEAP_TABLE = {
        0: "no warning",
        1: "last minute has 61 seconds",
        2: "last minute has 59 seconds",
        3: "alarm condition (clock not synchronized)",
    }

class NTPPacket:
    """NTP packet class.

    This represents an NTP packet.
    """
    """packet format to pack/unpack"""
    _PACKET_FORMAT = "!B B B b 11I"

    #setup some properties by using common descriptor
    orig_timestamp = TimeToHighLow()
    recv_timestamp = TimeToHighLow()
    tx_timestamp = TimeToHighLow()
    ref_timestamp = TimeToHighLow()


    def __init__(self, version=4, mode=4, tx_timestamp=0):
        """Constructor.

        Parameters:
        version      -- NTP version
        mode         -- packet mode (client, server)
        tx_timestamp -- packet transmit timestamp
        """

        """leap second indicator"""
        self.leap = 0
        self.version = version
        self.mode = mode
        self.stratum = 1
        self.poll = 10
        self.precision = -10
        self.root_delay = 0
        self.root_dispersion = 0
        self.ref_id = 0

        #timestamps
        self.ref_timestamp  = 0
        self.orig_timestamp = 0
        self.recv_timestamp = 0
        self.tx_timestamp = tx_timestamp

    def __str__(self):
        '''create a nice string representation of the packet data modelled off
        of format described in RFC2030:

                     1                   2                   3
       0 1 2 3 4 5 6 7 8 9 0 1 2 3 4 5 6 7 8 9 0 1 2 3 4 5 6 7 8 9 0 1
      +-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+
      |LI | VN  |Mode |    Stratum    |     Poll      |   Precision   |
      +-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+
      |                          Root Delay                           |
      +-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+
      |                       Root Dispersion                         |
      +-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+
      |                     Reference Identifier                      |
      +-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+
      |                                                               |
      |                   Reference Timestamp (64)                    |
      |                                                               |
      +-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+
      |                                                               |
      |                   Originate Timestamp (64)                    |
      |                                                               |
      +-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+
      |                                                               |
      |                    Receive Timestamp (64)                     |
      |                                                               |
      +-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+
      |                                                               |
      |                    Transmit Timestamp (64)                    |
      |                                                               |
      +-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+
      |                 Key Identifier (optional) (32)                |
      +-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+
      |                                                               |
      |                                                               |
      |                 Message Digest (optional) (128)               |
      |                                                               |
      |                                                               |
      +-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+'''
        packet_str = f'''\
LI|VN|Mode|Stratum|Poll|Precision: {self.leap}|{self.version}|{self.mode}-{NTP.MODE_TABLE[self.mode]}|{self.stratum}|{self.poll}|{self.precision}
Root Delay                       : {self.root_delay}
Root Dispersion                  : {self.root_dispersion}
Reference Identifier             : {self.ref_id}
Reference Timestamp (64)         : {self.get_timestamp_string(self.ref_timestamp)} : {self.ref_timestamp} 
Originate Timestamp (64)         : {self.get_timestamp_string(self.orig_timestamp)} : {self.orig_timestamp}
Receive Timestamp (64)           : {self.get_timestamp_string(self.recv_timestamp)} : {self.recv_timestamp}
Transmit Timestamp (64)          : {self.get_timestamp_string(self.tx_timestamp)}   : {self.tx_timestamp}'''
        return packet_str

    def get_timestamp_string(self, timestamp):
        return time.strftime('%Y-%m-%d-%H:%M:%S', time.localtime(timestamp))
    def to_data(self):
        """Convert this NTPPacket to a buffer that can be sent over a socket.

        Returns:
        buffer representing this packet

        Raises:
        NTPException -- in case of invalid field
        """
        try:
            packed = struct.pack(NTPPacket._PACKET_FORMAT,
                (self.leap << 6 | self.version << 3 | self.mode),
                self.stratum,
                self.poll,
                self.precision,
                int(self.root_delay) << 16 | _to_frac(self.root_delay, 16),
                int(self.root_dispersion) << 16 |
                _to_frac(self.root_dispersion, 16),
                self.ref_id,
                self.ref_timestamp_high,
                self.ref_timestamp_low,
                self.orig_timestamp_high,
                self.orig_timestamp_low,
                self.recv_timestamp_high,
                self.recv_timestamp_low,
                self.tx_timestamp_high,
                self.tx_timestamp_low)
        except struct.error:
            raise NTPException("Invalid NTP packet fields.")
        return packed

    @classmethod
    def from_data(cls, data):
        """Populate this instance from a NTP packet payload received from
        the network.

        Parameters:
        data -- buffer payload

        Raises:
        NTPException -- in case of invalid packet format
        """
        try:
            unpacked = struct.unpack(NTPPacket._PACKET_FORMAT,
                    data[0:struct.calcsize(NTPPacket._PACKET_FORMAT)])
        except struct.error:
            raise NTPException("Invalid NTP packet.")

        instance = cls()
        instance.leap = unpacked[0] >> 6 & 0x3
        instance.version = unpacked[0] >> 3 & 0x7
        instance.mode = unpacked[0] & 0x7
        instance.stratum = unpacked[1]
        instance.poll = unpacked[2]
        instance.precision = unpacked[3]
        instance.root_delay = float(unpacked[4])/2**16
        instance.root_dispersion = float(unpacked[5])/2**16
        instance.ref_id = unpacked[6]
        instance.ref_timestamp_high  = unpacked[7]
        instance.ref_timestamp_low   = unpacked[8]
        instance.orig_timestamp_high = unpacked[9]
        instance.orig_timestamp_low  = unpacked[10]
        instance.recv_timestamp_high = unpacked[11]
        instance.recv_timestamp_low  = unpacked[12]
        instance.tx_timestamp_high   = unpacked[13]
        instance.tx_timestamp_low    = unpacked[14]
        return instance

class SntpCore:
    def __init__(self,address, port, wait_interval, client=False):
        sock = socket.socket(socket.AF_INET,socket.SOCK_DGRAM)
        sock.bind((address,port))
        logger.info("local socket: %s", sock.getsockname());
        self.socket = sock
        self.wait_interval = wait_interval
        logging.debug("Broadcast interval set to: %d", wait_interval)
        self.port = port
        self.client=client
        self.send_queue = queue.Queue()

        if wait_interval:
            self.interface_addresses, self.broadcast_addresses = get_network_addresses()
            logger.debug("broadcast addresses: %s", self.broadcast_addresses)
        else:
            self.interface_addresses = []

    def pre_send_hook(self, pkt):
        "override me"
        pass

    def prepare_tx_outbound(self, timestamp, addrs):
        "override me"
        raise NotImplementedError("Need to override prepare_outbound method")

    def handle_received_packet(self, timestamp, addr, data):
        "override me"
        pkt = NTPPacket.from_data(data)
        #logger.debug("Received Packet details: \n%s", pkt)
        return pkt


    def run(self):
        last_output_time = time.time()
        while True:
            rlist,wlist,elist = select.select([self.socket],[self.socket],[],1);
            if len(rlist) != 0:
                sock = rlist[0]
                try:
                    data,addr = sock.recvfrom(1024)
                except socket.error as msg:
                    logging.critical(msg);
                logger.info("Received packet from %s", addr[0])
                recvTimestamp = time.time()
                self.handle_received_packet(recvTimestamp,addr,data)
            if len(wlist) != 0:
                sock = wlist[0]
                if not self.send_queue.empty():
                    pkt,addr = self.send_queue.get()
                    self.send_packet(sock, pkt, addr)
                else:
                    current_time = time.time()
                    if self.wait_interval and current_time - last_output_time > self.wait_interval:
                        last_output_time = current_time
                        self.prepare_tx_outbound(current_time, self.broadcast_addresses)
            time.sleep(0.1)

    def prepare_client_request(self, socket):
        request = NTPPacket(tx_timestamp = time.time())
        request.mode = 3 #client
        request.stratum = 0
        request.poll = 0
        request.precision = 0
        return request

    def send_packet(self, socket, pkt, addr):
        self.pre_send_hook(pkt)
        combined_addr = addr,self.port
        self.socket.sendto(pkt.to_data(),combined_addr)
        logger.info("Sending packet to %s:%d", combined_addr[0], combined_addr[1])
        #logger.debug("Sent Packet details: \n%s", pkt)

class InjectError(SntpCore):
    """Class which injects errors into the response"""

    def __init__(self, *args, p_error=0, error_list=None, **kwargs):
        super().__init__(*args, **kwargs)
        if error_list:
            self.error_list = [getattr(self, n) for n in error_list]
        else:
            self.error_list = (
                    self.originate_error,
                    self.li_error,
                    self.stratum_error,
                    self.vn_error)



        self.p_error = p_error

    def originate_error(self, pkt):
        logger.info("Injecting error: Modifying the originate timestamp...")
        pkt.orig_timestamp -= random.randint(1,1000)/10

    def li_error(self, pkt):
        logger.info("Injecting error: Setting LI to ALARM...")
        pkt.leap = 3

    def stratum_error(self, pkt):
        new_stratum = random.choice((1,15,16))
        logger.info("Injecting error: Setting stratum to: {}".format(new_stratum))
        pkt.stratum = new_stratum

    def vn_error(self, pkt):
        new_version = pkt.version + random.choice((1,-1))
        logger.info("Injecting error: Setting version to: {}".format(new_version))
        pkt.version = new_version

    def pre_send_hook(self, pkt):
        print("Does this happen?")
        if random.random() < self.p_error:
            random.choice(self.error_list)(pkt)


def get_parser():
    parser = argparse.ArgumentParser()
    parser.add_argument('-p', '--port', type=int, default=123,
        help='The port to listen on, defaults to 123')
    parser.add_argument('-a', '--address', default='0.0.0.0',
        help='The address to listen on, defaults to 0.0.0.0 (all interfaces)')
    parser.add_argument('-v', action='store_true', help='use verbose logging')
    parser.add_argument('-l', nargs=1, metavar='log_file_path',
            help='additionally log to a file')
    parser.add_argument('-e', type=float, default=0,
            help='Probability of error injection, float 0-1, defaults to 0')
    parser.add_argument('--errors', nargs='+', default=None, help='error functions to randomly invoke')
    return parser
