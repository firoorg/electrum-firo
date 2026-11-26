import base64
import hashlib
from unittest import mock, skipUnless

from electrum_firo import libsparkmobile, util
from electrum_firo.bitcoin import COIN
from electrum_firo.dash_tx import SPARK_SPEND_V2
from electrum_firo.spark_interface import (
    OP_SPARKMINT, OP_SPARKSMINT, SPARK_CHAUM_V2_START_HEIGHT,
    SPARK_GROUP_ELEMENT_SIZE, _canonical_spark_coin, spark_spend_fee,
)
from electrum_firo.transaction import (
    BCDataStream, PartialTransaction, PartialTxInput, PartialTxOutput, Transaction, TxOutpoint,
)

from .test_spark_qt import SparkQtTestCase


@skipUnless(libsparkmobile.is_available(), 'electrum_libsparkmobile is not built')
class TestNativePaymentRoutes(SparkQtTestCase):
    """Prepare all routes from the Send form against locally generated cover coins."""

    def review(self, window, from_spark, address, amount):
        from electrum_firo.gui.qt.confirm_tx_dialog import ConfirmTxDialog
        window.do_clear()
        window.tabs.setCurrentWidget(window.send_tab)
        combo = window.send_source_combo
        combo.setCurrentIndex(combo.findData(from_spark))
        combo.activated.emit(combo.currentIndex())
        window.payto_contacts([address])
        self.assertEqual(window.payto_e.toPlainText(), window.get_contact_payto(address))
        window.amount_e.setAmount(amount)
        window.amount_e.textEdited.emit(window.amount_e.text())
        self.assertTrue(window.send_button.isEnabled(), window.send_route_label.text())
        # Take the scheduled preview out of its worker thread, then run the real builder inline.
        with mock.patch.object(ConfirmTxDialog, 'bg_update', autospec=True) as schedule:
            window.send_button.click()
        schedule.assert_called_once()
        dialog = schedule.call_args.args[0]
        self.addCleanup(dialog.close)
        self.assertEqual(dialog.route.from_spark, from_spark)
        self.assertEqual(dialog.route.address, address)
        dialog.update_tx()
        self.assertIsNotNone(dialog.tx)
        dialog.update()
        tx = dialog.tx
        self.assertGreater(tx._spark_fee, 0)
        for label, value in ((dialog.amount_label, amount), (dialog.fee_label, tx._spark_fee),
                             (dialog.total_label, amount + tx._spark_fee)):
            self.assertEqual(label.text(), window.format_amount_and_units(value))
        return tx

    @staticmethod
    def recover(script, context, key):
        view_key = libsparkmobile.create_full_view_key(key)
        try:
            return libsparkmobile.identify_and_recover_coin(
                _canonical_spark_coin(script[1:]), context, view_key, is_testnet=True)
        finally:
            libsparkmobile.delete_full_view_key(view_key)

    def spend_context(self, tx):
        # V2 serializes S1, C1 and the linking tag T as fixed-size vectors. The
        # recipient uses the serialized vector of T as its private output context.
        stream = BCDataStream()
        stream.write(tx.extra_payload.sparkData)
        self.assertEqual(stream.read_bytes(2), b'\x02\x01')  # version 2, one input
        stream.read_compact_size()  # output count
        stream.read_uint64()  # input's cover set ID
        self.assertEqual(stream.read_compact_size(), 1)
        stream.read_bytes(8 + 32)  # one cover set ID and block hash
        stream.read_bytes(8 + 32)  # fee and extension commitment
        stream.read_bytes(2 * SPARK_GROUP_ELEMENT_SIZE)  # S1, C1
        return b'\x01' + stream.read_bytes(SPARK_GROUP_ELEMENT_SIZE)

    def test_native_mint_private_payment_and_hardware_receipt(self):
        window = self.open_wallet(native=True)
        hardware = self.open_wallet(hardware=True, native=True).wallet
        wallet = window.wallet
        self.assertFalse(hardware.spark_enabled)
        height = SPARK_CHAUM_V2_START_HEIGHT['testnet']
        info = util.TxMinedInfo(height=height, timestamp=1700000000, txpos=1, header_hash='ab' * 32)
        for account in (wallet, hardware):
            account.db.put('stored_height', height + 10)
        output = PartialTxOutput.from_address_and_value(wallet.get_receiving_address(), 6 * COIN)
        funding = PartialTransaction.from_io([PartialTxInput(
            prevout=TxOutpoint(bytes(range(32)), 0), script_sig=b'fixture')], [output])
        funding = Transaction(funding.serialize_to_network())
        wallet.receive_tx_callback(funding.txid(), funding, height)
        wallet.add_verified_tx(funding.txid(), info)
        own = wallet.getCurrentReceivingSparkAddress()
        self.assertTrue(window.set_contact('My Spark account', own))
        mint = self.review(window, False, own, 4 * COIN)
        script, = [o.scriptpubkey for o in mint.outputs() if o.scriptpubkey[0] == OP_SPARKMINT]
        context = libsparkmobile.serialize_mint_context([(i.prevout.txid[::-1], i.prevout.out_idx)
                                                        for i in mint.inputs()])
        recovered = self.recover(script, context, wallet.derive_spark_key(None))
        self.assertEqual((own, 4 * COIN), (recovered['address'], recovered['value']))

        # Supply a local confirmation record for the unsigned mint and two real
        # cover coins. Only the network's confirmation and set anchor are simulated.
        mint_id = '97' * 32
        wallet.add_verified_tx(mint_id, info)
        coin = _canonical_spark_coin(script[1:])
        tag = recovered['l_tag_hash']
        wallet.db.put('spark_coins', {tag: dict(value=4 * COIN, group_id=1, tx_checked=True,
            txid=mint_id, l_tag_hash=tag, serialized_coin=base64.b64encode(coin).decode(),
            context=base64.b64encode(context).decode())})
        other_key = bytes(range(32))
        other = libsparkmobile.get_address(other_key, is_testnet=True)
        self.assertTrue(window.set_contact('Supplier', other))
        rows = [(coin, bytes.fromhex(mint_id))]
        for index in (1, 2):
            tx_hash = bytes([index]) * 32
            ctx = libsparkmobile.serialize_mint_context([(tx_hash, index)])
            (decoy, _), = libsparkmobile.create_spark_mint_recipients(
                [(other, index * COIN, '')], ctx, generate=True, is_testnet=True)
            rows.append((_canonical_spark_coin(decoy[1:]), tx_hash))
        cover = dict(set_id=1, raw_coins=rows, block_hash=bytes.fromhex(info.header_hash),
                     set_hash=hashlib.sha256(b''.join(row[0] for row in rows)).digest())
        self.enter_patch(mock.patch.object(wallet, 'get_spark_cover_sets', return_value=[cover]))
        window.update_wallet()
        private = self.review(window, True, other, COIN)
        self.assertEqual(private.tx_type, SPARK_SPEND_V2)
        # The fee shown before sending is the one the transaction declares,
        # so the details dialog can show it afterwards.
        self.assertEqual(spark_spend_fee(private), private._spark_fee)
        self.assertEqual(wallet.get_tx_info(private).fee, private._spark_fee)
        # Max is quoted from the fee estimate alone. It must match what the
        # built transaction then pays.
        everything = wallet.prepareSendSpark(address=other, amount='!')
        self.assertEqual(wallet.estimateSparkSendAll(other),
                         (everything._spark_send_amount, everything._spark_fee))
        self.assertIsNone(wallet.db.get('spark_view_key_hex'))
        spend_context = self.spend_context(private)
        recovered_outputs = [self.recover(o.scriptpubkey, spend_context, other_key)
                             for o in private.outputs() if o.scriptpubkey[0] == OP_SPARKSMINT]
        self.assertEqual([(other, COIN)], [(c['address'], c['value']) for c in recovered_outputs if c])

        amount = 125000000
        address = hardware.get_receiving_address()
        self.assertTrue(window.set_contact('Hardware wallet', address))
        public = self.review(window, True, address, amount)
        self.assertEqual(public.tx_type, SPARK_SPEND_V2)
        wire = Transaction(public.serialize_to_network())
        hardware.receive_history_callback(address, [(wire.txid(), 0)], {})
        hardware.receive_tx_callback(wire.txid(), wire, 0)
        self.assertEqual(hardware.get_balance(), (0, amount, 0))
        hardware.receive_history_callback(address, [(wire.txid(), height)], {})
        hardware.add_verified_tx(wire.txid(), info)
        self.assertEqual(hardware.get_balance(), (amount, 0, 0))
        self.assertEqual(hardware.get_spark_balance().total, 0)
        entry, = hardware.get_spark_aware_history()
        self.assertEqual(entry['spark_activity'], 'Received from Spark at transparent address')
