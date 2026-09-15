"""Rosen payment-URI validation and transparent transaction outputs."""

import re

from .bitcoin import make_op_return
from .i18n import _
from .transaction import PartialTxOutput
from .util import InvalidBitcoinURI, InvoiceError


CHAIN_NAMES = ('Ergo', 'Cardano', 'Bitcoin', 'Ethereum', 'Binance', 'Doge',
               'Bitcoin Runes', 'Firo')


def parse_payload(hex_data: str) -> bytes:
    # Match the URI contract used by Firo Qt: chain, two BE64 fees,
    # one address-length byte, then the destination address bytes.
    if (not isinstance(hex_data, str) or not 38 <= len(hex_data) <= 160
            or len(hex_data) % 2 or not re.fullmatch('[0-9a-fA-F]+', hex_data)):
        raise InvalidBitcoinURI(_('Invalid Rosen OP_RETURN hex (maximum 80 bytes).'))
    data = bytes.fromhex(hex_data)
    if data[0] >= len(CHAIN_NAMES) or data[17] == 0 or len(data) != 18 + data[17]:
        raise InvalidBitcoinURI(_('Invalid Rosen destination chain or address length.'))
    return data


def normalize_uri_query(query: dict) -> dict:
    """Validate bridge-specific fields before the ordinary amount conversion."""
    out = {}
    for key, value in query.items():
        if key.startswith('req-'):
            key = key[4:]
            if key not in ('address', 'amount', 'label', 'message', 'op_return'):
                raise InvalidBitcoinURI(_('Unsupported required bridge URI parameter.'))
        if key in out:
            raise InvalidBitcoinURI(_('Duplicate bridge URI parameter.'))
        out[key] = value
    out['op_return'] = parse_payload(out['op_return']).hex()
    if any(key in out for key in ('r', 'name', 'sig')):
        raise InvalidBitcoinURI(_('Bridge metadata cannot be combined with a payment request.'))
    if not re.fullmatch(r'[0-9]+(?:\.[0-9]{1,8})?', out.get('amount', '')):
        raise InvalidBitcoinURI(_('A bridge URI requires an exact decimal FIRO amount.'))
    return out


def add_output(outputs, uri, *, payment_request=None):
    """Bind metadata to its URI recipient/amount; also safe to call twice."""
    outputs = list(outputs)
    if not uri or 'op_return' not in uri:
        return outputs
    if payment_request:
        raise InvoiceError(_('Bridge metadata cannot be combined with a payment request.'))
    try:
        data = parse_payload(uri['op_return'])
        recipient = PartialTxOutput.from_address_and_value(uri['address'], uri['amount'])
    except (InvalidBitcoinURI, KeyError, ValueError) as e:
        raise InvoiceError(_('Invalid bridge payment URI.')) from e
    metadata = PartialTxOutput(scriptpubkey=make_op_return(data), value=0)
    expected = [recipient, metadata]
    # Do not silently attach old metadata to an edited recipient or amount.
    if (len(outputs) not in (1, 2)
            or any((actual.scriptpubkey, actual.value) != (wanted.scriptpubkey, wanted.value)
                   for actual, wanted in zip(outputs, expected))):
        raise InvoiceError(_('Bridge recipient or amount changed. Clear the form and paste a new URI.'))
    return expected


def bridge_payload(outputs):
    """Recognize canonical Rosen outputs, including those in saved invoices."""
    for output in outputs:
        script = output.scriptpubkey
        if not script or script[0] != 0x6a:
            continue
        offset = 3 if script[1:2] == b'\x4c' else 2
        try:
            data = parse_payload(script[offset:].hex())
        except InvalidBitcoinURI:
            continue
        if script == make_op_return(data):
            return data
    return None


def check_send(outputs, *, is_private=False, tx_type=0):
    """Check requested outputs before coin selection adds change."""
    data = bridge_payload(outputs)
    if data is None:
        return
    recipients = [o for o in outputs if o.address]
    metadata = [o for o in outputs if o.scriptpubkey == make_op_return(data)]
    if (len(outputs) != 2 or len(recipients) != 1 or len(metadata) != 1
            or metadata[0].value != 0
            or not isinstance(recipients[0].value, int) or recipients[0].value <= 0
            or is_private or tx_type):
        raise InvoiceError(_('Use one fixed-amount, regular transparent payment per bridge URI.'))


def format_details(data: bytes) -> str:
    return _('Rosen Bridge\nDestination chain: {}\nDestination address (hex): {}\n'
             'Bridge fee: {} atomic units\nNetwork fee: {} atomic units').format(
                 CHAIN_NAMES[data[0]], data[18:].hex(),
                 int.from_bytes(data[1:9], 'big'), int.from_bytes(data[9:17], 'big'))
