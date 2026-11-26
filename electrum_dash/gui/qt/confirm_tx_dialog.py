#!/usr/bin/env python
#
# Electrum - lightweight Bitcoin client
# Copyright (2019) The Electrum Developers
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

from decimal import Decimal
from typing import TYPE_CHECKING, NamedTuple, Optional, Union

from PyQt5.QtGui import QTextOption
from PyQt5.QtWidgets import QVBoxLayout, QLabel, QGridLayout, QPushButton, QLineEdit, QPlainTextEdit

from electrum_firo.i18n import _
from electrum_firo.util import NotEnoughFunds, NoDynamicFeeEstimates
from electrum_firo.plugin import run_hook
from electrum_firo.transaction import Transaction, PartialTransaction
from electrum_firo.simple_config import FEERATE_WARNING_HIGH_FEE, FEE_RATIO_HIGH_WARNING
from electrum_firo.wallet import InternalAddressCorruption

from .util import (WindowModalDialog, ColorScheme, HelpLabel, Buttons, CancelButton,
                   WaitingDialog, PasswordLineEdit)

from .fee_slider import FeeSlider, FeeComboBox

if TYPE_CHECKING:
    from .main_window import ElectrumWindow



class TxEditor:

    def __init__(self, *, window: 'ElectrumWindow', make_tx,
                 output_value: Union[int, str] = None, is_sweep: bool,
                 is_ps_tx=False):
        self.main_window = window
        self.make_tx = make_tx
        self.output_value = output_value
        self.tx = None  # type: Optional[PartialTransaction]
        self.config = window.config
        self.wallet = window.wallet
        self.not_enough_funds = False
        self.no_dynfee_estimates = False
        self.needs_update = False
        self.is_ps_tx = is_ps_tx
        self.password_required = self.wallet.has_keystore_encryption() and not is_sweep
        self.main_window.gui_object.timer.timeout.connect(self.timer_actions)

    def timer_actions(self):
        if self.needs_update:
            self.update_tx()
            self.update()
            self.needs_update = False

    def fee_slider_callback(self, dyn, pos, fee_rate):
        if dyn:
            if self.config.use_mempool_fees():
                self.config.set_key('depth_level', pos, False)
            else:
                self.config.set_key('fee_level', pos, False)
        else:
            self.config.set_key('fee_per_kb', fee_rate, False)
        self.needs_update = True

    def get_fee_estimator(self):
        return None

    def update_tx(self, *, fallback_to_zero_fee: bool = False):
        fee_estimator = self.get_fee_estimator()
        try:
            self.tx = self.make_tx(fee_estimator)
            self.not_enough_funds = False
            self.no_dynfee_estimates = False
        except NotEnoughFunds:
            self.not_enough_funds = True
            self.tx = None
            if fallback_to_zero_fee:
                try:
                    self.tx = self.make_tx(0)
                except BaseException:
                    return
            else:
                return
        except NoDynamicFeeEstimates:
            self.no_dynfee_estimates = True
            self.tx = None
            try:
                self.tx = self.make_tx(0)
            except NotEnoughFunds:
                self.not_enough_funds = True
                return
            except BaseException:
                return
        except InternalAddressCorruption as e:
            self.tx = None
            self.main_window.show_error(str(e))
            raise

    def bg_check_have_enough_funds_assuming_zero_fees(self, on_success):
        parent = self.main_window

        def _bg_have_enough_funds_assuming_zero_fees():
            try:
                tx = self.make_tx(0)
            except NotEnoughFunds:
                return False
            else:
                return True
        WaitingDialog(parent, _("Preparing transaction..."),
                      _bg_have_enough_funds_assuming_zero_fees,
                      on_success, parent.on_error)




class PaymentRoute(NamedTuple):
    address: str
    from_spark: bool
    to_spark: bool
    own_spark: bool = False

    def summary(self):
        source = _('Spark') if self.from_spark else _('Transparent')
        destination = _('Spark') if self.to_spark else _('Transparent')
        if self.own_spark:
            destination = _('Your Spark balance')
        return _('{source} → {destination}').format(source=source, destination=destination)

    def disclosure(self):
        if self.from_spark and self.to_spark:
            return _('Spark hides the sender, recipient and payment amount on the blockchain.')
        if self.to_spark:
            return _('The transparent inputs and amount entering Spark are public. The receiving Spark address stays private.')
        if self.from_spark:
            return _('The recipient address and payment amount will be public. Your Spark address stays private.')
        return _('The funding inputs, recipient address and payment amount will be public.')

    def action(self):
        if self.own_spark:
            return _('Move to Spark')
        if self.from_spark and self.to_spark:
            return _('Send privately')
        return _('Send to Spark address') if self.to_spark else _('Send to transparent address')


class ConfirmTxDialog(TxEditor, WindowModalDialog):
    # set fee and return password (after pw check)

    def __init__(self, *, window: 'ElectrumWindow', make_tx, output_value: Union[int, str], is_sweep: bool,
        is_ps_tx=False, route: Optional[PaymentRoute] = None):

        TxEditor.__init__(self, window=window, make_tx=make_tx, output_value=output_value,
                          is_sweep=is_sweep, is_ps_tx=is_ps_tx)
        self.route = route
        self.wallet_note = ''
        WindowModalDialog.__init__(self, window, _("Review payment") if route else _("Confirm Transaction"))
        vbox = QVBoxLayout()
        self.setLayout(vbox)
        if route:
            # A Spark address has 144 characters. Keep it visible at the user's font size.
            self.setMinimumWidth(48 * self.fontMetrics().averageCharWidth() + 72)
            vbox.addWidget(QLabel(route.summary()))
            recipient = QPlainTextEdit(route.address)
            recipient.setAccessibleName(_('Recipient address'))
            recipient.setReadOnly(True)
            recipient.setWordWrapMode(QTextOption.WrapAnywhere)
            lines = 5 if route.to_spark else 2
            recipient.setFixedHeight(lines * recipient.fontMetrics().lineSpacing() + 12)
            recipient.setTabChangesFocus(True)
            vbox.addWidget(QLabel(_('Your Spark address') if route.own_spark else _('Recipient address')))
            vbox.addWidget(recipient)
        grid = QGridLayout()
        vbox.addLayout(grid)
        self.amount_label = QLabel('')
        amount_title = _('Recipient receives') if route else _('Amount to be sent')
        if route and route.own_spark:
            amount_title = _('Move to Spark')
        grid.addWidget(QLabel(amount_title), 0, 0)
        grid.addWidget(self.amount_label, 0, 1)

        msg = _('The network fee is paid by the sender.')
        self.fee_label = QLabel('')
        grid.addWidget(HelpLabel(_("Network fee"), msg), 1, 0)
        grid.addWidget(self.fee_label, 1, 1)

        self.extra_fee_label = QLabel(_("Additional fees") + ": ")
        self.extra_fee_label.setVisible(False)
        self.extra_fee_value = QLabel('')
        self.extra_fee_value.setVisible(False)
        grid.addWidget(self.extra_fee_label, 2, 0)
        grid.addWidget(self.extra_fee_value, 2, 1)

        self.fee_slider = FeeSlider(self, self.config, self.fee_slider_callback)
        self.fee_combo = FeeComboBox(self.fee_slider)
        self.fee_combo.setMinimumHeight(self.fee_combo.fontMetrics().height() + 12)
        if route and route.from_spark:
            self.fee_slider.hide()
            self.fee_combo.hide()
        else:
            grid.addWidget(HelpLabel(_("Fee rate") + ": ", self.fee_combo.help_msg), 5, 0)
            grid.addWidget(self.fee_slider, 5, 1)
            grid.addWidget(self.fee_combo, 5, 2)
        if route:
            self.total_label = QLabel()
            grid.addWidget(QLabel(_('Total from Spark') if route.from_spark else _('Total from Transparent')), 3, 0)
            grid.addWidget(self.total_label, 3, 1)
            self.batch_label = QLabel()
            self.batch_label.hide()
            grid.addWidget(self.batch_label, 4, 0, 1, -1)
            disclosure = QLabel(route.disclosure())
            if route.from_spark and not route.to_spark:
                disclosure.setObjectName('public_payment_notice')
            disclosure.setWordWrap(True)
            vbox.addWidget(disclosure)

        self.message_label = QLabel(self.default_message())
        grid.addWidget(self.message_label, 6, 0, 1, -1)
        self.pw_label = QLabel(_('Password'))
        self.pw = PasswordLineEdit()
        grid.addWidget(self.pw_label, 8, 0)
        grid.addWidget(self.pw, 8, 1, 1, -1)
        self.preview_button = QPushButton(_('Advanced'))
        self.preview_button.clicked.connect(self.on_preview)
        grid.addWidget(self.preview_button, 0, 2)
        self.send_button = QPushButton(route.action() if route else _('Send'))
        self.send_button.setObjectName('primary_action')
        self.send_button.clicked.connect(self.on_send)
        self.send_button.setDefault(True)
        vbox.addLayout(Buttons(CancelButton(self), self.send_button))
        self.is_send = False

    def bg_update(self, on_success):
        parent = self.main_window
        WaitingDialog(parent, _("Preparing transaction..."), self.update_tx,
                      on_success, parent.on_error)

    def default_message(self):
        return _('Enter your password to proceed') if self.password_required else ''

    def on_preview(self):
        self.accept()

    def run(self):
        cancelled = not self.exec_()
        password = self.pw.text() or None
        return cancelled, self.is_send, password, self.tx

    def on_send(self):
        password = self.pw.text() or None
        if self.password_required:
            if password is None:
                self.main_window.show_error(_("Password required"), parent=self)
                return
            try:
                psman = self.wallet.psman
                if psman.is_hw_ks and psman.is_ps_ks_encrypted():
                    psman.ps_keystore.check_password(password)
                else:
                    self.wallet.check_password(password)
            except Exception as e:
                self.main_window.show_error(str(e), parent=self)
                return
        self.is_send = True
        self.accept()

    def toggle_send_button(self, enable: bool, *, message: str = None):
        if message is None:
            self.message_label.setStyleSheet(None)
            self.message_label.setText(self.default_message())
        else:
            self.message_label.setStyleSheet(ColorScheme.RED.as_stylesheet())
            self.message_label.setText(message)
        self.pw.setEnabled(enable)
        self.send_button.setEnabled(enable)

    def update(self):
        tx = self.tx
        amount = getattr(tx, '_spark_send_amount', self.output_value)
        if tx and amount == '!':
            if self.route:
                amount = sum(o.value for o in tx.outputs() if o.address == self.route.address)
            else:
                amount = tx.output_value()
        self.amount_label.setText(_('Max') if amount == '!' else self.main_window.format_amount_and_units(amount))

        if self.not_enough_funds:
            text = (_('Not enough available Spark funds for this payment and its fee.')
                    if self.route and self.route.from_spark
                    else self.main_window.get_text_not_enough_funds_mentioning_frozen())
            self.toggle_send_button(False, message=text)
            return

        if not tx:
            return

        psman = self.main_window.wallet.psman
        if (psman.is_hw_ks
                and psman.is_ps_ks_inputs_in_tx(tx)
                and psman.is_ps_ks_encrypted()):
            self.password_required = True

        self.pw_label.setVisible(self.password_required)
        self.pw.setVisible(self.password_required)

        fee = getattr(tx, '_spark_fee', None)
        if fee is None:
            fee = tx.get_fee()
        assert fee is not None
        self.fee_label.setText(self.main_window.format_amount_and_units(fee))
        batch = [tx] + list(getattr(tx, 'sparkMints', []))
        extra_fees = 0
        for part in batch:
            extra = run_hook('get_tx_extra_fee', self.wallet, part)
            if extra:
                extra_fees += extra[1]
        self.extra_fee_label.setVisible(bool(extra_fees))
        self.extra_fee_value.setVisible(bool(extra_fees))
        self.extra_fee_value.setText(self.main_window.format_amount_and_units(extra_fees))
        tx_size = sum(part.estimated_size() for part in batch)
        if self.route:
            total = amount + fee + extra_fees
            self.total_label.setText(self.main_window.format_amount_and_units(total))
            self.batch_label.setText(_('This payment uses {} transactions.').format(len(batch)))
            self.batch_label.setVisible(len(batch) > 1)
        fee_warning_tuple = self.wallet.get_tx_fee_warning(
            invoice_amt=amount, tx_size=tx_size, fee=fee)
        if fee_warning_tuple:
            allow_send, long_warning, short_warning = fee_warning_tuple
            self.toggle_send_button(allow_send, message=long_warning)
        else:
            self.toggle_send_button(True)
