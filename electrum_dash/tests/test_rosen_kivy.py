"""Opt-in UI tests: RUN_KIVY_TESTS=1 python -m unittest electrum_firo.tests.test_rosen_kivy.

Run separately from Qt tests: Kivy initializes a native window, but these tests
never load a wallet, contact a server, or send a payment. Exercise real screen
handlers and properties without the full application's KV layout/bootstrap.
"""

import os
from decimal import Decimal
from types import SimpleNamespace
import unittest
from unittest import mock

from electrum_firo.util import parse_URI
from electrum_firo.wallet import Abstract_Wallet
from . import ElectrumTestCase
from .test_rosen import ADDRESS, OTHER_ADDRESS, PAYLOAD, URI

RUN_KIVY_TESTS = os.environ.get('RUN_KIVY_TESTS') == '1'
if RUN_KIVY_TESTS:
    from kivy.clock import Clock
    from electrum_firo.gui.kivy.main_window import ElectrumWindow
    from electrum_firo.gui.kivy.uix.screens import SendScreen


@unittest.skipUnless(RUN_KIVY_TESTS, 'opt-in Kivy UI tests')
class TestRosenKivy(ElectrumTestCase):

    def setUp(self):
        super().setUp()
        self.screen = SendScreen(__no_builder=True)
        self.wallet = mock.Mock()
        self.wallet.create_invoice.side_effect = lambda **kw: Abstract_Wallet.create_invoice(
            SimpleNamespace(get_local_height=lambda: 100), **kw)
        self.app = SimpleNamespace(
            wallet=self.wallet, send_screen=self.screen, asyncio_loop=None,
            show_info=mock.Mock(), show_error=mock.Mock(), switch_to=mock.Mock(),
            format_amount_and_units=lambda amount: str(Decimal(amount) / 100000000) + ' FIRO',
            get_amount=lambda amount: int(Decimal(amount.split()[0]) * 100000000),
            broadcast=mock.Mock(), sign_tx=mock.Mock(), tx_dialog=mock.Mock())
        self.app.on_pr = lambda pr: ElectrumWindow.on_pr(self.app, pr)
        self.screen.app = self.app
        self.screen.do_clear()

    def tearDown(self):
        Clock.tick()
        super().tearDown()

    def assert_bridge(self):
        invoice = self.screen.read_invoice()
        self.assertIsNotNone(invoice)
        self.assertEqual(invoice.outputs[0].address, ADDRESS)
        self.assertEqual(invoice.outputs[0].value, 400000000000)
        self.assertEqual(invoice.outputs[1].scriptpubkey.hex(), '6a26' + PAYLOAD)
        self.assertEqual(invoice.outputs[1].value, 0)

    def test_link_clear_and_replacement(self):
        self.screen.set_URI(URI)
        self.assert_bridge()
        self.assertIn('Binance', self.app.show_info.call_args[0][0])
        self.screen.do_clear()
        self.screen.set_URI(f'firo:{OTHER_ADDRESS}?amount=1')
        invoice = self.screen.read_invoice()
        self.assertEqual(len(invoice.outputs), 1)
        self.assertEqual(invoice.outputs[0].address, OTHER_ADDRESS)

    def test_bridge_uri_property_notifies_ui_bindings(self):
        changes = []
        self.screen.bind(parsed_URI=lambda screen, uri: changes.append(uri))
        self.screen.set_URI(URI)
        self.assertEqual(changes[-1]['op_return'], PAYLOAD)
        self.screen.do_clear()
        self.assertIsNone(changes[-1])

    def test_invalid_bridge_replacement_clears_old_data(self):
        self.screen.set_URI(URI)
        self.screen.set_URI(URI + '&op_return=')
        self.assertIsNone(self.screen.parsed_URI)
        self.assertEqual(self.screen.address, '')
        self.assertEqual(self.screen.amount, '')

    def test_amount_recipient_max_and_private_changes_fail_closed(self):
        for field, value in [('amount', '1 FIRO'), ('address', OTHER_ADDRESS),
                             ('is_max', True), ('is_ps', True)]:
            with self.subTest(field=field):
                self.screen.set_URI(URI)
                setattr(self.screen, field, value)
                self.app.show_error.reset_mock()
                self.assertIsNone(self.screen.read_invoice())
                self.app.show_error.assert_called_once()

    def test_late_payment_request_cannot_clear_bridge(self):
        self.screen.set_URI(URI)
        request = mock.Mock()
        self.app.on_pr(request)
        Clock.tick()
        request.verify.assert_not_called()
        self.assert_bridge()

    def test_bridge_pasted_during_verification_or_before_ui_callback(self):
        for during_verify in (True, False):
            for verified in (True, False):
                with self.subTest(during_verify=during_verify, verified=verified):
                    self.screen.do_clear()
                    request = mock.Mock(error='old request failed')
                    def verify(contacts):
                        if during_verify:
                            self.screen.set_URI(URI)
                        return verified
                    request.verify.side_effect = verify
                    self.app.on_pr(request)
                    if not during_verify:
                        self.screen.set_URI(URI)
                    Clock.tick()
                    self.assert_bridge()
        self.app.show_error.assert_not_called()

    def test_ordinary_payment_request_still_applies(self):
        request = mock.Mock()
        request.verify.return_value = True
        request.has_expired.return_value = False
        request.get_requestor.return_value = ADDRESS
        request.get_amount.return_value = 100000000
        self.wallet.get_invoice.return_value = None
        self.app.on_pr(request)
        Clock.tick()
        self.assertIs(self.screen.payment_request, request)
        self.assertEqual(self.screen.address, ADDRESS)
        self.assertEqual(self.screen.amount, '1 FIRO')

    def test_saved_bridge_does_not_inherit_live_bip70_request(self):
        self.screen.set_URI(URI)
        invoice = self.screen.read_invoice()
        self.screen.payment_request = mock.Mock()
        self.screen.save_invoice_ext = mock.Mock()
        self.screen.save_invoice = mock.Mock()
        self.wallet.has_password.return_value = False
        self.wallet.can_sign.return_value = True
        self.app.sign_tx.side_effect = lambda tx, pw, success, failure: success(tx)
        tx = mock.Mock()
        tx.is_complete.return_value = True
        self.screen.send_tx(tx, invoice, mock.Mock(), None)
        self.app.broadcast.assert_called_once_with(tx, None)
