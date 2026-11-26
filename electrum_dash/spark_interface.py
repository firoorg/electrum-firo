import asyncio
import base64
import json
import os
import random
import shutil
import threading
from typing import TYPE_CHECKING, Dict, List, Optional, Sequence, Tuple

from aiorpcx import RPCError, run_in_thread

from . import constants, libsparkmobile, util
from .bitcoin import COIN, b58_address_to_hash160, is_address
from .blockchain import hash_header
from .crypto import sha256d
from .dash_tx import SPARK_SPEND_TYPES, SPARK_SPEND_V2, FiroSparkSpend
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
# Mainnet had 11 groups in November 2026. The cap bounds how many full sets a
# server can make the wallet download and store.
MAX_SPARK_GROUP_ID = 1_000
MAX_SPARK_GROUPS_PER_REFRESH = 4
SPARK_SET_CACHE_VERSION = 2
SPARK_COIN_CHECK_VERSION = 2
# Must match nSparkChaumV2StartBlock in Firo Core's chainparams.cpp.
SPARK_CHAUM_V2_START_HEIGHT = {
    'mainnet': 1_371_000,
    'testnet': 210_800,
    'devnet': 3_700,
    'regtest': 700,
}


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
        'serialized_coin': c['serialized_coin'],
        'context': c.get('context') or c.get('serial_context'),
        'group_id': int(c['group_id']),
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
        # The coin must be where the node parses it: right after the opcode.
        if (script and script[0] in (OP_SPARKMINT, OP_SPARKSMINT)
                and script[1:1 + len(serialized_coin)] == serialized_coin):
            return True
    return False


def spark_mint_amount(tx: PartialTransaction) -> int:
    return sum(o.value for o in tx.outputs() if o.scriptpubkey and o.scriptpubkey[0] == OP_SPARKMINT)


def spark_spend_fee(tx: Transaction) -> Optional[int]:
    """The fee that a Spark spend declares in its payload, else None.

    The payload opens with the cover set ids, then a map from set id to block
    hash, then the fee. Version 2 puts its version, input count and output
    count in front.
    """
    if tx.tx_type not in SPARK_SPEND_TYPES:
        return None
    payload = getattr(tx.extra_payload, 'sparkData', None) or b''
    try:
        if tx.tx_type == SPARK_SPEND_V2:
            inputs, pos = _read_compact_size(payload, 1)
            _outputs, pos = _read_compact_size(payload, pos)
        else:
            inputs, pos = _read_compact_size(payload, 0)
        pos += 8 * inputs
        block_hashes, pos = _read_compact_size(payload, pos)
        pos += (8 + 32) * block_hashes
    except (IndexError, KeyError, ValueError):
        return None
    if len(payload) < pos + 8:
        return None
    return int.from_bytes(payload[pos:pos + 8], 'little')


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
        coins=serializedCoins,
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
    return libsparkmobile.estimate_spark_fee(
        privateKeyHex,
        send_amount=sendAmount,
        subtract_fee_from_amount=subtractFeeFromAmount,
        coins=serializedCoins,
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


def _merge_anon_sets(existing: dict, incoming: dict) -> List[str]:
    changed = []
    for gid, entry in (incoming or {}).items():
        current = existing.get(gid)
        if (current is None
                or _anon_set_entry_size(entry) >= _anon_set_entry_size(current)):
            if current != entry:
                existing[gid] = entry
                changed.append(gid)
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


def _read_json(path: str):
    try:
        with open(path, 'r', encoding='utf-8') as f:
            return json.load(f)
    except (FileNotFoundError, OSError, ValueError):
        return None


def _write_json(path: str, value) -> None:
    tmp = f'{path}.tmp'
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(value, f)
        os.replace(tmp, path)
    except OSError as e:
        _logger.error(f'could not write spark shared cache {path}: {e!r}')


class SparkSharedCache:
    # Used tags and groups are kept in `path`, and each anonymity set in its
    # own file in `sets_dir`, so an update rewrites only the sets that changed.
    _EMPTY = {
        'spark_anon_sets': {},
        'spark_used_tags': {},
        'spark_groups': {},
    }

    def __init__(self, path: str):
        self.path = path
        self.sets_dir = os.path.splitext(path)[0] + '_sets'
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

    def _set_path(self, gid) -> str:
        return os.path.join(self.sets_dir, f'{gid}.json')

    def _load_locked(self, gids=None) -> None:
        """Reads the used tags, the groups, and the sets in `gids` (all if None)."""
        loaded = _read_json(self.path)
        if isinstance(loaded, dict):
            for key in ('spark_used_tags', 'spark_groups'):
                if key in loaded:
                    self._data[key] = loaded[key]
        if gids is None:
            try:
                gids = [name[:-len('.json')] for name in os.listdir(self.sets_dir)
                        if name.endswith('.json')]
            except OSError:
                gids = []
        for gid in gids:
            entry = _read_json(self._set_path(gid))
            if entry is not None:
                self._data['spark_anon_sets'][gid] = entry

    def _flush_locked(self, gids) -> None:
        for gid in gids:
            _write_json(self._set_path(gid), self._data['spark_anon_sets'][gid])
        _write_json(self.path, {key: value for key, value in self._data.items()
                                if key != 'spark_anon_sets'})

    def update(self, *, anon_sets=None, used_tags=None, groups=None) -> None:
        with self._lock:
            self._load_locked(gids=list(anon_sets or ()))
            changed_sets = _merge_anon_sets(
                self._data['spark_anon_sets'], _deepcopy_db_val(anon_sets))
            changed = bool(changed_sets)
            for key, incoming in (('spark_used_tags', used_tags),
                                  ('spark_groups', groups)):
                if incoming is None:
                    continue
                incoming = json.loads(json.dumps(incoming))
                stored = self._data.get(key)
                if not isinstance(stored, dict):
                    stored = self._data[key] = {}
                    changed = True
                changed |= _merge_dict(stored, incoming)
            if changed:
                self._flush_locked(changed_sets)

    def discard(self, *, sets=(), used_tags: bool = False) -> None:
        """Forgets cached answers that a server has since contradicted."""
        if not sets and not used_tags:
            return
        with self._lock:
            for gid in map(str, sets):
                self._data['spark_anon_sets'].pop(gid, None)
                self._data['spark_groups'].pop(gid, None)
                try:
                    os.remove(self._set_path(gid))
                except OSError:
                    pass
            if used_tags:
                self._data['spark_used_tags'] = {}
            self._flush_locked([])

    def clear(self) -> None:
        with self._lock:
            self._data = json.loads(json.dumps(self._EMPTY))
            shutil.rmtree(self.sets_dir, ignore_errors=True)
            self._flush_locked([])


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
        self.retrying = False
        NetworkJobOnDefaultServer.__init__(self, network)

    def _reset(self):
        super()._reset()
        self._wake = asyncio.Event()
        self.syncing = False
        self._recover_pending = False
        self._mempool_txids_checked = set()
        self._missing_spend_checks = {}
        # Groups fetched again from this server after a cached row of theirs
        # named the wrong transaction for one of our coins.
        self._refetched_groups = set()

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
                was_retrying = self.retrying
                try:
                    await self._update_pending_spark_spends()
                    if self._recover_pending:
                        self._recover_pending = False
                        await self.recover_spark(is_rescan=True)
                    else:
                        await self.refreshSparkData()
                except SparkServerMisbehaving as e:
                    self.retrying = True
                    self.logger.warning(
                        f'Spark: disconnecting misbehaving server: {e!r}')
                    raise
                except Exception as e:
                    self.retrying = True
                    self.logger.info(f'Spark sync failed: {e!r}')
                else:
                    self.retrying = False
                finally:
                    if self.syncing or self.retrying != was_retrying:
                        self.syncing = False
                        await self._notify_wallet_updated()
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=60)
            except asyncio.TimeoutError:
                pass
            self._wake.clear()

    async def _request(self, method, params=(), timeout=120):
        session = self.interface.session
        session.allow_spark_messages()
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

    async def _fetch_sectors(self, groupId, blockHash, numberOfCoinsToFetch,
                             start_index=0):
        rows = []
        end_index = start_index + numberOfCoinsToFetch
        for start in range(start_index, end_index, self.sectorSize):
            end = min(start + self.sectorSize, end_index)
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
        """Returns the rows of unchecked mempool coins and the txids of all
        Spark transactions in the server's mempool (None if it gave no list)."""
        try:
            mempool_txids_b64 = await self._large_request(
                'spark.getmempoolsparktxids')
            hex_to_b64 = {self._decode_b64(b64)[::-1].hex(): b64
                          for b64 in (mempool_txids_b64 or [])}
        except Exception as e:
            self.logger.info(f'Spark mempool txid fetch failed: {e!r}')
            return [], None
        mempool_txids_hex = set(hex_to_b64.keys())
        self._mempool_txids_checked &= mempool_txids_hex
        to_check = list(mempool_txids_hex - self._mempool_txids_checked)
        self.logger.info(
            f'Spark mempool txids={mempool_txids_hex!r} '
            f'already_checked={self._mempool_txids_checked!r} '
            f'to_check={to_check!r}')
        if not to_check:
            return [], mempool_txids_hex
        try:
            response = await self._large_request(
                'spark.getmempoolsparktxs', ({'txids': to_check},))
        except Exception as e:
            self.logger.info(f'Spark mempool tx data fetch failed: {e!r}')
            return [], mempool_txids_hex
        self.logger.info(f'Spark mempool tx data raw response: {response!r}')
        rows = []
        for txid_key, data in (response.items()
                               if isinstance(response, dict) else ()):
            # The mempool is a preview, so a malformed entry is skipped: it
            # must not stop the scan of the confirmed sets.
            if not isinstance(data, dict):
                continue
            coins = data.get('coins') or []
            serial_contexts = data.get('serial_context') or []
            if (not isinstance(coins, list) or not coins
                    or not isinstance(serial_contexts, list)
                    or not serial_contexts):
                continue
            context_b64 = serial_contexts[0]
            if txid_key in hex_to_b64:
                txid_hex = txid_key
                txid_b64 = hex_to_b64[txid_key]
            elif txid_key in hex_to_b64.values():
                txid_b64 = txid_key
                txid_hex = self._decode_b64(txid_key)[::-1].hex()
            else:
                try:
                    txid_b64 = base64.b64encode(
                        bytes.fromhex(txid_key)[::-1]).decode('ascii')
                except ValueError:
                    continue
                txid_hex = txid_key
            for coin_b64 in coins:
                rows.append([coin_b64, txid_b64, context_b64])
            self._mempool_txids_checked.add(txid_hex)
        self.logger.info(f'Spark mempool rows built: {len(rows)}')
        return rows, mempool_txids_hex

    async def runFetchAndUpdateSparkAnonSetCacheForGroupId(
            self, groupId, anon_sets, coins, groups, rawCoinsBySetId=None,
            discarded=None):
        meta = await self._request(
            'spark.getsparkanonymitysetmeta', (str(groupId),))
        blockHash, setHash, size = _parse_set_meta(meta)
        prevMeta = FiroCacheCoordinator.getLatestSetInfoForGroupId(
            self.wallet.db, groupId, anon_sets=anon_sets)
        prevSize = int(prevMeta['size']) if prevMeta else 0
        if (prevMeta and 0 < size < prevSize and prevMeta.get('blockHeight')
                and prevMeta['blockHash'] != blockHash):
            # A server that has not reached the cached block yet has a smaller
            # set, and that is no contradiction: keep the cache until it has.
            anchor = await self._fetch_sectors(
                groupId, blockHash, 1, start_index=0)
            height = await self._verify_set_block(blockHash, anchor[0])
            if height < int(prevMeta['blockHeight']):
                self.logger.info(
                    f'Spark group {groupId}: server is at height {height}, '
                    f'behind the cached set; keeping the cache')
                return coins
        # The cached set is what an earlier server said, and nothing ties it
        # to the chain. A set can only grow, and at one block it has one size
        # and hash. Where this server says otherwise, one of the two is wrong
        # and we cannot tell which, so the group is fetched again in full.
        if prevMeta and (
                size < prevSize
                or (prevMeta['blockHash'] == blockHash
                    and (size, setHash) != (prevSize, prevMeta['setHash']))):
            self.logger.warning(
                f'Spark group {groupId}: server reports size {size}, cache '
                f'has {prevSize}; fetching the group again')
            anon_sets.pop(str(groupId), None)
            groups.pop(str(groupId), None)
            if discarded is not None:
                discarded.add(groupId)
            prevMeta, prevSize = None, 0

        entry = FiroCacheCoordinator._entry(
            self.wallet.db, groupId, anon_sets=anon_sets)
        cache_version = (entry or {}).get('cache_version')
        if (prevMeta and prevMeta['blockHash'] == blockHash
                and cache_version == SPARK_SET_CACHE_VERSION):
            groups[str(groupId)] = {
                'block_hash': blockHash,
                'set_hash': setHash,
                'size': size,
            }
            return coins

        rebuild = prevMeta is not None and (
            cache_version != SPARK_SET_CACHE_VERSION or size == prevSize)
        numberOfCoinsToFetch = size if rebuild else size - prevSize
        if numberOfCoinsToFetch > 0 and not self.syncing:
            self.syncing = True
            util.trigger_callback('wallet_updated', self.wallet)

        blockHeight = None
        rows = []
        if numberOfCoinsToFetch > 0:
            rows = await self._fetch_sectors(
                groupId, blockHash, numberOfCoinsToFetch,
                start_index=0)
        if rows:
            blockHeight = await self._verify_set_block(blockHash, rows[0])
        elif size:
            anchor_row = await self._fetch_sectors(
                groupId, blockHash, 1, start_index=0)
            blockHeight = await self._verify_set_block(blockHash, anchor_row[0])

        if rows or rebuild:
            if rawCoinsBySetId is not None:
                rawCoinsBySetId[groupId] = rows
            version_coins = list(reversed(rows))
            whole = rebuild or not entry
            versions = [] if whole else list(entry.get('versions') or [])
            if rows:
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
                'cache_version': SPARK_SET_CACHE_VERSION,
                # A set that is fetched whole gets a new epoch, so that every
                # wallet sharing the cache scans all of its rows again.
                'epoch': (os.urandom(8).hex() if whole
                          else entry.get('epoch', '')),
            }

        groups[str(groupId)] = {
            'block_hash': blockHash,
            'set_hash': setHash,
            'size': size,
        }
        return coins

    async def _fetch_used_tags(self, start: int) -> List[Tuple[str, str]]:
        """The server's used tags from position `start`, as (tag hash, txid)."""
        response = await self._large_request(
            'spark.getusedcoinstagstxhashes', (str(start),))
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
        return [(tag_hash, self._decode_b64(row[1])[::-1].hex())
                for tag_hash, row in zip(hashes, tag_rows)]

    async def runFetchAndUpdateSparkUsedCoinTags(self, used_tags) -> bool:
        """Appends the server's new used tags to `used_tags`. Returns True if
        the cached tags were dropped and the whole list was fetched again."""
        tag_count = len(used_tags)
        if not tag_count and not self.syncing:
            self.syncing = True
            util.trigger_callback('wallet_updated', self.wallet)
        # Tags are fetched by position, so tags that are not on the chain
        # would shift every later fetch. Ask again for the last position we
        # hold: a tag there that we lack means the cached list is not a
        # prefix of this server's list. A server that is a few blocks behind
        # returns nothing here, and the cache is kept.
        start = max(0, tag_count - 1)
        rows = await self._fetch_used_tags(start)
        restarted = bool(tag_count and rows and rows[0][0] not in used_tags)
        if restarted:
            self.logger.warning(
                'Spark: cached used tags do not match the server, '
                'fetching them again')
            used_tags.clear()
            start = 0
            rows = await self._fetch_used_tags(0)
        used_tags.update(rows)
        self.logger.info(
            f'refreshSparkData: used tags {tag_count} -> {len(used_tags)} '
            f'(+{len(rows)} rows from offset {start})')
        return restarted

    async def _batch_fetch_transactions(self, txids: List[str]) -> Dict[str, dict]:
        result = {}
        if not txids:
            return result
        batch_size = 100
        self.interface.session.allow_spark_messages()
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

    @classmethod
    def _cached_row_txids(cls, coin: dict, anon_sets: Optional[dict]) -> set:
        """The txids that the cached rows of the coin's set give for it."""
        entry = (anon_sets or {}).get(str(coin.get('group_id'))) or {}
        txids = set()
        for version in entry.get('versions') or []:
            for row in version.get('coins') or []:
                if (isinstance(row, (list, tuple)) and len(row) >= 2
                        and row[0] == coin.get('serialized_coin')):
                    try:
                        txids.add(cls._decode_b64(row[1])[::-1].hex())
                    except Exception:
                        pass
        return txids

    async def _verify_coin_transactions(self, coins: dict, anon_sets=None,
                                        mempool_txids=None) -> set:
        """Checks each coin against the transaction it is said to be in.

        Returns the ids of the cached sets that name a wrong transaction for
        one of our coins. `mempool_txids` is the server's current list of
        Spark mempool transactions, or None if it is not known.
        """
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
        bad_groups = set()
        for tag in rejected:
            coin = coins.pop(tag)
            self.logger.warning(
                f'Spark: dropping coin claimed to be in {coin.get("txid")}, '
                f'the transaction does not contain it')
            if coin.get('txid') in self._cached_row_txids(coin, anon_sets):
                bad_groups.add(int(coin['group_id']))
        if mempool_txids is not None:
            # A coin seen only in the mempool is forgotten once the server no
            # longer knows its transaction. If the transaction comes back or
            # is mined, the coin is found again from the mempool or its set.
            gone = [tag for tag, coin in coins.items()
                    if coin.get('txid') in to_fetch
                    and coin['txid'] not in txs
                    and coin['txid'] not in mempool_txids
                    and not coin.get('is_used')
                    and not self._cached_row_txids(coin, anon_sets)]
            for tag in gone:
                coin = coins.pop(tag)
                self.logger.info(
                    f'Spark: forgetting unconfirmed coin, its transaction '
                    f'{coin.get("txid")} is no longer known to the server')
        for coin in coins.values():
            info = wallet.db.get_verified_tx(coin.get('txid'))
            if info and coin.get('tx_checked'):
                coin['height'] = info.height
                coin['timestamp'] = info.timestamp
            else:
                coin['height'] = None
        return bad_groups

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
                if tx.txid() != txid:
                    raise ValueError(f'server returned tx {tx.txid()}')
            except Exception as e:
                self.logger.warning(
                    f'Spark: could not import spend transaction {txid}: {e!r}')
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
            # Cached groups that this server contradicts. They leave the
            # shared cache when this refresh is saved, and are fetched again.
            discarded = {int(g) for g in anon_sets if int(g) > latestGroupId}
            if discarded:
                self.logger.warning(
                    f'Spark: dropping cached groups above the latest group '
                    f'id {latestGroupId}: {sorted(discarded)}')
                for gid in discarded:
                    anon_sets.pop(str(gid), None)
                    groups.pop(str(gid), None)

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
                    groupId, anon_sets, coins, groups, discarded=discarded)
                self.logger.info(
                    f'Spark group {groupId} of {latestGroupId} synced')
                if percent_increment is not None:
                    current_percent += percent_increment
                    util.trigger_callback(
                        'spark_refresh_progress', self.wallet, current_percent)

            await self._anchor_cached_sets(anon_sets)
            tags_restarted = await self.runFetchAndUpdateSparkUsedCoinTags(
                used_tags)
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
                # The rows up to the recorded block were scanned already,
                # unless the set has been fetched whole since (a new epoch).
                epoch = (anon_sets.get(str(groupId)) or {}).get('epoch', '')
                scanned = groupIdBlockHashMap.get(str(groupId))
                if isinstance(scanned, str):
                    scanned = ['', scanned]  # written before sets had epochs
                lastCheckedHash = (
                    scanned[1] if isinstance(scanned, list)
                    and len(scanned) == 2 and scanned[0] == epoch else None)
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
                    groupIdBlockHashMap[str(groupId)] = [epoch, info['blockHash']]

            if rawCoinsBySetId and not self.syncing:
                self.syncing = True
                util.trigger_callback('wallet_updated', self.wallet)
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

            mempool_rows, mempool_txids = await self._fetch_mempool_spark_rows()
            if mempool_rows:
                found = await run_in_thread(
                    self._identify_sector, mempool_rows, latestGroupId, view_key_hex)
                coins.update(found)

            bad_groups = await self._verify_coin_transactions(
                coins, anon_sets, mempool_txids)
            # A cached row that names the wrong transaction hides the coin,
            # so its group is not kept: the next refresh fetches it again.
            # Once per server is enough. If its own rows are the wrong ones,
            # asking again would only repeat the download on every refresh.
            refetch = bad_groups - self._refetched_groups
            self._refetched_groups |= refetch
            for gid in refetch:
                anon_sets.pop(str(gid), None)
                groups.pop(str(gid), None)
                discarded.add(gid)

            for coin in coins.values():
                spent_txid = used_tags.get(coin['l_tag_hash'])
                if spent_txid:
                    coin['is_used'] = True
                    coin['spent_txid'] = spent_txid

            state['chain_height'] = local_height
            state['chain_tip_hash'] = tip_hash
            state['scan_complete'] = not more_groups_pending and not refetch
            state.pop('groups', None)
            state.pop('used_tags_count', None)
            if not rolled_back:
                self._merge_used_flags(coins)
            shared.discard(sets=discarded, used_tags=tags_restarted)
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

    def is_spark_receiving_address(self, address: str) -> bool:
        address = address.lower()
        if address == self.getCurrentReceivingSparkAddress():
            return True
        return any(entry.get('address') == address and entry.get('subtype', 'receiving') == 'receiving'
                   for entry in self._spark_address_book().values())

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
        # The view key is derived again at each enable and lives in memory
        # only. Earlier versions wrote it to the wallet file: remove that copy.
        self.db.put('spark_view_key_hex', None)
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
        view_key = getattr(self, 'spark_view_key_hex', None)
        if not view_key:
            raise RuntimeError(_('Spark key is not available'))
        return libsparkmobile.get_address_from_full_view_key_hex(
            view_key, diversifier, is_testnet=is_testnet)

    @property
    def sparkChangeAddress(self) -> Optional[str]:
        cached = getattr(self, '_spark_change_address', None)
        if cached:
            return cached
        if self.spark_view_key_hex:
            self._spark_change_address = self._generateSparkAddress(
                libsparkmobile.SPARK_CHANGE_DIVERSIFIER)
            return self._spark_change_address
        return None

    def getCurrentReceivingSparkAddress(self) -> str:
        if not self.spark_view_key_hex:
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

    def estimateSparkSendAll(self, address: str,
                             password=None) -> Tuple[int, int]:
        """Returns what `address` receives, and the fee, when every spendable
        Spark coin is sent to it. Only the fee is estimated: no proof is
        built, so this is quick enough for the GUI thread."""
        if not self.spark_enabled:
            raise RuntimeError(_('Spark is disabled for this session'))
        self._ensure_spark_send_supported()
        coins = self.get_spark_spendable_coins()
        available = sum(int(c['value']) for c in coins)
        if not available:
            raise NotEnoughFunds(_('No spendable Spark coins found'))
        to_spark = libsparkmobile.is_valid_spark_address(
            address, is_testnet=bool(constants.net.TESTNET))
        key_data = self.derive_spark_key(password)
        try:
            fee = self._sparkSendAllFee(
                key_data, available, _ffi_spend_coins(coins),
                privateRecipients=int(to_spark),
                transparentRecipients=int(not to_spark))
        finally:
            del key_data
        if fee >= available:
            raise NotEnoughFunds(_('Insufficient Spark balance'))
        return available - fee, fee

    def _sparkSendAllFee(self, key_data: bytes, amount: int, serializedCoins,
                         *, privateRecipients: int,
                         transparentRecipients: int) -> int:
        return int(_asyncSparkFeesWrapper(
            privateKeyHex=key_data,
            index=self.sparkIndex,
            sendAmount=amount,
            subtractFeeFromAmount=True,
            serializedCoins=serializedCoins,
            privateRecipientsCount=privateRecipients,
            utxoNum=transparentRecipients,
            additionalTxSize=0))

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

                # What this transaction pays as fee: the amount set aside for
                # it, plus a leftover too small to come back as change.
                nFeePaid = nFeeRet
                if nChange > 0:
                    if nChange < dust:
                        nFeePaid += nChange
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
                nFeeNeeded = SimpleConfig.estimate_fee_for_feerate(
                    mintFeeRatePerKB, nBytes + nBytesBuffer)

                if nFeePaid >= nFeeNeeded:
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
                    built_tx._spark_fee = nFeePaid
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
            # The inputs ran out before the amount and its fees were covered.
            raise NotEnoughFunds(_('Insufficient balance to create spark mint(s)'))
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

        selected = utxos if utxos is not None else coins
        coinControl = selected is not None
        availableUtxos = list(selected if coinControl else self.get_spendable_coins(
            domain, nonlocal_only=nonlocal_only))
        if not availableUtxos:
            raise NotEnoughFunds(_('No transparent inputs are available to move to Spark.'))
        for c in availableUtxos:
            self.add_input_info(c)

        if sparkRecipients is None:
            if not spark_address:
                raise ValueError(_('Missing spark recipients.'))
            if amount == '!':
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
        # The send dialogs show only the first transaction, so it carries
        # the totals over all of them.
        first._spark_fee = sum(tx._spark_fee for tx in sparkMints)
        first._spark_send_amount = sum(spark_mint_amount(tx) for tx in sparkMints)
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
        self._ensure_spark_send_supported()
        if not libsparkmobile.is_available():
            raise RuntimeError('electrum_libsparkmobile is not available')
        key_data = self.derive_spark_key(password)
        try:
            return self._prepareSendSpark(key_data, **kwargs)
        finally:
            del key_data

    def _ensure_spark_send_supported(self) -> None:
        activation_height = SPARK_CHAUM_V2_START_HEIGHT.get(
            constants.net.NET_NAME)
        if (activation_height is None
                or self.get_local_height() + 1 < activation_height):
            raise RuntimeError(_(
                'Spark V2 sends are not active on this network yet'))

    def _prepareSendSpark(
            self, key_data: bytes, *,
            recipients: Sequence[Tuple[str, int]] = None,
            sparkRecipients: Sequence[Tuple[str, int, str]] = None,
            address: str = None,
            amount=None,
            memo: str = '') -> PartialTransaction:
        self._ensure_spark_send_supported()
        transparentRecipients = list(recipients or [])
        privateSparkRecipients = list(sparkRecipients or [])
        if address is not None:
            if amount is None:
                raise ValueError(_('Invalid Amount'))
            is_spark_dest = libsparkmobile.is_valid_spark_address(
                address, is_testnet=bool(constants.net.TESTNET))
            if not is_spark_dest and not is_address(address):
                raise ValueError(_('Invalid Address'))
            if is_spark_dest:
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
            estimatedFee = self._sparkSendAllFee(
                key_data, txAmount, serializedCoins,
                privateRecipients=len(privateSparkRecipients),
                transparentRecipients=recipientCount)

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
