"""Run with QT_QPA_PLATFORM=offscreen; no wallet, network or GUI interaction."""

import gc
import os
import unittest
from unittest import mock

from electrum_firo.simple_config import SimpleConfig
from electrum_firo.util import InvoiceError
from . import ElectrumTestCase
from .test_rosen import ADDRESS, OTHER_ADDRESS, PAYLOAD, URI

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
try:
    from PyQt5.QtWidgets import QApplication, QWidget, QPushButton, QCheckBox, QLabel
except ImportError:
    QApplication = None
else:
    from PyQt5 import sip
    from PyQt5.QtCore import QCoreApplication, QEvent
    from electrum_firo.gui.qt.main_window import ElectrumWindow
    from electrum_firo.gui.qt.paytoedit import PayToEdit
    from electrum_firo.gui.qt.amountedit import BTCAmountEdit, FreezableLineEdit


if QApplication is not None:
    class SendForm(QWidget):
        """Real send widgets and handlers, without the rest of the wallet window."""

        pay_to_URI = ElectrumWindow.pay_to_URI
        do_clear = ElectrumWindow.do_clear
        read_outputs = ElectrumWindow.read_outputs
        set_onchain = ElectrumWindow.set_onchain
        lock_amount = ElectrumWindow.lock_amount
        spend_max = ElectrumWindow.spend_max
        on_pr = ElectrumWindow.on_pr

        def __init__(self, config):
            super().__init__()
            self.config = config
            self.amount_e = BTCAmountEdit(lambda: 8)
            self.fiat_send_e = FreezableLineEdit()
            self.message_e = FreezableLineEdit()
            self.max_button = QPushButton()
            self.max_button.setCheckable(True)
            self.ps_cb = QCheckBox()
            self.payto_e = PayToEdit(self)
            self.payment_request = None
            self.payto_URI = None
            self.rosen_label = QLabel()
            for widget in (self.amount_e, self.fiat_send_e, self.message_e,
                           self.max_button, self.ps_cb, self.payto_e, self.rosen_label):
                widget.setParent(self)
            self.extra_payload = mock.Mock()
            self.extra_payload.get_extra_data.return_value = (0, b'')
            self.show_error = mock.Mock()
            self.show_send_tab = mock.Mock()
            self.hide_extra_payload = mock.Mock()
            self.reset_privatesend = mock.Mock(side_effect=lambda: self.ps_cb.setChecked(False))
            self.update_status = mock.Mock()


@unittest.skipIf(QApplication is None, 'PyQt5 is not installed')
class TestRosenQt(ElectrumTestCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.owns_app = QApplication.instance() is None
        cls.app = QApplication.instance() or QApplication([])

    @classmethod
    def tearDownClass(cls):
        gc.collect()
        QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
        if cls.owns_app:
            sip.delete(cls.app)
        cls.app = None
        super().tearDownClass()

    def setUp(self):
        super().setUp()
        self.form = SendForm(SimpleConfig({'electrum_path': self.electrum_path}))

    def tearDown(self):
        # Collect Python reference cycles while the Qt widgets are still alive.
        gc.collect()
        self.form.close()
        self.form.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
        del self.form
        super().tearDown()

    def assert_bridge(self):
        outputs = self.form.read_outputs()
        self.assertEqual(len(outputs), 2)
        self.assertEqual(outputs[0].address, ADDRESS)
        self.assertEqual(outputs[0].value, 400000000000)
        self.assertEqual(outputs[1].scriptpubkey.hex(), '6a26' + PAYLOAD)
        self.assertEqual(outputs[1].value, 0)
        self.assertTrue(self.form.amount_e.isReadOnly())
        self.assertTrue(self.form.payto_e.isReadOnly())
        self.assertTrue(self.form.fiat_send_e.isReadOnly())
        self.assertFalse(self.form.max_button.isEnabled())

    def test_uri_paste_real_text_changed_handler(self):
        self.form.payto_e.setText(URI)
        self.assert_bridge()
        self.assert_bridge()  # fee/preview refresh does not duplicate metadata
        self.assertIn('Binance', self.form.rosen_label.text())

    def test_payment_link_dispatch(self):
        self.form.pay_to_URI(URI)
        self.assert_bridge()

    def test_ordinary_uri_preserves_omitted_fields(self):
        self.form.amount_e.setAmount(123456789)
        self.form.message_e.setText('existing memo')
        self.form.pay_to_URI(f'firo:{ADDRESS}')
        self.assertEqual(self.form.amount_e.get_amount(), 123456789)
        self.assertEqual(self.form.message_e.text(), 'existing memo')
        self.form.pay_to_URI('firo:invalid-address')
        self.form.show_error.assert_called_once()
        self.assertEqual(self.form.amount_e.get_amount(), 123456789)
        self.assertEqual(self.form.message_e.text(), 'existing memo')

    def test_invalid_new_uri_clears_old_payment(self):
        self.form.pay_to_URI(URI)
        self.form.pay_to_URI(URI + '&op_return=')
        self.form.show_error.assert_called_once()
        self.assertIsNone(self.form.payto_URI)
        self.assertEqual(self.form.read_outputs(), [])
        self.assertEqual(self.form.rosen_label.text(), '')

    def test_clear_and_replace_never_reuse_metadata(self):
        self.form.pay_to_URI(URI)
        self.form.do_clear()
        self.assertFalse(self.form.payto_e.isReadOnly())
        self.assertFalse(self.form.amount_e.isReadOnly())
        self.assertFalse(self.form.fiat_send_e.isReadOnly())
        self.form.pay_to_URI(f'firo:{OTHER_ADDRESS}?amount=1')
        outputs = self.form.read_outputs()
        self.assertEqual(len(outputs), 1)
        self.assertEqual(outputs[0].address, OTHER_ADDRESS)
        self.assertEqual(self.form.rosen_label.text(), '')

    def test_programmatic_amount_changes_fail_closed(self):
        self.form.pay_to_URI(URI)
        self.form.amount_e.setAmount(1)
        with self.assertRaises(InvoiceError):
            self.form.read_outputs()

    def test_private_or_special_mode_fails_closed(self):
        self.form.pay_to_URI(URI)
        self.form.ps_cb.setChecked(True)
        with self.assertRaises(InvoiceError):
            self.form.read_outputs()
        self.form.ps_cb.setChecked(False)
        self.form.extra_payload.get_extra_data.return_value = (1, b'')
        with self.assertRaises(InvoiceError):
            self.form.read_outputs()

    def test_spend_max_does_not_change_bridge_amount(self):
        self.form.pay_to_URI(URI)
        self.form.spend_max()
        self.form.show_error.assert_called_once()
        self.assert_bridge()

    def test_late_payment_request_cannot_replace_bridge(self):
        self.form.pay_to_URI(URI)
        request = mock.Mock()
        self.form.on_pr(request)
        request.verify.assert_not_called()
        self.assert_bridge()

    def test_queued_old_payment_request_cannot_clear_or_replace_bridge(self):
        for handler in (ElectrumWindow.payment_request_ok, ElectrumWindow.payment_request_error):
            with self.subTest(handler=handler.__name__):
                self.form.pay_to_URI(URI)
                # An old callback can set this while do_clear is transitioning
                # between URIs, then queue a signal for later UI processing.
                self.form.payment_request = mock.Mock()
                handler(self.form)
                self.assertIsNone(self.form.payment_request)
                self.assert_bridge()

    def test_saved_bridge_does_not_inherit_live_bip70_request(self):
        self.form.pay_to_URI(URI)
        from electrum_firo.transaction import PartialTransaction
        tx = PartialTransaction.from_io([], self.form.read_outputs())
        self.form.payment_request = mock.Mock()
        self.form.save_pending_invoice = mock.Mock()
        self.form.broadcast_or_show = mock.Mock()
        self.form.sign_tx_with_password = mock.Mock(side_effect=lambda tx, **kw: kw['callback'](True))
        dialog = mock.Mock()
        dialog.run.return_value = (False, True, 'password', tx)
        ElectrumWindow._conf_dlg_or_preview_dlg(self.form, dialog, None)
        self.form.broadcast_or_show.assert_called_once_with(tx, None)

    def test_advanced_preview_does_not_inherit_live_bip70_request(self):
        from types import SimpleNamespace
        from electrum_firo.transaction import PartialTransaction
        from electrum_firo.gui.qt.transaction_dialog import BaseTxDialog
        self.form.pay_to_URI(URI)
        tx = PartialTransaction.from_io([], self.form.read_outputs())
        window = mock.Mock()
        dialog = SimpleNamespace(main_window=window, tx=tx, update=mock.Mock())
        BaseTxDialog.do_broadcast(dialog)
        window.broadcast_transaction.assert_called_once_with(tx, None)
        self.assertTrue(dialog.saved)
