import asyncio
import base64
import os
import struct
import tempfile
from unittest import mock, skipUnless

from electrum_firo import keystore, libsparkmobile
from electrum_firo.bitcoin import address_to_script
from electrum_firo.dash_tx import SPARK_SPEND, SPARK_SPEND_V2, FiroSparkSpend
from electrum_firo.logging import get_logger
from electrum_firo.simple_config import SimpleConfig
from electrum_firo.bitcoin import hash160_to_exp2pkh, hash160_to_p2pkh
from electrum_firo.interface import NotificationSession
from electrum_firo.spark_interface import (
    FiroCacheCoordinator, MAX_SPARK_GROUP_ID, MAX_SPARK_SET_SIZE,
    OP_SPARKMINT,
    SPARK_CHAUM_V2_START_HEIGHT,
    SPARK_SET_CACHE_VERSION, SPEND_MISSING_CHECKS_BEFORE_RELEASE,
    SparkServerMisbehaving,
    SparkSharedCache, SparkSynchronizer, _is_tx_not_found_error,
    _parse_group_id, _parse_set_meta, _tx_contains_spark_coin,
    is_exchange_address, spark_spend_fee)
from electrum_firo.transaction import PartialTransaction, PartialTxOutput, Transaction
from electrum_firo.util import NotEnoughFunds, TxMinedInfo, bfh
from electrum_firo.wallet import Abstract_Wallet

from . import TestCaseForTestnet
from .test_wallet_vertical import WalletIntegrityHelper


SEED = 'cycle rocket west magnet parrot shuffle foot correct salt library feed song'
COIN_BYTES = (b'\x00' + bytes(range(1, 103)) + bytes([60]) + b'\x11' * 60
              + bytes([16]) + b'\x22' * 16 + bytes([32]) + b'\x33' * 32
              + (7000).to_bytes(8, 'little'))
MINT_SCRIPT = bytes([OP_SPARKMINT]) + COIN_BYTES + b'\x44' * 66
ROW_BYTES = COIN_BYTES + b'\x55' * 32 + b'\xfd\x9a\x01' + b'\x66' * 410


def _varint(n: int) -> bytes:
    if n < 0xfd:
        return bytes([n])
    return b'\xfd' + struct.pack('<H', n)


def _raw_tx(inputs, outputs) -> str:
    raw = struct.pack('<i', 1) + _varint(len(inputs))
    for txid, n in inputs:
        raw += bytes.fromhex(txid)[::-1] + struct.pack('<I', n)
        raw += _varint(0) + struct.pack('<I', 0xfffffffe)
    raw += _varint(len(outputs))
    for script, value in outputs:
        raw += struct.pack('<q', value) + _varint(len(script)) + script
    raw += struct.pack('<I', 0)
    return raw.hex()


def _spark_coin(txid: str, value: int, serialized=ROW_BYTES, **kw) -> dict:
    coin = {
        'txid': txid, 'value': value, 'type': 'mint', 'group_id': 1,
        'height': None, 'is_used': False, 'l_tag_hash': 'tag-' + txid[:8],
        'serialized_coin': base64.b64encode(serialized).decode(),
        'context': '', 'serial_context': '',
    }
    coin.update(kw)
    return coin


def _set_row(coin: dict) -> list:
    """The anonymity set row that pairs `coin` with its txid."""
    return [coin['serialized_coin'],
            base64.b64encode(bytes.fromhex(coin['txid'])[::-1]).decode(), '']


def _bip39_keystore(derivation=None):
    root_seed = keystore.bip39_to_seed(SEED, '')
    return keystore.from_bip43_rootseed(
        root_seed, derivation or keystore.bip44_derivation(0), xtype='standard')


def _wallet(ks, config, seed_type=None):
    wallet = WalletIntegrityHelper.create_standard_wallet(
        ks, gap_limit=2, config=config)
    if seed_type:
        wallet.db.put('seed_type', seed_type)
    return wallet


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _sync_for(wallet) -> SparkSynchronizer:
    sync = object.__new__(SparkSynchronizer)
    sync.wallet = wallet
    sync.logger = get_logger(__name__)
    sync._missing_spend_checks = {}
    sync._refetched_groups = set()
    return sync


class TestSparkWallet(TestCaseForTestnet):

    def setUp(self):
        super().setUp()
        self.config = SimpleConfig({'electrum_path': self.electrum_path})
        patcher = mock.patch.object(Abstract_Wallet, 'save_db')
        patcher.start()
        self.addCleanup(patcher.stop)
        self.wallet = _wallet(_bip39_keystore(), self.config, 'bip39')
        self.wallet.spark_enabled = True
        self.wallet.db.put('stored_height', 200)

    def _fund(self, value: int) -> Transaction:
        addr = self.wallet.get_receiving_addresses()[0]
        funding = Transaction(_raw_tx(
            [('11' * 32, 0)], [(bfh(address_to_script(addr)), value)]))
        self.wallet.add_transaction(funding)
        self.wallet.add_unverified_tx(funding.txid(), 100)
        return funding

    def test_can_use_spark_only_for_bip39_wallets_on_the_standard_path(self):
        self.assertTrue(self.wallet.can_use_spark())
        electrum_seed = _wallet(keystore.from_seed(SEED, '', False), self.config)
        self.assertFalse(electrum_seed.can_use_spark())
        other_path = _wallet(_bip39_keystore("m/44'/1'/1'"), self.config, 'bip39')
        self.assertFalse(other_path.can_use_spark())
        xprv = _wallet(keystore.from_xprv(
            self.wallet.keystore.get_master_private_key(None)), self.config)
        self.assertFalse(xprv.can_use_spark())
        with self.assertRaises(RuntimeError):
            electrum_seed.derive_spark_key(None)
        imported = WalletIntegrityHelper.create_imported_wallet(
            config=self.config, privkeys=True)
        self.assertFalse(imported.can_use_spark())
        multisig = WalletIntegrityHelper.create_multisig_wallet(
            [keystore.from_seed(SEED, '', True),
             keystore.from_xpub(self.wallet.keystore.get_master_public_key())],
            '1of2', config=self.config)
        self.assertFalse(multisig.can_use_spark())
        with self.assertRaises(RuntimeError):
            imported.derive_spark_key(None)

    def test_no_password_or_spend_key_kept_on_wallet(self):
        self.assertFalse(hasattr(self.wallet, 'spark_password'))
        self.assertFalse(hasattr(self.wallet, 'spark_key_data'))

    def test_spark_send_is_rejected_before_v2_activation(self):
        with mock.patch.object(self.wallet, 'get_local_height',
                               return_value=210_798), \
                mock.patch.object(self.wallet, 'derive_spark_key') as derive:
            with self.assertRaisesRegex(RuntimeError, 'not active'):
                self.wallet.prepareSendSpark(
                    password=None, address='unused', amount=1)
        derive.assert_not_called()
        with mock.patch.object(self.wallet, 'get_local_height',
                               return_value=210_799):
            self.wallet._ensure_spark_send_supported()

    def test_spark_send_is_enabled_after_v2_activation_on_each_network(self):
        self.assertEqual(
            {'mainnet', 'testnet', 'devnet', 'regtest'},
            set(SPARK_CHAUM_V2_START_HEIGHT))
        for network, activation_height in SPARK_CHAUM_V2_START_HEIGHT.items():
            with mock.patch(
                    'electrum_firo.spark_interface.constants.net.NET_NAME',
                    network), \
                    mock.patch.object(
                        self.wallet, 'get_local_height',
                        return_value=activation_height - 1):
                self.wallet._ensure_spark_send_supported()

    def test_self_mint_history_shows_only_fee(self):
        funding = self._fund(10 * 10**8)
        minted, fee = 10 * 10**8 - 100_000, 100_000
        mint = Transaction(_raw_tx(
            [(funding.txid(), 0)],
            [(MINT_SCRIPT, minted)]))
        self.wallet.add_transaction(mint)
        self.wallet.add_unverified_tx(mint.txid(), 101)
        coin = _spark_coin(mint.txid(), minted, height=101, tx_checked=True)
        self.wallet.db.put('spark_coins', {coin['l_tag_hash']: coin})

        history = self.wallet.get_full_history()
        self.assertEqual(-fee, history[mint.txid()]['bc_value'].value)
        self.assertEqual('Moved to Spark', history[mint.txid()]['spark_activity'])
        self.assertEqual(minted, history[mint.txid()]['spark_moved'].value)
        self.assertFalse(history[mint.txid()]['incoming'])
        last = list(history.values())[-1]
        transparent = sum(self.wallet.get_balance())
        spark = self.wallet.get_spark_balance().total
        self.assertEqual(transparent + spark, last['balance'].value)

    def test_mint_honors_empty_input_selection(self):
        self._fund(10**8)
        with mock.patch('electrum_firo.spark_interface.libsparkmobile.is_available', return_value=True), \
                mock.patch.object(self.wallet, 'get_spendable_coins') as all_coins:
            for amount in (5000, '!'):
                for selection in ({'coins': []}, {'utxos': []}):
                    with self.subTest(amount=amount, selection=selection), self.assertRaises(NotEnoughFunds):
                        self.wallet.prepareSparkMintTransaction(spark_address='spark', amount=amount, **selection)
            all_coins.assert_not_called()

    @skipUnless(libsparkmobile.is_available(), 'electrum_libsparkmobile is not built')
    def test_mint_leftover_below_dust_is_paid_as_fee(self):
        funded = 10**8
        self._fund(funded)
        address = libsparkmobile.get_address(self.wallet.derive_spark_key(None), 1, is_testnet=True)

        def mint(amount):
            return self.wallet.prepareSparkMintTransaction(
                spark_address=address, amount=amount, coins=self.wallet.get_spendable_coins(None))
        fee = mint(funded // 2)._spark_fee
        for leftover in (self.wallet.dust_threshold() - 1, 1, 0):
            with self.subTest(leftover=leftover):
                tx = mint(funded - fee - leftover)
                paid = sum(i.value_sats() for i in tx.inputs()) - sum(o.value for o in tx.outputs())
                self.assertEqual(1, len(tx.outputs()))
                self.assertEqual(funded - fee - leftover, tx._spark_send_amount)
                self.assertEqual((fee + leftover, fee + leftover), (paid, tx._spark_fee))
        # The amount fits the balance, but the amount and its fee do not.
        with self.assertRaises(NotEnoughFunds):
            mint(funded - 1)

    def test_split_mint_shows_totals(self):
        self._fund(10**8)

        def mint(value, fee):
            tx = PartialTransaction()
            tx._outputs = [PartialTxOutput(scriptpubkey=MINT_SCRIPT, value=value)]
            tx._spark_fee = fee
            return tx
        mints = [mint(1000, 10), mint(2000, 20)]
        lib = 'electrum_firo.spark_interface.libsparkmobile'
        with mock.patch(f'{lib}.is_available', return_value=True), \
                mock.patch(f'{lib}.is_valid_spark_address', return_value=True), \
                mock.patch.object(self.wallet, '_createSparkMintTransactions',
                                  return_value=mints):
            first = self.wallet.prepareSparkMintTransaction(
                spark_address='spark', amount=3000)
        self.assertEqual([mints[1]], first.sparkMints)
        self.assertEqual(30, first._spark_fee)
        self.assertEqual(3000, first._spark_send_amount)

    def test_spark_coin_needs_tx_check_and_spv(self):
        coin = _spark_coin('22' * 32, 5000, height=50)
        self.assertIsNone(self.wallet.get_spark_coin_height(coin))
        coin['tx_checked'] = True
        self.assertIsNone(self.wallet.get_spark_coin_height(coin))
        self.wallet.db.add_verified_tx(
            coin['txid'], TxMinedInfo(height=50, timestamp=1, txpos=1,
                                      header_hash='00' * 32))
        self.assertEqual(50, self.wallet.get_spark_coin_height(coin))

    def test_fabricated_mint_row_is_not_credited(self):
        real = Transaction(_raw_tx(
            [('33' * 32, 0)], [(MINT_SCRIPT, 7000)]))
        honest = _spark_coin(real.txid(), 7000, l_tag_hash='honest')
        forged = _spark_coin(real.txid(), 9_000_000,
                             serialized=b'\x42' * 100, l_tag_hash='forged')
        coins = {'honest': honest, 'forged': forged}
        sync = _sync_for(self.wallet)

        async def fetch(txids):
            return {real.txid(): {'hex': real.serialize(),
                                  'height': 123, 'blocktime': 1}}
        sync._batch_fetch_transactions = fetch
        _run(sync._verify_coin_transactions(coins))

        self.assertNotIn('forged', coins)
        self.assertTrue(coins['honest']['tx_checked'])
        self.assertIsNone(coins['honest']['height'])
        self.assertEqual(123, self.wallet.get_unverified_txs()[real.txid()])

    def test_server_returning_other_tx_is_rejected(self):
        other = Transaction(_raw_tx(
            [('44' * 32, 0)], [(MINT_SCRIPT, 7000)]))
        coin = _spark_coin('55' * 32, 7000)
        coins = {coin['l_tag_hash']: coin}
        sync = _sync_for(self.wallet)

        async def fetch(txids):
            return {'55' * 32: {'hex': other.serialize(), 'height': 5}}
        sync._batch_fetch_transactions = fetch
        _run(sync._verify_coin_transactions(coins))
        self.assertFalse(coin.get('tx_checked'))
        self.assertNotIn('55' * 32, self.wallet.get_unverified_txs())

    def test_rejected_coin_names_the_cached_set_that_gave_its_txid(self):
        real = Transaction(_raw_tx(
            [('33' * 32, 0)], [(MINT_SCRIPT, 7000)]))
        forged = _spark_coin(real.txid(), 7000, serialized=b'\x42' * 100)
        sync = _sync_for(self.wallet)
        sync._batch_fetch_transactions = mock.AsyncMock(return_value={
            real.txid(): {'hex': real.serialize(), 'height': 123}})
        cached = {'1': {'versions': [{'coins': [_set_row(forged)]}]}}
        self.assertEqual({1}, _run(sync._verify_coin_transactions(
            {'forged': dict(forged)}, cached)))
        # The coin came from the mempool, so no cached set is at fault.
        self.assertEqual(set(), _run(sync._verify_coin_transactions(
            {'forged': dict(forged)}, {})))

    def test_unconfirmed_coin_is_forgotten_once_its_transaction_is_gone(self):
        sync = _sync_for(self.wallet)
        sync._batch_fetch_transactions = mock.AsyncMock(return_value={})
        gone, pending = '66' * 32, '77' * 32

        def left(*args):
            coins = {txid: _spark_coin(txid, 5000) for txid in (gone, pending)}
            _run(sync._verify_coin_transactions(coins, *args))
            return set(coins)
        self.assertEqual({gone, pending}, left())  # mempool list unknown
        self.assertEqual({pending}, left({}, {pending}))
        # A coin that a cached set holds was mined: it is kept.
        cached = {'1': {'versions': [{'coins': [_set_row(_spark_coin(gone, 5000))]}]}}
        self.assertEqual({gone, pending}, left(cached, {pending}))

    def test_imported_spend_must_match_its_txid(self):
        other = Transaction(_raw_tx(
            [('44' * 32, 0)], [(MINT_SCRIPT, 7000)]))
        claimed = '99' * 32
        sync = _sync_for(self.wallet)

        async def fetch(txids):
            return {claimed: {'hex': other.serialize(), 'height': 0}}
        sync._batch_fetch_transactions = fetch
        with mock.patch.object(self.wallet, 'getSparkSpendTransactionIds',
                               return_value=[('tag', claimed)]):
            _run(sync._import_missing_spark_spend_transactions())
        self.assertIsNone(self.wallet.db.get_transaction(other.txid()))

    def test_dropped_spend_releases_reservation(self):
        spend_txid = '66' * 32
        coin = _spark_coin('77' * 32, 5000, is_used=True,
                           spent_txid=spend_txid, l_tag_hash='mine')
        self.wallet.db.put('spark_coins', {'mine': coin})
        self.wallet.db.put('spark_scan_state',
                           {'pending_spark_spends': [spend_txid]})
        sync = _sync_for(self.wallet)
        for _ in range(SPEND_MISSING_CHECKS_BEFORE_RELEASE - 1):
            self.assertFalse(sync._maybe_release_dropped_spend(spend_txid, {}))
        self.assertTrue(self.wallet.db.get('spark_coins')['mine']['is_used'])
        self.assertTrue(sync._maybe_release_dropped_spend(spend_txid, {}))
        released = self.wallet.db.get('spark_coins')['mine']
        self.assertFalse(released['is_used'])
        self.assertNotIn('spent_txid', released)
        self.assertEqual(
            [], self.wallet.db.get('spark_scan_state')['pending_spark_spends'])

    def test_spend_with_canonical_tag_is_not_released(self):
        spend_txid = '88' * 32
        coin = _spark_coin('99' * 32, 5000, is_used=True,
                           spent_txid=spend_txid, l_tag_hash='mine')
        self.wallet.db.put('spark_coins', {'mine': coin})
        sync = _sync_for(self.wallet)
        for _ in range(SPEND_MISSING_CHECKS_BEFORE_RELEASE + 1):
            self.assertFalse(sync._maybe_release_dropped_spend(
                spend_txid, {'mine': spend_txid}))
        self.assertTrue(self.wallet.db.get('spark_coins')['mine']['is_used'])

    def _history_fixture(self):
        funding = self._fund(10 * 10**8)
        mint = Transaction(_raw_tx(
            [(funding.txid(), 0)],
            [(MINT_SCRIPT, 10 * 10**8 - 100_000)]))
        self.wallet.add_transaction(mint)
        self.wallet.add_unverified_tx(mint.txid(), 101)
        spend = Transaction(_raw_tx([('ab' * 32, 0)], [(b'\x6a', 0)]))
        self.wallet.add_transaction(spend, allow_unrelated=True)
        self_mint = _spark_coin(mint.txid(), 10 * 10**8 - 100_000,
                                height=101, timestamp=1001, tx_checked=True,
                                l_tag_hash='self-mint', is_used=True,
                                spent_txid=spend.txid())
        received = _spark_coin('cd' * 32, 3 * 10**8, height=102,
                               timestamp=1002, tx_checked=True,
                               l_tag_hash='received')
        change = _spark_coin(spend.txid(), 9 * 10**8, l_tag_hash='change')
        self.wallet.db.put('spark_coins', {
            'self-mint': self_mint, 'received': received, 'change': change})
        return mint, spend

    def test_detailed_history_matches_full_history(self):
        mint, spend = self._history_fixture()
        full = self.wallet.get_full_history()
        detailed = self.wallet.get_detailed_history()
        full_values = {k: v['bc_value'].value for k, v in full.items()}
        detailed_values = {i['txid']: i['bc_value'].value
                           for i in detailed['transactions']}
        self.assertEqual(full_values, detailed_values)
        self.assertEqual(-100_000, detailed_values[mint.txid()])
        self.assertEqual(3 * 10**8, detailed_values['cd' * 32])
        self.assertEqual('Received in Spark', full['cd' * 32]['spark_activity'])
        self.assertEqual(9 * 10**8 - (10 * 10**8 - 100_000),
                         detailed_values[spend.txid()])
        end_balance = detailed['summary']['end']['BTC_balance'].value
        self.assertEqual(sum(full_values.values()), end_balance)
        self.assertEqual(sum(self.wallet.get_balance())
                         + self.wallet.get_spark_balance().total, end_balance)

    def test_exchange_address_rejected_before_proof(self):
        exchange = hash160_to_exp2pkh(bytes(20))
        self.assertTrue(is_exchange_address(exchange))
        self.assertFalse(is_exchange_address(hash160_to_p2pkh(bytes(20))))
        self.wallet.db.put('spark_coins', {'c': _spark_coin(
            'ef' * 32, 10**8, height=1, tx_checked=True, l_tag_hash='c')})
        with mock.patch.object(self.wallet, '_ensure_spark_send_supported'), \
                mock.patch('electrum_firo.spark_interface._createSparkSend') as proof, \
                mock.patch('electrum_firo.libsparkmobile.is_valid_spark_address',
                           return_value=False) as validate_spark_address:
            with self.assertRaises(ValueError) as ctx:
                self.wallet._prepareSendSpark(
                    bytes(32), address=exchange, amount=1000)
            proof.assert_not_called()
        self.assertEqual(1, validate_spark_address.call_count)
        self.assertIn('exchange address', str(ctx.exception))


class TestSparkHelpers(TestCaseForTestnet):

    def test_sector_fetch_starts_after_cached_prefix(self):
        sync = _sync_for(mock.Mock())
        sync.sectorSize = 3
        requested = []

        async def request(method, params=(), timeout=120):
            start, end = map(int, params[2:])
            requested.append((start, end))
            return {'coins': [['coin', 'txid', 'context']
                              for _ in range(end - start)]}

        sync._request = request
        rows = _run(sync._fetch_sectors(
            1, 'block-hash', 5, start_index=4))

        self.assertEqual([(4, 7), (7, 9)], requested)
        self.assertEqual(5, len(rows))

    def test_set_update_rebuilds_legacy_cache_and_verifies_anchor(self):
        wallet = mock.Mock()
        sync = _sync_for(wallet)
        sync.syncing = False
        old_block_hash = base64.b64encode(b'\x01' * 32).decode()
        block_hash = base64.b64encode(b'\x02' * 32).decode()
        rows = [[f'coin-{i}', f'txid-{i}', f'context-{i}']
                for i in range(5)]
        requests = []
        sync._request = mock.AsyncMock(return_value={
            'blockHash': block_hash,
            'setHash': base64.b64encode(b'\x03' * 32).decode(),
            'size': 5,
        })

        async def fetch_sectors(group_id, current_block_hash, count,
                                start_index=0):
            requests.append((start_index, count))
            return rows[start_index:start_index + count]

        sync._fetch_sectors = fetch_sectors
        sync._verify_set_block = mock.AsyncMock(return_value=120)
        anon_sets = {'1': {'versions': [{
            'block_hash': old_block_hash,
            'block_height': 100,
            'set_hash': old_block_hash,
            'size': 4,
            'coins': [[f'old-{i}', f'old-txid-{i}', f'old-context-{i}']
                      for i in range(4)],
        }]}}
        groups = {}

        with mock.patch('electrum_firo.spark_interface.util.trigger_callback'):
            _run(sync.runFetchAndUpdateSparkAnonSetCacheForGroupId(
                1, anon_sets, {}, groups))

        self.assertEqual([(0, 5)], requests)
        sync._verify_set_block.assert_awaited_once_with(
            block_hash, rows[0])
        self.assertEqual(120, anon_sets['1']['versions'][-1]['block_height'])
        self.assertEqual(SPARK_SET_CACHE_VERSION,
                         anon_sets['1']['cache_version'])
        self.assertEqual([list(reversed(rows))],
                         [v['coins'] for v in anon_sets['1']['versions']])

    def test_set_growth_fetches_newest_prefix(self):
        wallet = mock.Mock()
        sync = _sync_for(wallet)
        sync.syncing = False
        old_block_hash = base64.b64encode(b'\x01' * 32).decode()
        block_hash = base64.b64encode(b'\x02' * 32).decode()
        old_row = ['old-coin', 'old-txid', 'old-context']
        new_row = ['new-coin', 'new-txid', 'new-context']
        requests = []
        sync._request = mock.AsyncMock(return_value={
            'blockHash': block_hash,
            'setHash': base64.b64encode(b'\x03' * 32).decode(),
            'size': 5,
        })

        async def fetch_sectors(group_id, current_block_hash, count,
                                start_index=0):
            requests.append((start_index, count))
            return [new_row]

        sync._fetch_sectors = fetch_sectors
        sync._verify_set_block = mock.AsyncMock(return_value=120)
        anon_sets = {'1': {
            'cache_version': SPARK_SET_CACHE_VERSION,
            'versions': [{
                'block_hash': old_block_hash,
                'block_height': 100,
                'set_hash': old_block_hash,
                'size': 4,
                'coins': [old_row],
            }],
        }}
        groups = {}

        with mock.patch('electrum_firo.spark_interface.util.trigger_callback'):
            _run(sync.runFetchAndUpdateSparkAnonSetCacheForGroupId(
                1, anon_sets, {}, groups))

        self.assertEqual([(0, 1)], requests)
        sync._verify_set_block.assert_awaited_once_with(block_hash, new_row)
        self.assertEqual(
            [new_row, old_row],
            FiroCacheCoordinator.getSetCoinsForGroupId(
                wallet.db, 1, anon_sets=anon_sets))

    def test_set_that_contradicts_the_cache_is_fetched_whole(self):
        block_a = base64.b64encode(b'\x01' * 32).decode()
        block_b = base64.b64encode(b'\x02' * 32).decode()
        set_hash = base64.b64encode(b'\x03' * 32).decode()
        rows = [[f'coin-{i}', f'txid-{i}', f'context-{i}'] for i in range(3)]
        # The server reports 3 coins at block B. The cache holds a larger set
        # from height 100, or another size for the same block.
        for cached_size, cached_block, server_height, requested in (
                (5, block_a, 120, [(0, 1), (0, 3)]),  # server is past the cached block
                (2, block_b, 120, [(0, 3)]),
                (5, block_a, 90, [(0, 1)])):          # server is behind: cache kept
            with self.subTest(cached_size=cached_size, server_height=server_height):
                sync = _sync_for(mock.Mock())
                sync.syncing = False
                sync._request = mock.AsyncMock(return_value={
                    'blockHash': block_b, 'setHash': set_hash, 'size': 3})
                requests = []

                async def fetch_sectors(group_id, block_hash, count, start_index=0):
                    requests.append((start_index, count))
                    return rows[start_index:start_index + count]
                sync._fetch_sectors = fetch_sectors
                sync._verify_set_block = mock.AsyncMock(return_value=server_height)
                stale = [['stale', 'stale-txid', 'stale-context']]
                anon_sets = {'1': {
                    'cache_version': SPARK_SET_CACHE_VERSION, 'epoch': 'old',
                    'versions': [{
                        'block_hash': cached_block, 'block_height': 100,
                        'set_hash': set_hash, 'size': cached_size,
                        'coins': stale}]}}
                groups, discarded = {'1': {'size': cached_size}}, set()
                with mock.patch('electrum_firo.spark_interface.util.trigger_callback'):
                    _run(sync.runFetchAndUpdateSparkAnonSetCacheForGroupId(
                        1, anon_sets, {}, groups, discarded=discarded))
                self.assertEqual(requested, requests)
                if server_height < 100:
                    self.assertEqual(set(), discarded)
                    self.assertEqual('old', anon_sets['1']['epoch'])
                    self.assertEqual([stale], [v['coins'] for v in anon_sets['1']['versions']])
                    self.assertEqual({'1': {'size': cached_size}}, groups)
                    continue
                self.assertEqual({1}, discarded)
                self.assertEqual([list(reversed(rows))],
                                 [v['coins'] for v in anon_sets['1']['versions']])
                self.assertNotIn(anon_sets['1']['epoch'], ('', 'old'))

    def test_used_tags_are_fetched_again_when_the_cache_is_not_a_prefix(self):
        def row(tag):
            return [base64.b64encode(bytes([tag]) * 34).decode(),
                    base64.b64encode(bytes([tag]) * 32).decode()]
        cases = (
            (b'abc', ['a', 'b', 'c'], False),  # a new tag follows the cached ones
            (b'a', ['a', 'b'], False),         # the server is behind: cache kept
            (b'axc', ['a', 'x', 'c'], True),   # another tag at our last position
        )
        for server, expected, restarted in cases:
            with self.subTest(server=server):
                sync = _sync_for(mock.Mock())
                sync.syncing = True
                starts = []

                async def request(method, params=()):
                    starts.append(int(params[0]))
                    return {'tagsandtxids': [row(tag) for tag in server[starts[-1]:]]}
                sync._large_request = request
                used = {'a': 'cached', 'b': 'cached'}
                with mock.patch('electrum_firo.spark_interface.libsparkmobile.hash_tags',
                                lambda tags: [chr(tag[0]) for tag in tags]):
                    self.assertEqual(restarted, _run(
                        sync.runFetchAndUpdateSparkUsedCoinTags(used)))
                self.assertEqual(expected, list(used))
                self.assertEqual([1, 0] if restarted else [1], starts)

    def test_spark_spend_fee_is_read_from_the_payload(self):
        fee = (1234).to_bytes(8, 'little')
        block_hashes = b'\x01' + bytes(8) + bytes(32)
        v1 = b'\x02' + bytes(16) + block_hashes + fee + b'proofs'
        v2 = b'\x02\x01\x02' + bytes(8) + block_hashes + fee + bytes(32)
        for tx_type, payload, expected in (
                (SPARK_SPEND, v1, 1234), (SPARK_SPEND_V2, v2, 1234),
                (SPARK_SPEND_V2, v2[:20], None), (SPARK_SPEND, b'', None),
                (0, v1, None)):
            tx = mock.Mock(tx_type=tx_type, extra_payload=FiroSparkSpend(payload))
            self.assertEqual(expected, spark_spend_fee(tx))

    def test_only_the_spark_session_accepts_large_messages(self):
        session = object.__new__(NotificationSession)
        session.interface = mock.Mock()
        session.interface.network.config.get.side_effect = lambda key, default: default
        framer = session.default_framer()
        self.assertEqual(2_000_000, framer.max_size)
        session.allow_spark_messages()
        self.assertEqual(64_000_000, framer.max_size)

    def test_tx_contains_spark_coin(self):
        tx = Transaction(_raw_tx(
            [('aa' * 32, 0)], [(MINT_SCRIPT, 1)]))
        self.assertTrue(_tx_contains_spark_coin(tx, COIN_BYTES))
        self.assertTrue(_tx_contains_spark_coin(tx, ROW_BYTES))
        forged = bytearray(ROW_BYTES)
        forged[10] ^= 1
        self.assertFalse(_tx_contains_spark_coin(tx, bytes(forged)))
        tail = Transaction(_raw_tx(
            [('aa' * 32, 0)], [(MINT_SCRIPT + bytes(forged), 1)]))
        self.assertFalse(_tx_contains_spark_coin(tail, bytes(forged)))
        self.assertFalse(_tx_contains_spark_coin(tx, b'\x01' * 100))
        self.assertFalse(_tx_contains_spark_coin(tx, b''))
        plain = Transaction(_raw_tx(
            [('aa' * 32, 0)], [(b'\x6a' + COIN_BYTES, 1)]))
        self.assertFalse(_tx_contains_spark_coin(plain, COIN_BYTES))

    def test_tx_not_found_error(self):
        self.assertTrue(_is_tx_not_found_error(Exception(
            'daemon error: No such mempool or blockchain transaction.')))
        self.assertFalse(_is_tx_not_found_error(Exception('timed out')))

    def test_server_bounds(self):
        self.assertEqual(3, _parse_group_id(3))
        self.assertEqual(3, _parse_group_id('3'))
        for bad in (0, -1, MAX_SPARK_GROUP_ID + 1, True, None, 'x', 10**12):
            with self.assertRaises(SparkServerMisbehaving):
                _parse_group_id(bad)
        self.assertEqual(('b', 's', 5), _parse_set_meta(
            {'blockHash': 'b', 'setHash': 's', 'size': 5}))
        for size in (-1, MAX_SPARK_SET_SIZE + 1, 'x', None):
            with self.assertRaises(SparkServerMisbehaving):
                _parse_set_meta({'blockHash': 'b', 'setHash': 's', 'size': size})

    def test_shared_cache_persists_same_shape_value_changes(self):
        import os, tempfile
        path = os.path.join(tempfile.mkdtemp(), 'cache.json')
        cache = SparkSharedCache(path)
        cache.update(used_tags={'t': 'a' * 64}, groups={'1': {'size': 1}})
        cache.update(used_tags={'t': 'b' * 64}, groups={'1': {'size': 1, 'block_hash': 'x'}})
        reloaded = SparkSharedCache(path)
        self.assertEqual('b' * 64, reloaded.snapshot_used_tags()['t'])
        self.assertEqual('x', reloaded.snapshot_groups()['1']['block_hash'])

    def test_shared_cache_rewrites_only_changed_sets(self):
        import os, tempfile
        from electrum_firo import spark_interface
        path = os.path.join(tempfile.mkdtemp(), 'cache.json')
        cache = SparkSharedCache(path)
        cache.update(anon_sets={'1': {'size': 2}, '2': {'size': 1}})
        written = []
        real_write = spark_interface._write_json

        def write(p, value):
            written.append(os.path.basename(p))
            real_write(p, value)
        with mock.patch.object(spark_interface, '_write_json', write):
            cache.update(anon_sets={'1': {'size': 2}, '2': {'size': 3}})
        self.assertEqual(['2.json', 'cache.json'], written)
        self.assertEqual({'1': {'size': 2}, '2': {'size': 3}},
                         SparkSharedCache(path).snapshot_anon_sets())
        cache.clear()
        self.assertEqual({}, SparkSharedCache(path).snapshot_anon_sets())

    def test_shared_cache_discard_lets_a_smaller_set_replace_a_larger_one(self):
        path = os.path.join(tempfile.mkdtemp(), 'cache.json')
        cache = SparkSharedCache(path)
        cache.update(anon_sets={'1': {'size': 2}, '2': {'size': 9}},
                     used_tags={'t': 'a' * 64},
                     groups={'1': {'size': 2}, '2': {'size': 9}})
        cache.update(anon_sets={'2': {'size': 3}})
        self.assertEqual(9, cache.snapshot_anon_sets()['2']['size'])
        cache.discard(sets=[2], used_tags=True)
        cache.update(anon_sets={'2': {'size': 3}})
        reloaded = SparkSharedCache(path)
        self.assertEqual({'1': {'size': 2}, '2': {'size': 3}},
                         reloaded.snapshot_anon_sets())
        self.assertEqual({}, reloaded.snapshot_used_tags())
        self.assertEqual({'1': {'size': 2}}, reloaded.snapshot_groups())

    def test_extended_timeouts_are_reference_counted(self):
        session = object.__new__(NotificationSession)
        session._extended_timeouts = []
        session._base_request_timeout = None
        session.sent_request_timeout = 30
        with session.extended_request_timeout(600):
            with session.extended_request_timeout(120):
                self.assertEqual(600, session.sent_request_timeout)
            self.assertEqual(600, session.sent_request_timeout)
            inner = session.extended_request_timeout(120)
            inner.__enter__()
        self.assertEqual(120, session.sent_request_timeout)
        inner.__exit__(None, None, None)
        self.assertEqual(30, session.sent_request_timeout)


class TestCoverSetAnchor(TestCaseForTestnet):

    BLOCK = bytes(range(32))
    BLOCK_HEX = BLOCK[::-1].hex()

    def _sync(self, header_hash, tx_height=50, local_height=100):
        wallet = mock.Mock()
        wallet.get_local_height.return_value = local_height
        sync = _sync_for(wallet)
        sync._header_hash_at = lambda h: header_hash if h == tx_height else None

        async def request(method, params=(), timeout=120):
            return {'height': tx_height}
        sync._request = request
        return sync

    def _row(self):
        return ['', base64.b64encode(b'\x11' * 32).decode(), '']

    def test_set_on_our_chain_is_accepted(self):
        sync = self._sync(self.BLOCK_HEX)
        height = _run(sync._verify_set_block(
            base64.b64encode(self.BLOCK).decode(), self._row()))
        self.assertEqual(50, height)

    def test_set_not_on_our_chain_disconnects(self):
        sync = self._sync('ff' * 32)
        with self.assertRaises(SparkServerMisbehaving):
            _run(sync._verify_set_block(
                base64.b64encode(self.BLOCK).decode(), self._row()))

    def test_spend_needs_fresh_anchored_set(self):
        from electrum_firo.address_synchronizer import AddressSynchronizer
        wallet = mock.Mock()
        wallet.network.blockchain.return_value.read_header.return_value = {'block_height': 50}
        info = {'blockHash': base64.b64encode(self.BLOCK).decode(),
                'blockHeight': 50}
        check = AddressSynchronizer._check_cover_set_anchor
        with mock.patch('electrum_firo.address_synchronizer.hash_header',
                        return_value=self.BLOCK_HEX):
            check(wallet, 1, info, 40)
            with self.assertRaises(RuntimeError):
                check(wallet, 1, info, 60)
            with self.assertRaises(RuntimeError):
                check(wallet, 1, dict(info, blockHeight=None), 0)
        with mock.patch('electrum_firo.address_synchronizer.hash_header',
                        return_value='ee' * 32):
            with self.assertRaises(RuntimeError):
                check(wallet, 1, info, 0)


class TestSparkRefresh(TestCaseForTestnet):
    """Run whole refreshes against a server that serves one group."""

    BLOCK = base64.b64encode(b'\x02' * 32).decode()
    SET_HASH = base64.b64encode(b'\x03' * 32).decode()

    def cached(self, size, coins):
        return {'cache_version': SPARK_SET_CACHE_VERSION, 'versions': [{
            'block_hash': self.BLOCK, 'block_height': 100, 'set_hash': self.SET_HASH,
            'size': size, 'coins': coins}]}

    def server(self, rows, tags=b''):
        """Returns a wallet, an empty shared cache, and a synchronizer whose
        server reports `rows` for group 1 and the used tags `tags`."""
        config = SimpleConfig({'electrum_path': self.electrum_path})
        with mock.patch.object(Abstract_Wallet, 'save_db'):
            wallet = _wallet(_bip39_keystore(), config, 'bip39')
        wallet.spark_enabled = True
        wallet.spark_view_key_hex = 'fixture'
        wallet.db.put('stored_height', 200)
        shared = SparkSharedCache(os.path.join(tempfile.mkdtemp(), 'cache.json'))

        async def request(method, params=(), timeout=120):
            if method == 'spark.getsparklatestcoinid':
                return 1
            if method == 'spark.getsparkanonymitysetmeta':
                return {'blockHash': self.BLOCK, 'setHash': self.SET_HASH, 'size': len(rows)}
            if method == 'spark.getusedcoinstagstxhashes':
                return {'tagsandtxids': [
                    [base64.b64encode(bytes([tag]) * 34).decode(), rows[0][1]]
                    for tag in tags[int(params[0]):]]}
            if method == 'spark.getmempoolsparktxids':
                return []
            raise AssertionError(method)

        async def fetch_sectors(group_id, block_hash, count, start_index=0):
            return rows[start_index:start_index + count]

        sync = _sync_for(wallet)
        sync._sync_lock = asyncio.Lock()
        sync.syncing = False
        sync._mempool_txids_checked = set()
        sync._shared_cache = lambda: shared
        sync._header_hash_at = lambda height: 'ab' * 32
        sync._request = request
        sync._fetch_sectors = fetch_sectors
        sync._verify_set_block = mock.AsyncMock(return_value=100)
        for name in ('_save_spark_db', '_update_pending_spark_spends',
                     '_import_missing_spark_spend_transactions',
                     '_notify_wallet_updated'):
            setattr(sync, name, mock.AsyncMock())
        return wallet, shared, sync

    def refresh(self, sync, found=None):
        """One refresh. `found` are the coins of ours among the scanned rows."""
        lib = 'electrum_firo.spark_interface.libsparkmobile'
        with mock.patch(f'{lib}.is_available', return_value=True), \
                mock.patch(f'{lib}.hash_tags', lambda tags: [chr(tag[0]) for tag in tags]), \
                mock.patch.object(SparkSynchronizer, '_identify_sector',
                                  side_effect=lambda rows, *_: dict(found or {}) if rows else {}), \
                mock.patch('electrum_firo.spark_interface.util.trigger_callback'):
            _run(sync.refreshSparkData())  # the server is not disconnected

    def test_refresh_replaces_cached_answers_the_server_contradicts(self):
        block = self.BLOCK
        txid = base64.b64encode(b'\x04' * 32).decode()
        rows = [[f'coin-{i}', txid, 'context'] for i in range(2)]
        wallet, shared, sync = self.server(rows, tags=b'ab')
        # An earlier server inflated group 1, invented group 7, and added a
        # used tag that is not on the chain.
        junk = [['junk', txid, 'junk']]
        shared.update(anon_sets={'1': self.cached(9, junk), '7': self.cached(4, junk)},
                      used_tags={'a': 'aa' * 32, 'junk': 'bb' * 32},
                      groups={'1': {'size': 9}, '7': {'size': 4}})
        self.refresh(sync)

        sets = shared.snapshot_anon_sets()
        self.assertEqual(['1'], list(sets))
        self.assertEqual(2, sets['1']['size'])
        self.assertEqual([list(reversed(rows))], [v['coins'] for v in sets['1']['versions']])
        self.assertEqual(['1'], list(shared.snapshot_groups()))
        self.assertEqual(['a', 'b'], list(shared.snapshot_used_tags()))
        state = wallet.db.get('spark_scan_state')
        self.assertEqual({'1:' + sets['1']['epoch']: block},
                         state['firo_spark_cache_set_block_hash_cache'])
        self.assertTrue(state['scan_complete'])

    def test_group_of_a_rejected_coin_is_fetched_again_once_per_server(self):
        real = Transaction(_raw_tx([('33' * 32, 0)], [(MINT_SCRIPT, 7000)]))
        coin = _spark_coin(real.txid(), 7000, serialized=b'\x42' * 100, l_tag_hash='ours')
        # The cached row pairs our coin with a transaction that lacks it.
        row = _set_row(coin)
        wallet, shared, sync = self.server([row])
        shared.update(anon_sets={'1': self.cached(1, [row])}, groups={'1': {'size': 1}})
        sync._batch_fetch_transactions = mock.AsyncMock(return_value={
            real.txid(): {'hex': real.serialize(), 'height': 123}})

        def refresh():
            self.refresh(sync, found={'ours': dict(coin)})
            self.assertEqual({}, wallet.db.get('spark_coins'))
            return list(shared.snapshot_anon_sets()), wallet.db.get('spark_scan_state')['scan_complete']
        # The group leaves the cache to be fetched again, and the scan is not
        # complete until it has been.
        self.assertEqual(([], False), refresh())
        # The server sends the same row again: asking once more would not help.
        self.assertEqual((['1'], True), refresh())


class TestSparkRescanState(TestCaseForTestnet):

    def test_rescan_forgets_the_synced_tip(self):
        config = SimpleConfig({'electrum_path': self.electrum_path})
        with mock.patch.object(Abstract_Wallet, 'save_db'):
            wallet = WalletIntegrityHelper.create_standard_wallet(
                keystore.from_seed(SEED, '', False), gap_limit=2, config=config)
            wallet.db.put('spark_scan_state', {
                'chain_height': 500, 'chain_tip_hash': 'ab' * 32,
                'firo_spark_cache_set_block_hash_cache': {'1': 'x'}})
            wallet.clear_spark_data(is_rescan=True)
        state = wallet.db.get('spark_scan_state')
        self.assertNotIn('chain_height', state)
        self.assertNotIn('chain_tip_hash', state)
