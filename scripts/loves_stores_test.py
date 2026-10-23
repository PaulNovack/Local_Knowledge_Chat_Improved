"""Download Love's store and fuel data with the approved client-credentials API."""
import argparse
import json
import os
from pathlib import Path
import sys
from urllib.parse import urljoin

import httpx

DEFAULT_API_BASE_URL = 'https://apis.loves.com/locations/api/'


def acquire_access_token(
    client: httpx.Client,
    token_url: str,
    client_id: str,
    client_secret: str,
    scope: str = '',
    auth_method: str = 'basic',
) -> str:
    form = {'grant_type': 'client_credentials'}
    if scope:
        form['scope'] = scope

    auth = None
    if auth_method == 'basic':
        auth = (client_id, client_secret)
    elif auth_method == 'body':
        form.update({'client_id': client_id, 'client_secret': client_secret})
    else:
        raise ValueError("LOVES_TOKEN_AUTH must be 'basic' or 'body'")

    response = client.post(token_url, data=form, auth=auth)
    if response.is_error:
        raise RuntimeError(f'OAuth token request failed with HTTP {response.status_code}')
    try:
        token = response.json().get('access_token')
    except (ValueError, AttributeError) as exc:
        raise RuntimeError('OAuth token response was not a JSON object') from exc
    if not isinstance(token, str) or not token:
        raise RuntimeError('OAuth token response did not include an access_token')
    return token


def fetch_all_stores(client: httpx.Client, api_base_url: str, access_token: str):
    stores_url = urljoin(api_base_url.rstrip('/') + '/', 'stores')
    response = client.get(stores_url, headers={'Authorization': f'Bearer {access_token}'})
    if response.is_error:
        raise RuntimeError(f"Love's /stores request failed with HTTP {response.status_code}")
    try:
        return response.json()
    except ValueError as exc:
        raise RuntimeError("Love's /stores response was not valid JSON") from exc


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=Path('loves_stores.json'))
    args = parser.parse_args()

    token_url = os.environ.get('LOVES_TOKEN_URL', '').strip()
    client_id = os.environ.get('LOVES_CLIENT_ID', '').strip()
    client_secret = os.environ.get('LOVES_CLIENT_SECRET', '')
    missing = [name for name, value in (
        ('LOVES_TOKEN_URL', token_url),
        ('LOVES_CLIENT_ID', client_id),
        ('LOVES_CLIENT_SECRET', client_secret),
    ) if not value]
    if missing:
        parser.error('Set required OAuth settings: ' + ', '.join(missing))

    api_base_url = os.environ.get('LOVES_API_BASE_URL', DEFAULT_API_BASE_URL).strip()
    auth_method = os.environ.get('LOVES_TOKEN_AUTH', 'basic').strip().lower()
    scope = os.environ.get('LOVES_SCOPE', '').strip()

    try:
        with httpx.Client(timeout=30.0) as client:
            access_token = acquire_access_token(
                client, token_url, client_id, client_secret, scope, auth_method,
            )
            stores = fetch_all_stores(client, api_base_url, access_token)
        args.output.write_text(json.dumps(stores, indent=2) + '\n', encoding='utf-8')
    except (httpx.HTTPError, OSError, RuntimeError, ValueError) as exc:
        print(f'Error: {exc}', file=sys.stderr)
        return 1

    print(f"Saved Love's store response to {args.output}")
    return 0


if __name__ == '__main__':
    raise SystemExit(main())