from types import SimpleNamespace
from unittest import mock

from electrum_firo.bitcoin import COIN
from electrum_firo.network import BestEffortRequestFailed
from electrum_firo.spark_interface import OP_SPARKMINT
from electrum_firo.transaction import PartialTxOutput

from .test_spark_qt import SPARK_ADDRESS, SparkQtTestCase


class TestSparkSendDrafts(SparkQtTestCase):
    def test_unlock_preserves_transparent_draft_and_max(self):
        window = self.open_wallet(enable=False)
        window.payto_e.setPlainText(window.wallet.get_receiving_address())
        window.amount_e.setAmount(2 * COIN)
        window.max_button.setChecked(True)
        with mock.patch.object(window, 'question', return_value=True):
            window.init_spark_session()
        self.assertTrue(window.wallet.spark_enabled)
        self.assertFalse(window.send_from_private)
        self.assertTrue(window.max_button.isChecked())
        self.assertEqual(2 * COIN, window.amount_e.get_amount())

    def test_max_invalidated_on_recipient_change_and_failed_source_quote(self):
        window = self.open_wallet(spark_funds=8 * COIN)
        window.payto_e.setPlainText(SPARK_ADDRESS)
        window.amount_e.setAmount(5 * COIN)
        window.max_button.setChecked(True)
        window.payto_e.setPlainText(window.wallet.get_receiving_address())
        self.assertFalse(window.max_button.isChecked())
        self.assertIsNone(window.amount_e.get_amount())

        window.toggle_send_balance_mode(window.send_source_combo.findData(False))
        window.payto_e.setPlainText(SPARK_ADDRESS)
        window.amount_e.setAmount(2 * COIN)
        window.max_button.setChecked(True)
        with mock.patch.object(window.wallet, 'has_keystore_encryption', return_value=True), \
                mock.patch.object(window, 'password_dialog', return_value=None):
            window.toggle_send_balance_mode(window.send_source_combo.findData(True))
        self.assertTrue(window.send_from_private)
        self.assertFalse(window.max_button.isChecked())
        self.assertIsNone(window.amount_e.get_amount())

    def test_max_from_spark_uses_the_fee_estimate_without_building_a_spend(self):
        window = self.open_wallet(spark_funds=8 * COIN)
        window.payto_e.setPlainText(SPARK_ADDRESS)
        with mock.patch.object(window.wallet, 'estimateSparkSendAll',
                               return_value=(8 * COIN - 500, 500)) as quote, \
                mock.patch.object(window.wallet, 'prepareSendSpark') as build:
            window.spend_max()
        quote.assert_called_once_with(SPARK_ADDRESS, None)
        build.assert_not_called()
        self.assertTrue(window.max_button.isChecked())
        self.assertEqual(8 * COIN - 500, window.amount_e.get_amount())

    def test_short_funds_message_for_spark_routes(self):
        from electrum_firo.gui.qt.confirm_tx_dialog import PaymentRoute
        window = self.open_wallet()
        dialog = mock.Mock(route=PaymentRoute(SPARK_ADDRESS, True, True), output_value=COIN)
        with mock.patch.object(window.wallet, 'get_balance', return_value=(2 * COIN, 0, 0)):
            window._on_conf_dlg_not_enough_funds(dialog, None)
        self.assertIn('set Pay from to Transparent', window.show_message.call_args.args[0])
        # A move into Spark has no lower fee to offer, so it reports at once.
        dialog.route = PaymentRoute(SPARK_ADDRESS, False, True)
        window._on_conf_dlg_not_enough_funds(dialog, None)
        self.assertEqual(window.get_text_not_enough_funds_mentioning_frozen(),
                         window.show_message.call_args.args[0])
        dialog.bg_check_have_enough_funds_assuming_zero_fees.assert_not_called()

    def test_pasted_payment_link_replaces_whole_draft_only_on_consent(self):
        window = self.open_wallet()
        first, second = window.wallet.get_receiving_addresses()[:2]
        window.payto_e.setPlainText(first)
        window.message_e.setText('First receipt')
        window.amount_e.setAmount(3 * COIN)
        window.max_button.setChecked(True)
        original = window.payment_draft_snapshot()
        uri = 'firo:' + second
        with mock.patch.object(window, 'question', return_value=False) as confirm:
            window.payto_e.setPlainText(uri)
        confirm.assert_called_once()
        self.assertEqual(original, window.payment_draft_snapshot())
        self.assertEqual(first, window._get_send_destination_address())
        with mock.patch.object(window, 'question', return_value=True) as confirm:
            window.payto_e.setPlainText(uri)
        confirm.assert_called_once()
        self.assertEqual(second, window._get_send_destination_address())
        self.assertIsNone(window.amount_e.get_amount())
        self.assertEqual('', window.message_e.text())
        self.assertFalse(window.max_button.isChecked())
        self.assertIsNone(window.payment_request)

    def test_replaced_payment_request_discards_late_response(self):
        window = self.open_wallet()
        uri_data = {'r': 'https://example.invalid/request'}
        with mock.patch('electrum_firo.gui.qt.main_window.util.parse_URI', return_value=uri_data) as parse:
            window.pay_to_URI('firo:?r=https://example.invalid/request')
        receive_request = parse.call_args.args[1]
        second = window.wallet.get_receiving_address()
        with mock.patch.object(window, 'question', return_value=True):
            window.pay_to_URI('firo:' + second)
        late_request = mock.Mock()
        late_request.verify.return_value = True
        receive_request(late_request)
        self.assertIsNone(window.payment_request)
        self.assertEqual(second, window._get_send_destination_address())

    def test_success_clears_only_matching_submitted_draft(self):
        window = self.open_wallet()
        window.payto_e.setPlainText(SPARK_ADDRESS)
        window.amount_e.setAmount(COIN)
        submitted = window.payment_draft_snapshot()
        tx = mock.Mock()
        tx.txid.return_value = 'submitted-id'
        tx._spark_used_tags = []
        path = 'electrum_firo.gui.qt.main_window.WaitingDialog'
        with mock.patch(path) as waiting:
            window.broadcast_transaction(tx, None, submitted_draft=submitted)
        finish = waiting.call_args.args[3]
        window.message_e.setText('New draft')
        finish((True, 'submitted-id'))
        self.assertEqual('New draft', window.message_e.text())
        window.do_clear()
        window.payto_e.setPlainText(SPARK_ADDRESS)
        window.amount_e.setAmount(COIN)
        finish((True, 'submitted-id'))
        self.assertEqual(COIN, window.amount_e.get_amount())
        with mock.patch(path) as waiting:
            window.broadcast_transaction(tx, None, submitted_draft=window.payment_draft_snapshot())
        waiting.call_args.args[3]((True, 'submitted-id'))
        self.assertFalse(window.has_payment_draft())

    def test_partial_mint_reports_recipient_amounts_and_uncertain_transaction(self):
        window = self.open_wallet()
        txs = []
        for txid, amount in [('first-id', 2 * COIN), ('second-id', 3 * COIN), ('third-id', 4 * COIN)]:
            tx = mock.Mock()
            tx.txid.return_value = txid
            tx.outputs.return_value = [
                PartialTxOutput(scriptpubkey=bytes([OP_SPARKMINT]), value=amount),
                PartialTxOutput(scriptpubkey=b'\x51', value=7 * COIN),
            ]
            txs.append(tx)
        path = 'electrum_firo.gui.qt.main_window.WaitingDialog'
        for accepted, uncertain, unsent in [(0, 2, 7), (1, 3, 4)]:
            with self.subTest(accepted=accepted):
                window.payto_e.setPlainText(SPARK_ADDRESS)
                window.amount_e.setAmount(9 * COIN)
                network = SimpleNamespace(is_connected=lambda: False, proxy=None,
                                          run_from_another_thread=mock.Mock(
                                              side_effect=[None] * accepted + [BestEffortRequestFailed()]))
                with mock.patch.object(window, 'network', network), \
                        mock.patch.object(window.wallet, 'confirmSparkMintTransactions'), \
                        mock.patch.object(window.wallet.psman, 'broadcast_transaction', new=mock.Mock()), \
                        mock.patch(path) as waiting:
                    window.broadcast_spark_mints(txs, None, wallet_note='Only submitted parts',
                                                 submitted_draft=window.payment_draft_snapshot())
                    work, finish = waiting.call_args.args[2:4]
                    finish(work())
                message = window.show_error.call_args.args[0]
                self.assertIn('Submitted {} of 3 transactions ({})'.format(
                    accepted, window.format_amount_and_units(2 * COIN if accepted else 0)), message)
                self.assertIn('{} ({}) before retrying'.format(
                    txs[accepted].txid(), window.format_amount_and_units(uncertain * COIN)), message)
                self.assertIn('Unsent recipient amount: ' + window.format_amount_and_units(unsent * COIN), message)
                self.assertFalse(window.has_payment_draft())
                for i, tx in enumerate(txs):
                    self.assertEqual('Only submitted parts' if i < accepted else '', window.wallet.get_label(tx.txid()))
