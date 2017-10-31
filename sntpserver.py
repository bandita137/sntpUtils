import time
import sys
import logging
import argparse
from functools import partial
from SntpLib import InjectError, NTPPacket, NTP, setup_logger, get_parser

logger = logging.getLogger()

class SntpServer(InjectError):
    def handle_received_packet(self, timestamp, addr, data):
        recvPacket = super().handle_received_packet(timestamp,addr,data)
        #could also check if packet is in broadcast mode
        if addr[0] in self.interface_addresses:
            logger.debug("Ignoring broadcast from self")
            return
        if NTP.MODE_TABLE[recvPacket.mode] == 'broadcast':
            logger.info("Ignoring broadcast from potentially other server")
            return
        #mode 4 == server
        sendPacket = NTPPacket(version=recvPacket.version,mode=4)
        sendPacket.orig_timestamp_high = recvPacket.tx_timestamp_high
        sendPacket.orig_timestamp_low = recvPacket.tx_timestamp_low
        sendPacket.ref_timestamp = recvTimestamp-5
        sendPacket.recv_timestamp = recvTimestamp
        sendPacket.tx_timestamp = time.time()
        logger.debug("Response Packet details: \n%s", sendPacket)
        self.send_queue.put((sendPacket,addr))

    def prepare_tx_outbound(self, timestamp, addrs):
        #mode == 5 == broadcast
        broadcastPacket = NTPPacket(tx_timestamp = timestamp, mode=5)
        broadcastPacket.ref_timestamp = broadcastPacket.tx_timestamp-5
        logger.debug("Broadcast Packet details: \n%s", broadcastPacket)
        for addr in addrs:
            self.send_queue.put((broadcastPacket,addr))



if __name__ == '__main__':

    parser = get_parser()
    parser.description = "Sntp Server"
    parser.add_argument('-b', type=int, default=0,
            help='broadcast interval in secs, defaults to 0 (no broadcast)')
    p = parser.parse_args()

    log_file = None
    if p.l:
        log_file = p.l[0]
    if p.v:
        setup_logger(logger, level=logging.DEBUG, file_path=log_file)
    else:
        setup_logger(logger, level=logging.INFO, file_path=log_file)

    if p.e > 0:
        cls = partial(SntpServer, p_error=p.e, error_list = p.errors)
    else:
        cls = SntpServer

    server = cls(p.address, p.port, p.b)

    while True:
        try:
            server.run()
        except KeyboardInterrupt:
            logger.info("Exiting...")
            break

