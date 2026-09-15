import copy
import json
from threading import RLock
from types import SimpleNamespace
from unittest import mock

from electrum_firo import bitcoin, rosen
from electrum_firo.ecc import ECPrivkey
from electrum_firo.invoices import Invoice
from electrum_firo.simple_config import SimpleConfig
from electrum_firo.transaction import (PartialTransaction, PartialTxInput,
                                       PartialTxOutput, Transaction, TxOutpoint)
from electrum_firo.util import InvalidBitcoinURI, InvoiceError, MyEncoder, parse_URI
from electrum_firo.wallet import Abstract_Wallet

from . import ElectrumTestCase


ADDRESS = 'aEF6fyd5jjCPcbiEBZJ2g8583caUme8T7Y'
OTHER_ADDRESS = 'a86PtGKN9izdM7kdZn1Jm7J2bfjWzYm8u5'
PAYLOAD = '0400000000773594000000000000197a691456fa9181b1e73d3fa296a23bce9e2fead7b04a4e'
URI = f'firo:{ADDRESS}?amount=4000&op_return={PAYLOAD}'


def recipient(address=ADDRESS, amount=4000 * bitcoin.COIN):
    return PartialTxOutput.from_address_and_value(address, amount)


class TestRosen(ElectrumTestCase):

    def test_real_uri_and_script(self):
        uri = parse_URI(URI)
        self.assertEqual(uri['address'], ADDRESS)
        self.assertEqual(uri['amount'], 400000000000)
        outputs = rosen.add_output([recipient()], uri)
        self.assertEqual(outputs[1].value, 0)
        self.assertEqual(outputs[1].scriptpubkey.hex(), '6a26' + PAYLOAD)
        self.assertEqual(len(outputs[1].serialize_to_network()), 49)
        self.assertIn('Binance', rosen.format_details(bytes.fromhex(PAYLOAD)))
        self.assertIn(PAYLOAD[36:], rosen.format_details(bytes.fromhex(PAYLOAD)))

    def test_pushdata_boundaries(self):
        for size, prefix in [(19, '6a13'), (75, '6a4b'), (76, '6a4c4c'), (80, '6a4c50')]:
            with self.subTest(size=size):
                data = bytes(17) + bytes([size - 18]) + bytes([0x42]) * (size - 18)
                self.assertEqual(rosen.parse_payload(data.hex()), data)
                output = PartialTxOutput(scriptpubkey=bitcoin.make_op_return(data), value=0)
                self.assertEqual(output.scriptpubkey.hex(), prefix + data.hex())
                self.assertEqual(rosen.bridge_payload([output]), data)

    def test_malformed_metadata_rejected(self):
        cases = ['', '0', 'gg' * 38, PAYLOAD + '00', PAYLOAD[:-2],
                 '08' + PAYLOAD[2:], PAYLOAD[:34] + '00' + PAYLOAD[36:],
                 PAYLOAD[:34] + '15' + PAYLOAD[36:], '00' * 81,
                 PAYLOAD + '%20', PAYLOAD + '%0a']
        for value in cases:
            with self.subTest(value=value):
                with self.assertRaises(InvalidBitcoinURI):
                    parse_URI(f'firo:{ADDRESS}?amount=4000&op_return={value}')

    def test_amount_is_exact_and_required(self):
        for amount in ['', '0', '-1', '1e3', 'NaN', 'Infinity', '1X8', '1.000000001',
                       '18920903', '%204000', '+4000', '4000.']:
            with self.subTest(amount=amount):
                with self.assertRaises(InvalidBitcoinURI):
                    parse_URI(f'firo:{ADDRESS}?amount={amount}&op_return={PAYLOAD}')
        with self.assertRaises(InvalidBitcoinURI):
            parse_URI(f'firo:{ADDRESS}?op_return={PAYLOAD}')
        for amount, expected in [('0.00000001', 1), ('1.23456789', 123456789),
                                 ('004000.00000000', 400000000000)]:
            self.assertEqual(parse_URI(f'firo:{ADDRESS}?amount={amount}&op_return={PAYLOAD}')['amount'], expected)

    def test_duplicates_and_conflicting_requests(self):
        for suffix in ['&op_return=', '&op_return=' + PAYLOAD, '&req-op_return=' + PAYLOAD,
                       '&amount=', '&amount=4000', '&req-amount=4000', '&r=',
                       '&r=https://example.invalid/payment', '&name=x', '&sig=x',
                       '&req-unknown=x', '&address=' + OTHER_ADDRESS]:
            with self.subTest(suffix=suffix), self.assertRaises(InvalidBitcoinURI):
                parse_URI(URI + suffix)
        with self.assertRaises(InvalidBitcoinURI):
            parse_URI(f'firo:?amount=4000&op_return={PAYLOAD}')
        parsed = parse_URI(URI.replace('op_return=', 'req-op_return=').replace('amount=', 'req-amount='))
        self.assertEqual(parsed['op_return'], PAYLOAD)
        self.assertEqual(parsed['amount'], 400000000000)

    def test_case_and_url_encoding(self):
        self.assertEqual(parse_URI(URI.replace(PAYLOAD, PAYLOAD.upper()))['op_return'], PAYLOAD)
        self.assertEqual(parse_URI(URI.replace('op_return', '%6fp_return'))['op_return'], PAYLOAD)

    def test_ordinary_uri_unchanged(self):
        self.assertEqual(parse_URI(f'firo:{ADDRESS}?amount=&message='), {'address': ADDRESS})
        uri = parse_URI(f'firo:{ADDRESS}?amount=1&label=test')
        outputs = [recipient(amount=bitcoin.COIN)]
        self.assertEqual(rosen.add_output(outputs, uri), outputs)
        self.assertEqual(rosen.add_output(outputs, None), outputs)

    def test_binding_and_idempotence(self):
        uri = parse_URI(URI)
        original = [recipient()]
        outputs = rosen.add_output(original, uri)
        self.assertEqual(len(original), 1)
        again = rosen.add_output(outputs, uri)
        self.assertEqual([o.serialize_to_network() for o in outputs],
                         [o.serialize_to_network() for o in again])
        cases = [[], [recipient(OTHER_ADDRESS)], [recipient(amount=1)],
                 [recipient(amount='!')], [recipient(), recipient()],
                 [recipient(), PartialTxOutput(scriptpubkey=b'\x6a', value=0)]]
        for changed in cases:
            with self.subTest(outputs=changed), self.assertRaises(InvoiceError):
                rosen.add_output(changed, uri)
        with self.assertRaises(InvoiceError):
            rosen.add_output(original, uri, payment_request=object())

    def test_send_guards(self):
        outputs = rosen.add_output([recipient()], parse_URI(URI))
        rosen.check_send(outputs)
        for changed in [outputs + outputs, outputs + [recipient(OTHER_ADDRESS)],
                        [recipient(amount='!'), outputs[1]],
                        [recipient(amount=0), outputs[1]],
                        [recipient(), PartialTxOutput(scriptpubkey=outputs[1].scriptpubkey, value=1)]]:
            with self.subTest(outputs=changed), self.assertRaises(InvoiceError):
                rosen.check_send(changed)
        with self.assertRaises(InvoiceError):
            rosen.check_send(outputs, is_private=True)
        with self.assertRaises(InvoiceError):
            rosen.check_send(outputs, tx_type=1)
        # Unrelated manual script payments remain supported.
        rosen.check_send([recipient(), PartialTxOutput(scriptpubkey=b'\x6a\x01\x42', value=0)])

    def test_invoice_roundtrip_preserves_metadata(self):
        wallet = SimpleNamespace(get_local_height=lambda: 100)
        invoice = Abstract_Wallet.create_invoice(wallet, outputs=[recipient()],
                                                message='bridge', pr=None, URI=parse_URI(URI))
        restored = Invoice.from_json(json.loads(json.dumps(invoice, cls=MyEncoder)))
        self.assertEqual(restored.amount_sat, 400000000000)
        self.assertEqual(restored.get_address(), ADDRESS)
        self.assertEqual(restored.outputs[1].scriptpubkey.hex(), '6a26' + PAYLOAD)
        rosen.check_send(restored.outputs)
        # A later ordinary invoice must not inherit bridge metadata.
        ordinary = Abstract_Wallet.create_invoice(wallet, outputs=[recipient(OTHER_ADDRESS)],
                                                 message='', pr=None, URI=None)
        self.assertEqual(len(ordinary.outputs), 1)

    def test_wallet_fee_selection_signing_and_serialization(self):
        secret = bytes.fromhex('11' * 32)
        pubkey = ECPrivkey(secret).get_public_key_bytes(compressed=True)
        source = bitcoin.public_key_to_p2pkh(pubkey)
        coin = PartialTxInput(prevout=TxOutpoint(bytes.fromhex('22' * 32), 0))
        coin._trusted_value_sats = 5000 * bitcoin.COIN
        coin._trusted_address = source
        coin.block_height = 1
        coin.script_type = 'p2pkh'
        coin.num_sig = 1
        coin.pubkeys = [pubkey]
        config = SimpleConfig({'electrum_path': self.electrum_path, 'dynamic_fees': False})
        wallet = SimpleNamespace(config=config, network=None,
                                 add_input_info=lambda item: None,
                                 get_change_addresses_for_new_transaction=lambda address: [source],
                                 dust_threshold=lambda: 546)
        outputs = rosen.add_output([recipient()], parse_URI(URI))
        unsigned = []
        for fee_rate in [1, 2, 10, 1]:
            with mock.patch.object(PartialTransaction, 'add_info_from_wallet'):
                tx = Abstract_Wallet.make_unsigned_transaction(
                    wallet, coins=[copy.deepcopy(coin)], outputs=outputs,
                    fee=lambda size: size * fee_rate)
            self.assertGreaterEqual(tx.get_fee(), tx.estimated_size() * fee_rate)
            unsigned.append(tx.serialize_to_network(include_sigs=False))
            tx.sign({pubkey.hex(): (secret, True)})
            self.assertTrue(tx.is_complete())
            decoded = Transaction(tx.serialize_to_network())
            actual = [(o.scriptpubkey, o.value) for o in decoded.outputs()]
            self.assertIn((recipient().scriptpubkey, 400000000000), actual)
            self.assertEqual(sum(script == bytes.fromhex('6a26' + PAYLOAD) and value == 0
                                 for script, value in actual), 1)
        self.assertEqual(unsigned[0], unsigned[-1])
        self.assertEqual(len(outputs), 2)

    def test_wallet_rejects_incompatible_saved_payment(self):
        outputs = rosen.add_output([recipient()], parse_URI(URI))
        for kwargs in [{'min_rounds': 1}, {'tx_type': 1}]:
            with self.subTest(kwargs=kwargs), self.assertRaises(InvoiceError):
                Abstract_Wallet.make_unsigned_transaction(None, coins=[], outputs=outputs, **kwargs)

    def test_invoice_requires_both_outputs_in_one_eligible_transaction(self):
        wallet = SimpleNamespace(get_local_height=lambda: 100, lock=RLock(),
                                 transaction_lock=RLock(), db=mock.Mock())
        invoice = Abstract_Wallet.create_invoice(wallet, outputs=[recipient()],
                                                message='', pr=None, URI=parse_URI(URI))
        recipient_hash = bitcoin.script_to_scripthash(invoice.outputs[0].scriptpubkey.hex())
        old_id, new_id = '33' * 32, '44' * 32
        old = TxOutpoint(bytes.fromhex(old_id), 0)
        new = TxOutpoint(bytes.fromhex(new_id), 0)
        heights = {old_id: SimpleNamespace(height=50, conf=100),
                   new_id: SimpleNamespace(height=101, conf=1)}
        wallet.get_tx_height = heights.__getitem__
        wallet.db.get_prevouts_by_scripthash.side_effect = lambda script: (
            [(new, 400000000000)] if script == recipient_hash else [(old, 0)])
        self.assertEqual(Abstract_Wallet._is_onchain_invoice_paid(wallet, invoice, 1), (False, []))
        # Even two eligible transactions must not jointly satisfy the invoice.
        heights[old_id].height = 101
        self.assertEqual(Abstract_Wallet._is_onchain_invoice_paid(wallet, invoice, 1), (False, []))
        wallet.db.get_prevouts_by_scripthash.side_effect = lambda script: (
            [(new, 400000000000)] if script == recipient_hash else [(new, 0)])
        self.assertEqual(Abstract_Wallet._is_onchain_invoice_paid(wallet, invoice, 1), (True, [new_id]))
        self.assertEqual(Abstract_Wallet._is_onchain_invoice_paid(wallet, invoice, 2), (False, []))
        heights[new_id].height = 100
        self.assertEqual(Abstract_Wallet._is_onchain_invoice_paid(wallet, invoice, 1), (False, []))
        heights[new_id].height, heights[new_id].conf = 0, 0
        self.assertEqual(Abstract_Wallet._is_onchain_invoice_paid(wallet, invoice, 0), (True, [new_id]))
        self.assertEqual(Abstract_Wallet._is_onchain_invoice_paid(wallet, invoice, 1), (False, []))
