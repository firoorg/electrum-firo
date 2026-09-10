import os
import sys
import base64
import ctypes
from ctypes import (
    POINTER, Structure, c_char, c_char_p, c_int, c_int64, c_ubyte, c_uint64,
    c_void_p,
)
from typing import Dict, List, Optional, Sequence, Tuple

from .logging import get_logger


_logger = get_logger(__name__)

SPARK_KEY_INDEX = 1
# Spends carry a 32-byte extension commitment. Plain spends commit to nothing
# and pass zeros; a Spark Name spend passes its computed commitment.

BIP44_SPARK_CHAIN = 6
SPARK_CHANGE_DIVERSIFIER = 0x270F
OP_SPARKMINT = 0xd1
OP_SPARKSMINT = 0xd2
OP_SPARKSPEND = 0xd3


class _ValidAddr(Structure):
    _fields_ = [('isValid', c_int), ('errorMessage', c_void_p)]


class _CMintedCoinData(Structure):
    _fields_ = [('address', c_char_p), ('value', c_uint64), ('memo', c_char_p)]


class _CCRecipient(Structure):
    _fields_ = [
        ('pubKey', POINTER(c_ubyte)), ('pubKeyLength', c_int),
        ('cAmount', c_uint64), ('subtractFee', c_int),
    ]


class _CCRecipientList(Structure):
    _fields_ = [('list', POINTER(_CCRecipient)), ('length', c_int)]


class _TxInputData(Structure):
    _fields_ = [
        ('txHash', POINTER(c_ubyte)), ('txHashLength', c_int), ('vout', c_int),
    ]


class _SerializedMintContextResult(Structure):
    _fields_ = [('context', POINTER(c_ubyte)), ('contextLength', c_int)]


class _AggregateCoinData(Structure):
    _fields_ = [
        ('type', c_char),
        ('diversifier', c_uint64),
        ('value', c_uint64),
        ('address', c_void_p),
        ('memo', c_void_p),
        ('lTagHash', c_void_p),
        ('encryptedDiversifier', POINTER(c_ubyte)),
        ('encryptedDiversifierLength', c_int),
        ('serial', POINTER(c_ubyte)),
        ('serialLength', c_int),
        ('nonceHex', c_void_p),
        ('nonceHexLength', c_int),
    ]


class _CCDataStream(Structure):
    _fields_ = [('data', POINTER(c_ubyte)), ('length', c_int)]


class _CRecip(Structure):
    _fields_ = [('amount', c_uint64), ('subtractFee', c_int)]


class _COutputCoinData(Structure):
    _fields_ = [
        ('address', c_char_p), ('addressLength', c_int),
        ('value', c_uint64), ('memo', c_char_p), ('memoLength', c_int),
    ]


class _COutputRecipient(Structure):
    _fields_ = [('output', POINTER(_COutputCoinData)), ('subtractFee', c_int)]


class _CCoverSetData(Structure):
    _fields_ = [
        ('cover_set', POINTER(_CCDataStream)), ('cover_setLength', c_int),
        ('cover_set_representation', POINTER(c_ubyte)),
        ('cover_set_representationLength', c_int),
        ('setId', c_int),
    ]


class _BlockHashAndId(Structure):
    _fields_ = [
        ('hash', POINTER(c_ubyte)), ('hashLength', c_int), ('id', c_int),
    ]


class _SpendCoinData(Structure):
    _fields_ = [
        ('serializedCoin', POINTER(_CCDataStream)),
        ('serializedCoinContext', POINTER(_CCDataStream)),
        ('groupId', c_int), ('height', c_int),
    ]


class _OutputScript(Structure):
    _fields_ = [('bytes', POINTER(c_ubyte)), ('length', c_int)]


class _UsedCoin(Structure):
    _fields_ = [
        ('serializedCoin', POINTER(_CCDataStream)),
        ('serializedCoinContext', POINTER(_CCDataStream)),
        ('groupId', c_int), ('height', c_int),
    ]


class _SparkSpendTransactionResult(Structure):
    _fields_ = [
        ('data', POINTER(c_ubyte)), ('dataLength', c_int),
        ('outputScripts', POINTER(_OutputScript)),
        ('outputScriptsLength', c_int),
        ('usedCoins', POINTER(_UsedCoin)), ('usedCoinsLength', c_int),
        ('fee', c_int64), ('isError', c_int),
    ]


class _SparkFeeResult(Structure):
    _fields_ = [('error', c_void_p), ('fee', c_int64)]


def _load():
    pkg = os.path.dirname(__file__)
    if sys.platform == 'darwin':
        names = ('libelectrum_libsparkmobile.dylib',)
    elif sys.platform in ('windows', 'win32'):
        names = ('libelectrum_libsparkmobile.dll', 'electrum_libsparkmobile.dll')
    else:
        names = ('libelectrum_libsparkmobile.so', 'libelectrum_libsparkmobile.so.0')
    errs = []
    for name in names:
        for path in (os.path.join(pkg, name), name):
            try:
                lib = ctypes.cdll.LoadLibrary(path)
            except BaseException as e:
                errs.append(e)
                continue
            lib.getAddress.argtypes = [
                POINTER(c_ubyte), c_int, c_int, c_int, c_int]
            lib.getAddress.restype = c_void_p
            lib.isValidSparkAddress.argtypes = [c_char_p, c_int]
            lib.isValidSparkAddress.restype = POINTER(_ValidAddr)
            lib.serializeMintContext.argtypes = [POINTER(_TxInputData), c_int]
            lib.serializeMintContext.restype = POINTER(_SerializedMintContextResult)
            lib.cCreateSparkMintRecipients.argtypes = [
                POINTER(_CMintedCoinData), c_int, POINTER(c_ubyte), c_int,
                c_int, c_int]
            lib.cCreateSparkMintRecipients.restype = POINTER(_CCRecipientList)
            lib.getFullViewKeyFromPrivateKeyData.argtypes = [
                POINTER(c_ubyte), c_int, c_int]
            lib.getFullViewKeyFromPrivateKeyData.restype = c_void_p
            lib.deserializeFullViewKey.argtypes = [POINTER(c_ubyte), c_int]
            lib.deserializeFullViewKey.restype = c_void_p
            lib.serializeFullViewKey.argtypes = [c_void_p, POINTER(c_int)]
            lib.serializeFullViewKey.restype = POINTER(c_ubyte)
            lib.getAddressFromFullViewKey.argtypes = [
                c_void_p, c_int, c_int, c_int]
            lib.getAddressFromFullViewKey.restype = c_void_p
            lib.deleteFullViewKey.argtypes = [c_void_p]
            lib.deleteFullViewKey.restype = None
            lib.idAndRecoverCoinByFullViewKey.argtypes = [
                POINTER(c_ubyte), c_int, c_void_p,
                POINTER(c_ubyte), c_int, c_int]
            lib.idAndRecoverCoinByFullViewKey.restype = POINTER(
                _AggregateCoinData)
            lib.hashTags.argtypes = [POINTER(c_ubyte), c_int, c_int]
            lib.hashTags.restype = c_void_p
            lib.estimateSparkFee.argtypes = [
                POINTER(c_ubyte), c_int, c_int, c_int64, c_int,
                POINTER(_SpendCoinData), c_int, c_int, c_int, c_int]
            lib.estimateSparkFee.restype = POINTER(_SparkFeeResult)
            lib.cCreateSparkSpendTransaction.argtypes = [
                POINTER(c_ubyte), c_int, c_int,
                POINTER(_CRecip), c_int,
                POINTER(_COutputRecipient), c_int,
                POINTER(_SpendCoinData), c_int,
                POINTER(_CCoverSetData), c_int,
                POINTER(_BlockHashAndId), c_int,
                POINTER(c_ubyte), c_int, c_int, c_int]
            lib.cCreateSparkSpendTransaction.restype = POINTER(
                _SparkSpendTransactionResult)
            lib.native_free.argtypes = [c_void_p]
            lib.native_free.restype = None
            return lib
    _logger.info(f'electrum_libsparkmobile not loaded: {errs!r}')
    return None


try:
    _lib = _load()
except BaseException as e:
    _logger.error(f'failed to load electrum_libsparkmobile: {e!r}')
    _lib = None


def is_available() -> bool:
    return _lib is not None


def _require():
    if not _lib:
        raise RuntimeError('electrum_libsparkmobile is not available')
    return _lib


def get_address(key_data: bytes, diversifier: int = 1, *,
                index: int = SPARK_KEY_INDEX, is_testnet: bool = False) -> str:
    lib = _require()
    if len(key_data) != 32:
        raise ValueError('key_data must be 32 bytes')
    buf = (c_ubyte * 32).from_buffer_copy(key_data)
    ptr = lib.getAddress(buf, len(key_data), index, diversifier,
                         int(is_testnet))
    if not ptr:
        raise RuntimeError('getAddress failed')
    try:
        return ctypes.cast(ptr, c_char_p).value.decode('ascii')
    finally:
        lib.native_free(ptr)


def is_valid_spark_address(address: str, *, is_testnet: bool = False) -> bool:
    lib = _require()
    try:
        encoded = address.encode('ascii')
    except (AttributeError, UnicodeEncodeError):
        return False
    res = lib.isValidSparkAddress(encoded, int(is_testnet))
    if not res:
        return False
    try:
        if res.contents.errorMessage:
            lib.native_free(res.contents.errorMessage)
        return bool(res.contents.isValid)
    finally:
        lib.native_free(res)


def create_full_view_key(key_data: bytes, *,
                         index: int = SPARK_KEY_INDEX) -> c_void_p:
    if len(key_data) != 32:
        raise ValueError('key_data must be 32 bytes')
    key = (c_ubyte * 32).from_buffer_copy(key_data)
    result = _require().getFullViewKeyFromPrivateKeyData(key, 32, index)
    if not result:
        raise RuntimeError('getFullViewKeyFromPrivateKeyData failed')
    return result


def delete_full_view_key(full_view_key: c_void_p) -> None:
    _require().deleteFullViewKey(full_view_key)


def deserialize_full_view_key(full_view_key_hex: str) -> c_void_p:
    raw = bytes.fromhex(full_view_key_hex)
    buf = (c_ubyte * len(raw)).from_buffer_copy(raw)
    result = _require().deserializeFullViewKey(buf, len(raw))
    if not result:
        raise RuntimeError('deserializeFullViewKey failed')
    return result


def serialize_full_view_key(full_view_key: c_void_p) -> bytes:
    lib = _require()
    size = c_int()
    ptr = lib.serializeFullViewKey(full_view_key, ctypes.byref(size))
    if not ptr:
        raise RuntimeError('serializeFullViewKey failed')
    try:
        return ctypes.string_at(ptr, size.value)
    finally:
        lib.native_free(ptr)


def get_full_view_key_hex(key_data: bytes, *,
                          index: int = SPARK_KEY_INDEX) -> str:
    view_key = create_full_view_key(key_data, index=index)
    try:
        return serialize_full_view_key(view_key).hex()
    finally:
        delete_full_view_key(view_key)


def get_address_from_full_view_key_hex(
        full_view_key_hex: str, diversifier: int, *,
        index: int = SPARK_KEY_INDEX, is_testnet: bool = False) -> str:
    lib = _require()
    view_key = deserialize_full_view_key(full_view_key_hex)
    try:
        ptr = lib.getAddressFromFullViewKey(
            view_key, index, diversifier, int(is_testnet))
        if not ptr:
            raise RuntimeError('getAddressFromFullViewKey failed')
        try:
            return ctypes.cast(ptr, c_char_p).value.decode('ascii')
        finally:
            lib.native_free(ptr)
    finally:
        delete_full_view_key(view_key)


def identify_and_recover_coin(
        serialized_coin: bytes, context: bytes, full_view_key: c_void_p, *,
        is_testnet: bool = False) -> Optional[Dict]:
    lib = _require()
    coin_buf = (c_ubyte * len(serialized_coin)).from_buffer_copy(serialized_coin)
    context_buf = (c_ubyte * len(context)).from_buffer_copy(context)
    result = lib.idAndRecoverCoinByFullViewKey(
        coin_buf, len(serialized_coin), full_view_key,
        context_buf, len(context), int(is_testnet))
    if not result:
        return None
    data = result.contents
    try:
        return {
            'type': ord(data.type),
            'diversifier': int(data.diversifier),
            'value': int(data.value),
            'address': ctypes.string_at(data.address).decode('ascii'),
            'memo': ctypes.string_at(data.memo).decode('utf-8'),
            'l_tag_hash': ctypes.string_at(data.lTagHash).decode('ascii'),
            'encrypted_diversifier': ctypes.string_at(
                data.encryptedDiversifier,
                data.encryptedDiversifierLength).hex(),
            'serial': ctypes.string_at(
                data.serial, data.serialLength).decode('ascii'),
            'nonce': ctypes.string_at(
                data.nonceHex, data.nonceHexLength).decode('ascii'),
        }
    finally:
        for ptr in (
                data.address, data.memo, data.lTagHash,
                data.encryptedDiversifier, data.serial, data.nonceHex):
            if ptr:
                lib.native_free(ptr)
        lib.native_free(result)


def hash_tags(tags: Sequence[bytes]) -> List[str]:
    if not tags:
        return []
    if any(len(tag) != 34 for tag in tags):
        raise ValueError('each Spark tag must be 34 bytes')
    lib = _require()
    raw = b''.join(tags)
    buf = (c_ubyte * len(raw)).from_buffer_copy(raw)
    result = lib.hashTags(buf, len(raw), len(tags))
    if not result:
        raise RuntimeError('hashTags failed')
    try:
        hashes = ctypes.string_at(result, 64 * len(tags)).decode('ascii')
        return [hashes[i:i + 64] for i in range(0, len(hashes), 64)]
    finally:
        lib.native_free(result)


def _as_hash_bytes(value, name: str) -> bytes:
    """Normalise a 32-byte hash that may arrive as bytes, hex or base64.

    The native ABI reads exactly 32 bytes, so the length is checked here
    instead of being inferred on the other side of the FFI boundary.
    """
    if isinstance(value, str):
        text = ''.join(value.split())
        try:
            if len(text) == 64:
                value = bytes.fromhex(text)
            else:
                value = base64.b64decode(text, validate=True)
        except Exception:
            raise ValueError(f'{name} is not valid hex or base64')
    value = bytes(value)
    if len(value) != 32:
        raise ValueError(f'{name} must be 32 bytes')
    return value


def _make_data_stream(raw: bytes, keep: list) -> _CCDataStream:
    buf = (c_ubyte * len(raw)).from_buffer_copy(raw)
    keep.append(buf)
    stream = _CCDataStream()
    stream.data = ctypes.cast(buf, POINTER(c_ubyte))
    stream.length = len(raw)
    return stream


def _make_spend_coins(
        coins: Sequence[Dict], keep: list) -> POINTER(_SpendCoinData):
    n = len(coins)
    arr = (_SpendCoinData * n)()
    for i, coin in enumerate(coins):
        serialized = coin.get('serialized_coin') or coin.get('serializedCoin')
        context = (coin.get('serialized_coin_context')
                   or coin.get('context')
                   or coin.get('serial_context'))
        if isinstance(serialized, str):
            serialized = base64.b64decode(''.join(serialized.splitlines()))
        if isinstance(context, str):
            context = base64.b64decode(''.join(context.splitlines()))
        coin_stream = _make_data_stream(serialized, keep)
        ctx_stream = _make_data_stream(context, keep)
        keep.extend((coin_stream, ctx_stream))
        arr[i].serializedCoin = ctypes.pointer(coin_stream)
        arr[i].serializedCoinContext = ctypes.pointer(ctx_stream)
        arr[i].groupId = int(coin['group_id'])
        arr[i].height = int(coin['height'])
    keep.append(arr)
    return ctypes.cast(arr, POINTER(_SpendCoinData))


def estimate_spark_fee(
        key_data: bytes, *,
        send_amount: int,
        subtract_fee_from_amount: bool,
        coins: Sequence[Dict],
        private_recipients_count: int,
        utxo_num: int = 0,
        additional_tx_size: int = 0,
        index: int = SPARK_KEY_INDEX) -> int:
    lib = _require()
    if len(key_data) != 32:
        raise ValueError('key_data must be 32 bytes')
    keep = []
    key = (c_ubyte * 32).from_buffer_copy(key_data)
    coin_arr = _make_spend_coins(coins, keep)
    res = lib.estimateSparkFee(
        key, 32, index, int(send_amount), int(subtract_fee_from_amount),
        coin_arr, len(coins), int(private_recipients_count),
        int(utxo_num), int(additional_tx_size))
    if not res:
        raise RuntimeError('estimateSparkFee failed')
    try:
        if res.contents.error:
            msg = ctypes.string_at(res.contents.error).decode('utf-8', 'replace')
            lib.native_free(res.contents.error)
            raise RuntimeError(msg)
        return int(res.contents.fee)
    finally:
        lib.native_free(res)


def create_spark_spend_transaction(
        key_data: bytes, *,
        recipients: Sequence[Tuple[int, bool]],
        private_recipients: Sequence[Tuple[str, int, str, bool]],
        coins: Sequence[Dict],
        anonymity_sets: Sequence[Dict],
        id_and_block_hashes: Sequence[Tuple[int, bytes]],
        tx_hash: bytes,
        additional_tx_size: int = 0,
        index: int = SPARK_KEY_INDEX,
        is_testnet: bool = False) -> Dict:
    lib = _require()
    if len(key_data) != 32:
        raise ValueError('key_data must be 32 bytes')
    tx_hash = _as_hash_bytes(tx_hash, 'tx_hash')
    keep = []
    key = (c_ubyte * 32).from_buffer_copy(key_data)

    recip_n = len(recipients)
    recip_arr = (_CRecip * max(recip_n, 1))()
    for i, (amount, subtract) in enumerate(recipients):
        recip_arr[i].amount = int(amount)
        recip_arr[i].subtractFee = int(subtract)
    keep.append(recip_arr)

    priv_n = len(private_recipients)
    priv_arr = (_COutputRecipient * max(priv_n, 1))()
    outputs = (_COutputCoinData * max(priv_n, 1))()
    for i, (address, value, memo, subtract) in enumerate(private_recipients):
        addr_b = address.encode('ascii')
        memo_b = (memo or '').encode('utf-8')
        outputs[i].address = addr_b
        outputs[i].addressLength = len(addr_b)
        outputs[i].value = int(value)
        outputs[i].memo = memo_b
        outputs[i].memoLength = len(memo_b)
        keep.extend((addr_b, memo_b))
        priv_arr[i].output = ctypes.pointer(outputs[i])
        priv_arr[i].subtractFee = int(subtract)
    keep.extend((outputs, priv_arr))

    coin_arr = _make_spend_coins(coins, keep)

    set_n = len(anonymity_sets)
    cover_arr = (_CCoverSetData * max(set_n, 1))()
    for i, aset in enumerate(anonymity_sets):
        set_coins = aset['coins']
        streams = (_CCDataStream * max(len(set_coins), 1))()
        for j, serialized in enumerate(set_coins):
            if isinstance(serialized, str):
                serialized = base64.b64decode(''.join(serialized.splitlines()))
            streams[j] = _make_data_stream(serialized, keep)
        keep.append(streams)
        cover_arr[i].cover_set = ctypes.cast(streams, POINTER(_CCDataStream))
        cover_arr[i].cover_setLength = len(set_coins)
        set_hash = aset['set_hash']
        if isinstance(set_hash, str):
            set_hash = base64.b64decode(''.join(set_hash.splitlines()))
        hash_buf = (c_ubyte * len(set_hash)).from_buffer_copy(set_hash)
        keep.append(hash_buf)
        cover_arr[i].cover_set_representation = ctypes.cast(
            hash_buf, POINTER(c_ubyte))
        cover_arr[i].cover_set_representationLength = len(set_hash)
        cover_arr[i].setId = int(aset['set_id'])
    keep.append(cover_arr)

    id_n = len(id_and_block_hashes)
    id_arr = (_BlockHashAndId * max(id_n, 1))()
    for i, (set_id, block_hash) in enumerate(id_and_block_hashes):
        buf = (c_ubyte * 32).from_buffer_copy(
            _as_hash_bytes(block_hash, 'block_hash'))
        keep.append(buf)
        id_arr[i].hash = ctypes.cast(buf, POINTER(c_ubyte))
        id_arr[i].hashLength = 32
        id_arr[i].id = int(set_id)
    keep.append(id_arr)

    tx_buf = (c_ubyte * 32).from_buffer_copy(tx_hash)
    res = lib.cCreateSparkSpendTransaction(
        key, 32, index,
        recip_arr if recip_n else None, recip_n,
        priv_arr if priv_n else None, priv_n,
        coin_arr, len(coins),
        cover_arr if set_n else None, set_n,
        id_arr if id_n else None, id_n,
        tx_buf, 32, int(additional_tx_size), int(is_testnet))
    if not res:
        raise RuntimeError('cCreateSparkSpendTransaction failed')
    try:
        if res.contents.data and res.contents.dataLength > 0:
            data = ctypes.string_at(
                res.contents.data, res.contents.dataLength)
        else:
            data = b''
        if res.contents.isError:
            raise RuntimeError(
                data.decode('utf-8', 'replace')
                or 'cCreateSparkSpendTransaction failed')
        scripts = []
        for i in range(res.contents.outputScriptsLength):
            s = res.contents.outputScripts[i]
            scripts.append(ctypes.string_at(s.bytes, s.length))
            lib.native_free(s.bytes)
        used = []
        for i in range(res.contents.usedCoinsLength):
            u = res.contents.usedCoins[i]
            used.append({
                'serialized_coin': ctypes.string_at(
                    u.serializedCoin.contents.data,
                    u.serializedCoin.contents.length),
                'serialized_coin_context': ctypes.string_at(
                    u.serializedCoinContext.contents.data,
                    u.serializedCoinContext.contents.length),
                'group_id': int(u.groupId),
                'height': int(u.height),
            })
            lib.native_free(u.serializedCoin.contents.data)
            lib.native_free(u.serializedCoin)
            lib.native_free(u.serializedCoinContext.contents.data)
            lib.native_free(u.serializedCoinContext)
        if res.contents.outputScripts:
            lib.native_free(res.contents.outputScripts)
        if res.contents.usedCoins:
            lib.native_free(res.contents.usedCoins)
        return {
            'payload': data,
            'output_scripts': scripts,
            'fee': int(res.contents.fee),
            'used_coins': used,
        }
    finally:
        if res.contents.data:
            lib.native_free(res.contents.data)
        lib.native_free(res)


def serialize_mint_context(inputs: Sequence[Tuple[bytes, int]]) -> bytes:

    lib = _require()
    n = len(inputs)
    if not n:
        raise ValueError('no inputs')
    arr = (_TxInputData * n)()
    keep = []
    for i, (txid, vout) in enumerate(inputs):
        if len(txid) != 32:
            raise ValueError('txid must be 32 bytes')
        buf = (c_ubyte * 32).from_buffer_copy(txid)
        keep.append(buf)
        arr[i].txHash = ctypes.cast(buf, POINTER(c_ubyte))
        arr[i].txHashLength = 32
        arr[i].vout = int(vout)
    res = lib.serializeMintContext(arr, n)
    if not res or not res.contents.context:
        raise RuntimeError('serializeMintContext failed')
    try:
        return ctypes.string_at(res.contents.context, res.contents.contextLength)
    finally:
        lib.native_free(res.contents.context)
        lib.native_free(res)


def create_spark_mint_recipients(
        outputs: Sequence[Tuple[str, int, str]],
        serial_context: bytes = b'',
        *,
        generate: bool = False,
        is_testnet: bool = False) -> List[Tuple[bytes, int]]:
    lib = _require()
    n = len(outputs)
    if not n:
        raise ValueError('no outputs')
    arr = (_CMintedCoinData * n)()
    for i, (address, value, memo) in enumerate(outputs):
        arr[i].address = address.encode('ascii')
        arr[i].value = int(value)
        arr[i].memo = (memo or '').encode('utf-8')
    if serial_context:
        ctx = (c_ubyte * len(serial_context)).from_buffer_copy(serial_context)
        ctx_ptr, ctx_len = ctypes.cast(ctx, POINTER(c_ubyte)), len(serial_context)
    else:
        ctx_ptr, ctx_len = None, 0
    res = lib.cCreateSparkMintRecipients(
        arr, n, ctx_ptr, ctx_len, int(generate), int(is_testnet))
    if not res:
        raise RuntimeError('cCreateSparkMintRecipients failed')
    try:
        out = []
        for i in range(res.contents.length):
            r = res.contents.list[i]
            out.append((ctypes.string_at(r.pubKey, r.pubKeyLength), int(r.cAmount)))
            lib.native_free(r.pubKey)
        if res.contents.list:
            lib.native_free(res.contents.list)
        return out
    finally:
        lib.native_free(res)
