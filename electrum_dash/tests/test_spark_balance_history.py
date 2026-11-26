import asyncio
import os
from unittest import mock

from electrum_firo.bitcoin import COIN, address_to_script
from electrum_firo.simple_config import SimpleConfig
from electrum_firo.spark_interface import SparkSharedCache
from electrum_firo.transaction import Transaction
from electrum_firo.util import bfh
from electrum_firo.wallet import Abstract_Wallet

from . import TestCaseForTestnet
from .test_spark_interface import MINT_SCRIPT, _bip39_keystore, _raw_tx, _run, _spark_coin, _sync_for, _wallet
from .test_spark_qt import SparkQtTestCase


class TestSparkBalanceHistory(TestCaseForTestnet):
    def setUp(self):
        super().setUp()
        save = mock.patch.object(Abstract_Wallet, 'save_db')
        save.start()
        self.addCleanup(save.stop)
        self.wallet = _wallet(_bip39_keystore(), SimpleConfig({'electrum_path': self.electrum_path}), 'bip39')
        self.wallet.spark_enabled = True

    def test_transparent_address_history_preserves_its_own_deltas(self):
        wallet = self.wallet
        first, second = wallet.get_receiving_addresses()[:2]
        funding = Transaction(_raw_tx([('11' * 32, 0)], [
            (bfh(address_to_script(first)), 2 * COIN), (bfh(address_to_script(second)), COIN)]))
        wallet.add_transaction(funding)
        fee = 2000
        mint = Transaction(_raw_tx([(funding.txid(), 0)], [(MINT_SCRIPT, 2 * COIN - fee)]))
        wallet.add_transaction(mint)
        unrelated = 'ab' * 32
        wallet.db.put('spark_coins', {
            'own': _spark_coin(mint.txid(), 2 * COIN - fee),
            'received': _spark_coin(unrelated, 9 * COIN),
        })
        all_rows = wallet.get_full_history()
        self.assertEqual(-fee, all_rows[mint.txid()]['value'].value)
        self.assertEqual(9 * COIN, all_rows[unrelated]['value'].value)
        first_rows = wallet.get_full_history(onchain_domain=[first])
        self.assertEqual({funding.txid(), mint.txid()}, set(first_rows))
        self.assertEqual(2 * COIN, first_rows[funding.txid()]['value'].value)
        self.assertEqual(-2 * COIN, first_rows[mint.txid()]['value'].value)
        second_rows = wallet.get_full_history(onchain_domain=[second])
        self.assertEqual({funding.txid()}, set(second_rows))
        self.assertEqual(COIN, second_rows[funding.txid()]['balance'].value)
        self.assertFalse(wallet.get_full_history(onchain_domain=[]))

    def test_historical_scan_batches_failure_retry_and_restart(self):
        wallet = self.wallet
        self.assertEqual('waiting', wallet.get_spark_scan_status())
        wallet.db.put('spark_scan_state', {'chain_height': 100})
        self.assertEqual('partial', wallet.get_spark_scan_status())
        wallet.spark_view_key_hex = 'fixture'
        sync = wallet.spark_synchronizer = _sync_for(wallet)
        sync.retrying = sync.syncing = sync._recover_pending = False
        sync._sync_lock = asyncio.Lock()
        sync._wake = mock.Mock(wait=mock.AsyncMock(side_effect=asyncio.CancelledError))
        sync._header_hash_at = mock.Mock(return_value=None)
        sync._shared_cache = mock.Mock(return_value=SparkSharedCache(os.path.join(self.electrum_path, 'sets.json')))
        sync._request = mock.AsyncMock(side_effect=[6, OSError('offline'), 6])
        sync._fetch_mempool_spark_rows = mock.AsyncMock(return_value=([], set()))
        sync.runFetchAndUpdateSparkUsedCoinTags = mock.AsyncMock(return_value=False)
        sync._notify_wallet_updated = mock.AsyncMock()
        sync.trigger = mock.Mock()

        async def fetch_group(group_id, anon_sets, groups, discarded=None):
            anon_sets[str(group_id)] = {'versions': [
                {'block_hash': str(group_id), 'set_hash': '', 'size': 0, 'coins': []}]}

        sync.runFetchAndUpdateSparkAnonSetCacheForGroupId = fetch_group
        with mock.patch.object(wallet, 'is_up_to_date', return_value=True), \
                mock.patch('electrum_firo.spark_interface.libsparkmobile.is_available', return_value=True):
            for status in ('partial', 'retrying', 'complete'):
                with self.subTest(status=status), self.assertRaises(asyncio.CancelledError):
                    _run(sync.main())
                self.assertEqual(status, wallet.get_spark_scan_status())
                self.assertEqual(status == 'complete', wallet.db.get('spark_scan_state')['scan_complete'])
        wallet.spark_synchronizer = None
        self.assertEqual('complete', wallet.get_spark_scan_status())
        wallet.clear_spark_data()
        self.assertEqual('waiting', wallet.get_spark_scan_status())


class TestSparkBalanceHistoryQt(SparkQtTestCase):
    def test_gross_available_matches_both_pool_balances(self):
        window = self.open_wallet(spark_funds=3 * COIN)
        wallet = window.wallet
        address = wallet.get_receiving_addresses()[0]
        tx = Transaction(_raw_tx([('11' * 32, 0)], [(bfh(address_to_script(address)), 2 * COIN)]))
        wallet.add_transaction(tx)
        wallet.db.put('spark_scan_state', {'scan_complete': True, 'chain_height': 42})
        window.show_send_tab()
        for spark, amount, label in ((False, 2 * COIN, window.transparent_available_label),
                                     (True, 3 * COIN, window.spark_available_label)):
            window.toggle_send_balance_mode(window.send_source_combo.findData(spark))
            window.update_balances()
            self.assertEqual(amount, window.av_amnt.get_amount())
            self.assertIn(window.format_amount_and_units(amount), label.text())
        wallet.db.put('spark_scan_state', {'chain_height': 42})
        window.update_balances()
        window.update_available_amount()
        self.assertIn('found so far', window.spark_available_label.text())
        self.assertIsNone(window.av_amnt.get_amount())
        with mock.patch.object(wallet, 'spark_synchronizer', mock.Mock(retrying=True, syncing=False)):
            window.update_balances()
            self.assertIn('Retrying', window.spark_available_label.text())

    def test_local_spark_receipt_details_and_copy(self):
        from PyQt5.QtCore import QPoint

        window = self.open_wallet()
        txid = 'ab' * 32
        window.wallet.db.put('spark_coins', {'received': _spark_coin(txid, 9 * COIN)})
        self.assertIsNone(window.wallet.db.get_transaction(txid))
        self.assertIsNone(window.history_model.get_domain())
        entry = window.wallet.get_full_history()[txid]
        with mock.patch.dict(window.history_model.transactions, {txid: entry}):
            window.history_list.show_transaction(txid)
        message = window.show_message.call_args
        self.assertEqual('Spark receipt', message.kwargs['title'])
        self.assertIn(txid, message.args[0])
        self.assertIn(window.format_amount_and_units(9 * COIN), message.args[0])
        index = mock.Mock()
        index.internalPointer.return_value = entry
        with mock.patch.object(window.history_list, 'indexAt', return_value=index), \
                mock.patch('electrum_firo.gui.qt.history_list.QMenu') as menu, \
                mock.patch.object(window.history_list, 'place_text_on_clipboard') as copy:
            window.history_list.create_menu(QPoint())
            actions = dict(call.args for call in menu.return_value.addAction.call_args_list)
            actions['Copy transaction ID']()
            copy.assert_called_once_with(txid, title='Transaction ID')
