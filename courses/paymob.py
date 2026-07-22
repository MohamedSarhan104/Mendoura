import hashlib
import hmac

import requests
from django.conf import settings

BASE_URL = 'https://accept.paymob.com/api'

# Exact field order Paymob documents for the transaction-processed callback's
# HMAC calculation. Do not reorder -- the signature will not match.
HMAC_FIELDS = [
    'amount_cents', 'created_at', 'currency', 'error_occured',
    'has_parent_transaction', 'id', 'integration_id', 'is_3d_secure',
    'is_auth', 'is_capture', 'is_refunded', 'is_standalone_payment',
    'is_voided', 'order', 'owner', 'pending', 'source_data_pan',
    'source_data_sub_type', 'source_data_type', 'success',
]


class PaymobError(Exception):
    pass


def get_auth_token() -> str:
    response = requests.post(
        f'{BASE_URL}/auth/tokens', json={'api_key': settings.PAYMOB_API_KEY}, timeout=15)
    response.raise_for_status()
    return response.json()['token']


def create_order(auth_token: str, amount_cents: int, merchant_order_id: str) -> int:
    response = requests.post(f'{BASE_URL}/ecommerce/orders', json={
        'auth_token': auth_token,
        'delivery_needed': False,
        'amount_cents': amount_cents,
        'currency': 'EGP',
        'merchant_order_id': merchant_order_id,
        'items': [],
    }, timeout=15)
    response.raise_for_status()
    return response.json()['id']


def get_payment_key(auth_token: str, order_id: int, amount_cents: int, billing_data: dict) -> str:
    response = requests.post(f'{BASE_URL}/acceptance/payment_keys', json={
        'auth_token': auth_token,
        'amount_cents': amount_cents,
        'expiration': 3600,
        'order_id': order_id,
        'billing_data': billing_data,
        'currency': 'EGP',
        'integration_id': settings.PAYMOB_INTEGRATION_ID_CARD,
    }, timeout=15)
    response.raise_for_status()
    return response.json()['token']


def get_checkout_iframe_url(payment_token: str) -> str:
    return f'{BASE_URL}/acceptance/iframes/{settings.PAYMOB_IFRAME_ID}?payment_token={payment_token}'


def initiate_checkout(amount_cents: int, merchant_order_id: str, billing_data: dict) -> str:
    """Runs Paymob's three-step checkout flow and returns the iframe URL to redirect to."""
    auth_token = get_auth_token()
    order_id = create_order(auth_token, amount_cents, merchant_order_id)
    payment_token = get_payment_key(auth_token, order_id, amount_cents, billing_data)
    return get_checkout_iframe_url(payment_token)


def flatten_callback_obj(obj: dict) -> dict:
    """Paymob's raw callback `obj` nests `order` as an object and `source_data`
    as a sub-dict. HMAC verification needs the flat field names Paymob's docs
    use: order's bare id, and source_data.{pan,sub_type,type} flattened out."""
    flat = dict(obj)
    order = obj.get('order')
    flat['order'] = order.get('id') if isinstance(order, dict) else order
    owner = obj.get('owner')
    flat['owner'] = owner.get('id') if isinstance(owner, dict) else owner
    source_data = obj.get('source_data') or {}
    flat['source_data_pan'] = source_data.get('pan', '')
    flat['source_data_sub_type'] = source_data.get('sub_type', '')
    flat['source_data_type'] = source_data.get('type', '')
    return flat


def verify_hmac(transaction_data: dict, received_hmac: str) -> bool:
    """Recompute Paymob's HMAC over the callback's transaction fields and compare
    using a constant-time check. `transaction_data` is the callback's `obj`, with
    `order` and `owner` already reduced to their bare IDs and `source_data.*`
    fields flattened to `source_data_pan` / `source_data_sub_type` / `source_data_type`."""
    concatenated = ''.join(str(transaction_data.get(field, '')) for field in HMAC_FIELDS)
    computed = hmac.new(
        settings.PAYMOB_HMAC_SECRET.encode(), concatenated.encode(), hashlib.sha512
    ).hexdigest()
    return hmac.compare_digest(computed, received_hmac or '')
