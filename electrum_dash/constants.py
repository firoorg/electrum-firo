# -*- coding: utf-8 -*-
#
# Electrum - lightweight Bitcoin client
# Copyright (C) 2018 The Electrum developers
#
# Permission is hereby granted, free of charge, to any person
# obtaining a copy of this software and associated documentation files
# (the "Software"), to deal in the Software without restriction,
# including without limitation the rights to use, copy, modify, merge,
# publish, distribute, sublicense, and/or sell copies of the Software,
# and to permit persons to whom the Software is furnished to do so,
# subject to the following conditions:
#
# The above copyright notice and this permission notice shall be
# included in all copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND,
# EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF
# MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND
# NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT HOLDERS
# BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER IN AN
# ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN
# CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.

import os
import gzip
import json

from . import coins
from .logging import get_logger
from .util import inv_dict, all_subclasses
from . import bitcoin


_logger = get_logger(__name__)


def read_json(filename, default):
    path = os.path.join(os.path.dirname(__file__), filename)
    try:
        with open(path, 'r') as f:
            r = json.loads(f.read())
    except:
        r = default
    return r


def read_json_gz(filename, default):
    path = os.path.join(os.path.dirname(__file__), filename)
    try:
        with gzip.open(path, 'rb') as f:
            data = f.read()
            r = json.loads(data.decode('utf-8'))
    except:
        _logger.info(f'file not found: {filename}')
        r = default
    return r


GIT_REPO_URL = "https://github.com/firoorg/electrum-firo/"
GIT_REPO_ISSUES_URL = "https://github.com/firoorg/electrum-firo/issues"

BIP39_WALLET_FORMATS = read_json('bip39_wallet_formats.json', [])


CHUNK_SIZE = 2016


class AbstractNet:

    NET_NAME: str
    TESTNET: bool
    WIF_PREFIX: int
    ADDRTYPE_P2PKH: int
    ADDRTYPE_P2SH: int
    GENESIS: str
    BIP44_COIN_TYPE: int
    ADDRTYPE_EXP2PKH: int

    @classmethod
    def max_checkpoint(cls) -> int:
        return max(0, len(cls.CHECKPOINTS) * CHUNK_SIZE - 1)

    @classmethod
    def rev_genesis_bytes(cls) -> bytes:
        return bytes.fromhex(bitcoin.rev_hex(cls.GENESIS))


class BitcoinMainnet(AbstractNet):

    NET_NAME = "mainnet"
    TESTNET = False
    WIF_PREFIX = 210
    ADDRTYPE_P2PKH = 82  # 0x52
    ADDRTYPE_EXP2PKH = 185 # 0xb9
    ADDRTYPE_P2SH = 7  # 0x07
    SEGWIT_HRP = "xzc"
    GENESIS = "4381deb85b1b2c9843c222944b616d997516dcbd6a964e1eaf0def0830695233"
    DEFAULT_PORTS = {'t': '50001', 's': '50002'}
    DEFAULT_SERVERS = read_json('servers.json', {})
    # CHECKPOINTS = read_json_gz('checkpoints.json.gz', [])
    CHECKPOINTS = []

    XPRV_HEADERS = {
        'standard':    0x0488ade4,  # xprv
        'p2wpkh-p2sh': 0x049d7878,  # yprv
        'p2wsh-p2sh':  0x0295b005,  # Yprv
        'p2wpkh':      0x04b2430c,  # zprv
        'p2wsh':       0x02aa7a99,  # Zprv
    }
    XPRV_HEADERS_INV = inv_dict(XPRV_HEADERS)
    XPUB_HEADERS = {
        'standard':    0x0488b21e,  # xpub
        'p2wpkh-p2sh': 0x049d7cb2,  # ypub
        'p2wsh-p2sh':  0x0295b43f,  # Ypub
        'p2wpkh':      0x04b24746,  # zpub
        'p2wsh':       0x02aa7ed3,  # Zpub
    }
    BIP44_COIN_TYPE = 136
    COIN = coins.Firo()
    XPUB_HEADERS_INV = inv_dict(XPUB_HEADERS)
    DIP3_ACTIVATION_HEIGHT = 5000
    # DRKV_HEADER = 0x02fe52f8  # drkv
    # DRKP_HEADER = 0x02fe52cc  # drkp


class BitcoinTestnet(AbstractNet):

    NET_NAME = "testnet"
    TESTNET = True
    WIF_PREFIX = 185
    ADDRTYPE_P2PKH = 65
    ADDRTYPE_EXP2PKH = 185 # 0xb9
    ADDRTYPE_P2SH = 178
    SEGWIT_HRP = "txzc"
    GENESIS = "aa22adcc12becaf436027ffe62a8fb21b234c58c23865291e5dc52cf53f64fca"
    DEFAULT_PORTS = {'t': '51001', 's': '51002'}
    DEFAULT_SERVERS = read_json('servers_testnet.json', {})
    # CHECKPOINTS = read_json_gz('checkpoints_testnet.json.gz', [])
    CHECKPOINTS = []

    XPRV_HEADERS = {
        'standard':    0x04358394,  # tprv
        'p2wpkh-p2sh': 0x044a4e28,  # uprv
        'p2wsh-p2sh':  0x024285b5,  # Uprv
        'p2wpkh':      0x045f18bc,  # vprv
        'p2wsh':       0x02575048,  # Vprv
    }
    XPRV_HEADERS_INV = inv_dict(XPRV_HEADERS)
    XPUB_HEADERS = {
        'standard':    0x043587cf,  # tpub
        'p2wpkh-p2sh': 0x044a5262,  # upub
        'p2wsh-p2sh':  0x024289ef,  # Upub
        'p2wpkh':      0x045f1cf6,  # vpub
        'p2wsh':       0x02575483,  # Vpub
    }
    XPUB_HEADERS_INV = inv_dict(XPUB_HEADERS)
    # DRKV_HEADER = 0x3a8061a0  # DRKV
    # DRKP_HEADER = 0x3a805837  # DRKP
    BIP44_COIN_TYPE = 1
    COIN = coins.FiroTestnet()
    DIP3_ACTIVATION_HEIGHT = 5000


class BitcoinRegtest(AbstractNet):

    NET_NAME = "regtest"
    TESTNET = False
    WIF_PREFIX = 239
    ADDRTYPE_P2PKH = 65
    ADDRTYPE_EXP2PKH = 185 # 0xb9
    ADDRTYPE_P2SH = 178
    SEGWIT_HRP = "txzc" 
    GENESIS = "a42b98f04cc2916e8adfb5d9db8a2227c4629bc205748ed2f33180b636ee885b"
    DEFAULT_PORTS = {'t': '50001', 's': '50002'}
    DEFAULT_SERVERS = read_json('servers_regtest.json', {})
    CHECKPOINTS = []

    XPRV_HEADERS = {
        'standard':    0x04358394,  # tprv
        # 'p2wpkh-p2sh': 0x044a4e28,  # uprv
        # 'p2wsh-p2sh':  0x024285b5,  # Uprv
        # 'p2wpkh':      0x045f18bc,  # vprv
        # 'p2wsh':       0x02575048,  # Vprv
    }
    XPRV_HEADERS_INV = inv_dict(XPRV_HEADERS)
    XPUB_HEADERS = {
        'standard':    0x043587cf,  # tpub
        # 'p2wpkh-p2sh': 0x044a5262,  # upub
        # 'p2wsh-p2sh':  0x024289ef,  # Upub
        # 'p2wpkh':      0x045f1cf6,  # vpub
        # 'p2wsh':       0x02575483,  # Vpub
    }
    XPUB_HEADERS_INV = inv_dict(XPUB_HEADERS)
    # DRKV_HEADER = 0x3a8061a0  # DRKV
    # DRKP_HEADER = 0x3a805837  # DRKP
    BIP44_COIN_TYPE = 136
    COIN = coins.FiroRegtest()
    DIP3_ACTIVATION_HEIGHT = 5000


# class BitcoinRegtest(BitcoinTestnet):

#     NET_NAME = "regtest"
#     GENESIS = "000008ca1832a4baf228eb1553c03d3a2c8e02399550dd6ea8d65cec3ef23d2e" # Dash regtest genesis
#     DEFAULT_SERVERS = read_json('servers_regtest.json', {})
#     COIN = coins.FiroTestnet()
#     DIP3_ACTIVATION_HEIGHT = 5000
#     CHECKPOINTS = []


NETS_LIST = tuple(all_subclasses(AbstractNet))

# don't import net directly, import the module instead (so that net is singleton)
net = BitcoinMainnet


def set_mainnet():
    global net
    net = BitcoinMainnet


def set_testnet():
    global net
    net = BitcoinTestnet


def set_regtest():
    global net
    net = BitcoinRegtest
