import asyncio
import base64
import json
import os
import random
import threading
from typing import TYPE_CHECKING, Dict, List, Optional, Sequence, Tuple

from aiorpcx import RPCError, run_in_thread

from . import constants, libsparkmobile, util
from .bitcoin import COIN, b58_address_to_hash160, is_address
from .blockchain import hash_header
from .crypto import sha256d
from .dash_tx import SPARK_SPEND_V2, FiroSparkSpend
from .interface import GracefulDisconnect
from .i18n import _
from .logging import get_logger
from .simple_config import SimpleConfig
from .transaction import PartialTransaction, PartialTxInput, PartialTxOutput, Transaction, TxOutpoint
from .util import NetworkJobOnDefaultServer, NotEnoughFunds, bfh

if TYPE_CHECKING:
    from .address_synchronizer import AddressSynchronizer
    from .network import Network

_logger = get_logger(__name__)

kDefaultSparkIndex = 1
MAX_NEW_TX_WEIGHT = 1_000_000
SPARK_OUT_LIMIT_PER_TX = 16
OP_SPARKMINT = 0xd1
OP_SPARKSMINT = 0xd2
OP_SPARKSPEND = 0xd3
MINT_INPUT_SEQUENCE = 0xffffffff - 1
SPEND_MISSING_CHECKS_BEFORE_RELEASE = 3
MAX_SPARK_SET_SIZE = 32_768
MAX_SPARK_GROUP_ID = 100_000
MAX_SPARK_GROUPS_PER_REFRESH = 4
SPARK_COIN_CHECK_VERSION = 2


class SparkServerMisbehaving(GracefulDisconnect):
    pass


def _is_set_row(row) -> bool:
    return (isinstance(row, (list, tuple)) and len(row) == 3
            and all(isinstance(x, str) for x in row))


def _parse_group_id(value) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise SparkServerMisbehaving(f'bad spark group id {value!r}')
    try:
        group_id = int(value)
    except ValueError:
        raise SparkServerMisbehaving(f'bad spark group id {value!r}')
    if not 1 <= group_id <= MAX_SPARK_GROUP_ID:
        raise SparkServerMisbehaving(f'spark group id out of range: {group_id}')
    return group_id


def _parse_set_meta(meta) -> Tuple[str, str, int]:
    if not isinstance(meta, dict):
        raise SparkServerMisbehaving(f'bad spark set meta {meta!r}')
    block_hash, set_hash, size = (
        meta.get('blockHash'), meta.get('setHash'), meta.get('size'))
    if not isinstance(block_hash, str) or not isinstance(set_hash, str):
        raise SparkServerMisbehaving(f'bad spark set meta {meta!r}')
    try:
        size = int(size)
    except (TypeError, ValueError):
        raise SparkServerMisbehaving(f'bad spark set size {size!r}')
    if not 0 <= size <= MAX_SPARK_SET_SIZE:
        raise SparkServerMisbehaving(f'spark set size out of range: {size}')
    return block_hash, set_hash, size


def _deepcopy_db_val(val):
    if val is None:
        return {}
    return json.loads(json.dumps(val))


def _ffi_spend_coins(coins) -> List[dict]:
    return [{
        'serializedCoin': c['serialized_coin'],
        'serializedCoinContext': c.get('context') or c.get('serial_context'),
        'groupId': int(c['group_id']),
        'height': int(c['height']),
    } for c in coins]


def _spark_coin_type_label(value) -> Optional[str]:
    if value in (0, 'mint'):
        return 'mint'
    if value in (1, 'spend'):
        return 'spend'
    return None


class MutableSparkRecipient:
    def __init__(self, address: str, value: int, memo: str = ''):
        self.address = address
        self.value = int(value)
        self.memo = memo or ''

    def __repr__(self):
        return (f'MutableSparkRecipient{{ address: {self.address}, '
                f'value: {self.value}, memo: {self.memo} }}')


SPARK_GROUP_ELEMENT_SIZE = 34


def _read_compact_size(data: bytes, pos: int) -> Tuple[int, int]:
    first = data[pos]
    pos += 1
    if first < 253:
        return first, pos
    width = {253: 2, 254: 4, 255: 8}[first]
    if len(data) - pos < width:
        raise ValueError('truncated compact size')
    return int.from_bytes(data[pos:pos + width], 'little'), pos + width


def _canonical_spark_coin(serialized: bytes) -> bytes:
    pos = 1 + 3 * SPARK_GROUP_ELEMENT_SIZE
    if not serialized or len(serialized) < pos:
        raise ValueError('truncated spark coin')
    for _field in range(3):
        size, pos = _read_compact_size(serialized, pos)
        pos += size
    if serialized[0] == 0:
        pos += 8
    if pos > len(serialized):
        raise ValueError('truncated spark coin')
    return serialized[:pos]


def _tx_contains_spark_coin(tx: Transaction, serialized_coin: bytes) -> bool:
    try:
        serialized_coin = _canonical_spark_coin(serialized_coin)
    except (ValueError, IndexError, KeyError):
        return False
    for o in tx.outputs():
        script = o.scriptpubkey
        if (script and script[0] in (OP_SPARKMINT, OP_SPARKSMINT)
                and serialized_coin in script[1:]):
            return True
    return False


def is_exchange_address(address: str) -> bool:
    try:
        addrtype, _h160 = b58_address_to_hash160(address)
    except Exception:
        return False
    return addrtype == constants.net.ADDRTYPE_EXP2PKH


def _is_tx_not_found_error(e: Exception) -> bool:
    msg = str(getattr(e, 'message', '') or e).lower()
    return ('no such mempool or blockchain transaction' in msg
            or 'not found' in msg)


def _verify_mint_serial_context(tx: PartialTransaction,
                                serial_context: bytes) -> None:
    """Refuse to hand out a mint whose inputs no longer match its context.

    Firo Core derives the authoritative serial context from the confirmed
    transaction's final vin order and each input's actual nSequence. If the
    inputs are ever reordered after the context was serialised (BIP69 sorting
    in PartialTransaction.from_io, coin control, a later edit) or a sequence
    differs, the mint can still confirm, but no one can reconstruct the
    context from chain data and the coins become unrecoverable. Fail here
    instead.
    """
    inputs = list(tx.inputs())
    for txin in inputs:
        if txin.nsequence != MINT_INPUT_SEQUENCE:
            raise RuntimeError(
                'Spark mint input sequence was changed after the serial '
                'context was built; refusing to create unrecoverable coins')
    rebuilt = libsparkmobile.serialize_mint_context(
        [(i.prevout.txid[::-1], i.prevout.out_idx) for i in inputs])
    if rebuilt != serial_context:
        raise RuntimeError(
            'Spark mint inputs no longer match the serial context the coins '
            'were bound to; refusing to create unrecoverable coins')


def _sum(utxos: Sequence[PartialTxInput]) -> int:
    return sum(int(c.value_sats() or 0) for c in utxos)


def _createSparkSend(*, privateKeyHex: bytes, index: int,
                     recipients, privateRecipients, serializedCoins,
                     allAnonymitySets, idAndBlockHashes, txHash: bytes,
                     additionalTxSize: int = 0,
                     isTestNet: bool = False) -> Dict:
    ffi_recipients = [(int(r['amount']), bool(r['subtractFeeFromAmount']))
                      for r in recipients]
    ffi_private = [
        (r['sparkAddress'], int(r['amount']), r.get('memo') or '',
         bool(r['subtractFeeFromAmount']))
        for r in privateRecipients
    ]
    spend_coins = [{
        'serialized_coin': c['serializedCoin'],
        'context': c['serializedCoinContext'],
        'group_id': int(c['groupId']),
        'height': int(c['height']),
    } for c in serializedCoins]
    anonymity_sets = [{
        'set_id': int(a['setId']),
        'set_hash': a['setHash'],
        'coins': [row['serializedCoin'] for row in a['set']],
    } for a in allAnonymitySets]
    id_and_block_hashes = [(int(x['setId']), x['blockHash'])
                           for x in idAndBlockHashes]
    return libsparkmobile.create_spark_spend_transaction(
        privateKeyHex,
        recipients=ffi_recipients,
        private_recipients=ffi_private,
        coins=spend_coins,
        anonymity_sets=anonymity_sets,
        id_and_block_hashes=id_and_block_hashes,
        tx_hash=txHash,
        additional_tx_size=additionalTxSize,
        index=index,
        is_testnet=isTestNet)


def _asyncSparkFeesWrapper(*, privateKeyHex: bytes, index: int, sendAmount: int,
                           subtractFeeFromAmount: bool, serializedCoins,
                           privateRecipientsCount: int, utxoNum: int,
                           additionalTxSize: int = 0) -> int:
    coins = [{
        'serialized_coin': c['serializedCoin'],
        'context': c['serializedCoinContext'],
        'group_id': int(c['groupId']),
        'height': int(c['height']),
    } for c in serializedCoins]
    return libsparkmobile.estimate_spark_fee(
        privateKeyHex,
        send_amount=sendAmount,
        subtract_fee_from_amount=subtractFeeFromAmount,
        coins=coins,
        private_recipients_count=privateRecipientsCount,
        utxo_num=utxoNum,
        additional_tx_size=additionalTxSize,
        index=index)


def _anon_set_entry_size(entry) -> int:
    try:
        versions = entry.get('versions') or []
        if versions:
            return max(int(v.get('size', 0)) for v in versions)
        return int(entry.get('size', 0))
    except (AttributeError, TypeError, ValueError):
        return 0


def _merge_anon_sets(existing: dict, incoming: dict) -> bool:
    changed = False
    for gid, entry in (incoming or {}).items():
        current = existing.get(gid)
        if (current is None
                or _anon_set_entry_size(entry) >= _anon_set_entry_size(current)):
            if current != entry:
                existing[gid] = entry
                changed = True
    return changed


def _merge_dict(existing: dict, incoming: dict) -> bool:
    changed = False
    for key, value in (incoming or {}).items():
        if key not in existing or existing[key] != value:
            existing[key] = value
            changed = True
    return changed


_SPARK_SHARED_CACHES: Dict[str, 'SparkSharedCache'] = {}
_SPARK_SHARED_CACHES_LOCK = threading.Lock()


def get_spark_shared_cache(config) -> 'SparkSharedCache':
    net_name = getattr(constants.net, 'NET_NAME', 'mainnet')
    with _SPARK_SHARED_CACHES_LOCK:
        cache = _SPARK_SHARED_CACHES.get(net_name)
        if cache is None:
            path = os.path.join(config.path, f'spark_cache_{net_name}.json')
            cache = SparkSharedCache(path)
            _SPARK_SHARED_CACHES[net_name] = cache
        return cache


class SparkSharedCache:
    _EMPTY = {
        'spark_anon_sets': {},
        'spark_used_tags': {},
        'spark_groups': {},
    }

    def __init__(self, path: str):
        self.path = path
        self._lock = threading.RLock()
        self._data = json.loads(json.dumps(self._EMPTY))
        self._load_locked()

    def get(self, key, default=None):
        with self._lock:
            if key not in self._data:
                return default
            return json.loads(json.dumps(self._data[key]))

    def snapshot_anon_sets(self) -> dict:
        return self.get('spark_anon_sets', {}) or {}

    def snapshot_used_tags(self) -> dict:
        return self.get('spark_used_tags', {}) or {}

    def snapshot_groups(self) -> dict:
        return self.get('spark_groups', {}) or {}

    def _load_locked(self) -> None:
        try:
            with open(self.path, 'r', encoding='utf-8') as f:
                loaded = json.load(f)
        except (FileNotFoundError, OSError, ValueError):
            return
        if isinstance(loaded, dict):
            for key in self._EMPTY:
                if key in loaded:
                    self._data[key] = loaded[key]

    def _flush_locked(self) -> None:
        tmp = f'{self.path}.tmp'
        try:
            with open(tmp, 'w', encoding='utf-8') as f:
                json.dump(self._data, f)
            os.replace(tmp, self.path)
        except OSError as e:
            _logger.error(
                f'could not write spark shared cache {self.path}: {e!r}')

    def update(self, *, anon_sets=None, used_tags=None, groups=None) -> None:
        with self._lock:
            self._load_locked()
            changed = False
            for key, incoming, merge in (
                    ('spark_anon_sets', anon_sets, _merge_anon_sets),
                    ('spark_used_tags', used_tags, _merge_dict),
                    ('spark_groups', groups, _merge_dict)):
                if incoming is None:
                    continue
                incoming = json.loads(json.dumps(incoming))
                stored = self._data.get(key)
                if not isinstance(stored, dict):
                    stored = self._data[key] = {}
                    changed = True
                changed |= merge(stored, incoming)
            if changed:
                self._flush_locked()

    def clear(self) -> None:
        with self._lock:
            self._data = json.loads(json.dumps(self._EMPTY))
            self._flush_locked()


class FiroCacheCoordinator:
    @staticmethod
    def _anon_sets(db, anon_sets=None) -> dict:
        if anon_sets is not None:
            return anon_sets
        return db.get('spark_anon_sets', {}) or {}

    @staticmethod
    def _normalize_entry(entry: dict) -> dict:
        if entry.get('versions'):
            return entry
        coins = list(entry.get('coins') or [])
        return {
            **entry,
            'versions': [{
                'block_hash': entry.get('block_hash', ''),
                'set_hash': entry.get('set_hash', ''),
                'size': int(entry.get('size', len(coins))),
                'coins': coins,
            }],
        }

    @staticmethod
    def _entry(db, groupId: int, anon_sets=None) -> Optional[dict]:
        entry = FiroCacheCoordinator._anon_sets(db, anon_sets).get(str(groupId))
        if not entry:
            return None
        return FiroCacheCoordinator._normalize_entry(entry)

    @staticmethod
    def getSetCoinsForGroupId(db, groupId: int,
                              afterBlockHash: str = None,
                              anon_sets=None) -> List[List[str]]:
        entry = FiroCacheCoordinator._entry(db, groupId, anon_sets=anon_sets)
        if not entry:
            return []
        versions = entry['versions']
        if afterBlockHash is None:
            coins = []
            for version in versions:
                coins.extend(version.get('coins') or [])
        else:
            start_idx = 0
            for i, version in enumerate(versions):
                if version.get('block_hash') == afterBlockHash:
                    start_idx = i + 1
                    break
            coins = []
            for version in versions[start_idx:]:
                coins.extend(version.get('coins') or [])
        return list(reversed(coins))

    @staticmethod
    def getLatestSetInfoForGroupId(db, groupId: int,
                                   anon_sets=None) -> Optional[Dict]:
        entry = FiroCacheCoordinator._entry(db, groupId, anon_sets=anon_sets)
        if not entry:
            return None
        latest = max(
            entry['versions'],
            key=lambda v: int(v.get('size', 0)),
        )
        return {
            'coinGroupId': groupId,
            'blockHash': latest['block_hash'],
            'setHash': latest['set_hash'],
            'size': int(latest.get('size', 0)),
            'blockHeight': latest.get('block_height'),
        }

    @staticmethod
    def checkSetInfoForGroupIdExists(db, groupId: int, anon_sets=None) -> bool:
        return str(groupId) in FiroCacheCoordinator._anon_sets(db, anon_sets)

    @staticmethod
    def getUsedCoinTags(used_tags: dict, startNumber: int = 0) -> List[str]:
        return list(used_tags.keys())[startNumber:]

    @staticmethod
    def getUsedCoinTxidsFor(used_tags: dict,
                            tags: Sequence[str]) -> List[Tuple[str, str]]:
        return [(tag, used_tags[tag]) for tag in tags if tag in used_tags]


class SparkSynchronizer(NetworkJobOnDefaultServer):
    sectorSize = 1500

    def __init__(self, network: 'Network', wallet: 'AddressSynchronizer'):
        self.wallet = wallet
        self._sync_lock = asyncio.Lock()
        NetworkJobOnDefaultServer.__init__(self, network)

    def _reset(self):
        super()._reset()
        self._wake = asyncio.Event()
        self.syncing = False
        self._recover_pending = False
        self._mempool_txids_checked = set()
        self._missing_spend_checks = {}

    async def _run_tasks(self, *, taskgroup):
        await super()._run_tasks(taskgroup=taskgroup)
        async with taskgroup as group:
            await group.spawn(self.main)

    def trigger(self):
        self.network.asyncio_loop.call_soon_threadsafe(self._wake.set)

    def trigger_recover(self):
        self._recover_pending = True
        self.trigger()

    async def _save_spark_db(self):
        await run_in_thread(self.wallet.save_db)

    def _shared_cache(self) -> 'SparkSharedCache':
        return get_spark_shared_cache(self.wallet.config)

    def _migrate_legacy_spark_cache(self, shared: 'SparkSharedCache') -> None:
        legacy_sets = self.wallet.db.get('spark_anon_sets')
        legacy_tags = self.wallet.db.get('spark_used_tags')
        if not legacy_sets and not legacy_tags:
            return
        legacy_state = self.wallet.db.get('spark_scan_state') or {}
        legacy_groups = legacy_state.get('groups')
        shared.update(
            anon_sets=legacy_sets if isinstance(legacy_sets, dict) else None,
            used_tags=legacy_tags if isinstance(legacy_tags, dict) else None,
            groups=legacy_groups if isinstance(legacy_groups, dict) else None)
        self.wallet.db.put('spark_anon_sets', {})
        self.wallet.db.put('spark_used_tags', {})

    def _merge_used_flags(self, coins: dict) -> None:
        current = self.wallet.db.get('spark_coins') or {}
        state = self.wallet.db.get('spark_scan_state') or {}
        pending = set(state.get('pending_spark_spends') or [])
        for tag, coin in coins.items():
            latest = current.get(tag)
            if (latest and latest.get('is_used') and not coin.get('is_used')
                    and latest.get('spent_txid') in pending):
                coin['is_used'] = True
                if latest.get('spent_txid'):
                    coin['spent_txid'] = latest['spent_txid']

    async def _notify_wallet_updated(self):
        util.trigger_callback('wallet_updated', self.wallet)

    async def main(self):
        while True:
            if (self.wallet.spark_enabled
                    and self.wallet.is_up_to_date()
                    and self.wallet.spark_view_key_hex):
                try:
                    await self._update_pending_spark_spends()
                    if self._recover_pending:
                        self._recover_pending = False
                        await self.recover_spark(is_rescan=True)
                    else:
                        await self.refreshSparkData()
                except SparkServerMisbehaving as e:
                    self.logger.warning(
                        f'Spark: disconnecting misbehaving server: {e!r}')
                    raise
                except Exception as e:
                    self.logger.info(f'Spark sync failed: {e!r}')
                finally:
                    if self.syncing:
                        self.syncing = False
                        await self._notify_wallet_updated()
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=60)
            except asyncio.TimeoutError:
                pass
            self._wake.clear()

    async def _request(self, method, params=(), timeout=120):
        session = self.interface.session
        with session.extended_request_timeout(timeout):
            return await session.send_request(
                method, list(params), timeout=timeout)

    async def _large_request(self, method, params=()):
        return await self._request(method, params, timeout=600)

    @staticmethod
    def _decode_b64(value):
        return base64.b64decode(''.join(value.splitlines()))

    @classmethod
    def _identify_sector(cls, rows, groupId, view_key_hex):
        coins = {}
        if not rows:
            return coins
        view_key = libsparkmobile.deserialize_full_view_key(view_key_hex)
        try:
            for row in rows:
                if not isinstance(row, (list, tuple)) or len(row) != 3:
                    continue
                serialized_b64, txid_b64, context_b64 = row
                try:
                    txid = cls._decode_b64(txid_b64)[::-1].hex()
                except Exception as e:
                    _logger.error(
                        f'Skipping spark set row with malformed txid: {e!r}')
                    continue
                try:
                    recovered = libsparkmobile.identify_and_recover_coin(
                        cls._decode_b64(serialized_b64),
                        cls._decode_b64(context_b64),
                        view_key,
                        is_testnet=bool(constants.net.TESTNET))
                except Exception as e:
                    _logger.error(
                        f'Error identifying spark coin in tx {txid} '
                        f'(this is not expected): {e!r}')
                    continue
                if recovered is None:
                    continue
                type_label = _spark_coin_type_label(recovered.get('type'))
                if type_label is None:
                    _logger.error(
                        f'Unknown spark coin type detected: '
                        f'{recovered.get("type")!r}')
                    continue
                recovered.update({
                    'type': type_label,
                    'group_id': groupId,
                    'txid': txid,
                    'height': None,
                    'is_used': False,
                    'serialized_coin': serialized_b64,
                    'context': context_b64,
                    'serial_context': context_b64,
                    'is_locked': None,
                })
                coins[recovered['l_tag_hash']] = recovered
        finally:
            libsparkmobile.delete_full_view_key(view_key)
        return coins

    @classmethod
    def _recover_saved_coins(cls, coins, view_key_hex):
        for coin in coins.values():
            coin.setdefault('serial_context', coin.get('context'))
            coin.setdefault('is_locked', None)
            label = _spark_coin_type_label(coin.get('type'))
            if label is not None:
                coin['type'] = label
        pending = [c for c in coins.values()
                   if ('type' not in c and c.get('serialized_coin')
                       and c.get('context'))]
        if not pending:
            return
        view_key = libsparkmobile.deserialize_full_view_key(view_key_hex)
        try:
            for coin in pending:
                recovered = libsparkmobile.identify_and_recover_coin(
                    cls._decode_b64(coin['serialized_coin']),
                    cls._decode_b64(coin['context']), view_key,
                    is_testnet=bool(constants.net.TESTNET))
                if recovered:
                    label = _spark_coin_type_label(recovered.get('type'))
                    if label is not None:
                        recovered['type'] = label
                    coin.update(recovered)
        finally:
            libsparkmobile.delete_full_view_key(view_key)

    async def _fetch_sectors(self, groupId, blockHash, numberOfCoinsToFetch):
        rows = []
        for start in range(0, numberOfCoinsToFetch, self.sectorSize):
            end = min(start + self.sectorSize, numberOfCoinsToFetch)
            response = await self._request(
                'spark.getsparkanonymitysetsector',
                (str(groupId), blockHash, str(start), str(end)))
            coins = response.get('coins') if isinstance(response, dict) else None
            if (not isinstance(coins, list) or len(coins) != end - start
                    or not all(_is_set_row(r) for r in coins)):
                raise SparkServerMisbehaving(
                    f'bad anonymity set sector {start}-{end} '
                    f'for group {groupId}')
            rows.extend(coins)
            self.logger.info(
                f'Spark group {groupId}: fetched {len(rows)}/'
                f'{numberOfCoinsToFetch} coins')
        return rows

    async def _fetch_mempool_spark_rows(self):
        try:
            mempool_txids_b64 = await self._large_request(
                'spark.getmempoolsparktxids')
        except Exception as e:
            self.logger.info(f'Spark mempool txid fetch failed: {e!r}')
            return []
        hex_to_b64 = {}
        for b64 in (mempool_txids_b64 or []):
            hex_to_b64[self._decode_b64(b64)[::-1].hex()] = b64
        mempool_txids_hex = set(hex_to_b64.keys())
        self._mempool_txids_checked &= mempool_txids_hex
        to_check = list(mempool_txids_hex - self._mempool_txids_checked)
        self.logger.info(
            f'Spark mempool txids={mempool_txids_hex!r} '
            f'already_checked={self._mempool_txids_checked!r} '
            f'to_check={to_check!r}')
        if not to_check:
            return []
        try:
            response = await self._large_request(
                'spark.getmempoolsparktxs', ({'txids': to_check},))
        except Exception as e:
            self.logger.info(f'Spark mempool tx data fetch failed: {e!r}')
            return []
        self.logger.info(f'Spark mempool tx data raw response: {response!r}')
        rows = []
        for txid_key, data in (response or {}).items():
            coins = data.get('coins') or []
            serial_contexts = data.get('serial_context') or []
            if not coins or not serial_contexts:
                continue
            context_b64 = serial_contexts[0]
            if txid_key in hex_to_b64:
                txid_hex = txid_key
                txid_b64 = hex_to_b64[txid_key]
            elif txid_key in hex_to_b64.values():
                txid_b64 = txid_key
                txid_hex = self._decode_b64(txid_key)[::-1].hex()
            else:
                txid_hex = txid_key
                txid_b64 = base64.b64encode(
                    bytes.fromhex(txid_key)[::-1]).decode('ascii')
            for coin_b64 in coins:
                rows.append([coin_b64, txid_b64, context_b64])
            self._mempool_txids_checked.add(txid_hex)
        self.logger.info(f'Spark mempool rows built: {len(rows)}')
        return rows

    async def runFetchAndUpdateSparkAnonSetCacheForGroupId(
            self, groupId, anon_sets, coins, groups, rawCoinsBySetId=None):
        meta = await self._request(
            'spark.getsparkanonymitysetmeta', (str(groupId),))
        blockHash, setHash, size = _parse_set_meta(meta)
        prevMeta = FiroCacheCoordinator.getLatestSetInfoForGroupId(
            self.wallet.db, groupId, anon_sets=anon_sets)
        prevSize = int(prevMeta['size']) if prevMeta else 0
        if size < prevSize:
            raise SparkServerMisbehaving(
                f'spark group {groupId} shrank from {prevSize} to {size}')

        if prevMeta and prevMeta['blockHash'] == blockHash:
            groups[str(groupId)] = {
                'block_hash': blockHash,
                'set_hash': setHash,
                'size': size,
            }
            return coins

        numberOfCoinsToFetch = size - prevSize
        if numberOfCoinsToFetch > 0 and not self.syncing:
            self.syncing = True
            util.trigger_callback('wallet_updated', self.wallet)

        rows = []
        if numberOfCoinsToFetch > 0:
            rows = await self._fetch_sectors(
                groupId, blockHash, numberOfCoinsToFetch)

        if rows:
            blockHeight = await self._verify_set_block(blockHash, rows[0])
            if rawCoinsBySetId is not None:
                rawCoinsBySetId[groupId] = rows
            version_coins = list(reversed(rows))
            entry = FiroCacheCoordinator._entry(
                self.wallet.db, groupId, anon_sets=anon_sets)
            versions = list((entry or {}).get('versions') or [])
            versions.append({
                'block_hash': blockHash,
                'block_height': blockHeight,
                'set_hash': setHash,
                'size': size,
                'coins': version_coins,
            })
            anon_sets[str(groupId)] = {
                'block_hash': blockHash,
                'set_hash': setHash,
                'size': size,
                'versions': versions,
            }

        groups[str(groupId)] = {
            'block_hash': blockHash,
            'set_hash': setHash,
            'size': size,
        }
        return coins

    async def runFetchAndUpdateSparkUsedCoinTags(self, used_tags):
        tag_count = len(used_tags)
        if not tag_count and not self.syncing:
            self.syncing = True
            util.trigger_callback('wallet_updated', self.wallet)
        response = await self._large_request(
            'spark.getusedcoinstagstxhashes', (str(tag_count),))
        tag_rows = (response.get('tagsandtxids')
                    if isinstance(response, dict) else None)
        if (not isinstance(tag_rows, list)
                or not all(isinstance(r, (list, tuple)) and len(r) == 2
                           and all(isinstance(x, str) for x in r)
                           for r in tag_rows)):
            raise SparkServerMisbehaving('bad spark used coin tags reply')
        hashes = await run_in_thread(
            libsparkmobile.hash_tags,
            [self._decode_b64(row[0]) for row in tag_rows])
        for tag_hash, row in zip(hashes, tag_rows):
            used_tags[tag_hash] = self._decode_b64(row[1])[::-1].hex()
        self.logger.info(
            f'refreshSparkData: used tags {tag_count} -> {len(used_tags)} '
            f'(+{len(tag_rows)} rows from offset {tag_count})')

    async def _batch_fetch_transactions(self, txids: List[str]) -> Dict[str, dict]:
        result = {}
        if not txids:
            return result
        batch_size = 100
        for start in range(0, len(txids), batch_size):
            batch = txids[start:start + batch_size]
            async with self.interface.session.send_batch() as batcher:
                for txid in batch:
                    batcher.add_request(
                        'blockchain.transaction.get', (txid, True))
            for txid, tx in zip(batch, batcher.results):
                if isinstance(tx, dict):
                    result[txid] = tx
        return result

    async def _update_pending_spark_spends(self):
        state = _deepcopy_db_val(self.wallet.db.get('spark_scan_state'))
        pending = list(state.get('pending_spark_spends') or [])
        for coin in self.wallet.db.get('spark_coins', {}).values():
            spent_txid = coin.get('spent_txid')
            if not spent_txid:
                continue
            mined = self.wallet.get_tx_height(spent_txid)
            if mined.conf == 0 and spent_txid not in pending:
                pending.append(spent_txid)
        still_pending = []
        updated = False
        canonical_tags = None
        for txid in dict.fromkeys(pending):
            try:
                result = await self._large_request(
                    'blockchain.transaction.get', (txid, True))
            except RPCError as e:
                if not _is_tx_not_found_error(e):
                    still_pending.append(txid)
                    continue
                if canonical_tags is None:
                    canonical_tags = self._shared_cache().snapshot_used_tags()
                if self._maybe_release_dropped_spend(txid, canonical_tags):
                    updated = True
                else:
                    still_pending.append(txid)
                continue
            except Exception:
                still_pending.append(txid)
                continue
            self._missing_spend_checks.pop(txid, None)
            height = result.get('height') if isinstance(result, dict) else None
            if not isinstance(height, int) or height <= 0:
                still_pending.append(txid)
                continue
            tx = self.wallet.db.get_transaction(txid)
            if not tx:
                still_pending.append(txid)
                continue
            self.wallet.receive_tx_callback(txid, tx, height)
            updated = True
        state['pending_spark_spends'] = still_pending
        self.wallet.db.put('spark_scan_state', _deepcopy_db_val(state))
        if updated:
            await self._save_spark_db()
            await self._notify_wallet_updated()

    def _maybe_release_dropped_spend(self, txid: str,
                                     canonical_tags: dict) -> bool:
        count = self._missing_spend_checks.get(txid, 0) + 1
        self._missing_spend_checks[txid] = count
        if count < SPEND_MISSING_CHECKS_BEFORE_RELEASE:
            return False
        coins = self.wallet.db.get('spark_coins') or {}
        tags = [tag for tag, c in coins.items()
                if c.get('spent_txid') == txid]
        if any(tag in canonical_tags for tag in tags):
            return False
        self.logger.info(
            f'Spark spend {txid} was dropped by the network; '
            f'releasing {len(tags)} reserved coin(s)')
        self._missing_spend_checks.pop(txid, None)
        self.wallet.release_spark_reservation(txid)
        return True

    async def _verify_coin_transactions(self, coins: dict) -> None:
        wallet = self.wallet
        unverified = wallet.get_unverified_txs()
        to_fetch = list({
            c['txid'] for c in coins.values()
            if c.get('txid')
            and (not c.get('tx_checked')
                 or (not wallet.db.is_in_verified_tx(c['txid'])
                     and c['txid'] not in unverified))})
        txs = await self._batch_fetch_transactions(to_fetch)
        rejected = set()
        for txid in to_fetch:
            data = txs.get(txid)
            if not isinstance(data, dict):
                continue
            try:
                raw = data.get('hex') or await self.interface.get_transaction(txid)
                tx = Transaction(raw)
                tx.deserialize()
                if tx.txid() != txid:
                    raise ValueError(f'server returned tx {tx.txid()}')
            except Exception as e:
                self.logger.warning(
                    f'Spark: could not validate transaction {txid}: {e!r}')
                continue
            for tag, coin in coins.items():
                if coin.get('txid') != txid:
                    continue
                try:
                    serialized = self._decode_b64(coin['serialized_coin'])
                except Exception:
                    serialized = b''
                if not _tx_contains_spark_coin(tx, serialized):
                    rejected.add(tag)
                    continue
                coin['tx_checked'] = True
            height = data.get('height')
            if isinstance(height, int) and height > 0:
                wallet.add_unverified_tx(txid, height)
        for tag in rejected:
            coin = coins.pop(tag)
            self.logger.warning(
                f'Spark: dropping coin claimed to be in {coin.get("txid")}, '
                f'the transaction does not contain it')
        for coin in coins.values():
            info = wallet.db.get_verified_tx(coin.get('txid'))
            if info and coin.get('tx_checked'):
                coin['height'] = info.height
                coin['timestamp'] = info.timestamp
            else:
                coin['height'] = None

    async def _verify_set_block(self, blockHash: str, newest_row) -> int:
        try:
            block_hash_hex = self._decode_b64(blockHash)[::-1].hex()
            txid = self._decode_b64(newest_row[1])[::-1].hex()
        except Exception:
            raise SparkServerMisbehaving(f'bad spark set block hash {blockHash!r}')
        data = await self._request('blockchain.transaction.get', (txid, True))
        height = data.get('height') if isinstance(data, dict) else None
        if not isinstance(height, int) or height <= 0:
            raise SparkServerMisbehaving(
                f'spark set block {block_hash_hex} has no mined coin {txid}')
        if height > self.wallet.get_local_height():
            raise RuntimeError(f'headers not synced to spark set height {height}')
        if self._header_hash_at(height) != block_hash_hex:
            raise SparkServerMisbehaving(
                f'spark set block {block_hash_hex} is not our block at '
                f'height {height}')
        return height

    async def _anchor_cached_sets(self, anon_sets: dict) -> None:
        for gid, entry in anon_sets.items():
            versions = (entry or {}).get('versions') or []
            if not versions:
                continue
            latest = max(versions, key=lambda v: int(v.get('size', 0)))
            if latest.get('block_height') or not latest.get('coins'):
                continue
            latest['block_height'] = await self._verify_set_block(
                latest['block_hash'], latest['coins'][-1])

    def _header_hash_at(self, height: int) -> Optional[str]:
        try:
            header = self.network.blockchain().read_header(height)
        except Exception:
            return None
        return hash_header(header) if header else None

    async def _import_missing_spark_spend_transactions(self):
        missing = [
            txid for _, txid in self.wallet.getSparkSpendTransactionIds()
            if txid and not self.wallet.db.get_transaction(txid)]
        if not missing:
            return
        txs = await self._batch_fetch_transactions(missing)
        updated = False
        for txid in missing:
            data = txs.get(txid)
            if not isinstance(data, dict):
                continue
            raw = data.get('hex')
            if not raw:
                try:
                    raw = await self.interface.get_transaction(txid)
                except Exception:
                    continue
            try:
                tx = Transaction(raw)
            except Exception:
                continue
            height = data.get('height')
            if isinstance(height, int) and height > 0:
                self.wallet.receive_tx_callback(txid, tx, height)
            else:
                from .address_synchronizer import TX_HEIGHT_UNCONFIRMED
                self.wallet.add_transaction(tx, allow_unrelated=True)
                self.wallet.add_unverified_tx(txid, TX_HEIGHT_UNCONFIRMED)
            updated = True
        if updated:
            await self._save_spark_db()
            await self._notify_wallet_updated()

    async def recover_spark(self, is_rescan: bool = True):
        if (not self.wallet.spark_enabled
                or not libsparkmobile.is_available()
                or not self.wallet.spark_view_key_hex):
            return
        if is_rescan:
            self._shared_cache().clear()
        self.wallet.clear_spark_data(is_rescan=is_rescan)
        await self.refreshSparkData()
        await self._import_missing_spark_spend_transactions()

    async def refreshSparkData(self, refreshProgressRange=None):
        if (not self.wallet.spark_enabled
                or not libsparkmobile.is_available()
                or not self.wallet.spark_view_key_hex):
            return
        async with self._sync_lock:
            view_key_hex = self.wallet.spark_view_key_hex
            if not view_key_hex:
                return
            shared = self._shared_cache()
            self._migrate_legacy_spark_cache(shared)
            state = _deepcopy_db_val(self.wallet.db.get('spark_scan_state'))
            coins = _deepcopy_db_val(self.wallet.db.get('spark_coins'))
            used_tags = shared.snapshot_used_tags()
            anon_sets = shared.snapshot_anon_sets()
            groups = shared.snapshot_groups()
            await run_in_thread(
                self._recover_saved_coins, coins, view_key_hex)
            local_height = self.wallet.get_local_height()
            tip_hash = self._header_hash_at(local_height)

            prev_height = int(state.get('chain_height', 0) or 0)
            prev_hash = state.get('chain_tip_hash')
            rolled_back = local_height < prev_height
            if not rolled_back and prev_hash:
                current = self._header_hash_at(prev_height)
                rolled_back = current is not None and current != prev_hash
            if rolled_back:
                self.logger.info(
                    f'Spark: chain fork detected at/below height '
                    f'{prev_height}, rebuilding the Spark cache')
                shared.clear()
                anon_sets, used_tags, groups = {}, {}, {}
                state.pop('firo_spark_cache_set_block_hash_cache', None)
                reserved = set(state.get('pending_spark_spends') or [])
                for c in coins.values():
                    c['height'] = None
                    c.pop('tx_checked', None)
                    if c.get('spent_txid') in reserved:
                        continue
                    c['is_used'] = False
                    c.pop('spent_txid', None)

            latestGroupId = _parse_group_id(await self._request(
                'spark.getsparklatestcoinid'))
            cachedGroupIds = [int(g) for g in anon_sets.keys()]
            if cachedGroupIds and latestGroupId < max(cachedGroupIds):
                raise SparkServerMisbehaving(
                    f'spark latest group id went back from '
                    f'{max(cachedGroupIds)} to {latestGroupId}')

            groupIds = []
            for groupId in range(1, latestGroupId):
                if not FiroCacheCoordinator.checkSetInfoForGroupIdExists(
                        self.wallet.db, groupId, anon_sets=anon_sets):
                    groupIds.append(groupId)
            more_groups_pending = len(groupIds) >= MAX_SPARK_GROUPS_PER_REFRESH
            groupIds = groupIds[:MAX_SPARK_GROUPS_PER_REFRESH - 1]
            groupIds.append(latestGroupId)
            self.logger.info(
                f'refreshSparkData: latestGroupId={latestGroupId} '
                f'groupIdsToFetch={groupIds} '
                f'cachedGroups={sorted(anon_sets.keys())} '
                f'knownCoins={len(coins)}')

            steps = len(groupIds) + 4
            percent_increment = None
            current_percent = 0.0
            if refreshProgressRange is not None:
                start_pct, end_pct = refreshProgressRange
                percent_increment = (end_pct - start_pct) / steps
                current_percent = start_pct

            for groupId in groupIds:
                coins = await self.runFetchAndUpdateSparkAnonSetCacheForGroupId(
                    groupId, anon_sets, coins, groups)
                self.logger.info(
                    f'Spark group {groupId} of {latestGroupId} synced')
                if percent_increment is not None:
                    current_percent += percent_increment
                    util.trigger_callback(
                        'spark_refresh_progress', self.wallet, current_percent)

            await self._anchor_cached_sets(anon_sets)
            await self.runFetchAndUpdateSparkUsedCoinTags(used_tags)
            if percent_increment is not None:
                current_percent += percent_increment
                util.trigger_callback(
                    'spark_refresh_progress', self.wallet, current_percent)

            if state.get('coin_check_version') != SPARK_COIN_CHECK_VERSION:
                state.pop('firo_spark_cache_set_block_hash_cache', None)
                state['coin_check_version'] = SPARK_COIN_CHECK_VERSION
            groupIdBlockHashMap = dict(
                state.get('firo_spark_cache_set_block_hash_cache') or {})
            rawCoinsBySetId = {}
            for groupId in range(1, latestGroupId + 1):
                lastCheckedHash = groupIdBlockHashMap.get(str(groupId))
                info = FiroCacheCoordinator.getLatestSetInfoForGroupId(
                    self.wallet.db, groupId, anon_sets=anon_sets)
                anonymitySetResult = FiroCacheCoordinator.getSetCoinsForGroupId(
                    self.wallet.db, groupId,
                    afterBlockHash=lastCheckedHash,
                    anon_sets=anon_sets)
                coinsRaw = [
                    [row[0], row[1], row[2]]
                    for row in anonymitySetResult
                    if isinstance(row, (list, tuple)) and len(row) >= 3
                ]
                if coinsRaw:
                    rawCoinsBySetId[groupId] = coinsRaw
                if info:
                    groupIdBlockHashMap[str(groupId)] = info['blockHash']

            for groupId, rows in rawCoinsBySetId.items():
                found = await run_in_thread(
                    self._identify_sector, rows, groupId, view_key_hex)
                self.logger.info(
                    f'refreshSparkData: group {groupId}: scanned {len(rows)} '
                    f'new set coins, identified {len(found)} as ours')
                coins.update(found)
            if percent_increment is not None:
                current_percent += percent_increment
                util.trigger_callback(
                    'spark_refresh_progress', self.wallet, current_percent)

            state['firo_spark_cache_set_block_hash_cache'] = groupIdBlockHashMap

            mempool_rows = await self._fetch_mempool_spark_rows()
            if mempool_rows:
                found = await run_in_thread(
                    self._identify_sector, mempool_rows, latestGroupId, view_key_hex)
                coins.update(found)

            await self._verify_coin_transactions(coins)

            for coin in coins.values():
                spent_txid = used_tags.get(coin['l_tag_hash'])
                if spent_txid:
                    coin['is_used'] = True
                    coin['spent_txid'] = spent_txid

            state['chain_height'] = local_height
            state['chain_tip_hash'] = tip_hash
            state.pop('groups', None)
            state.pop('used_tags_count', None)
            if not rolled_back:
                self._merge_used_flags(coins)
            shared.update(anon_sets=anon_sets, used_tags=used_tags,
                          groups=groups)
            self.wallet.db.put('spark_scan_state', _deepcopy_db_val(state))
            self.wallet.db.put('spark_coins', _deepcopy_db_val(coins))
            _confirmed = sum(1 for c in coins.values() if c.get('height'))
            _unused = sum(1 for c in coins.values() if not c.get('is_used'))
            self.logger.info(
                f'refreshSparkData: done. spark_coins={len(coins)} '
                f'(unused={_unused}, with_height={_confirmed}); '
                f'used_tags={len(used_tags)}')
            await self._save_spark_db()
            await self._update_pending_spark_spends()
            if percent_increment is not None:
                current_percent += percent_increment
                util.trigger_callback(
                    'spark_refresh_progress', self.wallet, current_percent)
            await self._import_missing_spark_spend_transactions()
            await self._notify_wallet_updated()
            if more_groups_pending:
                self.trigger()


class SparkInterfaceMixin:
    async def refreshSparkData(self, refreshProgressRange=None):
        sync = getattr(self, 'spark_synchronizer', None)
        if sync:
            await sync.refreshSparkData(refreshProgressRange)

    async def recoverSparkWallet(self):
        sync = getattr(self, 'spark_synchronizer', None)
        if sync:
            await sync.recover_spark(is_rescan=True)
        else:
            await self.refreshSparkData(None)

    def _spark_shared_cache(self) -> 'SparkSharedCache':
        return get_spark_shared_cache(self.config)

    def _highest_receiving_diversifier(self) -> Optional[int]:
        change = libsparkmobile.SPARK_CHANGE_DIVERSIFIER
        divs = []
        for key, entry in self._spark_address_book().items():
            if entry.get('subtype') == 'change':
                continue
            try:
                diversifier = int(key)
            except (TypeError, ValueError):
                continue
            if diversifier != change:
                divs.append(diversifier)
        return max(divs) if divs else None

    def _purge_change_from_address_book(self) -> None:
        book = self._spark_address_book()
        change_key = str(libsparkmobile.SPARK_CHANGE_DIVERSIFIER)
        if change_key not in book:
            return
        del book[change_key]
        self.db.put('spark_address_book', _deepcopy_db_val(book))

    def _spark_address_book(self) -> Dict[str, dict]:
        book = self.db.get('spark_address_book')
        if not isinstance(book, dict):
            return {}
        return _deepcopy_db_val(book)

    def _save_spark_address(self, diversifier: int, address: str,
                            subtype: str, *, save: bool = True) -> None:
        book = self._spark_address_book()
        book[str(diversifier)] = {'address': address, 'subtype': subtype}
        self.db.put('spark_address_book', _deepcopy_db_val(book))
        if save:
            self.save_db()

    def _init_spark_session(self, key_data: bytes) -> None:
        self.spark_view_key_hex = libsparkmobile.get_full_view_key_hex(
            key_data)
        self.db.put('spark_view_key_hex', self.spark_view_key_hex)
        book = self._spark_address_book()
        if '1' not in book:
            self._save_spark_address(
                1, self._generateSparkAddress(1), 'receiving', save=False)
            self.db.put('spark_current_receiving_diversifier', 1)
        self._purge_change_from_address_book()
        self._spark_change_address = self._generateSparkAddress(
            libsparkmobile.SPARK_CHANGE_DIVERSIFIER)
        self._current_spark_address = None
        self.getCurrentReceivingSparkAddress()
        self.save_db()

    def _generateSparkAddress(self, diversifier: int) -> str:
        is_testnet = bool(constants.net.TESTNET)
        view_key = getattr(self, 'spark_view_key_hex', None) or self.db.get(
            'spark_view_key_hex')
        if not view_key:
            raise RuntimeError(_('Spark key is not available'))
        return libsparkmobile.get_address_from_full_view_key_hex(
            view_key, diversifier, is_testnet=is_testnet)

    @property
    def sparkChangeAddress(self) -> Optional[str]:
        cached = getattr(self, '_spark_change_address', None)
        if cached:
            return cached
        if self.spark_view_key_hex or self.db.get('spark_view_key_hex'):
            self._spark_change_address = self._generateSparkAddress(
                libsparkmobile.SPARK_CHANGE_DIVERSIFIER)
            return self._spark_change_address
        return None

    def getCurrentReceivingSparkAddress(self) -> str:
        if not self.spark_view_key_hex and not self.db.get('spark_view_key_hex'):
            raise RuntimeError(_('Spark key is not available'))
        cached = getattr(self, '_current_spark_address', None)
        if cached:
            return cached
        diversifier = self._highest_receiving_diversifier()
        if diversifier is None:
            diversifier = int(self.db.get('spark_current_receiving_diversifier') or 1)
        entry = self._spark_address_book().get(str(diversifier))
        if entry and entry.get('address'):
            addr = entry['address']
        else:
            addr = self._generateSparkAddress(diversifier)
            self._save_spark_address(diversifier, addr, 'receiving')
        self._current_spark_address = addr
        self.db.put('spark_current_receiving_diversifier', diversifier)
        return addr

    def generateNextSparkAddress(self, *, saveToDB: bool = True) -> str:
        current_div = self._highest_receiving_diversifier()
        if current_div is None:
            current_div = int(self.db.get('spark_current_receiving_diversifier') or 0)
        diversifier = current_div + 1
        if diversifier == libsparkmobile.SPARK_CHANGE_DIVERSIFIER:
            diversifier += 1
        addr = self._generateSparkAddress(diversifier)
        self._current_spark_address = addr
        if saveToDB:
            self._save_spark_address(diversifier, addr, 'receiving', save=False)
            self.db.put('spark_current_receiving_diversifier', diversifier)
            self.save_db()
        return addr

    def getSparkSpendTransactionIds(self) -> List[Tuple[str, str]]:
        used_tags = self._spark_shared_cache().get('spark_used_tags', {}) or {}
        tags = [
            tag for tag, coin in (self.db.get('spark_coins', {}) or {}).items()
            if coin.get('is_used')]
        return FiroCacheCoordinator.getUsedCoinTxidsFor(used_tags, tags)

    def estimateFeeForSpark(self, amount: int, password=None) -> int:
        if self.is_watching_only():
            raise RuntimeError(
                _('Fee estimation is not supported for view only wallets'))
        spend_amount = int(amount)
        if spend_amount <= 0:
            return 0
        coins = self.get_spark_spendable_coins()
        if spend_amount > sum(int(c['value']) for c in coins):
            return 0
        key_data = self.derive_spark_key(password)
        try:
            estimate = self._estimateFeeForSpark(key_data, spend_amount, coins)
        finally:
            del key_data
        return max(0, int(estimate))

    def _estimateFeeForSpark(self, key_data: bytes, spend_amount: int,
                             coins) -> int:
        return _asyncSparkFeesWrapper(
            privateKeyHex=key_data,
            index=self.sparkIndex,
            sendAmount=spend_amount,
            subtractFeeFromAmount=True,
            serializedCoins=_ffi_spend_coins(coins),
            privateRecipientsCount=1,
            utxoNum=0,
            additionalTxSize=0)

    def confirmSendSpark(self, tx: PartialTransaction) -> str:
        used = (getattr(tx, '_spark_used_tags', None)
                or getattr(tx, 'usedSparkCoins', None))
        if used:
            self.note_spark_broadcast(tx)
            self.mark_spark_coins_used(used, spent_txid=tx.txid())
        sync = getattr(self, 'spark_synchronizer', None)
        if sync:
            sync.trigger()
        return tx.txid()

    def anonymizeAllSpark(self, password=None) -> List[PartialTransaction]:
        if self.is_watching_only():
            raise RuntimeError(
                _('Anonymizing is not supported for view only wallets'))
        spendableUtxos = list(self.get_spendable_coins())
        for utxo in spendableUtxos:
            self.add_input_info(utxo)
        if not spendableUtxos:
            raise NotEnoughFunds(_('No available UTXOs found to anonymize'))
        total = _sum(spendableUtxos)
        spark_addr = self.getCurrentReceivingSparkAddress()
        mints = self._createSparkMintTransactions(
            subtractFeeFromAmount=True,
            autoMintAll=True,
            availableUtxos=spendableUtxos,
            outputs=[MutableSparkRecipient(spark_addr, total, '')])
        return self.confirmSparkMintTransactions(mints, password)

    @property
    def sparkIndex(self) -> int:
        return kDefaultSparkIndex

    def _createSparkMintTransactions(
            self, *,
            availableUtxos: Sequence[PartialTxInput],
            outputs: List[MutableSparkRecipient],
            subtractFeeFromAmount: bool,
            autoMintAll: bool = False,
            change_addr: str = None,
            fee: int = None) -> List[PartialTransaction]:
        if not outputs:
            raise ValueError(_('Cannot mint without some recipients'))
        if len(outputs) != 1:
            raise ValueError(_('Only one Spark mint recipient is supported'))

        valueToMint = sum(o.value for o in outputs)
        if valueToMint <= 0:
            raise ValueError(_('Cannot mint amount=%s') % valueToMint)

        totalUtxosValue = _sum(availableUtxos)
        if valueToMint > totalUtxosValue:
            raise NotEnoughFunds(_('Insufficient balance to create spark mint(s)'))

        utxosByAddress: Dict[str, List[PartialTxInput]] = {}
        for utxo in availableUtxos:
            addr = utxo.address
            if not addr:
                continue
            utxosByAddress.setdefault(addr, []).append(utxo)
        valueAndUTXOs = list(utxosByAddress.values())
        if not valueAndUTXOs:
            raise NotEnoughFunds()

        nChangePosRequest = -1
        outputs_ = [MutableSparkRecipient(o.address, o.value, o.memo)
                    for o in outputs]

        minRelayFeeRatePerKB = 1000
        if fee is not None:
            mintFeeRatePerKB = max(0, int(fee))
        else:
            fee_per_kb = self.config.fee_per_kb() or 1000
            mintFeeRatePerKB = max(minRelayFeeRatePerKB, int(fee_per_kb))
        currentHeight = self.get_local_height()
        results: List[PartialTransaction] = []

        autoMintSparkAddress = outputs[0].address if autoMintAll else None
        change_addrs = self.get_change_addresses_for_new_transaction(change_addr)
        changeAddress = change_addrs[0] if change_addrs else None
        dust = self.dust_threshold()

        random.shuffle(valueAndUTXOs)

        while valueAndUTXOs:
            lockTime = (max(0, currentHeight - random.randint(0, 99))
                        if random.randint(0, 9) == 0 else currentHeight)
            txVersion = 1

            itr = valueAndUTXOs[0]
            valueToMintInTx = _sum(itr)
            if not autoMintAll:
                valueToMintInTx = min(valueToMintInTx, valueToMint)

            nFeeRet = 0
            skipCoin = False
            built_tx = None
            mintedValue = 0

            while True:
                mintedValue = valueToMintInTx
                if subtractFeeFromAmount:
                    nValueToSelect = mintedValue
                else:
                    nValueToSelect = mintedValue + nFeeRet

                if nValueToSelect > _sum(itr) and not subtractFeeFromAmount:
                    nValueToSelect = mintedValue
                    mintedValue -= nFeeRet

                if mintedValue <= 0:
                    valueAndUTXOs.remove(itr)
                    skipCoin = True
                    break

                nChangePosInOut = nChangePosRequest
                setCoins: List[PartialTxInput] = []

                remainingOutputs = [MutableSparkRecipient(o.address, o.value, o.memo)
                                    for o in outputs_]
                singleTxOutputs: List[MutableSparkRecipient] = []

                if autoMintAll:
                    singleTxOutputs.append(
                        MutableSparkRecipient(autoMintSparkAddress, mintedValue, ''))
                else:
                    remainingMintValue = mintedValue
                    while remainingMintValue > 0 and remainingOutputs:
                        singleMintValue = min(
                            remainingMintValue, remainingOutputs[0].value)
                        singleTxOutputs.append(MutableSparkRecipient(
                            remainingOutputs[0].address,
                            singleMintValue,
                            remainingOutputs[0].memo))
                        remainingMintValue -= singleMintValue
                        remainingOutputs[0].value -= singleMintValue
                        if remainingOutputs[0].value == 0:
                            remainingOutputs.pop(0)

                if subtractFeeFromAmount and nFeeRet > 0:
                    remainingFee = nFeeRet
                    outputIndex = 0
                    while singleTxOutputs and remainingFee > 0:
                        if outputIndex >= len(singleTxOutputs):
                            outputIndex = 0
                        outputsLeft = len(singleTxOutputs) - outputIndex
                        feeShare = remainingFee // outputsLeft
                        if remainingFee % outputsLeft:
                            feeShare += 1
                        if singleTxOutputs[outputIndex].value <= feeShare:
                            remainingFee -= singleTxOutputs[outputIndex].value
                            singleTxOutputs.pop(outputIndex)
                            continue
                        singleTxOutputs[outputIndex].value -= feeShare
                        remainingFee -= feeShare
                        outputIndex += 1
                    if not singleTxOutputs:
                        if autoMintAll:
                            raise ValueError(
                                _('UTXO value is too small to cover Spark mint fee'))
                        valueAndUTXOs.remove(itr)
                        skipCoin = True
                        break

                dummyRecipients = libsparkmobile.create_spark_mint_recipients(
                    [(o.address, o.value, '') for o in singleTxOutputs],
                    generate=False,
                    is_testnet=bool(constants.net.TESTNET))
                for _script, cAmount in dummyRecipients:
                    if cAmount < dust:
                        raise ValueError(_('Output amount too small'))

                nValueIn = 0
                for utxo in list(itr):
                    if nValueToSelect > nValueIn:
                        setCoins.append(utxo)
                        nValueIn += int(utxo.value_sats() or 0)
                if nValueIn < nValueToSelect:
                    raise NotEnoughFunds()

                nChange = nValueIn - nValueToSelect
                vout: List[PartialTxOutput] = []
                for (script, cAmount), out in zip(dummyRecipients, singleTxOutputs):
                    vout.append(PartialTxOutput(scriptpubkey=script, value=cAmount))

                fee_extra = 0
                if nChange > 0:
                    if nChange < dust:
                        fee_extra = nChange
                    else:
                        if not changeAddress:
                            raise RuntimeError(_('No change address'))
                        nChangePosInOut = random.randint(0, len(vout))
                        vout.insert(
                            nChangePosInOut,
                            PartialTxOutput.from_address_and_value(
                                changeAddress, nChange))

                for c in setCoins:
                    c.nsequence = MINT_INPUT_SEQUENCE
                est_tx = PartialTransaction()
                est_tx._inputs = list(setCoins)
                est_tx._outputs = list(vout)
                est_tx.locktime = lockTime
                est_tx.version = txVersion
                est_tx.invalidate_ser_cache()

                nBytes = est_tx.estimated_size()
                if nBytes * 4 > MAX_NEW_TX_WEIGHT:
                    raise ValueError(_('Transaction too large'))
                nBytesBuffer = 10 + 4 * len(setCoins)
                nFeeNeeded = (SimpleConfig.estimate_fee_for_feerate(
                    mintFeeRatePerKB, nBytes + nBytesBuffer) + fee_extra)

                if nFeeRet >= nFeeNeeded:
                    for used in setCoins:
                        if used in itr:
                            itr.remove(used)
                    if not itr:
                        valueAndUTXOs.remove(itr)

                    serialContext = libsparkmobile.serialize_mint_context(
                        [(i.prevout.txid[::-1], i.prevout.out_idx)
                         for i in setCoins])
                    recipients = libsparkmobile.create_spark_mint_recipients(
                        [(o.address, o.value, o.memo) for o in singleTxOutputs],
                        serialContext, generate=True,
                        is_testnet=bool(constants.net.TESTNET))

                    mint_i = 0
                    for i, out in enumerate(vout):
                        if (out.scriptpubkey
                                and out.scriptpubkey[0] == OP_SPARKMINT):
                            script, cAmount = recipients[mint_i]
                            vout[i] = PartialTxOutput(
                                scriptpubkey=script, value=cAmount)
                            mint_i += 1

                    outputs_ = remainingOutputs
                    valueToMint = sum(o.value for o in outputs_)

                    built_tx = PartialTransaction()
                    built_tx._inputs = list(setCoins)
                    built_tx._outputs = list(vout)
                    built_tx.locktime = lockTime
                    built_tx.version = txVersion
                    built_tx._spark_fee = nFeeRet
                    built_tx.invalidate_ser_cache()
                    _verify_mint_serial_context(built_tx, serialContext)
                    break

                nFeeRet = nFeeNeeded

            if skipCoin:
                continue
            if built_tx is None:
                break

            actualFee = (_sum(built_tx._inputs)
                         - sum(o.value for o in built_tx._outputs))
            if actualFee != built_tx._spark_fee:
                _logger.error(
                    f'Spark mint fee accounting mismatch: '
                    f'expected={built_tx._spark_fee}, actual={actualFee}')
                raise ValueError(_('Spark mint fee accounting mismatch'))

            vSize = built_tx.estimated_size()
            if built_tx._spark_fee < vSize:
                _logger.warning(
                    f'Fee rate below 1 sat/byte minimum relay fee: '
                    f'fee={built_tx._spark_fee} sats, vSize={vSize} bytes')
                raise ValueError(
                    _('Fee rate below 1 sat/byte minimum relay fee'))

            results.append(built_tx)
            if not autoMintAll and valueToMint <= 0:
                break

        if not autoMintAll and valueToMint > 0:
            raise ValueError(_('Failed to mint expected amounts'))
        if autoMintAll and not results:
            raise ValueError(_('No Spark mint transactions were created'))
        return results

    def prepareSparkMintTransaction(
            self, *,
            sparkRecipients: Sequence[MutableSparkRecipient] = None,
            utxos: Sequence[PartialTxInput] = None,
            subtractFeeFromAmount: bool = None,
            autoMintAll: bool = False,
            spark_address: str = None,
            amount=None,
            memo: str = '',
            coins: Sequence[PartialTxInput] = None,
            fee=None,
            change_addr: str = None,
            domain=None,
            nonlocal_only: bool = False) -> PartialTransaction:
        if not self.spark_enabled:
            raise RuntimeError(_('Spark is disabled for this session'))
        if not libsparkmobile.is_available():
            raise RuntimeError('electrum_libsparkmobile is not available')

        if sparkRecipients is None:
            if not spark_address:
                raise ValueError(_('Missing spark recipients.'))
            if amount == '!':
                availableUtxos = list(coins or self.get_spendable_coins(
                    domain, nonlocal_only=nonlocal_only))
                for c in availableUtxos:
                    self.add_input_info(c)
                mint_value = _sum(availableUtxos)
                subtractFeeFromAmount = True
            else:
                if not isinstance(amount, int) or amount <= 0:
                    raise ValueError(_('Invalid Amount'))
                mint_value = amount
            if not libsparkmobile.is_valid_spark_address(
                    spark_address, is_testnet=bool(constants.net.TESTNET)):
                raise ValueError(_('Invalid Spark address'))
            sparkRecipients = [MutableSparkRecipient(
                spark_address, mint_value, memo or '')]
        else:
            sparkRecipients = list(sparkRecipients)

        total = sum(r.value for r in sparkRecipients)
        if total <= 0:
            raise ValueError(_('Attempted send of zero amount'))

        coinControl = utxos is not None or coins is not None
        availableUtxos = list(utxos or coins or self.get_spendable_coins(
            domain, nonlocal_only=nonlocal_only))
        for c in availableUtxos:
            self.add_input_info(c)

        if coinControl and _sum(availableUtxos) < total:
            raise NotEnoughFunds(_('Insufficient selected UTXOs!'))

        isSendAllCoinControlUtxos = coinControl and total == _sum(availableUtxos)

        if subtractFeeFromAmount is None:
            if isSendAllCoinControlUtxos or amount == '!':
                subtractFeeFromAmount = True
            elif _sum(availableUtxos) < total:
                raise NotEnoughFunds(_('Insufficient balance'))
            elif _sum(availableUtxos) == total:
                subtractFeeFromAmount = True
            else:
                subtractFeeFromAmount = False

        if not availableUtxos:
            raise NotEnoughFunds(_('No available UTXOs found to anonymize'))

        sparkMints = self._createSparkMintTransactions(
            subtractFeeFromAmount=subtractFeeFromAmount,
            autoMintAll=autoMintAll,
            availableUtxos=availableUtxos,
            outputs=list(sparkRecipients),
            change_addr=change_addr,
            fee=fee)
        if not sparkMints:
            raise NotEnoughFunds()
        first = sparkMints[0]
        if len(sparkMints) > 1:
            first.sparkMints = sparkMints[1:]
        return first

    def confirmSparkMintTransactions(self, sparkMints: Sequence[PartialTransaction],
                                     password=None) -> List[PartialTransaction]:
        signed = []
        for tx in sparkMints:
            self.sign_transaction(tx, password)
            signed.append(tx)
        return signed

    def prepareSendSpark(self, *, password=None,
                         **kwargs) -> PartialTransaction:
        if not self.spark_enabled:
            raise RuntimeError(_('Spark is disabled for this session'))
        if not libsparkmobile.is_available():
            raise RuntimeError('electrum_libsparkmobile is not available')
        key_data = self.derive_spark_key(password)
        try:
            return self._prepareSendSpark(key_data, **kwargs)
        finally:
            del key_data

    def _prepareSendSpark(
            self, key_data: bytes, *,
            recipients: Sequence[Tuple[str, int]] = None,
            sparkRecipients: Sequence[Tuple[str, int, str]] = None,
            address: str = None,
            amount=None,
            memo: str = '') -> PartialTransaction:
        transparentRecipients = list(recipients or [])
        privateSparkRecipients = list(sparkRecipients or [])
        if address is not None:
            if amount is None:
                raise ValueError(_('Invalid Amount'))
            is_spark_dest = libsparkmobile.is_valid_spark_address(
                address, is_testnet=bool(constants.net.TESTNET))
            if not is_spark_dest and not is_address(address):
                raise ValueError(_('Invalid Address'))
            if libsparkmobile.is_valid_spark_address(
                    address, is_testnet=bool(constants.net.TESTNET)):
                privateSparkRecipients.append(
                    (address, amount if amount != '!' else 0, memo or ''))
            else:
                transparentRecipients.append((address, amount if amount != '!' else 0))

        if not transparentRecipients and not privateSparkRecipients:
            raise ValueError(_('No recipients provided.'))
        for addr, _amt in transparentRecipients:
            if is_exchange_address(addr):
                raise ValueError(_(
                    'Spending Spark funds to an exchange address is not '
                    'allowed. Send them to a transparent address of your '
                    'own wallet first and from there to the exchange '
                    'address.'))
        if len(privateSparkRecipients) >= SPARK_OUT_LIMIT_PER_TX - 1:
            raise ValueError(_('Spark shielded output limit exceeded.'))

        coins = self.get_spark_spendable_coins()
        if not coins:
            raise NotEnoughFunds(_('No spendable Spark coins found'))

        send_all_flag = (
            amount == '!'
            or any(a == '!' for _, a in transparentRecipients)
            or any(a == '!' for _, a, _ in privateSparkRecipients))

        def _amt(a):
            return 0 if a == '!' else int(a)

        transparentSumOut = sum(_amt(a) for _, a in transparentRecipients)
        sparkSumOut = sum(_amt(a) for _, a, _ in privateSparkRecipients)
        txAmount = transparentSumOut + sparkSumOut

        if transparentSumOut > 50_000 * COIN:
            raise ValueError(_('Spend to transparent address limit exceeded '
                               '(50,000 Firo per transaction).'))

        available = sum(int(c['value']) for c in coins)

        if send_all_flag:
            if len(transparentRecipients) + len(privateSparkRecipients) != 1:
                raise ValueError(_('Send-all requires a single recipient'))
            if transparentRecipients:
                transparentRecipients = [(transparentRecipients[0][0], available)]
            else:
                addr, _old_amt, mem = privateSparkRecipients[0]
                privateSparkRecipients = [(addr, available, mem)]
            transparentSumOut = sum(int(a) for _, a in transparentRecipients)
            sparkSumOut = sum(int(a) for _, a, _ in privateSparkRecipients)
            txAmount = transparentSumOut + sparkSumOut

        if txAmount > available:
            raise NotEnoughFunds(_('Insufficient Spark balance'))

        isSendAll = available == txAmount

        serializedCoins = _ffi_spend_coins(coins)

        myCoinGroupIds = {int(c['group_id']) for c in coins}
        coverSets = self.get_spark_cover_sets(myCoinGroupIds)

        allAnonymitySets = [{
            'setId': s['set_id'],
            'setHash': s['set_hash'],
            'set': [{'serializedCoin': row[0], 'txHash': row[1]}
                    for row in s['raw_coins']],
        } for s in coverSets]
        idAndBlockHashes = [{
            'groupId': s['set_id'],
            'blockHash': s['block_hash'],
        } for s in coverSets]

        if not allAnonymitySets:
            raise RuntimeError(_('Spark anonymity set is not ready yet.'))

        recipientCount = sum(1 for _, a in transparentRecipients if int(a) > 0)
        totalRecipientCount = recipientCount + len(privateSparkRecipients)

        estimatedFee = 0
        if isSendAll:
            estimatedFee = _asyncSparkFeesWrapper(
                privateKeyHex=key_data,
                index=self.sparkIndex,
                sendAmount=txAmount,
                subtractFeeFromAmount=True,
                serializedCoins=serializedCoins,
                privateRecipientsCount=len(privateSparkRecipients),
                utxoNum=recipientCount,
                additionalTxSize=0)

        transparent_outputs = []
        ffi_recipients = []
        for addr, amt in transparentRecipients:
            amt = int(amt)
            if amt <= 0:
                continue
            adj = (amt - (estimatedFee // totalRecipientCount)
                   if isSendAll and totalRecipientCount else amt)
            ffi_recipients.append({
                'address': addr,
                'amount': amt,
                'subtractFeeFromAmount': isSendAll,
            })
            transparent_outputs.append(
                PartialTxOutput.from_address_and_value(addr, adj))

        ffi_private = []
        for addr, amt, mem in privateSparkRecipients:
            amt = int(amt)
            ffi_private.append({
                'sparkAddress': addr,
                'amount': amt,
                'subtractFeeFromAmount': isSendAll,
                'memo': mem or '',
            })

        spark_in = PartialTxInput(
            prevout=TxOutpoint(txid=bytes(32), out_idx=0xffffffff),
            script_sig=bytes([OP_SPARKSPEND]),
            nsequence=0xffffffff)
        spark_in._trusted_value_sats = available

        tx = PartialTransaction()
        tx._inputs = [spark_in]
        tx._outputs = list(transparent_outputs)
        tx.locktime = self.get_local_height()
        tx.version = 3
        tx.tx_type = SPARK_SPEND_V2
        tx.extra_payload = FiroSparkSpend(b'')
        tx.invalidate_ser_cache()

        txHash = sha256d(bfh(tx.serialize_to_network(include_sigs=True)))

        idAndBlockHashesBytes = []
        for e in idAndBlockHashes:
            blockHash = e['blockHash']
            if isinstance(blockHash, str):
                blockHash = base64.b64decode(''.join(blockHash.splitlines()))
            idAndBlockHashesBytes.append({
                'setId': e['groupId'],
                'blockHash': blockHash,
            })

        spend = _createSparkSend(
            privateKeyHex=key_data,
            index=self.sparkIndex,
            recipients=ffi_recipients,
            privateRecipients=ffi_private,
            serializedCoins=serializedCoins,
            allAnonymitySets=allAnonymitySets,
            idAndBlockHashes=idAndBlockHashesBytes,
            txHash=txHash,
            additionalTxSize=0,
            isTestNet=bool(constants.net.TESTNET))

        for outputScript in spend['output_scripts']:
            tx._outputs.append(PartialTxOutput(scriptpubkey=outputScript, value=0))
        tx.extra_payload = FiroSparkSpend(spend['payload'])
        fee = int(spend['fee'])
        spark_in._trusted_value_sats = sum(o.value for o in tx._outputs) + fee
        tx.invalidate_ser_cache()

        used_tags = []
        for usedCoin in spend['used_coins']:
            used_ser = usedCoin['serialized_coin']
            used_group_id = int(usedCoin['group_id'])
            used_height = int(usedCoin['height'])
            match = None
            for coin in coins:
                if (int(coin['group_id']) != used_group_id
                        or int(coin['height']) != used_height):
                    continue
                raw = coin['serialized_coin']
                if isinstance(raw, str):
                    raw = base64.b64decode(''.join(raw.splitlines()))
                if raw.startswith(used_ser):
                    match = coin
                    break
            if match is None:
                raise RuntimeError(
                    _('Unexpectedly did not find used spark coin. '
                      'This should never happen.'))
            used_tags.append(match['l_tag_hash'])
        tx._spark_used_tags = used_tags
        tx._spark_fee = fee
        if isSendAll:
            tx._spark_send_amount = max(0, txAmount - fee)
        else:
            tx._spark_send_amount = txAmount
        return tx
