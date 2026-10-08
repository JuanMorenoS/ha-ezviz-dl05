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


def _coordinador(respuesta, sesion=None):
    c = object.__new__(EzvizDl05Coordinator)
    c.serial = "BG0000000"
    c.lock_no = 2
    c.data = {"resourceInfos": [RECURSO]}
    c.estado_nativo = False
    c.cerrojo_abierto = False
    c.momento_apertura = 0.0
    c.client = MagicMock()
    c.client._token = {"username": "user@example.com", "session_id": sesion or _jwt({"s": "firma", "aud": "cliente"})}
    c.client.get_latest_terminal_bind.return_value = ("terminalbind", "x")
    c.client._request_json.return_value = respuesta
    c.hass = MagicMock()
    c.hass.async_add_executor_job = AsyncMock(side_effect=lambda f, *a: f(*a))
    c.async_set_updated_data = MagicMock()
    c.async_request_refresh = AsyncMock()
    return c


async def test_apertura_usa_el_formato_del_app():
    c = _coordinador({"meta": {"code": 200}})
    await c.async_abrir()
    args, kwargs = c.client._request_json.call_args
    assert args == ("PUT", "/v3/iot-feature/action/BG0000000/DoorLock/0/DoorLockMgr/RemoteUnlockReq")
    info = kwargs["json_body"]["value"]["unLockInfo"]
    assert info["bindCode"] == "firmacliente"
    assert info["type"] == "unLinkIPC"
    assert info["userName"] == "user@example.com"
    assert len(info["randomCode"]) == 10 and info["randomCode"].isdigit()
    assert "lockNo" not in info
    assert c.cerrojo_abierto is True


async def test_sin_jwt_usa_bind_de_terminal():
    c = _coordinador({"meta": {"code": 200}}, sesion="no-es-jwt")
    await c.async_abrir()
    assert c.client._request_json.call_args.kwargs["json_body"]["value"]["unLockInfo"]["bindCode"] == "terminalbind"


async def test_rechazo_lanza_error():
    c = _coordinador({"meta": {"code": 400, "message": "bad"}})
    with pytest.raises(PyEzvizError, match="meta 400"):
        await c.async_abrir()
    assert c.cerrojo_abierto is False
