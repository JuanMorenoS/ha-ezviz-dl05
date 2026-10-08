"""Apertura remota con el formato del app EZVIZ y respuesta revisada."""

from __future__ import annotations

import base64
import json
from unittest.mock import AsyncMock, MagicMock

import pytest
from pyezvizapi.exceptions import PyEzvizError

from custom_components.ezviz_dl05.coordinator import EzvizDl05Coordinator

RECURSO = {"resourceId": "00000000000000000000000000000000", "localIndex": "0", "resourceIdentifier": "DoorLock"}


def _jwt(payload: dict) -> str:
    cuerpo = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
    return f"h.{cuerpo}.f"


CODIGO_OK = {"meta": {"code": 200}, "data": {"randomCode": "1234567890"}}
OK = {"meta": {"code": 200}, "data": {}}


def _coordinador(respuestas, sesion=None):
    c = object.__new__(EzvizDl05Coordinator)
    c.serial = "BG0000000"
    c.lock_no = 2
    c.data = {"resourceInfos": [RECURSO]}
    c.estado_nativo = False
    c.cerrojo_abierto = False
    c.momento_apertura = 0.0
    c.client = MagicMock()
    c.client.account = "user@example.com"
    c.client._token = {"username": "nombre-interno", "session_id": sesion or _jwt({"s": "firma", "aud": "cliente"})}
    c.client.get_latest_terminal_bind.return_value = ("terminalbind", "x")
    c.client._request_json.side_effect = respuestas
    c.hass = MagicMock()
    c.hass.async_add_executor_job = AsyncMock(side_effect=lambda f, *a: f(*a))
    c.async_set_updated_data = MagicMock()
    c.async_request_refresh = AsyncMock()
    return c


async def test_apertura_pide_codigo_y_lo_usa():
    c = _coordinador([CODIGO_OK, OK])
    await c.async_abrir()
    pedir, abrir = c.client._request_json.call_args_list
    base = "/v3/iot-feature/action/BG0000000/DoorLock/0/DoorLockMgr"
    assert pedir.args == ("PUT", f"{base}/QueryRemoteUnlockRandomCode")
    assert pedir.kwargs["json_body"] == {"value": {}}
    assert abrir.args == ("PUT", f"{base}/RemoteUnlockReq")
    assert abrir.kwargs["json_body"] == {
        "value": {
            "unLockInfo": {
                "bindCode": "firmacliente",
                "randomCode": "1234567890",
                "type": "unLinkIPC",
                "userName": "user@example.com",
            }
        }
    }
    assert c.cerrojo_abierto is True


async def test_sin_jwt_usa_bind_de_terminal():
    c = _coordinador([CODIGO_OK, OK], sesion="no-es-jwt")
    await c.async_abrir()
    cuerpo = c.client._request_json.call_args_list[1].kwargs["json_body"]
    assert cuerpo["value"]["unLockInfo"]["bindCode"] == "terminalbind"


async def test_codigo_rechazado_no_intenta_abrir():
    c = _coordinador([{"meta": {"code": 400, "message": "bad"}}])
    with pytest.raises(PyEzvizError, match="codigo de apertura.*meta 400"):
        await c.async_abrir()
    assert c.client._request_json.call_count == 1
    assert c.cerrojo_abierto is False


async def test_sin_random_code_no_intenta_abrir():
    c = _coordinador([{"meta": {"code": 200}, "data": {}}])
    with pytest.raises(PyEzvizError, match="randomCode"):
        await c.async_abrir()
    assert c.client._request_json.call_count == 1


async def test_apertura_rechazada_lanza_error():
    c = _coordinador([CODIGO_OK, {"meta": {"code": 400, "message": "bad"}}])
    with pytest.raises(PyEzvizError, match="apertura remota.*meta 400"):
        await c.async_abrir()
    assert c.cerrojo_abierto is False


async def test_error_http_muestra_la_respuesta_de_ezviz():
    import requests

    respuesta = requests.Response()
    respuesta.status_code = 400
    respuesta._content = b'{"meta":{"code":400,"message":"parameter error"}}'
    try:
        raise requests.HTTPError(response=respuesta)
    except requests.HTTPError as causa:
        error = PyEzvizError()
        error.__cause__ = causa
    c = _coordinador([CODIGO_OK, error])
    with pytest.raises(PyEzvizError, match="parameter error"):
        await c.async_abrir()
    assert c.cerrojo_abierto is False
