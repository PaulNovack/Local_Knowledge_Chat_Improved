from urllib.parse import parse_qs

import httpx

from scripts.loves_stores_test import acquire_access_token, fetch_all_stores


def test_client_credentials_fetches_all_stores():
    seen = []

    def handle(request):
        seen.append(request)
        if request.url.path == '/oauth/token':
            assert request.headers['authorization'].startswith('Basic ')
            assert parse_qs(request.content.decode()) == {'grant_type': ['client_credentials']}
            return httpx.Response(200, json={'access_token': 'test-token'})
        assert request.url == 'https://api.example.test/locations/api/stores'
        assert request.headers['authorization'] == 'Bearer test-token'
        return httpx.Response(200, json=[{'storeNumber': 123}, {'storeNumber': 456}])

    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        token = acquire_access_token(
            client, 'https://api.example.test/oauth/token', 'client-id', 'client-secret',
        )
        stores = fetch_all_stores(client, 'https://api.example.test/locations/api/', token)

    assert len(seen) == 2
    assert stores == [{'storeNumber': 123}, {'storeNumber': 456}]