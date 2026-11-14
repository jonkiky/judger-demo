"""Local OpenAI-compatible routing and fail-closed output contracts."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import HTTPException
from backend.app import gateway, settings


@pytest.fixture
def servers(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, 'DATA', tmp_path)
    monkeypatch.setattr(settings, 'CHAT_API_BASE', 'http://127.0.0.1:8081/v1')
    monkeypatch.setattr(settings, 'EMBED_API_BASE', 'http://127.0.0.1:8080/v1')
    monkeypatch.setattr(settings, 'CHAT_SERVER_MODEL', '')
    monkeypatch.setattr(settings, 'EMBED_SERVER_MODEL', '')
    models = {'8081': 'chat.gguf', '8080': 'embed.gguf'}
    def get(url, **kwargs):
        assert url.endswith('/v1/models')
        model = models[str(httpx.URL(url).port)]
        return httpx.Response(200, json={'data': [{'id': model}]}, request=httpx.Request('GET', url))
    monkeypatch.setattr(gateway.httpx, 'get', get)
    return models


def test_profile_pins_routes_without_overwriting_ollama(servers):
    old = settings.DATA / 'model-profile.json'
    old.write_text('{"legacy": true}')
    assert gateway.profile()['chat']['model'] == 'chat.gguf'
    assert old.read_text() == '{"legacy": true}'
    servers['8081'] = 'replacement.gguf'
    with pytest.raises(HTTPException) as error:
        gateway.profile()
    assert error.value.status_code == 503


def test_nonlocal_endpoint_rejected(servers, monkeypatch):
    monkeypatch.setattr(settings, 'CHAT_API_BASE', 'http://example.com/v1')
    with pytest.raises(HTTPException) as error:
        gateway.profile()
    assert error.value.status_code == 503


def test_chat_routes_to_generation_server(servers, monkeypatch):
    result = {'choices': [{'finish_reason': 'stop', 'message': {'content': '{}'}}]}
    call = AsyncMock(return_value=SimpleNamespace(model_dump=lambda: result))
    monkeypatch.setattr(gateway.litellm, 'acompletion', call)
    response = asyncio.run(gateway.chat(gateway.ChatRequest(model=settings.MODEL_ALIAS, messages=[{'role': 'user', 'content': 'hello'}])))
    assert response == result
    args = call.call_args.kwargs
    assert args['model'] == 'openai/chat.gguf'
    assert args['api_base'] == 'http://127.0.0.1:8081/v1'
    assert 'num_ctx' not in args


@pytest.mark.parametrize('vector,valid', [([1.0]*512, True), ([1.0]*2560, False), ([0.0]*512, False), ([[1.0, 1.0]]*512, False), ([float('nan')]*512, False)])
def test_embedding_routes_and_validates(servers, monkeypatch, vector, valid):
    call = AsyncMock(return_value=SimpleNamespace(data=[{'embedding': vector}]))
    monkeypatch.setattr(gateway.litellm, 'aembedding', call)
    request = gateway.EmbedRequest(model=settings.EMBED_ALIAS, input='test')
    if valid:
        result = asyncio.run(gateway.embedding(request))
        output = result['data'][0]['embedding']
        assert sum(x*x for x in output) == pytest.approx(1.0)
    else:
        with pytest.raises(HTTPException) as error:
            asyncio.run(gateway.embedding(request))
        assert error.value.status_code == 502
    args = call.call_args.kwargs
    assert args['api_base'] == 'http://127.0.0.1:8080/v1'
    assert args['model'] == 'openai/embed.gguf'
    assert args['dimensions'] == 512
    assert args['allowed_openai_params'] == ['dimensions']
    assert 'truncate' not in args


def test_unavailable_server_returns_503(servers, monkeypatch):
    def unavailable(*args, **kwargs):
        raise httpx.ConnectError('offline')
    monkeypatch.setattr(gateway.httpx, 'get', unavailable)
    with pytest.raises(HTTPException) as error:
        gateway.profile()
    assert error.value.status_code == 503


def test_configured_model_must_exist(servers, monkeypatch):
    monkeypatch.setattr(settings, 'CHAT_SERVER_MODEL', 'missing')
    with pytest.raises(HTTPException) as error:
        gateway.profile()
    assert error.value.status_code == 503


def test_truncated_chat_rejected(servers, monkeypatch):
    call = AsyncMock(return_value=SimpleNamespace(model_dump=lambda: {'choices': [{'finish_reason': 'length'}]}))
    monkeypatch.setattr(gateway.litellm, 'acompletion', call)
    with pytest.raises(HTTPException) as error:
        asyncio.run(gateway.chat(gateway.ChatRequest(model=settings.MODEL_ALIAS, messages=[])))
    assert error.value.status_code == 502


def test_qwen_full_vector_reduced_and_normalized(servers, monkeypatch):
    servers['8080'] = 'Qwen/Qwen3-Embedding-4B-GGUF:Q4_K_M'
    call = AsyncMock(return_value=SimpleNamespace(data=[{'embedding': [1.0]*512 + [10.0]*2048}]))
    monkeypatch.setattr(gateway.litellm, 'aembedding', call)
    result = asyncio.run(gateway.embedding(gateway.EmbedRequest(model=settings.EMBED_ALIAS, input='test')))
    vector = result['data'][0]['embedding']
    assert len(vector) == 512
    assert vector[0] == pytest.approx(1 / (512 ** 0.5))
