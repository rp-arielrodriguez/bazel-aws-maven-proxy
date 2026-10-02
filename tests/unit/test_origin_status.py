"""Real Flask route status with controlled S3 and upstream boundaries."""
import io
import os
import sys
import threading
import urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import MagicMock, patch

import pytest
from botocore.exceptions import ClientError

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '../../s3proxy'))
import app


def s3_error(code):
    return ClientError({'Error': {'Code': code, 'Message': 'origin failure'}}, 'GetObject')


@pytest.fixture
def origin_route(tmp_path, monkeypatch):
    client = MagicMock()
    monkeypatch.setattr(app, 'CACHE_DIR', str(tmp_path))
    monkeypatch.setattr(app, 'S3_BUCKET_NAME', 'primary')
    monkeypatch.setattr(app, 'READ_FALLBACK_BUCKET', 'mirror')
    monkeypatch.setattr(app, 'UPSTREAM_WRITE_BACK', False)
    monkeypatch.setattr(app, 'get_s3_client', lambda: client)
    app.app.config['TESTING'] = True
    with app.app.test_client() as flask_client:
        yield client, flask_client, tmp_path


@pytest.mark.parametrize('code', ['403', 'AccessDenied', 'ExpiredToken', 'SlowDown', 'InternalError'])
@pytest.mark.parametrize('fallback', [False, True])
def test_s3_failure_is_500_without_trying_public_upstream(origin_route, code, fallback):
    s3, client, cache = origin_route
    s3.download_file.side_effect = ([s3_error('NoSuchKey'), s3_error(code)]
                                    if fallback else s3_error(code))
    with patch.object(app.urllib.request, 'urlopen') as upstream:
        response = client.get('/m2/com/recargapay/example/1/example.jar')
    assert response.status_code == 500
    assert s3.download_file.call_count == (2 if fallback else 1)
    upstream.assert_not_called()
    assert not list(cache.rglob('*.jar'))
    assert not [p for p in cache.rglob('*') if p.is_file()]


@pytest.mark.parametrize('code, expected', [(200, 200), (404, 404), (500, 502), (503, 502)])
def test_actual_upstream_http_status_reaches_flask_route(origin_route, monkeypatch, code, expected):
    s3, client, cache = origin_route
    s3.download_file.side_effect = s3_error('NoSuchKey')
    paths = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            paths.append(self.path)
            self.send_response(code)
            self.end_headers()
            self.wfile.write(b'origin error')

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setattr(app, 'UPSTREAM_MAVEN_URL', f'http://127.0.0.1:{server.server_port}')
    try:
        response = client.get('/m2/org/example/1/example.jar')
    finally:
        server.shutdown()
        thread.join()
        server.server_close()
    assert response.status_code == expected
    assert paths == ['/org/example/1/example.jar']
    assert s3.download_file.call_count == 2
    if expected == 200:
        assert response.data == b'origin error'
        assert (cache / 'm2/org/example/1/example.jar').read_bytes() == response.data
        response.close()
    else:
        assert not [p for p in cache.rglob('*') if p.is_file()]
    if expected == 502:
        assert response.json == {'error': 'Upstream Maven repository request failed'}


@pytest.mark.parametrize('failure', [urllib.error.URLError('connection failed'), TimeoutError('timed out')])
def test_upstream_transport_failure_is_502(origin_route, failure):
    s3, client, cache = origin_route
    s3.download_file.side_effect = s3_error('NoSuchKey')
    with patch.object(app.urllib.request, 'urlopen', side_effect=failure):
        response = client.get('/m2/org/example/1/example.jar')
    assert response.status_code == 502
    assert not [p for p in cache.rglob('*') if p.is_file()]


@pytest.mark.parametrize('code', [404, 500])
def test_upstream_error_body_is_closed(origin_route, code):
    s3, client, _ = origin_route
    s3.download_file.side_effect = s3_error('NoSuchKey')
    body = io.BytesIO(b'error')
    failure = urllib.error.HTTPError('https://origin.example/item', code, 'failure', {}, body)
    with patch.object(app.urllib.request, 'urlopen', side_effect=failure):
        response = client.get('/m2/org/example/1/example.jar')
    assert response.status_code == (404 if code == 404 else 502)
    assert body.closed


@pytest.mark.parametrize('code', ['NoSuchKey', '404'])
def test_missing_s3_object_is_404(origin_route, monkeypatch, code):
    s3, client, cache = origin_route
    monkeypatch.setattr(app, 'UPSTREAM_MAVEN_URL', '')
    s3.download_file.side_effect = s3_error(code)
    response = client.get('/m2/com/recargapay/example/1/example.jar')
    assert response.status_code == 404
    assert s3.download_file.call_count == 2
    assert not [p for p in cache.rglob('*') if p.is_file()]


@pytest.mark.parametrize('group', ['com/recarga', 'com/recargapay'])
@pytest.mark.parametrize('code, expected', [('NoSuchKey', 404), ('404', 404), ('AccessDenied', 500), ('403', 500)])
@pytest.mark.parametrize('method', ['get', 'head'])
def test_private_endpoint_never_uses_public_or_mirror(origin_route, monkeypatch, group, code, expected, method):
    s3, client, cache = origin_route
    monkeypatch.setattr(app, 'UPSTREAM_MAVEN_URL', 'https://public.example/maven2')
    monkeypatch.setattr(app, 'UPSTREAM_WRITE_BACK', True)
    artifact = group + '/example/1/example.jar'
    # A previous public pull-through copy must not satisfy this request.
    poisoned = cache / 'm2' / artifact
    poisoned.parent.mkdir(parents=True)
    poisoned.write_bytes(b'public-copy')
    s3.download_file.side_effect = s3_error(code)
    with patch.object(app.urllib.request, 'urlopen') as upstream:
        response = getattr(client, method)('/private-m2/' + artifact)
    assert response.status_code == expected
    assert s3.download_file.call_count == 1
    assert s3.download_file.call_args.args[:2] == ('primary', 'm2/' + artifact)
    upstream.assert_not_called()
    s3.put_object.assert_not_called()
    assert poisoned.read_bytes() == b'public-copy'


def test_private_endpoint_reads_primary_instead_of_mixed_cache(origin_route):
    s3, client, cache = origin_route
    artifact = 'com/recarga/example/1/example.pom'
    old = cache / 'm2' / artifact
    old.parent.mkdir(parents=True)
    old.write_bytes(b'public-copy')
    def download(bucket, key, filename):
        assert (bucket, key) == ('primary', 'm2/' + artifact)
        with open(filename, 'wb') as out:
            out.write(b'private-copy')
    s3.download_file.side_effect = download
    with patch.object(app.urllib.request, 'urlopen') as upstream:
        response = client.get('/private-m2/' + artifact)
    assert response.status_code == 200
    assert response.data == b'private-copy'
    response.close()
    upstream.assert_not_called()
    assert old.read_bytes() == b'public-copy'


@pytest.mark.parametrize('artifact', ['com/../secret', 'com//secret', 'com/./secret'])
def test_private_endpoint_rejects_ambiguous_paths(origin_route, artifact):
    s3, client, _ = origin_route
    response = client.get('/private-m2/' + artifact)
    assert response.status_code == 400
    s3.download_file.assert_not_called()
