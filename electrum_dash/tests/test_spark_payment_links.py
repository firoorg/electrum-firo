import asyncio
import os
from types import SimpleNamespace
from unittest import mock, skipUnless

from electrum_firo import bitcoin, ecc, libsparkmobile, paymentrequest, util
from electrum_firo.contacts import Contacts
from electrum_firo.wallet_db import WalletDB

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

    def test_invalid_contact_records_cannot_partially_replace_wallet(self):
        first = bitcoin.hash160_to_p2pkh(bytes(20))
        second = bitcoin.hash160_to_p2pkh(bytes(range(20)))
        original = {first: ['address', 'Original']}
        db = WalletDB('', manual_upgrades=False)
        db.put('contacts', original)
        contacts = Contacts(db)
        path = os.path.join(self.electrum_path, 'invalid.json')
        for bad in (None, '', [], ['address'], ['address', 'extra', 3],
                    [42, 'Label'], ['address', None], {'type': 'address', 'name': 'Label'}):
            with self.subTest(record=bad):
                util.write_json_file(path, {'contacts': {'contacts': {
                    first: ['address', 'Replacement'], second: bad}}})
                with self.assertRaises(util.FileImportFailed):
                    contacts.import_file(path)
                self.assertEqual(dict(contacts), original)
                self.assertEqual(dict(Contacts(db)), original)
        db.put('contacts', {first: original[first], second: ['address']})
        self.assertEqual(dict(Contacts(db)), original)
        util.write_json_file(path, {'contacts': []})
        with self.assertRaises(util.FileImportFailed):
            contacts.import_file(path)

    def read_payment_link(self, uri):
        loop = asyncio.new_event_loop()
        self.addCleanup(loop.close)
        async def read():
            completed = loop.create_future()
            util.parse_URI(uri, completed.set_result, loop=loop)
            return await asyncio.wait_for(completed, 2)
        return loop.run_until_complete(read())

    def test_signed_transparent_link_reconstructs_and_verifies_request(self):
        key = ecc.ECPrivkey(bytes(range(32)))
        signer = bitcoin.pubkey_to_address('p2pkh', key.get_public_key_hex())
        address = bitcoin.hash160_to_p2pkh(bytes(20))
        alias = 'merchant.example'
        unsigned = paymentrequest.make_unsigned_request(SimpleNamespace(
            get_address=lambda: address, amount_sat=125000000, time=1700000000,
            exp=3600, message='Invoice 23'))
        unsigned.pki_type = 'dnssec+btc'
        unsigned.pki_data = alias.encode()
        unsigned.signature = key.sign_message(unsigned.SerializeToString(), True)
        link = util.create_bip21_uri(address, 125000000, 'Invoice 23', extra_query_params={
            'name': alias, 'sig': bitcoin.base_encode(unsigned.signature, base=58),
            'time': '1700000000', 'exp': '3600'})
        request = self.read_payment_link(link)
        self.assertEqual(request.raw, unsigned.SerializeToString())
        contacts = mock.Mock()
        contacts.resolve.return_value = {'address': signer, 'validated': True}
        self.assertTrue(request.verify(contacts), request.error)
        contacts.resolve.assert_called_once_with(alias)
        self.assertEqual((request.get_requestor(), request.get_address(), request.get_amount()),
                         (alias, address, 125000000))
        failed = self.read_payment_link(link)
        contacts.resolve.side_effect = ValueError('alias unavailable')
        self.assertFalse(failed.verify(contacts))
        self.assertIn('alias unavailable', failed.error)

    def test_bip70_fetch_failure_completes_callback(self):
        with mock.patch.object(paymentrequest, 'get_payment_request',
                               side_effect=ValueError('invalid payment response')) as fetch:
            request = self.read_payment_link('pay:?r=https://merchant.example/request')
            fetch.assert_awaited_once_with('https://merchant.example/request')
        self.assertIn('invalid payment response', request.error)
        self.assertFalse(request.verify(None))
        for uri in ('pay:destination?r=example', 'pay:?amount=1', 'pay://merchant?r=example'):
            with self.assertRaises(util.InvalidBitcoinURI):
                util.parse_URI(uri)


class TestPaymentContactsQt(SparkQtTestCase):

    def test_spark_bip70_links_preserve_draft_and_never_start_fetch(self):
        window = self.open_wallet()
        window.payto_e.setPlainText(SPARK_ADDRESS)
        window.amount_e.setAmount(1234)
        draft = window.payment_draft_snapshot()
        with mock.patch.object(paymentrequest, 'get_payment_request') as fetch:
            for query in ('name=merchant.example&sig=11', 'r=https://merchant.example/request'):
                window.pay_to_URI(f'firo:{SPARK_ADDRESS}?{query}')
                self.assertEqual(window.payment_draft_snapshot(), draft)
                self.assertFalse(window.payto_e.is_pr)
                self.assertIn('Spark', window.show_error.call_args.args[0])
            fetch.assert_not_called()

    def test_bip70_unavailable_worker_unfreezes_send_form(self):
        window = self.open_wallet()
        loop = asyncio.new_event_loop()
        self.addCleanup(loop.close)
        with mock.patch.object(asyncio, 'get_event_loop', return_value=loop):
            window.pay_to_URI('pay:?r=https://merchant.example/request')
        self.assertFalse(window.payto_e.is_pr)
        self.assertFalse(window.payto_e.isReadOnly())
        self.assertIn('unavailable', window.show_message.call_args.args[0])

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
