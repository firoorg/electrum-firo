import os
import threading
from types import SimpleNamespace
from unittest import SkipTest, mock

from electrum_firo import keystore, libsparkmobile, util
from electrum_firo.address_synchronizer import SparkBalance
from electrum_firo.bitcoin import COIN
from electrum_firo.dash_tx import FiroSparkSpend, SPARK_SPEND_V2
from electrum_firo.exchange_rate import FxThread
from electrum_firo.plugins.trezor.trezor import TrezorKeyStore
from electrum_firo.simple_config import SimpleConfig
from electrum_firo.spark_interface import OP_SPARKSPEND
from electrum_firo.storage import WalletStorage
from electrum_firo.transaction import PartialTransaction, PartialTxInput, PartialTxOutput, Transaction, TxOutpoint
from electrum_firo.wallet import Wallet
from electrum_firo.wallet_db import WalletDB

from . import TestCaseForTestnet
from .test_spark_interface import _bip39_keystore

try:
    from PyQt5.QtCore import QCoreApplication, QEvent, Qt, QTimer
    from PyQt5.QtWidgets import QApplication, QPlainTextEdit
except ImportError:
    QApplication = None

if QApplication is not None:
    from electrum_firo.gui.qt.confirm_tx_dialog import ConfirmTxDialog, PaymentRoute
    from electrum_firo.gui.qt.main_window import ElectrumWindow


SPARK_ADDRESS = 'st1' + 'q' * 140


class TestSparkQt(TestCaseForTestnet):
    """Exercise Qt's real controls without a network connection or signing device."""

    @classmethod
    def setUpClass(cls):
        if QApplication is None:
            raise SkipTest("PyQt5 is required for the wallet UI tests")
        super().setUpClass()
        cls.app = QApplication.instance() or QApplication(["spark-qt-tests", "-platform", "offscreen"])

    def open_wallet(self, *, hardware=False, enable=True, spark_funds=0):
        config = SimpleConfig({'electrum_path': self.electrum_path, 'check_updates': False})
        ks = _bip39_keystore()
        db = WalletDB('', manual_upgrades=False)
        if hardware:
            # Only the public key is present, just as in a hardware wallet file.
            self.enter_patch(mock.patch.dict(keystore.hw_keystores, {'trezor': TrezorKeyStore}))
            db.put('keystore', dict(type='hardware', hw_type='trezor', xpub=ks.xpub,
                                    derivation=ks.get_derivation_prefix()))
        else:
            db.put('seed_type', 'bip39')
            db.put('keystore', ks.dump())
        db.put('wallet_type', 'standard')
        db.put('gap_limit', 2)
        storage = WalletStorage(os.path.join(self.electrum_path, 'wallet'))
        wallet = Wallet(db, storage, config=config)
        wallet.synchronize()
        self.enter_patch(mock.patch.object(libsparkmobile, 'is_available', return_value=True))
        self.enter_patch(mock.patch.object(libsparkmobile, 'is_valid_spark_address',
                                         side_effect=lambda address, **_: address == SPARK_ADDRESS))
        self.enter_patch(mock.patch.object(util, 'trigger_callback'))
        self.enter_patch(mock.patch.object(ElectrumWindow, 'question', return_value=enable))
        self.enter_patch(mock.patch.object(ElectrumWindow, 'show_message'))
        self.enter_patch(mock.patch.object(ElectrumWindow, 'show_error'))
        if not hardware:
            self.enter_patch(mock.patch.object(wallet, 'enable_spark',
                                               side_effect=lambda _: setattr(wallet, 'spark_enabled', True)))
            self.enter_patch(mock.patch.object(wallet, 'get_spark_balance', return_value=SparkBalance(
                spark_funds, spark_funds, 0, 0)))
            self.enter_patch(mock.patch.object(wallet, 'getCurrentReceivingSparkAddress',
                                               return_value=SPARK_ADDRESS))

        fx = FxThread(config, None)
        self.addCleanup(util.unregister_callback, fx.set_proxy)
        gui = mock.Mock(app=self.app, config=config, timer=QTimer(), tray=None, gui_thread=threading.current_thread(),
                        daemon=SimpleNamespace(network=None, fx=fx))
        window = ElectrumWindow(gui, wallet)
        window.setAttribute(Qt.WA_DeleteOnClose)
        self.addCleanup(QCoreApplication.sendPostedEvents, None, QEvent.DeferredDelete)
        self.addCleanup(window.close)
        return window

    def enter_patch(self, patcher):
        result = patcher.start()
        self.addCleanup(patcher.stop)
        return result

    def test_hardware_receives_transparent_output_of_spark_spend(self):
        window = self.open_wallet(hardware=True)
        wallet = window.wallet
        self.assertFalse(wallet.spark_enabled)
        address = wallet.get_receiving_address()
        window.tabs.setCurrentWidget(window.receive_tab)
        self.assertEqual(window.receive_address_e.text(), address)
        self.assertIn('including payments sent from Spark', window.receive_help.text())
        self.assertFalse(window.receive_type_combo.isVisible())
        self.assertFalse(window.spark_available_label.isVisible())
        window.tabs.setCurrentWidget(window.send_tab)
        window.payto_e.setPlainText(SPARK_ADDRESS)
        self.assertIn('hardware wallet', window.send_route_label.text())
        self.assertFalse(window.send_button.isEnabled())
        window.payto_e.setPlainText(address)
        self.assertTrue(window.send_button.isEnabled())
        value = 125000000
        txin = PartialTxInput(prevout=TxOutpoint(bytes(32), 0xffffffff),
                              script_sig=bytes([OP_SPARKSPEND]), nsequence=0xffffffff)
        output = PartialTxOutput.from_address_and_value(address, value)
        # Only the public output is relevant to the recipient's address synchronizer.
        fixture = PartialTransaction.from_io([txin], [output], tx_type=SPARK_SPEND_V2,
                                             extra_payload=FiroSparkSpend(b'fixture'))
        tx = Transaction(fixture.serialize_to_network())
        wallet.receive_history_callback(address, [(tx.txid(), 0)], {})
        wallet.receive_tx_callback(tx.txid(), tx, 0)
        self.assertEqual((0, value, 0), wallet.get_balance())
        self.assertEqual(0, wallet.get_spark_balance().total)
        self.assertEqual(value, wallet.get_tx_delta(tx.txid(), address))
        for show_type in (True, False):
            wallet.config.set_key('show_dip2_tx_type', show_type)
            entry, = wallet.get_spark_aware_history()
            self.assertEqual(entry['spark_activity'], 'Received from Spark at transparent address')

    def test_spark_source_cannot_save_a_transparent_funded_invoice(self):
        window = self.open_wallet(spark_funds=2 * COIN)
        window.tabs.setCurrentWidget(window.send_tab)
        self.assertTrue(window.send_from_private)
        window.payto_e.setPlainText(window.wallet.get_receiving_address())
        window.amount_e.setAmount(COIN)
        window.message_e.setText('Local accounting note')
        self.assertFalse(window.save_button.isEnabled() and window.save_button.isVisible())
        window.do_save_invoice()
        self.assertFalse(window.wallet.get_unpaid_invoices())
        with mock.patch.object(window, 'pay_sparksend_dialog') as preview:
            window.send_button.click()
        args, kwargs = preview.call_args
        self.assertEqual((window.wallet.get_receiving_address(), COIN), args[:2])
        self.assertNotIn('Local accounting note', (*args, *kwargs.values()))

    def test_spark_prefix_on_contact_label_keeps_transparent_recipient(self):
        window = self.open_wallet()
        address = window.wallet.get_receiving_address()
        window.payto_e.setPlainText('sm1 savings  <{}>'.format(address))
        self.assertEqual(address, window._get_send_destination_address())
        self.assertTrue(window.send_button.isEnabled())
        self.assertIn('Spark → Transparent', window.send_route_label.text())
        for text, message in [('st1bad', 'valid Spark address'), ('sm1bad', 'different network')]:
            with self.subTest(text=text):
                window.payto_e.setPlainText(text)
                self.assertFalse(window.send_button.isEnabled())
                self.assertIn(message, window.send_route_label.text())

    def test_move_to_previous_spark_receiving_address(self):
        window = self.open_wallet()
        previous = 'st1' + 'p' * 140
        external = 'st1' + 'r' * 140
        with mock.patch.object(window.wallet, '_generateSparkAddress', side_effect=[previous, SPARK_ADDRESS]):
            window.wallet.generateNextSparkAddress()
            window.wallet.generateNextSparkAddress()
        for address in (previous, previous.upper(), SPARK_ADDRESS):
            with self.subTest(address=address):
                route = window._payment_route(address, from_spark=False, to_spark=True)
                self.assertTrue(route.own_spark)
        self.assertFalse(window._payment_route(external, from_spark=False, to_spark=True).own_spark)

    def test_receive_type_switch_updates_address_and_qr_together(self):
        window = self.open_wallet()
        window.tabs.setCurrentWidget(window.receive_tab)
        for spark in (True, False, True):
            window.receive_type_combo.setCurrentIndex(window.receive_type_combo.findData(spark))
            window.request_list.update()
            address = SPARK_ADDRESS if spark else window.wallet.get_unused_address()
            self.assertEqual(window.receive_address_e.text(), address)
            self.assertEqual(window.receive_qr.data, address)
            self.assertEqual(window.receive_request_form.isVisible(), not spark)

    def test_refresh_updates_only_the_default_transparent_receiving_address(self):
        window = self.open_wallet()
        window.receive_type_combo.setCurrentIndex(window.receive_type_combo.findData(False))
        wallet = window.wallet
        first, second = wallet.get_receiving_addresses()[:2]
        self.assertEqual(window.receive_address_e.text(), first)
        with mock.patch.object(wallet, 'is_used', side_effect=lambda address: address == first):
            window.request_list.update()
            self.assertEqual(window.receive_address_e.text(), second)
            self.assertEqual(window.receive_qr.data, second)
            self.assertEqual(window.receive_address_e.toolTip(), '')

        with mock.patch.object(wallet, 'get_unused_address', return_value=None):
            window.show_unused_receive_address()
        window.request_list.update()
        self.assertEqual(window.receive_address_e.text(), first)
        self.assertEqual(window.receive_qr.data, first)

        window.receive_amount_e.setAmount(COIN)
        window.create_invoice()
        request_uri = window.receive_payreq_e.text()
        self.assertTrue(request_uri)
        window.create_bitcoin_request(2 * COIN, 'Second request', 3600)
        window.request_list.update()
        self.assertEqual(window.receive_address_e.text(), first)
        self.assertEqual(window.receive_qr.data, request_uri)
        self.assertTrue(window.request_list.currentIndex().isValid())

        fresh = wallet.create_new_address()
        wallet.delete_request(first)
        with mock.patch.object(wallet, 'is_used', side_effect=lambda address: address == first):
            window.request_list.update()
        self.assertEqual(window.request_list.std_model.rowCount(), 1)
        self.assertEqual(window.receive_payreq_e.text(), '')
        self.assertEqual(window.receive_address_e.text(), fresh)
        self.assertEqual(window.receive_qr.data, fresh)
        self.assertEqual(window.receive_address_e.toolTip(), '')

    def test_spark_recipient_keeps_wallet_note_out_of_both_builders(self):
        window = self.open_wallet(spark_funds=2 * COIN)
        window.tabs.setCurrentWidget(window.send_tab)
        window.payto_e.setPlainText(SPARK_ADDRESS)
        window.amount_e.setAmount(COIN)
        window.message_e.setText('Local accounting note')

        def prepare(dialog, on_success):
            self.assertEqual(dialog.wallet_note, 'Local accounting note')
            self.assertEqual(dialog.route.address, SPARK_ADDRESS)
            self.assertTrue(dialog.route.to_spark)
            dialog.make_tx(None)

        with mock.patch.object(ConfirmTxDialog, 'bg_update', prepare):
            for from_spark, builder in ((True, 'prepareSendSpark'), (False, 'prepareSparkMintTransaction')):
                window.send_source_combo.activated.emit(window.send_source_combo.findData(from_spark))
                with mock.patch.object(window.wallet, builder) as make_tx:
                    window.send_button.click()
                _, values = make_tx.call_args
                self.assertEqual(values['amount'], COIN)
                self.assertEqual(values.get('address') or values.get('spark_address'), SPARK_ADDRESS)
                self.assertFalse(values.get('memo'))

    def test_balance_displays_disabled_locked_syncing_and_known_zero(self):
        window = self.open_wallet(enable=False)
        self.assertIn('Disabled', window.spark_available_label.text())
        with mock.patch.object(window.wallet, 'has_keystore_encryption', return_value=True):
            window.update_balances()
            self.assertEqual('Locked', window.spark_available_label.text())
            self.assertTrue(window.enable_spark_button.isVisible())
        window.wallet.spark_enabled = True
        window.update_balances()
        self.assertIn('sync', window.spark_available_label.text().lower())
        window.wallet.db.put('spark_scan_state', {'chain_height': 100})
        window.update_balances()
        self.assertIn(window.format_amount_and_units(0), window.spark_available_label.text())

    def test_prepared_net_amount_fee_and_recipient_on_confirmation(self):
        window = self.open_wallet()
        second = mock.Mock()
        second.estimated_size.return_value = 200
        tx = SimpleNamespace(_spark_send_amount=COIN - 300, _spark_fee=300,
                             estimated_size=lambda: 100, sparkMints=[second])
        route = PaymentRoute(SPARK_ADDRESS, from_spark=False, to_spark=True)
        for amount_entered in (COIN, '!'):
            dialog = ConfirmTxDialog(window=window, make_tx=lambda _: tx, output_value=amount_entered,
                                     is_sweep=False, route=route)
            dialog.update_tx()
            dialog.update()
            self.assertEqual(dialog.findChild(QPlainTextEdit).toPlainText(), SPARK_ADDRESS)
            self.assertEqual(dialog.amount_label.text(), window.format_amount_and_units(COIN - 300))
            self.assertEqual(dialog.fee_label.text(), window.format_amount_and_units(300))
            self.assertEqual(dialog.total_label.text(), window.format_amount_and_units(COIN))
            self.assertFalse(dialog.batch_label.isHidden())
            self.assertIn('2', dialog.batch_label.text())
            dialog.close()
