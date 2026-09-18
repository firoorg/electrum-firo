"""Tests for the electrum_libsparkmobile C ABI boundary.

Everything crossing this boundary is either wallet key material or data a
remote Electrum server chose. The tests below cover the cases where a bad
input used to be trusted: addresses that decode but cannot receive funds,
buffers whose declared and real sizes disagree, and inputs that make the
native code throw.
"""
import ctypes
import hashlib
import unittest
from ctypes import POINTER, c_char_p, c_int, c_ubyte, c_void_p

from electrum_firo import libsparkmobile, spark_interface

from . import ElectrumTestCase


BECH32M_CHARSET = 'qpzry9x8gf2tvdw0s3jn54khce6mua7l'
BECH32M_CONST = 0x2bc830a3
ADDRESS_ENCODING_PREFIX = b's'
ADDRESS_NETWORK_MAINNET = ord('m')
AES_BLOCKSIZE = 16
GROUP_ELEMENT_SIZE = 34
TEST_KEY = bytes(range(32))


def _bech32_polymod(values):
    generator = [0x3b6a57b2, 0x26508e6d, 0x1ea119fa, 0x3d4233dd, 0x2a1462b3]
    chk = 1
    for value in values:
        top = chk >> 25
        chk = (chk & 0x1ffffff) << 5 ^ value
        for i in range(5):
            chk ^= generator[i] if ((top >> i) & 1) else 0
    return chk


def _bech32_hrp_expand(hrp):
    return [ord(c) >> 5 for c in hrp] + [0] + [ord(c) & 31 for c in hrp]


def _bech32m_encode(hrp, data):
    values = _bech32_hrp_expand(hrp) + data
    polymod = _bech32_polymod(values + [0, 0, 0, 0, 0, 0]) ^ BECH32M_CONST
    checksum = [(polymod >> 5 * (5 - i)) & 31 for i in range(6)]
    return hrp + '1' + ''.join(BECH32M_CHARSET[d] for d in data + checksum)


def _convertbits(data, frombits, tobits, pad=True):
    acc = 0
    bits = 0
    ret = []
    maxv = (1 << tobits) - 1
    for value in data:
        acc = (acc << frombits) | value
        bits += frombits
        while bits >= tobits:
            bits -= tobits
            ret.append((acc >> bits) & maxv)
    if pad and bits:
        ret.append((acc << (tobits - bits)) & maxv)
    return ret


def _f4grumble_encode(network: int, raw: bytes) -> bytes:
    """Python port of spark::F4Grumble::encode (SHA-512 Feistel scramble)."""
    l_M = len(raw)
    l_L = l_M // 2
    l_R = l_M - l_L

    def _xor(x, y):
        return bytes(a ^ b for a, b in zip(x, y))

    def G(i, u):
        return hashlib.sha512(
            b'SPARK_F4GRUMBLE_G' + bytes([network, i]) + u).digest()[:l_R]

    def H(i, u):
        return hashlib.sha512(
            b'SPARK_F4GRUMBLE_H' + bytes([network, i]) + u).digest()[:l_L]

    a, b = raw[:l_M // 2], raw[l_M // 2:]
    x = _xor(b, G(0, a))
    y = _xor(a, H(0, x))
    d = _xor(x, G(1, y))
    c = _xor(y, H(1, d))
    return c + d


def _infinity_address(network: int = ADDRESS_NETWORK_MAINNET) -> str:
    """A checksum-valid Spark address whose Q1 and Q2 are the point at infinity.

    GroupElement serialises as x(32) || oddness || infinity, and its
    deserialiser accepts the infinity flag. Such an address passes decoding,
    but coins minted to it cannot be identified or recovered by anyone.
    """
    infinity_point = bytes(32) + bytes([0, 1])
    raw = bytes(AES_BLOCKSIZE) + infinity_point + infinity_point
    scrambled = _f4grumble_encode(network, raw)
    hrp = (ADDRESS_ENCODING_PREFIX + bytes([network])).decode('ascii')
    return _bech32m_encode(hrp, _convertbits(scrambled, 8, 5, True))


@unittest.skipUnless(libsparkmobile.is_available(),
                     'electrum_libsparkmobile is not built')
class TestLibsparkmobileAddresses(ElectrumTestCase):

    def test_own_address_is_valid(self):
        address = libsparkmobile.get_address(TEST_KEY, 1)
        self.assertTrue(libsparkmobile.is_valid_spark_address(address))
        self.assertFalse(
            libsparkmobile.is_valid_spark_address(address, is_testnet=True))

    def test_infinity_address_is_rejected(self):
        address = _infinity_address()
        self.assertFalse(libsparkmobile.is_valid_spark_address(address))

    def test_malformed_addresses_are_rejected(self):
        address = libsparkmobile.get_address(TEST_KEY, 1)
        for bad in ('', 'not-an-address', address[:-1], address + 'q',
                    address[:10] + address[11:], 's1' + address,
                    address[:20].upper() + address[20:]):
            self.assertFalse(libsparkmobile.is_valid_spark_address(bad), bad)
        self.assertTrue(
            libsparkmobile.is_valid_spark_address(address.upper()))

    def test_full_view_key_roundtrip(self):
        for _ in range(50):
            key_hex = libsparkmobile.get_full_view_key_hex(TEST_KEY)
            self.assertTrue(key_hex)
            address = libsparkmobile.get_address_from_full_view_key_hex(
                key_hex, 1)
            self.assertEqual(libsparkmobile.get_address(TEST_KEY, 1), address)


@unittest.skipUnless(libsparkmobile.is_available(),
                     'electrum_libsparkmobile is not built')
class TestLibsparkmobileAbiGuards(ElectrumTestCase):
    """Calls the exports directly, bypassing the Python-side validation."""

    def setUp(self):
        super().setUp()
        self.lib = libsparkmobile._require()

    def test_hash_tags_requires_exact_tag_length(self):
        self.lib.hashTags.restype = c_void_p
        buf = (c_ubyte * 1).from_buffer_copy(b'\x00')
        self.assertIsNone(self.lib.hashTags(buf, 1, 3))
        self.assertIsNone(self.lib.hashTags(buf, 1, 1))
        self.assertIsNone(self.lib.hashTags(None, 0, 1))
        self.assertIsNone(self.lib.hashTags(buf, 34, -1))

    def test_hash_tags_rejects_wrong_size_from_python(self):
        with self.assertRaises(ValueError):
            libsparkmobile.hash_tags([b'\x00' * 33])
        self.assertEqual([], libsparkmobile.hash_tags([]))

    def test_hash_tag_survives_malformed_coordinates(self):
        self.lib.hashTag.argtypes = [c_char_p, c_char_p]
        self.lib.hashTag.restype = c_void_p
        self.assertIsNone(self.lib.hashTag(b'zzzz', b'zzzz'))
        self.assertIsNone(self.lib.hashTag(None, None))

    def test_mint_context_requires_32_byte_hashes(self):
        arr = (libsparkmobile._TxInputData * 1)()
        buf = (c_ubyte * 4).from_buffer_copy(b'\x01\x02\x03\x04')
        arr[0].txHash = ctypes.cast(buf, POINTER(c_ubyte))
        arr[0].txHashLength = 4
        arr[0].vout = 0
        self.assertFalse(self.lib.serializeMintContext(arr, 1))
        self.assertFalse(self.lib.serializeMintContext(None, 1))
        self.assertFalse(self.lib.serializeMintContext(arr, 0))

    def test_mint_context_is_stable(self):
        txid = bytes(range(32))
        first = libsparkmobile.serialize_mint_context([(txid, 0)])
        second = libsparkmobile.serialize_mint_context([(txid, 0)])
        self.assertEqual(first, second)
        other = libsparkmobile.serialize_mint_context(
            [(txid, 0), (bytes(32), 1)])
        reordered = libsparkmobile.serialize_mint_context(
            [(bytes(32), 1), (txid, 0)])
        self.assertNotEqual(other, reordered)

    def test_key_taking_exports_check_their_length(self):
        self.lib.getAddress.restype = c_void_p
        short = (c_ubyte * 16).from_buffer_copy(bytes(16))
        self.assertIsNone(self.lib.getAddress(short, 16, 1, 1, 0))
        self.assertIsNone(self.lib.getAddress(None, 32, 1, 1, 0))
        self.lib.getFullViewKeyFromPrivateKeyData.restype = c_void_p
        self.assertIsNone(
            self.lib.getFullViewKeyFromPrivateKeyData(short, 16, 1))

    def test_mint_recipients_reject_foreign_network_and_infinity(self):
        address = libsparkmobile.get_address(TEST_KEY, 1)
        with self.assertRaises(RuntimeError):
            libsparkmobile.create_spark_mint_recipients(
                [(address, 100000, '')], generate=False, is_testnet=True)
        with self.assertRaises(RuntimeError):
            libsparkmobile.create_spark_mint_recipients(
                [(_infinity_address(), 100000, '')], generate=False)


@unittest.skipUnless(libsparkmobile.is_available(),
                     'electrum_libsparkmobile is not built')
class TestMintSerialContextGuard(ElectrumTestCase):
    """Mint coins are bound to the exact input list of their transaction.

    Firo Core rebuilds the serial context from the confirmed transaction's vin
    order and sequences. If Electrum ever hands out a mint whose inputs were
    reordered or resequenced after the context was built, the transaction can
    still confirm while the coins become unrecoverable.
    """

    @staticmethod
    def _input(first_byte: int, out_idx: int):
        from electrum_firo.transaction import PartialTxInput, TxOutpoint
        txin = PartialTxInput(
            prevout=TxOutpoint(txid=bytes([first_byte]) * 32, out_idx=out_idx))
        txin.nsequence = spark_interface.MINT_INPUT_SEQUENCE
        return txin

    @staticmethod
    def _tx(inputs):
        from electrum_firo.transaction import PartialTransaction
        tx = PartialTransaction()
        tx._inputs = list(inputs)
        tx._outputs = []
        return tx

    def setUp(self):
        super().setUp()
        self.inputs = [self._input(1, 0), self._input(2, 1)]
        self.context = libsparkmobile.serialize_mint_context(
            [(i.prevout.txid[::-1], i.prevout.out_idx) for i in self.inputs])

    def test_matching_transaction_is_accepted(self):
        spark_interface._verify_mint_serial_context(
            self._tx(self.inputs), self.context)

    def test_reordered_inputs_are_rejected(self):
        with self.assertRaises(RuntimeError):
            spark_interface._verify_mint_serial_context(
                self._tx(reversed(self.inputs)), self.context)

    def test_changed_sequence_is_rejected(self):
        inputs = [self._input(1, 0), self._input(2, 1)]
        inputs[0].nsequence = 0xffffffff
        with self.assertRaises(RuntimeError):
            spark_interface._verify_mint_serial_context(
                self._tx(inputs), self.context)


@unittest.skipUnless(libsparkmobile.is_available(),
                     'electrum_libsparkmobile is not built')
class TestSpendV2AndNumericGuards(ElectrumTestCase):
    """Post-H2 contract: V2 only, 32-byte commitment, sane amounts."""

    def test_negative_fee_estimate_is_rejected(self):
        for amount in (-1, 21_000_001 * 100_000_000):
            with self.assertRaises(RuntimeError):
                libsparkmobile.estimate_spark_fee(
                    TEST_KEY, send_amount=amount,
                    subtract_fee_from_amount=False, coins=[],
                    private_recipients_count=1)

    def test_mint_amounts_out_of_range_are_rejected(self):
        address = libsparkmobile.get_address(TEST_KEY, 1)
        for value in (0, 2 ** 64 - 1, 21_000_001 * 100_000_000):
            with self.assertRaises(RuntimeError):
                libsparkmobile.create_spark_mint_recipients(
                    [(address, value, '')], generate=False)

    def test_negative_vout_is_rejected(self):
        arr = (libsparkmobile._TxInputData * 1)()
        buf = (c_ubyte * 32).from_buffer_copy(bytes(32))
        arr[0].txHash = ctypes.cast(buf, POINTER(c_ubyte))
        arr[0].txHashLength = 32
        arr[0].vout = -1
        self.assertFalse(libsparkmobile._require().serializeMintContext(arr, 1))

    def test_oversized_coin_blobs_are_rejected(self):
        view_key = libsparkmobile.create_full_view_key(TEST_KEY)
        try:
            self.assertIsNone(libsparkmobile.identify_and_recover_coin(
                b'\x00' * (64 * 1024 + 1), b'', view_key))
            self.assertIsNone(libsparkmobile.identify_and_recover_coin(
                b'', b'', view_key))
        finally:
            libsparkmobile.delete_full_view_key(view_key)


class TestSparkSpendTxType(ElectrumTestCase):
    """After the H2 fork the wallet must emit type 11, and read both."""

    def test_v2_type_is_registered(self):
        from electrum_firo import dash_tx
        self.assertEqual(11, dash_tx.SPARK_SPEND_V2)
        self.assertEqual((9, 11), dash_tx.SPARK_SPEND_TYPES)
        for tx_type in dash_tx.SPARK_SPEND_TYPES:
            self.assertIn(tx_type, dash_tx.SPEC_TX_HANDLERS)
            self.assertEqual('SparkSpend', dash_tx.SPEC_TX_NAMES[tx_type])

    def test_wallet_builds_v2_spends(self):
        self.assertEqual(spark_interface.SPARK_SPEND_V2, 11)
