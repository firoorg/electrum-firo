import asyncio
import base64
import struct
from unittest import mock

from electrum_firo import keystore
from electrum_firo.bitcoin import address_to_script
from electrum_firo.logging import get_logger
from electrum_firo.simple_config import SimpleConfig
from electrum_firo.bitcoin import hash160_to_exp2pkh, hash160_to_p2pkh
from electrum_firo.interface import NotificationSession
from electrum_firo.spark_interface import (
    MAX_SPARK_GROUP_ID, MAX_SPARK_SET_SIZE, OP_SPARKMINT,
    SPEND_MISSING_CHECKS_BEFORE_RELEASE, SparkServerMisbehaving,
    SparkSharedCache, SparkSynchronizer, _is_tx_not_found_error,
    _parse_group_id, _parse_set_meta, _tx_contains_spark_coin,
    is_exchange_address)
from electrum_firo.transaction import Transaction
from electrum_firo.util import TxMinedInfo, bfh
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
    return sync


class TestSparkWallet(TestCaseForTestnet):

    def setUp(self):
        super().setUp()
        self.config = SimpleConfig({'electrum_path': self.electrum_path})
        patcher = mock.patch.object(Abstract_Wallet, 'save_db')
        patcher.start()
        self.addCleanup(patcher.stop)
        ks = keystore.from_seed(SEED, '', False)
        self.wallet = WalletIntegrityHelper.create_standard_wallet(
            ks, gap_limit=2, config=self.config)
        self.wallet.spark_enabled = True
        self.wallet.db.put('stored_height', 200)

    def _fund(self, value: int) -> Transaction:
        addr = self.wallet.get_receiving_addresses()[0]
        funding = Transaction(_raw_tx(
            [('11' * 32, 0)], [(bfh(address_to_script(addr)), value)]))
        self.wallet.add_transaction(funding)
        self.wallet.add_unverified_tx(funding.txid(), 100)
        return funding

    def test_can_use_spark_only_for_single_bip32_software_keystore(self):
        self.assertTrue(self.wallet.can_use_spark())
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
        self.assertFalse(history[mint.txid()]['incoming'])
        last = list(history.values())[-1]
        transparent = sum(self.wallet.get_balance())
        spark = self.wallet.get_spark_balance().total
        self.assertEqual(transparent + spark, last['balance'].value)

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
        with mock.patch('electrum_firo.spark_interface._createSparkSend') as proof, \
                mock.patch('electrum_firo.libsparkmobile.is_valid_spark_address',
                           return_value=False):
            with self.assertRaises(ValueError) as ctx:
                self.wallet._prepareSendSpark(
                    bytes(32), address=exchange, amount=1000)
            proof.assert_not_called()
        self.assertIn('exchange address', str(ctx.exception))


class TestSparkHelpers(TestCaseForTestnet):

    def test_tx_contains_spark_coin(self):
        tx = Transaction(_raw_tx(
            [('aa' * 32, 0)], [(MINT_SCRIPT, 1)]))
        self.assertTrue(_tx_contains_spark_coin(tx, COIN_BYTES))
        self.assertTrue(_tx_contains_spark_coin(tx, ROW_BYTES))
        forged = bytearray(ROW_BYTES)
        forged[10] ^= 1
        self.assertFalse(_tx_contains_spark_coin(tx, bytes(forged)))
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
