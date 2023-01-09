# -*- coding: utf-8 -*-

import asyncio
import copy
import random
import time
import threading
from collections import deque
from uuid import uuid4

from . import util
from .dash_msg import PRIVATESEND_ENTRY_MAX_SIZE, DSPoolState
from .dash_ps_net import (PSMixSession, PRIVATESEND_SESSION_MSG_TIMEOUT,
                          MixSessionTimeout, MixSessionPeerClosed)
from .dash_ps_wallet import (PSDataMixin, PSKeystoreMixin, KeyPairsMixin,
                             KPStates, NotFoundInKeypairs, AddPSDataError,
                             SignWithKeypairsFailed)
from .dash_ps_util import (PSOptsMixin, PSUtilsMixin, PSGUILogHandler,
                           PSManLogAdapter, PSCoinRounds, PSStates,
                           PS_DENOMS_DICT, COLLATERAL_VAL, MIN_DENOM_VAL,
                           CREATE_COLLATERAL_VAL, CREATE_COLLATERAL_VALS,
                           PSTxWorkflow, PSDenominateWorkflow, calc_tx_fee)
from .dash_tx import PSTxTypes, SPEC_TX_NAMES, CTxIn
from .logging import Logger
from .transaction import Transaction, PartialTxOutput, PartialTransaction
from .util import (NoDynamicFeeEstimates, log_exceptions, OldTaskGroup,
                   NotEnoughFunds, bfh, is_android)
from .i18n import _


PS_DENOM_REVERSE_DICT = {int(v): k for k, v in PS_DENOMS_DICT.items()}


class TooManyUtxos(Exception):
    """Thrown when creating new denoms/collateral txs from coins"""


class TooLargeUtxoVal(Exception):
    """Thrown when creating new collateral txs from coins"""


class PSManager(Logger, PSKeystoreMixin, PSDataMixin, PSOptsMixin,
                PSUtilsMixin, KeyPairsMixin):
    '''Class representing wallet PrivateSend manager'''

    LOGGING_SHORTCUT = 'A'
    ADD_PS_DATA_ERR_MSG = _('Error on adding PrivateSend transaction data.')
    SPEND_TO_PS_ADDRS_MSG = _('For privacy reasons blocked attempt to'
                              ' transfer coins to PrivateSend address.')
    WATCHING_ONLY_MSG = _('This is a watching-only wallet.'
                          ' Mixing can not be run.')
    ALL_MIXED_MSG = _('PrivateSend mixing is done')
    CLEAR_PS_DATA_MSG = _('Are you sure to clear all wallet PrivateSend data?'
                          ' This is not recommended if there is'
                          ' no particular need.')
    NO_NETWORK_MSG = _('Can not start mixing. Network is not available')
    NO_DASH_NET_MSG = _('Can not start mixing. DashNet is not available')
    LLMQ_DATA_NOT_READY = _('LLMQ quorums data is not fully loaded.')
    MNS_DATA_NOT_READY = _('Masternodes data is not fully loaded.')
    NOT_ENABLED_MSG = _('PrivateSend mixing is not enabled')
    INITIALIZING_MSG = _('PrivateSend mixing is initializing.'
                         ' Please try again soon')
    MIXING_ALREADY_RUNNING_MSG = _('PrivateSend mixing is already running.')
    MIXING_NOT_RUNNING_MSG = _('PrivateSend mixing is not running.')
    FIND_UNTRACKED_RUN_MSG = _('PrivateSend mixing can not start. Process of'
                               ' finding untracked PS transactions'
                               ' is currently run')
    ERRORED_MSG = _('PrivateSend mixing can not start.'
                    ' Please check errors in PS Log tab')
    UNKNOWN_STATE_MSG = _('PrivateSend mixing can not start.'
                          ' Unknown state: {}')
    WAIT_MIXING_STOP_MSG = _('Mixing is not stopped. If mixing sessions ends'
                             ' prematurely additional pay collateral may be'
                             ' paid. Do you really want to close wallet?')
    NO_NETWORK_STOP_MSG = _('Network is not available')
    OTHER_COINS_ARRIVED_MSG1 = _('Some unknown coins arrived on addresses'
                                 ' reserved for PrivateSend use, txid: {}.')
    OTHER_COINS_ARRIVED_MSG2 = _('WARNING: it is not recommended to spend'
                                 ' these coins in regular transactions!')
    OTHER_COINS_ARRIVED_MSG3 = _('You can use these coins in PrivateSend'
                                 ' mixing process by manually selecting UTXO'
                                 ' and creating new denoms or new collateral,'
                                 ' depending on UTXO value.')
    OTHER_COINS_ARRIVED_Q = _('Do you want to use other coins now?')
    if is_android():
        NO_DYNAMIC_FEE_MSG = _('{}\n\nYou can switch fee estimation method'
                               ' on send screen')
        OTHER_COINS_ARRIVED_MSG4 = _('You can view and use these coins from'
                                     ' Coins popup from PrivateSend options.')
    else:
        NO_DYNAMIC_FEE_MSG = _('{}\n\nYou can switch to static fee estimation'
                               ' on Fees Preferences tab')
        OTHER_COINS_ARRIVED_MSG4 = _('You can view and use these coins from'
                                     ' Coins tab.')

    def __init__(self, wallet):
        Logger.__init__(self)
        PSDataMixin.__init__(self, wallet)
        PSKeystoreMixin.__init__(self, wallet)
        KeyPairsMixin.__init__(self, wallet)
        PSOptsMixin.__init__(self, wallet)
        PSUtilsMixin.__init__(self, wallet)

        self.log_handler = PSGUILogHandler(self)
        self.logger = PSManLogAdapter(self.logger, {'psman_id': id(self)})

        self.state_lock = threading.Lock()
        self.states = s = PSStates
        self.mixing_running_states = [s.StartMixing, s.Mixing, s.StopMixing]
        self.no_clean_history_states = [s.Initializing, s.Errored,
                                        s.StartMixing, s.Mixing, s.StopMixing,
                                        s.FindingUntracked]
        self.config = wallet.config
        self._state = PSStates.Unsupported
        self.wallet_types_supported = ['standard']
        self.keystore_types_supported = ['bip32', 'hardware']
        keystore = wallet.db.get('keystore')
        if keystore:
            self.w_ks_type = keystore.get('type', 'unknown')
        else:
            self.w_ks_type = 'unknown'
        self.w_type = wallet.wallet_type
        if (self.w_type in self.wallet_types_supported
                and self.w_ks_type in self.keystore_types_supported):
            if wallet.db.get_ps_data('ps_enabled', False):
                self.state = PSStates.Initializing
            else:
                self.state = PSStates.Disabled
        if self.unsupported:
            supported_w = ', '.join(self.wallet_types_supported)
            supported_ks = ', '.join(self.keystore_types_supported)
            this_type = self.w_type
            this_ks_type = self.w_ks_type
            self.unsupported_msg = _(f'PrivateSend is currently supported on'
                                     f' next wallet types: "{supported_w}"'
                                     f' and keystore types: "{supported_ks}".'
                                     f'\n\nThis wallet has type "{this_type}"'
                                     f' and kestore type "{this_ks_type}".')
        else:
            self.unsupported_msg = ''

        self.ps_keystore_has_history = False
        if self.is_hw_ks:
            self.enable_ps_keystore()

        self.network = None
        self.dash_net = None
        self.loop = None
        self.main_taskgroup = None

        self.mix_sessions_lock = asyncio.Lock()
        self.mix_sessions = {}  # dict peer -> PSMixSession
        self.recent_mixes_mns = deque([], 10)  # added from mixing sessions

        self.denoms_lock = threading.Lock()
        self.collateral_lock = threading.Lock()
        self.others_lock = threading.Lock()

        self.new_denoms_wfl_lock = threading.Lock()
        self.new_collateral_wfl_lock = threading.Lock()
        self.pay_collateral_wfl_lock = threading.Lock()
        self.denominate_wfl_lock = threading.Lock()
        self._not_enough_funds = False

        # electrum network disconnect time
        self.disconnect_time = 0

    @property
    def unsupported(self):
        '''Wallet and keystore types is not supported'''
        return self.state == PSStates.Unsupported

    @property
    def enabled(self):
        '''PS was enabled on this wallet'''
        return self.state not in [PSStates.Unsupported, PSStates.Disabled]

    @property
    def is_hw_ks(self):
        '''Wallet has hardware keystore type'''
        return self.w_ks_type == 'hardware'

    def enable_ps(self):
        '''Enables PS on this wallet, store changes in db.
        For hardware wallets PS Keystore must be created first'''
        if (self.w_type == 'standard' and self.is_hw_ks
                and 'ps_keystore' not in self.wallet.db.data):
            self.logger.info('ps_keystore for hw wallets must be created')
            return
        if not self.enabled:
            self.wallet.db.set_ps_data('ps_enabled', True)
            coro = self._enable_ps()
            asyncio.run_coroutine_threadsafe(coro, self.loop)

    async def _enable_ps(self):
        '''Start initialization, enable PS Keystore, find untracked txs'''
        if self.enabled:
            return
        self.state = PSStates.Initializing
        util.trigger_callback('ps-state-changes', self.wallet, None, None)
        _load_and_cleanup = self.load_and_cleanup
        await self.loop.run_in_executor(None, _load_and_cleanup)
        await self.find_untracked_ps_txs()
        self.wallet.save_db()

    def can_find_untracked(self):
        '''find untracked txs should run on synchronized wallet'''
        w = self.wallet
        network = self.network
        if network is None:
            return False

        server_height = network.get_server_height()
        if server_height == 0:
            return False

        local_height = network.get_local_height()
        if local_height < server_height:
            return False

        with w.lock:
            unverified_no_islock = []
            for txid in w.unverified_tx:
                if txid not in w.db.islocks:
                    unverified_no_islock.append(txid)
            if (unverified_no_islock
                    or not w.is_up_to_date()
                    or not w.synchronizer.is_up_to_date()):
                return False
        return True

    @property
    def state(self):
        '''Current state of PSManager (dash_ps_util.PSStates enum)'''
        return self._state

    @property
    def is_waiting(self):
        '''Mixing is running but no active workflows are found'''
        if self.state not in self.mixing_running_states:
            return False
        if self.keypairs_state in [KPStates.NeedCache, KPStates.Caching]:
            return False

        active_wfls_cnt = 0
        active_wfls_cnt += len(self.denominate_wfl_list)
        if self.new_denoms_wfl:
            active_wfls_cnt += 1
        if self.new_collateral_wfl:
            active_wfls_cnt += 1
        return (active_wfls_cnt == 0)

    @state.setter
    def state(self, state):
        '''Set current state of PSManager'''
        self._state = state

    def on_network_start(self, network):
        '''Run when network is connected to the wallet'''
        self.network = network
        util.register_callback(self.on_wallet_updated, ['wallet_updated'])
        util.register_callback(self.on_network_status, ['status'])
        self.dash_net = network.dash_net
        self.loop = network.asyncio_loop
        asyncio.ensure_future(self.clean_keypairs_on_timeout())
        asyncio.ensure_future(self.cleanup_staled_denominate_wfls())
        asyncio.ensure_future(self.trigger_postponed_notifications())

    def on_stop_threads(self):
        '''Run when the wallet is unloaded/stopped'''
        if self.state == PSStates.Mixing:
            self.stop_mixing()
        util.unregister_callback(self.on_wallet_updated)
        util.unregister_callback(self.on_network_status)

    def on_network_status(self, event, *args):
        '''Monitor if electrum network is disconnected and then stop mixing.'''
        connected = self.network.is_connected()
        if connected:
            self.disconnect_time = 0
        else:
            now = time.time()
            if self.disconnect_time == 0:
                self.disconnect_time = now
            if now - self.disconnect_time > 30:  # disconnected for 30 seconds
                if self.state == PSStates.Mixing:
                    self.stop_mixing(self.NO_NETWORK_STOP_MSG)

    async def on_wallet_updated(self, event, *args):
        '''Run when wallet/synchronizer's is_up_to_date state changed'''
        if not self.enabled:
            return
        w = args[0]
        if w != self.wallet:
            return
        if w.is_up_to_date():
            self._not_enough_funds = False
            if self.state in [PSStates.Initializing, PSStates.Ready]:
                await self.find_untracked_ps_txs()

    # Methods related to mixing process
    def start_mixing(self, password, nowait=True):
        '''Start mixing from GUI'''
        w = self.wallet
        msg = None
        if w.is_watching_only():
            msg = self.WATCHING_ONLY_MSG, 'err'
        elif self.all_mixed:
            msg = self.ALL_MIXED_MSG, 'inf'
        elif not self.network or not self.network.is_connected():
            msg = self.NO_NETWORK_MSG, 'err'
        elif not self.dash_net.run_dash_net:
            msg = self.NO_DASH_NET_MSG, 'err'
        if msg:
            msg, inf = msg
            self.logger.info(f'Can not start PrivateSend Mixing: {msg}')
            util.trigger_callback('ps-state-changes', w, msg, inf)
            return

        coro = self.find_untracked_ps_txs()
        asyncio.run_coroutine_threadsafe(coro, self.loop).result()

        with self.state_lock:
            if self.state == PSStates.Ready:
                self.state = PSStates.StartMixing
            elif self.state in [PSStates.Unsupported, PSStates.Disabled]:
                msg = self.NOT_ENABLED_MSG
            elif self.state == PSStates.Initializing:
                msg = self.INITIALIZING_MSG
            elif self.state in self.mixing_running_states:
                msg = self.MIXING_ALREADY_RUNNING_MSG
            elif self.state == PSStates.FindingUntracked:
                msg = self.FIND_UNTRACKED_RUN_MSG
            elif self.state == PSStates.FindingUntracked:
                msg = self.ERRORED_MSG
            else:
                msg = self.UNKNOWN_STATE_MSG.format(self.state)
        if msg:
            util.trigger_callback('ps-state-changes', w, msg, None)
            self.logger.info(f'Can not start PrivateSend Mixing: {msg}')
            return
        else:
            util.trigger_callback('ps-state-changes', w, None, None)

        fut = asyncio.run_coroutine_threadsafe(self._start_mixing(password),
                                               self.loop)
        if nowait:
            return
        try:
            fut.result(timeout=2)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            pass

    async def _start_mixing(self, password):
        if not self.enabled or not self.network:
            return

        assert not self.main_taskgroup
        self._not_enough_funds = False
        self.main_taskgroup = main_taskgroup = OldTaskGroup()
        self.logger.info('Starting PrivateSend Mixing')

        async def main():
            try:
                async with main_taskgroup as group:
                    if (self.w_type == 'standard'
                            and self.is_hw_ks):
                        await group.spawn(self._prepare_funds_from_hw_wallet())
                    await group.spawn(self._make_keypairs_cache(password))
                    await group.spawn(self._check_not_enough_funds())
                    await group.spawn(self._check_all_mixed())
                    await group.spawn(self._maintain_pay_collateral_tx())
                    await group.spawn(self._maintain_collateral_amount())
                    await group.spawn(self._maintain_denoms())
                    await group.spawn(self._mix_denoms())
            except Exception as e:
                self.logger.info(f'error starting mixing: {str(e)}')
                raise e
        asyncio.run_coroutine_threadsafe(main(), self.loop)
        with self.state_lock:
            self.state = PSStates.Mixing
        self.last_mix_start_time = time.time()
        self.logger.info('Started PrivateSend Mixing')
        w = self.wallet
        util.trigger_callback('ps-state-changes', w, None, None)

    async def stop_mixing_from_async_thread(self, msg, msg_type=None):
        '''Stop mixing programmatically'''
        await self.loop.run_in_executor(None, self.stop_mixing, msg, msg_type)

    def stop_mixing(self, msg=None, msg_type=None, nowait=True):
        '''Stop mixing from GUI'''
        w = self.wallet
        with self.state_lock:
            if self.state == PSStates.Mixing:
                self.state = PSStates.StopMixing
            elif self.state == PSStates.StopMixing:
                return
            else:
                msg = self.MIXING_NOT_RUNNING_MSG
                util.trigger_callback('ps-state-changes', w, msg, 'inf')
                self.logger.info(f'Can not stop PrivateSend Mixing: {msg}')
                return
        if msg:
            self.logger.info(f'Stopping PrivateSend Mixing: {msg}')
            if not msg_type or not msg_type.startswith('inf'):
                stopped_prefix = _('PrivateSend mixing is stopping!')
                msg = f'{stopped_prefix}\n\n{msg}'
            util.trigger_callback('ps-state-changes', w, msg, msg_type)
        else:
            self.logger.info('Stopping PrivateSend Mixing')
            util.trigger_callback('ps-state-changes', w, None, None)

        self.last_mix_stop_time = time.time()  # write early if later time lost
        fut = asyncio.run_coroutine_threadsafe(self._stop_mixing(), self.loop)
        if nowait:
            return
        try:
            fut.result(timeout=PRIVATESEND_SESSION_MSG_TIMEOUT+5)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            pass

    @log_exceptions
    async def _stop_mixing(self):
        '''Async part of stop_mixing'''
        if self.keypairs_state == KPStates.Caching:
            self.logger.info('Waiting for keypairs caching to finish')
            while self.keypairs_state == KPStates.Caching:
                await asyncio.sleep(0.5)
        if self.main_taskgroup:
            sess_cnt = len(self.mix_sessions)
            if sess_cnt > 0:
                self.logger.info(f'Waiting for {sess_cnt}'
                                 f' mixing sessions to finish')
                while sess_cnt > 0:
                    await asyncio.sleep(0.5)
                    sess_cnt = len(self.mix_sessions)
            try:
                await asyncio.wait_for(self.main_taskgroup.cancel_remaining(),
                                       timeout=2)
            except (asyncio.TimeoutError, asyncio.CancelledError) as e:
                self.logger.debug(f'Exception during main_taskgroup'
                                  f' cancellation: {repr(e)}')
            self.main_taskgroup = None
        with self.keypairs_state_lock:
            if self.keypairs_state == KPStates.Ready:
                self.logger.info('Mark keypairs as unused')
                self.keypairs_state = KPStates.Unused
        self.logger.info('Stopped PrivateSend Mixing')
        self.last_mix_stop_time = time.time()
        with self.state_lock:
            self.state = PSStates.Ready
        w = self.wallet
        util.trigger_callback('ps-state-changes', w, None, None)

    async def _check_all_mixed(self):
        '''Async task to check all_mixed and stop if done'''
        while not self.main_taskgroup.closed():
            await asyncio.sleep(10)
            if self.all_mixed:
                await self.stop_mixing_from_async_thread(self.ALL_MIXED_MSG,
                                                         'inf')

    async def _check_not_enough_funds(self):
        '''Async task to reset _not_enough_funds condition after 30 secs'''
        while not self.main_taskgroup.closed():
            if self._not_enough_funds:
                await asyncio.sleep(30)
                self._not_enough_funds = False
            await asyncio.sleep(5)

    async def _maintain_pay_collateral_tx(self):
        '''Async task to maintain actual pay collateral tx'''
        kp_wait_state = KPStates.Ready if self.need_password() else None

        while not self.main_taskgroup.closed():
            wfl = self.pay_collateral_wfl
            if wfl:
                if not wfl.completed or not wfl.tx_order:
                    await self.cleanup_pay_collateral_wfl()
            elif self.ps_collateral_cnt > 0:
                if kp_wait_state and self.keypairs_state != kp_wait_state:
                    self.logger.info('Pay collateral workflow waiting'
                                     ' for keypairs generation')
                    await asyncio.sleep(5)
                    continue
                if not self.get_confirmed_ps_collateral_data():
                    await asyncio.sleep(5)
                    continue
                await self.prepare_pay_collateral_wfl()
            await asyncio.sleep(0.25)

    async def _maintain_collateral_amount(self):
        '''Async task to maintain available collateral coins'''
        w = self.wallet
        kp_wait_state = KPStates.Ready if self.need_password() else None

        while not self.main_taskgroup.closed():
            wfl = self.new_collateral_wfl
            if wfl:
                if not wfl.completed or not wfl.tx_order:
                    await self.cleanup_new_collateral_wfl()
                elif wfl.next_to_send(w):
                    await self.broadcast_new_collateral_wfl()
                else:
                    for txid in wfl.tx_order:
                        tx = Transaction(wfl.tx_data[txid].raw_tx)
                        self._process_by_new_collateral_wfl(txid, tx)
            elif (not self._not_enough_funds
                    and not self.ps_collateral_cnt
                    and not self.calc_need_denoms_amounts(use_cache=True)):
                coins = await self.get_next_coins_for_mixing(for_denoms=False)
                if not coins:
                    await asyncio.sleep(5)
                    continue
                if not self.check_llmq_ready():
                    self.logger.info(_('New collateral workflow: {}')
                                     .format(self.LLMQ_DATA_NOT_READY))
                    await asyncio.sleep(5)
                    continue
                elif kp_wait_state and self.keypairs_state != kp_wait_state:
                    self.logger.info('New collateral workflow waiting'
                                     ' for keypairs generation')
                    await asyncio.sleep(5)
                    continue
                await self.create_new_collateral_wfl()
            await asyncio.sleep(0.25)

    async def _maintain_denoms(self):
        '''Async task which makes new denoms available for mixing'''
        w = self.wallet
        kp_wait_state = KPStates.Ready if self.need_password() else None

        while not self.main_taskgroup.closed():
            wfl = self.new_denoms_wfl
            if wfl:
                if not wfl.completed or not wfl.tx_order:
                    await self.cleanup_new_denoms_wfl()
                elif wfl.next_to_send(w):
                    await self.broadcast_new_denoms_wfl()
                else:
                    for txid in wfl.tx_order:
                        tx = Transaction(wfl.tx_data[txid].raw_tx)
                        self._process_by_new_denoms_wfl(txid, tx)
            elif (not self._not_enough_funds
                    and self.calc_need_denoms_amounts(use_cache=True)):
                coins = await self.get_next_coins_for_mixing()
                if not coins:
                    await asyncio.sleep(5)
                    continue
                if not self.check_llmq_ready():
                    self.logger.info(_('New denoms workflow: {}')
                                     .format(self.LLMQ_DATA_NOT_READY))
                    await asyncio.sleep(5)
                    continue
                elif kp_wait_state and self.keypairs_state != kp_wait_state:
                    self.logger.info('New denoms workflow waiting'
                                     ' for keypairs generation')
                    await asyncio.sleep(5)
                    continue
                await self.create_new_denoms_wfl()
            await asyncio.sleep(0.25)

    async def _mix_denoms(self):
        '''Async task which maintains mixing process'''
        kp_wait_state = KPStates.Ready if self.need_password() else None

        def _cleanup():
            for uuid in self.denominate_wfl_list:
                wfl = self.get_denominate_wfl(uuid)
                if wfl and not wfl.completed:
                    self._cleanup_denominate_wfl(wfl)
        await self.loop.run_in_executor(None, _cleanup)

        main_taskgroup = self.main_taskgroup
        while not main_taskgroup.closed():
            if (self._denoms_to_mix_cache
                    and self.pay_collateral_wfl
                    and self.active_denominate_wfl_cnt < self.max_sessions):
                if not self.check_llmq_ready():
                    self.logger.info(_('Denominate workflow: {}')
                                     .format(self.LLMQ_DATA_NOT_READY))
                    await asyncio.sleep(5)
                    continue
                elif not self.check_protx_info_completeness():
                    self.logger.info(_('Denominate workflow: {}')
                                     .format(self.MNS_DATA_NOT_READY))
                    await asyncio.sleep(5)
                    continue
                elif kp_wait_state and self.keypairs_state != kp_wait_state:
                    self.logger.info('Denominate workflow waiting'
                                     ' for keypairs generation')
                    await asyncio.sleep(5)
                    continue
                if self.state == PSStates.Mixing:
                    await main_taskgroup.spawn(self.start_denominate_wfl())
            await asyncio.sleep(0.25)

    async def start_mix_session(self, denom_value, dsq, wfl_lid):
        '''Start mixing session on MN from dsq in wfl identified by wfl_lid'''
        n_denom = PS_DENOMS_DICT[denom_value]
        sess = PSMixSession(self, denom_value, n_denom, dsq, wfl_lid)
        peer_str = sess.peer_str
        async with self.mix_sessions_lock:
            if peer_str in self.mix_sessions:
                raise Exception(f'Session with {peer_str} already exists')
            await sess.run_peer()
            self.mix_sessions[peer_str] = sess
            return sess

    async def stop_mix_session(self, peer_str):
        '''Stop mixing session identified by "ip:port" peer string'''
        async with self.mix_sessions_lock:
            sess = self.mix_sessions.pop(peer_str)
            if not sess:
                self.logger.debug(f'Peer {peer_str} not found in mix_session')
                return
            sess.close_peer()
            return sess

    # Workflow methods for pay collateral transaction
    def get_confirmed_ps_collateral_data(self):
        '''Return one of many possible confirmed collateral utxo'''
        w = self.wallet
        for outpoint, ps_collateral in w.db.get_ps_collaterals().items():
            addr, value = ps_collateral
            utxos = w.get_utxos([addr], min_rounds=PSCoinRounds.COLLATERAL,
                                confirmed_funding_only=True,
                                consider_islocks=True)
            utxos = self.filter_out_hw_ks_coins(utxos)
            inputs = []
            for utxo in utxos:
                if utxo.prevout.to_str() != outpoint:
                    continue
                w.add_input_info(utxo)
                inputs.append(utxo)
            if inputs:
                return outpoint, value, inputs
            else:
                self.logger.wfl_err(f'ps_collateral outpoint {outpoint}'
                                    f' is not confirmed')

    async def prepare_pay_collateral_wfl(self):
        '''Async prepare signed pay collateral tx in pay_collateral_wfl'''
        try:
            _prepare = self._prepare_pay_collateral_tx
            res = await self.loop.run_in_executor(None, _prepare)
            if res:
                txid, wfl = res
                self.logger.wfl_ok(f'Completed pay collateral workflow with'
                                   f' tx: {txid}, workflow: {wfl.lid}')
                self.wallet.save_db()
        except Exception as e:
            wfl = self.pay_collateral_wfl
            if wfl:
                self.logger.wfl_err(f'Error creating pay collateral tx:'
                                    f' {str(e)}, workflow: {wfl.lid}')
                await self.cleanup_pay_collateral_wfl(force=True)
            else:
                self.logger.wfl_err(f'Error during creation of pay collateral'
                                    f' workflow: {str(e)}')
            type_e = type(e)
            msg = None
            if type_e == NoDynamicFeeEstimates:
                msg = self.NO_DYNAMIC_FEE_MSG.format(str(e))
            elif type_e == NotFoundInKeypairs:
                msg = self.NOT_FOUND_KEYS_MSG
            elif type_e == SignWithKeypairsFailed:
                msg = self.SIGN_WIHT_KP_FAILED_MSG
            if msg:
                await self.stop_mixing_from_async_thread(msg)

    def _prepare_pay_collateral_tx(self):
        '''Prepare pay collateral tx and save it to pay_collateral_wfl'''
        with self.pay_collateral_wfl_lock:
            if self.pay_collateral_wfl:
                return
            uuid = str(uuid4())
            wfl = PSTxWorkflow(uuid=uuid)
            self.set_pay_collateral_wfl(wfl)
            self.logger.info(f'Started up pay collateral workflow: {wfl.lid}')

        res = self.get_confirmed_ps_collateral_data()
        if not res:
            raise Exception('No confirmed ps_collateral found')
        outpoint, value, inputs = res

        # check input addresses is in keypairs if keypairs cache available
        if self._keypairs_cache:
            input_addrs = [utxo.address for utxo in inputs]
            not_found_addrs = self._find_addrs_not_in_keypairs(input_addrs)
            if not_found_addrs:
                not_found_addrs = ', '.join(list(not_found_addrs))
                raise NotFoundInKeypairs(f'Input addresses is not found'
                                         f' in the keypairs cache:'
                                         f' {not_found_addrs}')

        self.add_ps_spending_collateral(outpoint, wfl.uuid)
        if value >= COLLATERAL_VAL*2:
            ovalue = value - COLLATERAL_VAL
            output_addr = None
            for addr, data in self.wallet.db.get_ps_reserved().items():
                if data == outpoint:
                    output_addr = addr
                    break
            if not output_addr:
                reserved = self.reserve_addresses(1, for_change=True,
                                                  data=outpoint)
                output_addr = reserved[0]
            outputs = [PartialTxOutput.from_address_and_value(output_addr, ovalue)]
        else:
            # OP_RETURN as ouptut script
            outputs = [PartialTxOutput(scriptpubkey=bfh('6a'), value=0)]

        tx = PartialTransaction.from_io(inputs[:], outputs[:], locktime=0)
        tx.inputs()[0].nsequence = 0xffffffff
        tx = self.sign_transaction(tx, None)
        txid = tx.txid()
        raw_tx = tx.serialize_to_network()
        tx_type = PSTxTypes.PAY_COLLATERAL
        wfl.add_tx(txid=txid, raw_tx=raw_tx, tx_type=tx_type)
        wfl.completed = True
        with self.pay_collateral_wfl_lock:
            saved = self.pay_collateral_wfl
            if not saved:
                raise Exception('pay_collateral_wfl not found')
            if saved.uuid != wfl.uuid:
                raise Exception('pay_collateral_wfl differs from original')
            self.set_pay_collateral_wfl(wfl)
        return txid, wfl

    async def cleanup_pay_collateral_wfl(self, force=False):
        '''Async cleanup of pay_collateral_wfl'''
        _cleanup = self._cleanup_pay_collateral_wfl
        changed = await self.loop.run_in_executor(None, _cleanup, force)
        if changed:
            self.wallet.save_db()

    def _cleanup_pay_collateral_wfl(self, force=False):
        '''Cleanup of pay_collateral_wfl'''
        with self.pay_collateral_wfl_lock:
            wfl = self.pay_collateral_wfl
            if not wfl or wfl.completed and wfl.tx_order and not force:
                return
        w = self.wallet
        if wfl.tx_order:
            for txid in wfl.tx_order[::-1]:  # use reversed tx_order
                if w.db.get_transaction(txid):
                    w.remove_transaction(txid)
                else:
                    self._cleanup_pay_collateral_wfl_tx_data(txid)
        else:
            self._cleanup_pay_collateral_wfl_tx_data()
        return True

    def _cleanup_pay_collateral_wfl_tx_data(self, txid=None):
        '''Cleanup of pay_collateral_wfl tx and its db supplementary data'''
        with self.pay_collateral_wfl_lock:
            wfl = self.pay_collateral_wfl
            if not wfl:
                return
            if txid:
                tx_data = wfl.pop_tx(txid)
                if tx_data:
                    self.set_pay_collateral_wfl(wfl)
                    self.logger.info(f'Cleaned up pay collateral tx:'
                                     f' {txid}, workflow: {wfl.lid}')
        if wfl.tx_order:
            return

        w = self.wallet
        for outpoint, uuid in list(w.db.get_ps_spending_collaterals().items()):
            if uuid != wfl.uuid:
                continue
            with self.collateral_lock:
                self.pop_ps_spending_collateral(outpoint)

        with self.pay_collateral_wfl_lock:
            saved = self.pay_collateral_wfl
            if saved and saved.uuid == wfl.uuid:
                self.clear_pay_collateral_wfl()
        self.logger.info(f'Cleaned up pay collateral workflow: {wfl.lid}')

    def _search_pay_collateral_wfl(self, txid, tx):
        '''Search available pay_collateral_wfl corresponding to txid/tx data'''
        err = self._check_pay_collateral_tx_err(txid, tx, full_check=False)
        if not err:
            wfl = self.pay_collateral_wfl
            if wfl and wfl.tx_order and txid in wfl.tx_order:
                return wfl

    def _check_on_pay_collateral_wfl(self, txid, tx):
        '''Check it's pay collateral tx, corresponding to pay_collateral_wfl'''
        wfl = self._search_pay_collateral_wfl(txid, tx)
        err = self._check_pay_collateral_tx_err(txid, tx)
        if not err:
            return True
        if wfl:
            raise AddPSDataError(f'{err}')
        else:
            return False

    def _process_by_pay_collateral_wfl(self, txid, tx):
        '''Process tx by pay_collateral_wfl, cleanup wfl/supplementary data'''
        wfl = self._search_pay_collateral_wfl(txid, tx)
        if not wfl:
            return

        with self.pay_collateral_wfl_lock:
            saved = self.pay_collateral_wfl
            if not saved or saved.uuid != wfl.uuid:
                return
            tx_data = wfl.pop_tx(txid)
            if tx_data:
                self.set_pay_collateral_wfl(wfl)
                self.logger.wfl_done(f'Processed tx: {txid} from pay'
                                     f' collateral workflow: {wfl.lid}')
        if wfl.tx_order:
            return

        w = self.wallet
        for outpoint, uuid in list(w.db.get_ps_spending_collaterals().items()):
            if uuid != wfl.uuid:
                continue
            with self.collateral_lock:
                self.pop_ps_spending_collateral(outpoint)

        with self.pay_collateral_wfl_lock:
            saved = self.pay_collateral_wfl
            if saved and saved.uuid == wfl.uuid:
                self.clear_pay_collateral_wfl()
        self.logger.wfl_done(f'Finished processing of pay collateral'
                             f' workflow: {wfl.lid}')

    def get_pay_collateral_tx(self):
        '''Get pay collateral tx prepared in pay_collateral_wfl'''
        wfl = self.pay_collateral_wfl
        if not wfl or not wfl.tx_order:
            return
        txid = wfl.tx_order[0]
        tx_data = wfl.tx_data.get(txid)
        if not tx_data:
            return
        return tx_data.raw_tx

    # Workflow methods for new collateral transaction
    def new_collateral_from_coins_info(self, coins):
        '''Get info on new collateral wfl prepared from GUI'''
        if not coins or len(coins) > 1:
            return
        coins_val = sum([c.value_sats() for c in coins])
        if (coins_val >= self.min_new_denoms_from_coins_val
                or coins_val < self.min_new_collateral_from_coins_val):
            return
        fee_per_kb = self.config.fee_per_kb()
        for collateral_val in CREATE_COLLATERAL_VALS[::-1]:
            new_collateral_fee = calc_tx_fee(1, 1, fee_per_kb, max_size=True)
            if coins_val - new_collateral_fee >= collateral_val:
                tx_type = SPEC_TX_NAMES[PSTxTypes.NEW_COLLATERAL]
                info = _('Transactions type: {}').format(tx_type)
                info += '\n'
                info += _('Count of transactions: {}').format(1)
                info += '\n'
                info += _('Total sent amount: {}').format(coins_val)
                info += '\n'
                info += _('Total output amount: {}').format(collateral_val)
                info += '\n'
                info += _('Total fee: {}').format(coins_val - collateral_val)
                return info

    def create_new_collateral_wfl_from_gui(self, coins, password):
        '''Create new collateral tx/wfl from PS coins from GUI'''
        if self.state in self.mixing_running_states:
            return None, ('Can not create new collateral as mixing'
                          ' process is currently run.')
        if len(coins) > 1:
            return None, ('Can not create new collateral amount,'
                          ' too many coins selected')
        wfl = self._start_new_collateral_wfl()
        if not wfl:
            return None, ('Can not create new collateral as other new'
                          ' collateral creation process is in progress')
        try:
            w = self.wallet
            txid, tx = self._make_new_collateral_tx(wfl, coins, password)
            if not w.add_transaction(tx):
                raise Exception(f'Transaction with txid: {txid}'
                                f' conflicts with current history')
            if not w.db.get_ps_tx(txid)[0] == PSTxTypes.NEW_COLLATERAL:
                self._add_ps_data(txid, tx, PSTxTypes.NEW_COLLATERAL)
            with self.new_collateral_wfl_lock:
                saved = self.new_collateral_wfl
                if not saved:
                    raise Exception('new_collateral_wfl not found')
                if saved.uuid != wfl.uuid:
                    raise Exception('new_collateral_wfl differs from original')
                wfl.completed = True
                self.set_new_collateral_wfl(wfl)
                self.logger.wfl_ok(f'Completed new collateral workflow'
                                   f' with tx: {txid},'
                                   f' workflow: {wfl.lid}')
            return wfl, None
        except Exception as e:
            err = str(e)
            self.logger.wfl_err(f'Error creating new collateral tx:'
                                f' {err}, workflow: {wfl.lid}')
            self._cleanup_new_collateral_wfl(force=True)
            self.logger.info(f'Cleaned up new collateral workflow:'
                             f' {wfl.lid}')
            return None, err

    async def create_new_collateral_wfl(self):
        '''Create new collateral tx/wfl from async mixing task'''
        coins_data = await self.get_next_coins_for_mixing(for_denoms=False)
        coins = coins_data['coins']
        _start = self._start_new_collateral_wfl
        wfl = await self.loop.run_in_executor(None, _start)
        if not wfl:
            return
        try:
            _make_tx = self._make_new_collateral_tx
            txid, tx = await self.loop.run_in_executor(None, _make_tx,
                                                       wfl, coins)
            w = self.wallet
            # add_transaction need run in network therad
            if not w.add_transaction(tx):
                raise Exception(f'Transaction with txid: {txid}'
                                f' conflicts with current history')

            def _after_create_tx():
                with self.new_collateral_wfl_lock:
                    saved = self.new_collateral_wfl
                    if not saved:
                        raise Exception('new_collateral_wfl not found')
                    if saved.uuid != wfl.uuid:
                        raise Exception('new_collateral_wfl differs'
                                        ' from original')
                    wfl.completed = True
                    self.set_new_collateral_wfl(wfl)
                    self.logger.wfl_ok(f'Completed new collateral workflow'
                                       f' with tx: {txid},'
                                       f' workflow: {wfl.lid}')
            await self.loop.run_in_executor(None, _after_create_tx)
            w.save_db()
        except Exception as e:
            self.logger.wfl_err(f'Error creating new collateral tx:'
                                f' {str(e)}, workflow: {wfl.lid}')
            await self.cleanup_new_collateral_wfl(force=True)
            type_e = type(e)
            msg = None
            if type_e == NoDynamicFeeEstimates:
                msg = self.NO_DYNAMIC_FEE_MSG.format(str(e))
            elif type_e == AddPSDataError:
                msg = self.ADD_PS_DATA_ERR_MSG
                type_name = SPEC_TX_NAMES[PSTxTypes.NEW_COLLATERAL]
                msg = f'{msg} {type_name} {txid}:\n{str(e)}'
            elif type_e == NotFoundInKeypairs:
                msg = self.NOT_FOUND_KEYS_MSG
            elif type_e == SignWithKeypairsFailed:
                msg = self.SIGN_WIHT_KP_FAILED_MSG
            elif type_e == NotEnoughFunds:
                self._not_enough_funds = True
            if msg:
                await self.stop_mixing_from_async_thread(msg)

    def _start_new_collateral_wfl(self):
        '''Start new_collateral_wfl with appropriate locking'''
        with self.new_collateral_wfl_lock:
            if self.new_collateral_wfl:
                return

            uuid = str(uuid4())
            wfl = PSTxWorkflow(uuid=uuid)
            self.set_new_collateral_wfl(wfl)
            self.logger.info(f'Started up new collateral workflow: {wfl.lid}')
            return self.new_collateral_wfl

    def _make_new_collateral_tx(self, wfl, coins=None, password=None):
        '''Make signed new collateral tx and save it to new_collateral_wfl'''
        with self.new_collateral_wfl_lock:
            saved = self.new_collateral_wfl
            if not saved:
                raise Exception('new_collateral_wfl not found')
            if saved.uuid != wfl.uuid:
                raise Exception('new_collateral_wfl differs from original')

        w = self.wallet
        fee_per_kb = self.config.fee_per_kb()
        uuid = wfl.uuid
        oaddr = self.reserve_addresses(1, data=uuid)[0]
        if not coins:
            # try select minimal denom utxo with mimial rounds
            coins = w.get_utxos(None, mature_only=True,
                                confirmed_funding_only=True,
                                consider_islocks=True, min_rounds=0)
            coins = [c for c in coins if c.value_sats() == MIN_DENOM_VAL]
            coins = self.filter_out_hw_ks_coins(coins)
            if not coins:
                raise NotEnoughFunds()
            coins = sorted(coins, key=lambda x: x.ps_rounds)
            coins = coins[0:1]

        no_change = False
        outputs = None
        coins_val = sum([c.value_sats() for c in coins])
        if (len(coins) == 1  # Minimal denom or PS other selected, no change
                and coins[0].ps_rounds is not None
                and coins[0].ps_rounds != PSCoinRounds.MIX_ORIGIN):
            if coins_val >= self.min_new_denoms_from_coins_val:
                raise TooLargeUtxoVal('To large utxo selected')
            no_change = True

        if no_change:
            for val in CREATE_COLLATERAL_VALS[::-1]:
                new_collateral_fee = calc_tx_fee(1, 1, fee_per_kb,
                                                 max_size=True)
                if coins_val - new_collateral_fee < val:
                    continue
                outputs = [PartialTxOutput.from_address_and_value(oaddr, val)]
                break
            if outputs is None:
                raise NotEnoughFunds()
        else:
            val = CREATE_COLLATERAL_VAL
            outputs = [PartialTxOutput.from_address_and_value(oaddr, val)]

        tx = w.make_unsigned_transaction(coins=coins, outputs=outputs)
        inputs = tx.inputs()
        # check input addresses is in keypairs if keypairs cache available
        if self._keypairs_cache:
            input_addrs = [utxo.address for utxo in inputs]
            not_found_addrs = self._find_addrs_not_in_keypairs(input_addrs)
            if not_found_addrs:
                not_found_addrs = ', '.join(list(not_found_addrs))
                raise NotFoundInKeypairs(f'Input addresses is not found'
                                         f' in the keypairs cache:'
                                         f' {not_found_addrs}')

        if no_change:
            tx = PartialTransaction.from_io(inputs[:], outputs[:], locktime=0)
            for txin in tx.inputs():
                txin.nsequence = 0xffffffff
        else:  # use first input address as a change, use selected inputs
            change_addr = inputs[0].address
            tx = w.make_unsigned_transaction(coins=inputs, outputs=outputs,
                                             change_addr=change_addr)
        tx = self.sign_transaction(tx, password)
        estimated_fee = calc_tx_fee(len(tx.inputs()), len(tx.outputs()),
                                    fee_per_kb, max_size=True)
        overfee = tx.get_fee() - estimated_fee
        assert overfee < self.min_new_collateral_from_coins_val, 'too high fee'
        txid = tx.txid()
        raw_tx = tx.serialize_to_network()
        tx_type = PSTxTypes.NEW_COLLATERAL
        wfl.add_tx(txid=txid, raw_tx=raw_tx, tx_type=tx_type)
        with self.new_collateral_wfl_lock:
            saved = self.new_collateral_wfl
            if not saved:
                raise Exception('new_collateral_wfl not found')
            if saved.uuid != wfl.uuid:
                raise Exception('new_collateral_wfl differs from original')
            self.set_new_collateral_wfl(wfl)
        return txid, tx

    async def cleanup_new_collateral_wfl(self, force=False):
        '''Async cleanup of new_collateral_wfl'''
        _cleanup = self._cleanup_new_collateral_wfl
        changed = await self.loop.run_in_executor(None, _cleanup, force)
        if changed:
            self.wallet.save_db()

    def _cleanup_new_collateral_wfl(self, force=False):
        '''Cleanup of new_collateral_wfl'''
        with self.new_collateral_wfl_lock:
            wfl = self.new_collateral_wfl
            if not wfl or wfl.completed and wfl.tx_order and not force:
                return
        w = self.wallet
        if wfl.tx_order:
            for txid in wfl.tx_order[::-1]:  # use reversed tx_order
                if w.db.get_transaction(txid):
                    w.remove_transaction(txid)
                else:
                    self._cleanup_new_collateral_wfl_tx_data(txid)
        else:
            self._cleanup_new_collateral_wfl_tx_data()
        return True

    def _cleanup_new_collateral_wfl_tx_data(self, txid=None):
        '''Cleanup of new_collateral_wfl tx and its db supplementary data'''
        with self.new_collateral_wfl_lock:
            wfl = self.new_collateral_wfl
            if not wfl:
                return
            if txid:
                tx_data = wfl.pop_tx(txid)
                if tx_data:
                    self.set_new_collateral_wfl(wfl)
                    self.logger.info(f'Cleaned up new collateral tx:'
                                     f' {txid}, workflow: {wfl.lid}')
        if wfl.tx_order:
            return

        w = self.wallet
        for addr in w.db.select_ps_reserved(data=wfl.uuid):
            self.pop_ps_reserved(addr)

        with self.new_collateral_wfl_lock:
            saved = self.new_collateral_wfl
            if saved and saved.uuid == wfl.uuid:
                self.clear_new_collateral_wfl()
        self.logger.info(f'Cleaned up new collateral workflow: {wfl.lid}')

    async def broadcast_new_collateral_wfl(self):
        '''Broadcast new collateral tx from new_collateral_wfl'''
        def _check_wfl():
            with self.new_collateral_wfl_lock:
                wfl = self.new_collateral_wfl
                if not wfl:
                    return
                if not wfl.completed:
                    return
            return wfl
        wfl = await self.loop.run_in_executor(None, _check_wfl)
        if not wfl:
            return
        w = self.wallet
        tx_data = wfl.next_to_send(w)
        if not tx_data:
            return
        txid = tx_data.txid
        sent, err = await tx_data.send(self)
        if err:
            def _on_fail():
                with self.new_collateral_wfl_lock:
                    saved = self.new_collateral_wfl
                    if not saved:
                        raise Exception('new_collateral_wfl not found')
                    if saved.uuid != wfl.uuid:
                        raise Exception('new_collateral_wfl differs'
                                        ' from original')
                    self.set_new_collateral_wfl(wfl)
                self.logger.wfl_err(f'Failed broadcast of new collateral tx'
                                    f' {txid}: {err}, workflow {wfl.lid}')
            await self.loop.run_in_executor(None, _on_fail)
        if sent:
            def _on_success():
                with self.new_collateral_wfl_lock:
                    saved = self.new_collateral_wfl
                    if not saved:
                        raise Exception('new_collateral_wfl not found')
                    if saved.uuid != wfl.uuid:
                        raise Exception('new_collateral_wfl differs'
                                        ' from original')
                    self.set_new_collateral_wfl(wfl)
                self.logger.wfl_done(f'Broadcasted transaction {txid} from new'
                                     f' collateral workflow: {wfl.lid}')
                if not wfl.next_to_send(w):
                    self.logger.wfl_done(f'Broadcast completed for new'
                                         f' collateral workflow: {wfl.lid}')
            await self.loop.run_in_executor(None, _on_success)

    def _search_new_collateral_wfl(self, txid, tx):
        '''Search available new_collateral_wfl corresponding to txid/tx data'''
        err = self._check_new_collateral_tx_err(txid, tx, full_check=False)
        if not err:
            wfl = self.new_collateral_wfl
            if wfl and wfl.tx_order and txid in wfl.tx_order:
                return wfl

    def _check_on_new_collateral_wfl(self, txid, tx):
        '''Check it's new collateral tx, corresponding to new_collateral_wfl'''
        wfl = self._search_new_collateral_wfl(txid, tx)
        err = self._check_new_collateral_tx_err(txid, tx)
        if not err:
            return True
        if wfl:
            raise AddPSDataError(f'{err}')
        else:
            return False

    def _process_by_new_collateral_wfl(self, txid, tx):
        '''Process tx by new_collateral_wfl, cleanup wfl/supplementary data'''
        wfl = self._search_new_collateral_wfl(txid, tx)
        if not wfl:
            return

        with self.new_collateral_wfl_lock:
            saved = self.new_collateral_wfl
            if not saved or saved.uuid != wfl.uuid:
                return
            tx_data = wfl.pop_tx(txid)
            if tx_data:
                self.set_new_collateral_wfl(wfl)
                self.logger.wfl_done(f'Processed tx: {txid} from new'
                                     f' collateral workflow: {wfl.lid}')
        if wfl.tx_order:
            return

        w = self.wallet
        for addr in w.db.select_ps_reserved(data=wfl.uuid):
            self.pop_ps_reserved(addr)

        with self.new_collateral_wfl_lock:
            saved = self.new_collateral_wfl
            if saved and saved.uuid == wfl.uuid:
                self.clear_new_collateral_wfl()
        self.logger.wfl_done(f'Finished processing of new collateral'
                             f' workflow: {wfl.lid}')

    # Workflow methods for new denoms transaction
    def new_denoms_from_coins_info(self, coins):
        '''Get info on new new denoms wfl prepared from GUI'''
        if not coins or len(coins) > 1:
            return
        coins_val = sum([c.value_sats() for c in coins])
        if coins_val < self.min_new_denoms_from_coins_val:
            return
        fee_per_kb = self.config.fee_per_kb()
        denoms_amounts = self._calc_denoms_amounts_from_coins(coins,
                                                              fee_per_kb)
        if denoms_amounts:
            tx_cnt = len(denoms_amounts)
            outputs_val = sum([sum(amounts) for amounts in denoms_amounts])
            tx_type = SPEC_TX_NAMES[PSTxTypes.NEW_DENOMS]
            info = _('Transactions type: {}').format(tx_type)
            info += '\n'
            info += _('Count of transactions: {}').format(tx_cnt)
            info += '\n'
            info += _('Total sent amount: {}').format(coins_val)
            info += '\n'
            info += _('Total output amount: {}').format(outputs_val)
            info += '\n'
            info += _('Total fee: {}').format(coins_val - outputs_val)
            return info

    def create_new_denoms_wfl_from_gui(self, coins, password):
        '''Create new denoms txs/wfl from PS coins from GUI'''
        if self.state in self.mixing_running_states:
            return None, ('Can not create new denoms as mixing process'
                          ' is currently run.')
        if len(coins) > 1:
            return None, ('Can not create new denoms,'
                          ' too many coins selected')
        wfl, outputs_amounts = self._start_new_denoms_wfl(coins,
                                                          use_all_coins=True)
        if not outputs_amounts:
            return None, ('Can not create new denoms,'
                          ' not enough coins selected')
        if not wfl:
            return None, ('Can not create new denoms as other new'
                          ' denoms creation process is in progress')
        last_tx_idx = len(outputs_amounts) - 1
        for i, tx_amounts in enumerate(outputs_amounts):
            try:
                w = self.wallet
                txid, tx = self._make_new_denoms_tx(wfl, tx_amounts,
                                                    last_tx_idx, i,
                                                    coins, password,
                                                    use_all_coins=True)
                if not w.add_transaction(tx):
                    raise Exception(f'Transaction with txid: {txid}'
                                    f' conflicts with current history')
                if not w.db.get_ps_tx(txid)[0] == PSTxTypes.NEW_DENOMS:
                    self._add_ps_data(txid, tx, PSTxTypes.NEW_DENOMS)
                self.logger.info(f'Created new denoms tx: {txid},'
                                 f' workflow: {wfl.lid}')
                if i == last_tx_idx:
                    with self.new_denoms_wfl_lock:
                        saved = self.new_denoms_wfl
                        if not saved:
                            raise Exception('new_denoms_wfl not found')
                        if saved.uuid != wfl.uuid:
                            raise Exception('new_denoms_wfl differs'
                                            ' from original')
                        wfl.completed = True
                        self.set_new_denoms_wfl(wfl)
                        self.logger.wfl_ok(f'Completed new denoms'
                                           f' workflow: {wfl.lid}')
                    return wfl, None
                else:
                    txin0 = copy.deepcopy(tx.inputs()[0])
                    txin0_addr = w.get_txin_address(txin0)
                    utxos = w.get_utxos([txin0_addr],
                                        min_rounds=PSCoinRounds.OTHER)
                    change_outpoint = None
                    for change_idx, o in enumerate(tx.outputs()):
                        if o.address == txin0_addr:
                            change_outpoint = f'{txid}:{change_idx}'
                            break
                    coins = []
                    for utxo in utxos:
                        if utxo.prevout.to_str() != change_outpoint:
                            continue
                        coins.append(utxo)
            except Exception as e:
                err = str(e)
                self.logger.wfl_err(f'Error creating new denoms tx:'
                                    f' {err}, workflow: {wfl.lid}')
                self._cleanup_new_denoms_wfl(force=True)
                self.logger.info(f'Cleaned up new denoms workflow:'
                                 f' {wfl.lid}')
                return None, err

    async def create_new_denoms_wfl(self):
        '''Create new denoms txs/wfl from async mixing task'''
        coins_data = await self.get_next_coins_for_mixing()
        coins = coins_data['coins']
        if not coins:
            return
        _start = self._start_new_denoms_wfl
        wfl, outputs_amounts = await self.loop.run_in_executor(None, _start,
                                                               coins)
        if not wfl:
            return
        last_tx_idx = len(outputs_amounts) - 1
        for i, tx_amounts in enumerate(outputs_amounts):
            try:
                w = self.wallet
                _make_tx = self._make_new_denoms_tx
                txid, tx = await self.loop.run_in_executor(None, _make_tx,
                                                           wfl, tx_amounts,
                                                           last_tx_idx, i,
                                                           coins)
                # add_transaction need run in network therad
                if not w.add_transaction(tx):
                    raise Exception(f'Transaction with txid: {txid}'
                                    f' conflicts with current history')

                def _after_create_tx():
                    with self.new_denoms_wfl_lock:
                        self.logger.info(f'Created new denoms tx: {txid},'
                                         f' workflow: {wfl.lid}')
                        if i == last_tx_idx:
                            saved = self.new_denoms_wfl
                            if not saved:
                                raise Exception('new_denoms_wfl not found')
                            if saved.uuid != wfl.uuid:
                                raise Exception('new_denoms_wfl differs'
                                                ' from original')
                            wfl.completed = True
                            self.set_new_denoms_wfl(wfl)
                            self.logger.wfl_ok(f'Completed new denoms'
                                               f' workflow: {wfl.lid}')
                    coins_data = self._get_next_coins_for_mixing()
                    coins = coins_data['coins']
                    txin0 = copy.deepcopy(tx.inputs()[0])
                    txin0_addr = w.get_txin_address(txin0)
                    if i != last_tx_idx:
                        utxos = w.get_utxos([txin0_addr])
                        change_outpoint = None
                        for change_idx, o in enumerate(tx.outputs()):
                            if o.address == txin0_addr:
                                change_outpoint = f'{txid}:{change_idx}'
                                break
                        for utxo in utxos:
                            if utxo.prevout.to_str() != change_outpoint:
                                continue
                            coins.append(utxo)
                    if self.group_origin_coins_by_addr:
                        coins = [c for c in coins if c.address == txin0_addr]
                    return coins
                coins = await self.loop.run_in_executor(None, _after_create_tx)
                w.save_db()
            except Exception as e:
                self.logger.wfl_err(f'Error creating new denoms tx:'
                                    f' {str(e)}, workflow: {wfl.lid}')
                await self.cleanup_new_denoms_wfl(force=True)
                type_e = type(e)
                msg = None
                if type_e == NoDynamicFeeEstimates:
                    msg = self.NO_DYNAMIC_FEE_MSG.format(str(e))
                elif type_e == AddPSDataError:
                    msg = self.ADD_PS_DATA_ERR_MSG
                    type_name = SPEC_TX_NAMES[PSTxTypes.NEW_DENOMS]
                    msg = f'{msg} {type_name} {txid}:\n{str(e)}'
                elif type_e == NotFoundInKeypairs:
                    msg = self.NOT_FOUND_KEYS_MSG
                elif type_e == SignWithKeypairsFailed:
                    msg = self.SIGN_WIHT_KP_FAILED_MSG
                elif type_e == NotEnoughFunds:
                    self._not_enough_funds = True
                if msg:
                    await self.stop_mixing_from_async_thread(msg)
                break

    def _start_new_denoms_wfl(self, coins, use_all_coins=False):
        '''Calculate outputs_amounts, start new_denoms_wfl
        with appropriate locking '''
        outputs_amounts = \
            self.calc_need_denoms_amounts(coins=coins,
                                          use_all_coins=use_all_coins)
        if not outputs_amounts:
            return None, None
        with self.new_denoms_wfl_lock, \
                self.pay_collateral_wfl_lock, \
                self.new_collateral_wfl_lock:
            if self.new_denoms_wfl:
                return None, None

            uuid = str(uuid4())
            wfl = PSTxWorkflow(uuid=uuid)
            self.set_new_denoms_wfl(wfl)
            self.logger.info(f'Started up new denoms workflow: {wfl.lid}')
            return wfl, outputs_amounts

    def _make_new_denoms_tx(self, wfl, tx_amounts, last_tx_idx, i,
                            coins, password=None, use_all_coins=False):
        '''Make signed new denoms tx, based on tx_amounts, with coins supplied
        and i idx of multi tx workflow. Save tx to new_denoms_wfl'''
        w = self.wallet
        # try to create new denoms tx with change outupt at first
        addrs_cnt = len(tx_amounts)
        oaddrs = self.reserve_addresses(addrs_cnt, data=wfl.uuid)
        outputs = [PartialTxOutput.from_address_and_value(addr, a)
                   for addr, a in zip(oaddrs, tx_amounts)]
        tx = w.make_unsigned_transaction(coins=coins, outputs=outputs)
        inputs = tx.inputs()
        # check input addresses is in keypairs if keypairs cache available
        if self._keypairs_cache:
            input_addrs = [utxo.address for utxo in inputs]
            not_found_addrs = self._find_addrs_not_in_keypairs(input_addrs)
            if not_found_addrs:
                not_found_addrs = ', '.join(list(not_found_addrs))
                raise NotFoundInKeypairs(f'Input addresses is not found'
                                         f' in the keypairs cache:'
                                         f' {not_found_addrs}')
        no_change = False
        fee_per_kb = self.config.fee_per_kb()
        if i == last_tx_idx:
            if use_all_coins:
                no_change = True

        if no_change:
            tx = PartialTransaction.from_io(inputs[:], outputs[:], locktime=0)
            for txin in tx.inputs():
                txin.nsequence = 0xffffffff
        else:
            # use first input address as a change, use selected inputs
            in0 = inputs[0].address
            tx = w.make_unsigned_transaction(coins=inputs, outputs=outputs,
                                             change_addr=in0)
        tx = self.sign_transaction(tx, password)
        estimated_fee = calc_tx_fee(len(tx.inputs()), len(tx.outputs()),
                                    fee_per_kb, max_size=True)
        overfee = tx.get_fee() - estimated_fee
        assert overfee < self.min_new_collateral_from_coins_val, 'too high fee'
        txid = tx.txid()
        raw_tx = tx.serialize_to_network()
        tx_type = PSTxTypes.NEW_DENOMS
        wfl.add_tx(txid=txid, raw_tx=raw_tx, tx_type=tx_type)
        with self.new_denoms_wfl_lock:
            saved = self.new_denoms_wfl
            if not saved:
                raise Exception('new_denoms_wfl not found')
            if saved.uuid != wfl.uuid:
                raise Exception('new_denoms_wfl differs from original')
            self.set_new_denoms_wfl(wfl)
        return txid, tx

    async def cleanup_new_denoms_wfl(self, force=False):
        '''Async cleanup of new_denoms_wfl'''
        _cleanup = self._cleanup_new_denoms_wfl
        changed = await self.loop.run_in_executor(None, _cleanup, force)
        if changed:
            self.wallet.save_db()

    def _cleanup_new_denoms_wfl(self, force=False):
        '''Cleanup of new_denoms_wfl'''
        with self.new_denoms_wfl_lock:
            wfl = self.new_denoms_wfl
            if not wfl or wfl.completed and wfl.tx_order and not force:
                return
        w = self.wallet
        if wfl.tx_order:
            for txid in wfl.tx_order[::-1]:  # use reversed tx_order
                if w.db.get_transaction(txid):
                    w.remove_transaction(txid)
                else:
                    self._cleanup_new_denoms_wfl_tx_data(txid)
        else:
            self._cleanup_new_denoms_wfl_tx_data()
        return True

    def _cleanup_new_denoms_wfl_tx_data(self, txid=None):
        '''Cleanup of new_denoms_wfl tx and its db supplementary data'''
        with self.new_denoms_wfl_lock:
            wfl = self.new_denoms_wfl
            if not wfl:
                return
            if txid:
                tx_data = wfl.pop_tx(txid)
                if tx_data:
                    self.set_new_denoms_wfl(wfl)
                    self.logger.info(f'Cleaned up new denoms tx:'
                                     f' {txid}, workflow: {wfl.lid}')
        if wfl.tx_order:
            return

        w = self.wallet
        for addr in w.db.select_ps_reserved(data=wfl.uuid):
            self.pop_ps_reserved(addr)

        with self.new_denoms_wfl_lock:
            saved = self.new_denoms_wfl
            if saved and saved.uuid == wfl.uuid:
                self.clear_new_denoms_wfl()
        self.logger.info(f'Cleaned up new denoms workflow: {wfl.lid}')

    async def broadcast_new_denoms_wfl(self):
        '''Broadcast new denoms tx from new_denoms_wfl'''
        def _check_wfl():
            with self.new_denoms_wfl_lock:
                wfl = self.new_denoms_wfl
                if not wfl:
                    return
                if not wfl.completed:
                    return
            return wfl
        wfl = await self.loop.run_in_executor(None, _check_wfl)
        if not wfl:
            return
        w = self.wallet
        tx_data = wfl.next_to_send(w)
        if not tx_data:
            return
        txid = tx_data.txid
        sent, err = await tx_data.send(self)
        if err:
            def _on_fail():
                with self.new_denoms_wfl_lock:
                    saved = self.new_denoms_wfl
                    if not saved:
                        raise Exception('new_denoms_wfl not found')
                    if saved.uuid != wfl.uuid:
                        raise Exception('new_denoms_wfl differs from original')
                    self.set_new_denoms_wfl(wfl)
                self.logger.wfl_err(f'Failed broadcast of new denoms tx'
                                    f' {txid}: {err}, workflow {wfl.lid}')
            await self.loop.run_in_executor(None, _on_fail)
        if sent:
            def _on_success():
                with self.new_denoms_wfl_lock:
                    saved = self.new_denoms_wfl
                    if not saved:
                        raise Exception('new_denoms_wfl not found')
                    if saved.uuid != wfl.uuid:
                        raise Exception('new_denoms_wfl differs from original')
                    self.set_new_denoms_wfl(wfl)
                self.logger.wfl_done(f'Broadcasted transaction {txid} from new'
                                     f' denoms workflow: {wfl.lid}')
                self.last_denoms_tx_time = time.time()
                if not wfl.next_to_send(w):
                    self.logger.wfl_done(f'Broadcast completed for new denoms'
                                         f' workflow: {wfl.lid}')
            await self.loop.run_in_executor(None, _on_success)

    def _search_new_denoms_wfl(self, txid, tx):
        '''Search available new_denoms_wfl corresponding to txid/tx data'''
        err = self._check_new_denoms_tx_err(txid, tx, full_check=False)
        if not err:
            wfl = self.new_denoms_wfl
            if wfl and wfl.tx_order and txid in wfl.tx_order:
                return wfl

    def _check_on_new_denoms_wfl(self, txid, tx):
        '''Check it's new denoms tx, corresponding to new_denoms_wfl'''
        wfl = self._search_new_denoms_wfl(txid, tx)
        err = self._check_new_denoms_tx_err(txid, tx)
        if not err:
            return True
        if wfl:
            raise AddPSDataError(f'{err}')
        else:
            return False

    def _process_by_new_denoms_wfl(self, txid, tx):
        '''Process tx by new_denoms_wfl, cleanup wfl/supplementary data'''
        wfl = self._search_new_denoms_wfl(txid, tx)
        if not wfl:
            return

        with self.new_denoms_wfl_lock:
            saved = self.new_denoms_wfl
            if not saved or saved.uuid != wfl.uuid:
                return
            tx_data = wfl.pop_tx(txid)
            if tx_data:
                self.set_new_denoms_wfl(wfl)
                self.logger.wfl_done(f'Processed tx: {txid} from new denoms'
                                     f' workflow: {wfl.lid}')
        if wfl.tx_order:
            return

        w = self.wallet
        for addr in w.db.select_ps_reserved(data=wfl.uuid):
            self.pop_ps_reserved(addr)

        with self.new_denoms_wfl_lock:
            saved = self.new_denoms_wfl
            if saved and saved.uuid == wfl.uuid:
                self.clear_new_denoms_wfl()
        self.logger.wfl_done(f'Finished processing of new denoms'
                             f' workflow: {wfl.lid}')

    # Workflow methods for denominate transaction
    async def cleanup_staled_denominate_wfls(self):
        '''Async task to cleanup already completed and stalled
        denominate workflows (no corresponding tx from MNs is arrived)'''
        def _cleanup_staled():
            changed = False
            for uuid in self.denominate_wfl_list:
                wfl = self.get_denominate_wfl(uuid)
                if not wfl or not wfl.completed:
                    continue
                now = time.time()
                if now - wfl.completed > self.wait_for_mn_txs_time:
                    self.logger.info(f'Cleaning staled denominate'
                                     f' workflow: {wfl.lid}')
                    self._cleanup_denominate_wfl(wfl)
                    changed = True
            return changed
        while True:
            if self.enabled:
                done = await self.loop.run_in_executor(None, _cleanup_staled)
                if done:
                    self.wallet.save_db()
            await asyncio.sleep(self.wait_for_mn_txs_time/12)

    async def start_denominate_wfl(self):
        '''Select suitable masternode and start single denominate workflow '''
        wfl = None
        try:
            _start = self._start_denominate_wfl
            dsq = None
            session = None
            if random.random() > 0.33:
                self.logger.debug('try to get masternode from recent dsq')
                recent_mns = self.recent_mixes_mns
                while self.state == PSStates.Mixing:
                    dsq = self.dash_net.get_recent_dsq(recent_mns)
                    if dsq is not None:
                        self.logger.debug(f'get dsq from recent dsq queue'
                                          f' {dsq.masternodeOutPoint}')
                        dval = PS_DENOM_REVERSE_DICT[dsq.nDenom]
                        wfl = await self.loop.run_in_executor(None,
                                                              _start, dval)
                        break
                    await asyncio.sleep(0.5)
            else:
                self.logger.debug('try to create new queue'
                                  ' on random masternode')
                wfl = await self.loop.run_in_executor(None, _start)
            if not wfl:
                return

            if self.state != PSStates.Mixing:
                raise Exception('Mixing is finished')
            else:
                session = await self.start_mix_session(wfl.denom, dsq, wfl.lid)

            pay_collateral_tx = self.get_pay_collateral_tx()
            if not pay_collateral_tx:
                raise Exception('Absent suitable pay collateral tx')
            if self.gather_mix_stat:
                self.mix_stat.dsa.send_msg()
            await session.send_dsa(pay_collateral_tx)
            while True:
                cmd, res = await session.read_next_msg(wfl)
                if cmd == 'dssu':
                    if self.gather_mix_stat:
                        self.mix_stat.dsa.on_dssu()
                    continue
                elif cmd == 'dsq':
                    if self.gather_mix_stat:
                        self.mix_stat.dsa.on_read_msg()
                    if session.fReady:
                        break
                    self.logger.debug(f'denominate workflow {wfl.lid}:'
                                      f' dsq not fReady')
                else:
                    raise Exception(f'Unsolicited cmd: {cmd} after dsa sent')

            pay_collateral_tx = self.get_pay_collateral_tx()
            if not pay_collateral_tx:
                raise Exception('Absent suitable pay collateral tx')

            final_tx = None
            if self.gather_mix_stat:
                self.mix_stat.dsi.send_msg()
            await session.send_dsi(wfl.inputs, pay_collateral_tx, wfl.outputs)
            while True:
                cmd, res = await session.read_next_msg(wfl)
                if cmd == 'dssu':
                    if self.gather_mix_stat:
                        self.mix_stat.dsi.on_dssu()
                    continue
                elif cmd == 'dsf':
                    if self.gather_mix_stat:
                        self.mix_stat.dsi.on_read_msg()
                    final_tx = PartialTransaction.from_tx(res)
                    break
                else:
                    raise Exception(f'Unsolicited cmd: {cmd} after dsi sent')

            signed_inputs = self._sign_inputs(final_tx, wfl.inputs)
            if self.gather_mix_stat:
                self.mix_stat.dss.send_msg()
            await session.send_dss(signed_inputs)
            while True:
                cmd, res = await session.read_next_msg(wfl)
                if cmd == 'dssu':
                    if self.gather_mix_stat:
                        self.mix_stat.dss.on_dssu()
                    continue
                elif cmd == 'dsc':
                    if self.gather_mix_stat:
                        self.mix_stat.dss.on_read_msg()
                    def _on_dsc():
                        with self.denominate_wfl_lock:
                            saved = self.get_denominate_wfl(wfl.uuid)
                            if saved:
                                saved.completed = time.time()
                                self.set_denominate_wfl(saved)
                                return saved
                            else:  # already processed from _add_ps_data
                                self.logger.debug(f'denominate workflow:'
                                                  f' {wfl.lid} not found')
                    saved = await self.loop.run_in_executor(None, _on_dsc)
                    if saved:
                        wfl = saved
                        self.wallet.save_db()
                    break
                else:
                    raise Exception(f'Unsolisited cmd: {cmd} after dss sent')
            self.logger.wfl_ok(f'Completed denominate workflow: {wfl.lid}')
        except Exception as e:
            type_e = type(e)
            if self.gather_mix_stat:
                if type_e == MixSessionTimeout:
                    self.mix_stat.on_timeout()
                elif type_e == MixSessionPeerClosed:
                    self.mix_stat.on_peer_closed()
                else:
                    self.mix_stat.on_error()
            if (type_e == MixSessionTimeout
                    and session and session.state == DSPoolState.QUEUE):
                denom_amount = util.format_satoshis(session.denom_value)
                self.logger.info(f'Denominate workflow {wfl.lid}:'
                                 f' timed out in pool state QUEUE,'
                                 f' no peers to mix {denom_amount} DASH')
            elif type_e != asyncio.CancelledError:
                if wfl:
                    self.logger.wfl_err(f'Error in denominate workflow:'
                                        f' {str(e)}, workflow: {wfl.lid}')
                else:
                    self.logger.wfl_err(f'Error during creation of denominate'
                                        f' workflow: {str(e)}')
                msg = None
                if type_e == NoDynamicFeeEstimates:
                    msg = self.NO_DYNAMIC_FEE_MSG.format(str(e))
                elif type_e == NotFoundInKeypairs:
                    msg = self.NOT_FOUND_KEYS_MSG
                elif type_e == SignWithKeypairsFailed:
                    msg = self.SIGN_WIHT_KP_FAILED_MSG
                if msg:
                    await self.stop_mixing_from_async_thread(msg)
        finally:
            if session:
                await self.stop_mix_session(session.peer_str)
            if wfl:
                await self.cleanup_denominate_wfl(wfl)

    def _select_denoms_to_mix(self, denom_value=None):
        '''Select denoms to mix based on denom value'''
        if not self._denoms_to_mix_cache:
            self.logger.debug('No suitable denoms to mix,'
                              ' _denoms_to_mix_cache is empty')
            return None, None

        if denom_value is not None:
            denoms = self.denoms_to_mix(denom_value=denom_value)
        else:
            denoms = self.denoms_to_mix()
        outpoints = list(denoms.keys())

        w = self.wallet
        icnt = 0
        txids = []
        inputs = []
        while icnt < random.randint(1, PRIVATESEND_ENTRY_MAX_SIZE):
            if not outpoints:
                break

            outpoint = outpoints.pop(random.randint(0, len(outpoints)-1))
            if not w.db.get_ps_denom(outpoint):  # already spent
                continue

            if w.db.get_ps_spending_denom(outpoint):  # reserved to spend
                continue

            txid = outpoint.split(':')[0]
            if txid in txids:  # skip outputs from same tx
                continue

            height = w.get_tx_height(txid).height
            islock = w.db.get_islock(txid)
            if not islock and height <= 0:  # skip not islocked/confirmed
                continue

            denom = denoms.pop(outpoint)
            if denom[2] >= self.mix_rounds:
                continue

            if not self.is_ps_ks(denom[0]) and self.is_hw_ks:
                continue  # skip denoms on hw keystore

            if denom_value is None:
                denom_value = denom[1]
            elif denom[1] != denom_value:  # skip other denom values
                continue

            inputs.append(outpoint)
            txids.append(txid)
            icnt += 1

        if not inputs:
            self.logger.debug(f'No suitable denoms to mix:'
                              f' denom_value={denom_value}')
            return None, None
        else:
            return inputs, denom_value

    def _start_denominate_wfl(self, denom_value=None):
        '''Start and save denominate wfl based on selected denom_value'''
        if self.active_denominate_wfl_cnt >= self.max_sessions:
            return
        selected_inputs, denom_value = self._select_denoms_to_mix(denom_value)
        if not selected_inputs:
            return

        with self.denominate_wfl_lock, self.denoms_lock:
            if self.active_denominate_wfl_cnt >= self.max_sessions:
                return
            icnt = 0
            inputs = []
            input_addrs = []
            w = self.wallet
            for outpoint in selected_inputs:
                denom = w.db.get_ps_denom(outpoint)
                if not denom:
                    continue  # already spent
                if w.db.get_ps_spending_denom(outpoint):
                    continue  # already used by other wfl
                if self.is_hw_ks and not self.is_ps_ks(denom[0]):
                    continue  # skip denoms from hardware keystore
                inputs.append(outpoint)
                input_addrs.append(denom[0])
                icnt += 1

            if icnt < 1:
                self.logger.debug(f'No suitable denoms to mix after'
                                  f' denoms_lock: denom_value={denom_value}')
                return

            uuid = str(uuid4())
            wfl = PSDenominateWorkflow(uuid=uuid)
            wfl.inputs = inputs
            wfl.denom = denom_value
            self.set_denominate_wfl(wfl)
            for outpoint in inputs:
                self.add_ps_spending_denom(outpoint, wfl.uuid)

        # check input addresses is in keypairs if keypairs cache available
        if self._keypairs_cache:
            not_found_addrs = self._find_addrs_not_in_keypairs(input_addrs)
            if not_found_addrs:
                not_found_addrs = ', '.join(list(not_found_addrs))
                raise NotFoundInKeypairs(f'Input addresses is not found'
                                         f' in the keypairs cache:'
                                         f' {not_found_addrs}')

        output_addrs = []
        found_outpoints = []
        for addr, data in w.db.get_ps_reserved().items():
            if data in inputs:
                output_addrs.append(addr)
                found_outpoints.append(data)
        for outpoint in inputs:
            if outpoint not in found_outpoints:
                force_main_ks = False
                if self.is_hw_ks:
                    denom = w.db.get_ps_denom(outpoint)
                    if denom[2] == self.mix_rounds - 1:
                        force_main_ks = True
                reserved = self.reserve_addresses(1, data=outpoint,
                                                  force_main_ks=force_main_ks)
                output_addrs.append(reserved[0])

        with self.denominate_wfl_lock:
            saved = self.get_denominate_wfl(wfl.uuid)
            if not saved:
                raise Exception(f'denominate_wfl {wfl.lid} not found')
            wfl = saved
            wfl.outputs = output_addrs
            self.set_denominate_wfl(saved)

        self.logger.info(f'Created denominate workflow: {wfl.lid}, with inputs'
                         f' value {wfl.denom}, count {len(wfl.inputs)}')
        return wfl

    def _sign_inputs(self, tx, inputs):
        '''Sign denominate tx inputs from dsf message'''
        signed_inputs = []
        tx = self._sign_denominate_tx(tx)
        for i in tx.inputs():
            if i.prevout.to_str() not in inputs:
                continue
            signed_inputs.append(CTxIn(i.prevout.txid[::-1], i.prevout.out_idx,
                                       i.script_sig, i.nsequence))
        return signed_inputs

    def _sign_denominate_tx(self, tx):
        '''Sign denominate tx to prepare signed inputs for dss message'''
        w = self.wallet
        mine_txins_cnt = 0
        for txin in tx.inputs():
            if not w.is_mine(w.get_txin_address(txin)):
                continue
            w.add_input_info(txin)
            mine_txins_cnt += 1
        self.sign_transaction(tx, None, mine_txins_cnt)
        return tx

    async def cleanup_denominate_wfl(self, wfl):
        '''Async cleanup of denominate_wfl'''
        _cleanup = self._cleanup_denominate_wfl
        changed = await self.loop.run_in_executor(None, _cleanup, wfl)
        if changed:
            self.wallet.save_db()

    def _cleanup_denominate_wfl(self, wfl):
        '''Cleanup of denominate_wfl and its db supplementary data'''
        with self.denominate_wfl_lock:
            saved = self.get_denominate_wfl(wfl.uuid)
            if not saved:  # already processed from _add_ps_data
                return
            else:
                wfl = saved

            completed = wfl.completed
            if completed:
                now = time.time()
                if now - wfl.completed <= self.wait_for_mn_txs_time:
                    return

        w = self.wallet
        for outpoint, uuid in list(w.db.get_ps_spending_denoms().items()):
            if uuid != wfl.uuid:
                continue
            with self.denoms_lock:
                self.pop_ps_spending_denom(outpoint)

        with self.denominate_wfl_lock:
            self.clear_denominate_wfl(wfl.uuid)
        self.logger.info(f'Cleaned up denominate workflow: {wfl.lid}')
        return True

    def _search_denominate_wfl(self, txid, tx):
        '''Search available denominate_wfl corresponding to txid/tx data'''
        err = self._check_denominate_tx_err(txid, tx, full_check=False)
        if not err:
            for uuid in self.denominate_wfl_list:
                wfl = self.get_denominate_wfl(uuid)
                if not wfl or not wfl.completed:
                    continue
                if self._check_denominate_tx_io_on_wfl(txid, tx, wfl):
                    return wfl

    def _check_on_denominate_wfl(self, txid, tx):
        '''Check it's new denoms tx, corresponding to denominate_wfl'''
        wfl = self._search_denominate_wfl(txid, tx)
        err = self._check_denominate_tx_err(txid, tx)
        if not err:
            return True
        if wfl:
            raise AddPSDataError(f'{err}')
        else:
            return False

    def _process_by_denominate_wfl(self, txid, tx):
        '''Process tx by denominate_wfl, cleanup wfl/supplementary data'''
        wfl = self._search_denominate_wfl(txid, tx)
        if not wfl:
            return

        w = self.wallet
        for outpoint, uuid in list(w.db.get_ps_spending_denoms().items()):
            if uuid != wfl.uuid:
                continue
            with self.denoms_lock:
                self.pop_ps_spending_denom(outpoint)

        with self.denominate_wfl_lock:
            self.clear_denominate_wfl(wfl.uuid)
        self.logger.wfl_done(f'Finished processing of denominate'
                             f' workflow: {wfl.lid} with tx: {txid}')
