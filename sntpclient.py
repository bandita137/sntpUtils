import time
import sys
import logging
import argparse
from functools import partial
from SntpLib import InjectError, NTPPacket, NTP, setup_logger, get_parser

logger = logging.getLogger()

class SntpClient(InjectError):
    def handle_received_packet(self, timestamp, addr, data):
        recvPacket = super(SntpClient, self).handle_received_packet(timestamp,addr,data)
        if addr[0] in self.interface_addresses:
            logger.debug("Ignoring request from self")
            return
        mode_str = NTP.MODE_TABLE[recvPacket.mode]
        if mode_str == 'client':
            logger.info("Ignoring client request from potentially other client")
            logger.debug("Ignoring packet with contents: \n%s", recvPacket)
            return
        else:
            logger.info("Received %s packet:\n%s", mode_str, recvPacket)

    def prepare_tx_outbound(self, timestamp, addrs):
        #mode == 3 == client
        requestPacket = NTPPacket(tx_timestamp = timestamp, mode=3)
        requestPacket.ref_timestamp = 0
        requestPacket.stratum = 0
        requestPacket.poll = 0
        requestPacket.precision = 0
        logger.debug("Request Packet details: \n%s", requestPacket)
        for addr in addrs:
            self.send_queue.put((requestPacket,(addr,self.port)))



if __name__ == '__main__':

    parser = get_parser()
    parser.description = "Sntp Client"
    parser.add_argument('-b', type=int, default=0,
            help='Server request interval in secs, defaults to 0 (no requests)')
    p = parser.parse_args()

    log_file = None
    if p.l:
        log_file = p.l[0]
    if p.v:
        setup_logger(logger, level=logging.DEBUG, file_path=log_file)
    else:
        setup_logger(logger, level=logging.INFO, file_path=log_file)

    if p.e > 0:
        cls = partial(SntpClient, p_error=p.e, error_list = p.errors)
    else:
        cls = SntpClient

    server = cls(p.address, p.port, p.b)

    while True:
        try:
            server.run()
        except KeyboardInterrupt:
            logger.info("Exiting...")
            break

