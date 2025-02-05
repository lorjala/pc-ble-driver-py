#
# Copyright (c) 2019 Nordic Semiconductor ASA
# All rights reserved.
#
# Redistribution and use in source and binary forms, with or without modification,
# are permitted provided that the following conditions are met:
#
#   1. Redistributions of source code must retain the above copyright notice, this
#   list of conditions and the following disclaimer.
#
#   2. Redistributions in binary form must reproduce the above copyright notice, this
#   list of conditions and the following disclaimer in the documentation and/or
#   other materials provided with the distribution.
#
#   3. Neither the name of Nordic Semiconductor ASA nor the names of other
#   contributors to this software may be used to endorse or promote products
#   derived from this software without specific prior written permission.
#
#   4. This software must only be used in or with a processor manufactured by Nordic
#   Semiconductor ASA, or in or with a processor manufactured by a third party that
#   is used in combination with a processor manufactured by Nordic Semiconductor.
#
#   5. Any software provided in binary or object form under this license must not be
#   reverse engineered, decompiled, modified and/or disassembled.
#
# THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS" AND
# ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE IMPLIED
# WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE ARE
# DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT HOLDER OR CONTRIBUTORS BE LIABLE FOR
# ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL DAMAGES
# (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR SERVICES;
# LOSS OF USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER CAUSED AND ON
# ANY THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY, OR TORT
# (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE OF THIS
# SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.
#


import unittest
from queue import Queue
from threading import Thread
import random
import string
import logging

from pc_ble_driver_py.observers import BLEDriverObserver, BLEAdapterObserver
from driver_setup import Settings, setup_adapter

from pc_ble_driver_py.ble_driver import (
    BLEAdvData,
    BLEGapIOCaps,
    BLEGapSecStatus,
    BLEGapSecParams,
    BLEGapSecKDist,
    util,
    driver
)

logger = logging.getLogger(__name__)
passkeyQueue = Queue()
authStatusQueue = Queue()


class Central(BLEDriverObserver, BLEAdapterObserver):
    def __init__(self, adapter):
        self.adapter = adapter
        logger.info("Central adapter is %d", self.adapter.driver.rpc_adapter.internal)
        self.conn_q = Queue()
        self.adapter.observer_register(self)
        self.adapter.driver.observer_register(self)
        self.conn_handle = None
        self.connecting = False
        self.lesc = True

    def start(self, connect_with):
        self.connect_with = connect_with
        self.connecting = False
        logger.info("scan_start, trying to find %s", self.connect_with)
        self.adapter.driver.ble_gap_scan_start()
        self.conn_handle = self.conn_q.get(timeout=10)
        Thread(
            target=self.adapter.authenticate,
            args=(self.conn_handle, None),
            kwargs={"bond": True, "mitm": True, "lesc": self.lesc, "io_caps": BLEGapIOCaps.keyboard_only},
        ).start()

    def stop(self):
        self.connecting = False

        if self.conn_handle:
            self.adapter.driver.ble_gap_disconnect(self.conn_handle)

    def on_gap_evt_adv_report(
        self, ble_driver, conn_handle, peer_addr, rssi, adv_type, adv_data
    ):
        if BLEAdvData.Types.complete_local_name in adv_data.records:
            dev_name_list = adv_data.records[BLEAdvData.Types.complete_local_name]
        elif BLEAdvData.Types.short_local_name in adv_data.records:
            dev_name_list = adv_data.records[BLEAdvData.Types.short_local_name]
        else:
            return

        dev_name = "".join(chr(e) for e in dev_name_list)

        if dev_name == self.connect_with and self.connecting is False:
            self.connecting = True
            address_string = "".join("{0:02X}".format(b) for b in peer_addr.addr)
            logger.info(
                "Trying to connect to peripheral advertising as %s, address: 0x%s",
                dev_name,
                address_string,
            )

            self.adapter.connect(peer_addr, tag=1)

    def on_gap_evt_connected(
        self, ble_driver, conn_handle, peer_addr, role, conn_params
    ):
        self.conn_q.put(conn_handle)

    def on_gap_evt_sec_params_request(self, ble_driver, conn_handle, peer_params):
        logger.info("Central on_gap_evt_sec_params_request. peer_params.lesc={}".format(peer_params.lesc))
        # Generate own private and public lesc keys if lesc is enabled.
        if peer_params.lesc and self.lesc:
            logger.debug("Generate LE Secure Connection ECDH private and public key.")
            keyset = self.adapter.driver.generate_lesc_keyset()
            ble_driver.ble_gap_sec_params_reply(conn_handle, BLEGapSecStatus.success, None, keyset)

    def on_gap_evt_lesc_dhkey_request(self, ble_driver, conn_handle, peer_public_key, oobd_req):
        logger.info("Central on_gap_evt_lesc_dhkey_request.")
        lesc_dhkey = self.adapter.driver.generate_lesc_dhkey(peer_public_key)
        self.adapter.driver.ble_gap_lesc_dhkey_reply(conn_handle, lesc_dhkey)

    def on_gap_evt_auth_key_request(
        self, ble_driver, conn_handle, **kwargs
    ):
        logger.info("Central on_gap_evt_auth_key_request.")
        passkey = passkeyQueue.get(timeout=10)
        pk = util.list_to_uint8_array(passkey)

        driver.sd_ble_gap_auth_key_reply(
            ble_driver.rpc_adapter,
            conn_handle,
            kwargs['key_type'],
            pk.cast(),
        )


class Peripheral(BLEDriverObserver, BLEAdapterObserver):
    def __init__(self, adapter):
        self.adapter = adapter
        logger.info(
            "Peripheral adapter is %d", self.adapter.driver.rpc_adapter.internal
        )
        self.conn_q = Queue()
        self.adapter.observer_register(self)
        self.adapter.driver.observer_register(self)

    def start(self, adv_name):
        adv_data = BLEAdvData(complete_local_name=adv_name)
        self.adapter.driver.ble_gap_adv_data_set(adv_data)
        self.adapter.driver.ble_gap_adv_start()

    def on_gap_evt_connected(
        self, ble_driver, conn_handle, peer_addr, role, conn_params
    ):
        self.conn_q.put(conn_handle)

    def on_gap_evt_sec_params_request(self, ble_driver, conn_handle, **kwargs):
        logger.info("Peripheral: on_gap_evt_sec_params_request.")
        sec_params = BLEGapSecParams.from_c(
            kwargs['peer_params']
        )

        sec_params.io_caps = BLEGapIOCaps.display_only
        sec_params.lesc = True
        sec_params.min_key_size = 7
        sec_params.kdist_own = BLEGapSecKDist(True, True, False, False)
        sec_params.kdist_peer = BLEGapSecKDist(True, True, False, False)

        logger.debug("Generate LE Secure Connection ECDH private and public key.")
        keyset = self.adapter.driver.generate_lesc_keyset()
        ble_driver.ble_gap_sec_params_reply(conn_handle, BLEGapSecStatus.success, sec_params, keyset)

    def on_gap_evt_lesc_dhkey_request(self, ble_driver, conn_handle, peer_public_key, oobd_req):
        logger.info("Peripheral: on_gap_evt_lesc_dhkey_request.")
        lesc_dhkey = self.adapter.driver.generate_lesc_dhkey(peer_public_key)

        self.adapter.driver.ble_gap_lesc_dhkey_reply(conn_handle, lesc_dhkey)

    def on_gap_evt_passkey_display(self, ble_driver, conn_handle, passkey):
        logger.info("Peripheral on_gap_evt_passkey_display.")
        passkeyQueue.put(passkey)

    def on_gap_evt_conn_sec_update(self, ble_driver, conn_handle, conn_sec):
        logger.info(f"Conn sec update: {conn_sec}")

    def on_gap_evt_auth_status(
                        self,
                        ble_driver,
                        conn_handle,
                        error_src,
                        bonded,
                        sm1_levels,
                        sm2_levels,
                        kdist_own,
                        kdist_peer,
                        auth_status
                    ):
        logger.info("Peripheral on_gap_evt_auth_status.")
        authStatusQueue.put(auth_status)


class LESCSecurity(unittest.TestCase):
    def setUp(self):
        settings = Settings.current()

        central = setup_adapter(
            settings.serial_ports[0],
            False,
            settings.baud_rate,
            settings.retransmission_interval,
            settings.response_timeout,
            settings.driver_log_level,
        )

        self.central = Central(central)

        peripheral = setup_adapter(
            settings.serial_ports[1],
            False,
            settings.baud_rate,
            settings.retransmission_interval,
            settings.response_timeout,
            settings.driver_log_level,
        )

        # Advertising name used by peripheral and central
        # to find peripheral and connect with it
        self.adv_name = "".join(
            random.choice(string.ascii_uppercase + string.digits) for _ in range(20)
        )
        self.peripheral = Peripheral(peripheral)

    def test_lesc_security(self):
        self.peripheral.start(self.adv_name)
        self.central.start(self.adv_name)
        authStatus = authStatusQueue.get(timeout=200)
        self.assertTrue(authStatus == BLEGapSecStatus.success)

    def tearDown(self):
        self.central.adapter.close()
        self.peripheral.adapter.close()


def test_suite():
    return unittest.TestLoader().loadTestsFromName(__name__)


if __name__ == "__main__":
    logging.basicConfig(
        level=Settings.current().log_level,
        format="%(asctime)s [%(thread)d/%(threadName)s] %(message)s",
    )
    unittest.main(argv=Settings.clean_args())
