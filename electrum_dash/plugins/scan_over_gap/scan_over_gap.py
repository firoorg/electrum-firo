#!/usr/bin/env python
# -*- coding: utf-8 -*-

import asyncio
import attr
from functools import partial
from collections import OrderedDict
from enum import IntEnum

from . import constants, util

from electrum_firo import bitcoin
from electrum_firo.i18n import _
from electrum_firo.logging import Logger
from electrum_firo.network import Network
from electrum_firo.plugin import BasePlugin
from .util import OldTaskGroup


def balance_total(*, confirmed, unconfirmed):
    return confirmed + unconfirmed


@attr.s
class Scan:
    derive = attr.ib(kw_only=True)
    create_addr = attr.ib(kw_only=True)
    num_addrs = attr.ib(kw_only=True)
    gap = attr.ib(kw_only=True)
    start_idx = attr.ib(kw_only=True)
    next_idx = attr.ib(kw_only=True)
    for_change = attr.ib(kw_only=True)
    ps_ks = attr.ib(type=bool, kw_only=True, default=False)
    addrs = attr.ib(default=attr.Factory(dict), kw_only=True)
    tasks = attr.ib(default=attr.Factory(dict), kw_only=True)
    balances = attr.ib(default=attr.Factory(dict), kw_only=True)
    errors = attr.ib(default=attr.Factory(dict), kw_only=True)
    active = attr.ib(kw_only=True, default=True)

    def derive_addrs(self, cnt):
        for i in range(self.next_idx, self.next_idx + cnt):
            self.addrs[i] = self.derive(i)
        self.next_idx += cnt

    def create_new_addrs(self, wallet):
        for i, balance in self.balances.items():
            if balance > 0:
                with wallet.lock:
                    while self.start_idx <= i + self.gap:
                        addr = self.create_addr()
                        if i not in self.addrs:
                            self.addrs[i] = addr
                        self.start_idx = self.num_addrs()
                        if self.next_idx < self.start_idx:
                            self.next_idx = self.start_idx
            self.balances[i] = 0

    @property
    def uncompleted(self):
        return set(self.addrs) - set(self.balances)

    @classmethod
    def get_key(cls, *, for_change, ps_ks):
        key = 'ps' if ps_ks else 'main'
        subkey = 'change' if for_change else ''
        return f'{key}_{subkey}' if subkey else f'{key}'

    @property
    def key(self):
        return Scan.get_key(for_change=self.for_change, ps_ks=self.ps_ks)

    @property
    def title(self):
        title = _('PrivateSend Account') if self.ps_ks else _('Main Account')
        subtitle = _('Change') if self.for_change else ''
        return f'{title} {subtitle}' if subtitle else f'{title}'


@attr.s
class WalletScan:
    progress = attr.ib(kw_only=True, default=0)
    running = attr.ib(kw_only=True, default=False)
    scans = attr.ib(kw_only=True, default=attr.Factory(OrderedDict))
    error = attr.ib(kw_only=True, default=None)


class ScanOverGapPlugin(BasePlugin, Logger):

    MIN_SCAN_CNT = 5
    DEF_SCAN_CNT = 20
    MAX_SCAN_CNT = 10_000

    MSG_TITLE = _('Scan Over Gap')
    MSG_SCAN_TITLE = _('Scan current wallet addresses beyond gap limits'
                       ' to search lost coins.')
    MSG_SCAN_COUNT = _('Count to scan:')
    MSG_PROGRESS = _('Progress:')
    MSG_RESET = _('Reset scans')
    MSG_Q_RESET = _('Reset all scans?')
    MSG_ADD_FOUND = _('Add found coins to wallet')
    MSG_SCAN_NEXT = _('Scan next {} addresses')
    MSG_SCAN = _('Scan')

    class Columns(IntEnum):
        KEY = 0
        TITLE = 1
        START_IDX = 2
        SCANNED_CNT = 3
        FOUND_BALANCE = 4

    COLUMN_HEADERS = ['', '', _('Addresses'), _('Scanned'), _('Found')]

    def __init__(self, parent, config, name):
        super(ScanOverGapPlugin, self).__init__(parent, config, name)
        self.wallet_scans = {}
        self.wallet_scans_lock = asyncio.Lock()
        self.format_amount = config.format_amount_and_units

    def is_available(self):
        if Network.get_instance():
            return True
        self.logger.warning(f'Plugin {self.name} unavailable in offline mode')
        return False

    async def on_progress(self, wallet, progress):
        async with self.wallet_scans_lock:
            wallet_scan = self.wallet_scans.get(wallet)
            if not wallet_scan:
                self.logger.warning(f'wallet_scan not found for {wallet}')
                return
            wallet_scan.progress = progress
            self.logger.debug(f'scan progress for {wallet} is {progress}')

    async def on_completed(self, wallet):
        async with self.wallet_scans_lock:
            wallet_scan = self.wallet_scans.get(wallet)
            if not wallet_scan:
                self.logger.warning(f'wallet_scan not found for {wallet}')
                return
            wallet_scan.running = False
            self.logger.debug(f'scan completed for {wallet}')

    async def on_error(self, wallet, e):
        async with self.wallet_scans_lock:
            wallet_scan = self.wallet_scans.get(wallet)
            if not wallet_scan:
                self.logger.warning(f'wallet_scan not found for {wallet}')
                return
            wallet_scan.error = e
            wallet_scan.running = False
            self.logger.debug(f'scan error for {wallet}: {str(e)}')

    async def init_scans(self, wallet, *, reset=False):
        w = wallet
        psman = w.psman
        db_num_change = w.db.num_change_addresses
        db_num_receiving = w.db.num_receiving_addresses
        is_ps_ks = (psman.ps_keystore is not None)
        new_scans_cnt = 0
        async with self.wallet_scans_lock:
            ws = self.wallet_scans.get(w) if not reset else None
            if not ws:
                self.wallet_scans[w] = ws = WalletScan()
            for ps_ks in [False, True] if is_ps_ks else [False]:
                for for_change in [0, 1]:
                    b_chg = bool(for_change)
                    key = Scan.get_key(for_change=for_change, ps_ks=ps_ks)
                    if key not in ws.scans:
                        derive = (partial(psman.derive_address, for_change)
                                  if ps_ks else
                                  partial(w.derive_address, for_change))
                        create_addr = (partial(psman.create_new_address, b_chg)
                                  if ps_ks else
                                  partial(w.create_new_address, b_chg))
                        num_addrs = (partial(db_num_change, ps_ks=ps_ks)
                                     if for_change else
                                     partial(db_num_receiving, ps_ks=ps_ks))
                        if ps_ks:
                            gap = (psman.gap_limit_for_change
                                   if for_change else
                                   psman.gap_limit)
                        else:
                            gap = (w.gap_limit_for_change
                                   if for_change else
                                   w.gap_limit)
                        start_idx = num_addrs()
                        s = Scan(derive=derive, create_addr=create_addr,
                                 num_addrs=num_addrs, gap=gap,
                                 start_idx=start_idx, next_idx=start_idx,
                                 for_change=for_change, ps_ks=ps_ks)
                        new_scans_cnt +=1
                        ws.scans[key] = s
        return new_scans_cnt

    async def do_scan(self, wallet, cnt):
        w = wallet
        try:
            async with self.wallet_scans_lock:
                ws = self.wallet_scans.get(w)
                if not ws or ws.running:
                    return
                ws.running = True
                scans = list(ws.scans.values())
            n = Network.get_instance()
            loop = n.asyncio_loop
            done_cnt = 0
            to_scan_cnt = 0
            for s in scans:
                if not s.active:
                    continue
                to_scan_cnt += len(s.uncompleted)
            if to_scan_cnt:
                self.logger.info(f'total count to rescan: {to_scan_cnt}')
            else:
                for s in scans:
                    if not s.active:
                        continue
                    async with self.wallet_scans_lock:
                        await loop.run_in_executor(None, s.derive_addrs, cnt)
                    to_scan_cnt += cnt
                self.logger.info(f'total count to scan: {to_scan_cnt}')
            if not to_scan_cnt:
                return
            async with OldTaskGroup() as group:
                for s in scans:
                    for i, addr in s.addrs.items():
                        if i in s.balances:
                            continue
                        script = bitcoin.address_to_script(addr)
                        scripthash = bitcoin.script_to_scripthash(script)
                        coro = n.get_balance_for_scripthash(scripthash)
                        s.tasks[i] = await group.spawn(coro)
                while True:
                    task = await group.next_done()
                    if task is None:
                        break
                    done_cnt += 1
                    await self.on_progress(w, 100*done_cnt/to_scan_cnt)
            for s in  scans:
                for i, task in s.tasks.items():
                    try:
                        balance = task.result()
                        s.balances[i] = balance_total(**balance)
                        if i in s.errors:
                            s.errors.pop(i)
                    except BaseException as e:
                        self.logger.info(f'Exception on get_balance {repr(e)}')
                        s.errors[i] = e
                s.tasks.clear()
            await self.on_completed(w)
        except Exception as e:
            self.logger.info(f'Exception during wallet_scan: {repr(e)}')
            await self.on_error(w, e)

    async def add_found(self, wallet):
        w = wallet
        try:
            n = Network.get_instance()
            loop = n.asyncio_loop
            async with self.wallet_scans_lock:
                ws = self.wallet_scans.get(w)
                if not ws or ws.running:
                    return
                ws.running = True
                scans = list(ws.scans.values())
                for s in scans:
                    await loop.run_in_executor(None, s.create_new_addrs, w)
            await self.on_completed(w)
        except Exception as e:
            self.logger.info(f'Exception during add_found: {repr(e)}')
            await self.on_error(w, e)
