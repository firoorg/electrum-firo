import os
from unittest import mock, skipUnless

from electrum_firo import bitcoin, libsparkmobile, util
from electrum_firo.contacts import Contacts

from . import TestCaseForTestnet
from .test_spark_qt import SPARK_ADDRESS, SparkQtTestCase


class TestPaymentLinks(TestCaseForTestnet):

    @skipUnless(libsparkmobile.is_available(), 'electrum_libsparkmobile is not built')
    def test_native_spark_payment_address_and_uri(self):
        key = bytes(range(32))
        address = libsparkmobile.get_address(key, is_testnet=True)
        self.assertEqual(util.parse_URI(address), {'address': address})
        self.assertEqual(util.create_bip21_uri(address, 125000000, 'Invoice 23'),
                         f'firo:{address}?amount=1.25&message=Invoice%2023')
        self.assertEqual(util.parse_URI(f'firo:{address}?amount=1.25&message=Invoice%2023'),
                         dict(address=address, amount=125000000, message='Invoice 23', memo='Invoice 23'))
        self.assertEqual(util.parse_URI(address.upper()), {'address': address.upper()})
        for invalid in (libsparkmobile.get_address(key), address[:-1], address + '\x00ignored'):
            with self.subTest(address=invalid):
                with self.assertRaises(util.InvalidBitcoinURI):
                    util.parse_URI(f'firo:{invalid}?amount=1')
                self.assertEqual(util.create_bip21_uri(invalid, 1, None), '')
        with mock.patch.object(libsparkmobile, 'is_available', return_value=False):
            with self.assertRaises(util.InvalidBitcoinURI):
                util.parse_URI(address)
            transparent = bitcoin.hash160_to_p2pkh(bytes(20))
            self.assertEqual(util.parse_URI(util.create_bip21_uri(transparent, 1, None)),
                             dict(address=transparent, amount=1))

    def test_unavailable_spark_contact_import_preserves_wallet(self):
        from electrum_firo.wallet_db import WalletDB
        db = WalletDB('', manual_upgrades=False)
        transparent = bitcoin.hash160_to_p2pkh(bytes(20))
        db.put('contacts', {SPARK_ADDRESS: ['address', transparent]})
        filename = os.path.join(self.electrum_path, 'contacts.json')
        util.write_json_file(filename, {SPARK_ADDRESS: ['address', 'Offline']})
        with mock.patch.object(libsparkmobile, 'is_available', return_value=False):
            contacts = Contacts(db)
            self.assertEqual(tuple(contacts[SPARK_ADDRESS]), ('address', transparent))
            with self.assertRaises(util.FileImportFailed):
                contacts.import_file(filename)
            with mock.patch.object(contacts, 'resolve_openalias') as resolve:
                with self.assertRaises(ValueError):
                    contacts.resolve(SPARK_ADDRESS)
                resolve.assert_not_called()
            self.assertEqual(contacts[SPARK_ADDRESS][1], transparent)


class TestPaymentContactsQt(SparkQtTestCase):

    def test_create_rename_export_import_and_pay_to_spark_contact(self):
        window = self.open_wallet()
        self.assertTrue(window.set_contact('Payroll', SPARK_ADDRESS))
        window.contact_list.on_edited(None, SPARK_ADDRESS, text='Payroll office')
        filename = os.path.join(self.electrum_path, 'contacts.json')
        window.contacts.export_file(filename)
        window.contacts.pop(SPARK_ADDRESS)
        window.update_completions()
        with mock.patch('electrum_firo.gui.qt.util.getOpenFileName', return_value=filename):
            window.import_contacts()
        self.assertEqual(tuple(Contacts(window.wallet.db)[SPARK_ADDRESS]), ('address', 'Payroll office'))
        self.assertEqual(window.contacts.resolve('Payroll office'),
                         {'address': SPARK_ADDRESS, 'type': 'contact'})
        self.assertIn(window.get_contact_payto(SPARK_ADDRESS), window.completions.stringList())
        for private in (True, False):
            with self.subTest(private=private):
                window.send_source_combo.setCurrentIndex(window.send_source_combo.findData(private))
                window.send_source_combo.activated.emit(window.send_source_combo.currentIndex())
                window.payto_contacts([SPARK_ADDRESS])
                self.assertEqual(window.payto_e.toPlainText(), f'Payroll office  <{SPARK_ADDRESS}>')
                self.assertEqual(window._get_send_destination_address(), SPARK_ADDRESS)
                self.assertEqual(window.payto_e.get_spark_address(), SPARK_ADDRESS)
                self.assertFalse(window.payto_e.get_errors())
        window.payto_contacts([SPARK_ADDRESS, window.wallet.get_receiving_address()])
        self.assertIn('one recipient', window.show_error.call_args.args[0])

    def test_scan_dispatches_once_and_sets_recipient_and_amount(self):
        window = self.open_wallet()
        for address in (SPARK_ADDRESS, window.wallet.get_receiving_address(), 'st1invalid'):
            with self.subTest(address=address):
                window.do_clear()
                uri = f'firo:{address}?amount=1.25'
                def scan(*, callback, **kwargs):
                    callback(True, '', uri)
                with mock.patch('electrum_firo.gui.qt.qrtextedit.scan_qrcode', side_effect=scan), \
                     mock.patch.object(window, 'pay_to_URI', wraps=window.pay_to_URI) as handle:
                    window.payto_e.qr_input()
                handle.assert_called_once()
                if address == 'st1invalid':
                    window.show_error.assert_called_once()
                else:
                    self.assertEqual(window._get_send_destination_address(), address)
                    self.assertEqual(window.amount_e.get_amount(), 125000000)
