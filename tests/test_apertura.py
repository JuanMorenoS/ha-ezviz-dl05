"""Apertura remota: rutas probadas y respuesta de EZVIZ revisada."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from pyezvizapi.exceptions import PyEzvizError

from custom_components.ezviz_dl05.coordinator import EzvizDl05Coordinator

RECURSO = {"resourceId": "00000000000000000000000000000000", "localIndex": "0", "resourceIdentifier": "DoorLock"}


def _coordinador(respuestas):
    c = object.__new__(EzvizDl05Coordinator)
    c.serial = "BG0000000"
    c.lock_no = 2
    c.data = {"resourceInfos": [RECURSO]}
    c.estado_nativo = False
    c.cerrojo_abierto = False
    c.momento_apertura = 0.0
    c.client = MagicMock()
    c.client._token = {"username": "user"}
    c.client.get_latest_terminal_bind.return_value = ("bind", "terminal-user")
    c.client._request_json.side_effect = respuestas
    c.hass = MagicMock()
    c.hass.async_add_executor_job = AsyncMock(side_effect=lambda f, *a: f(*a))
    c.async_set_updated_data = MagicMock()
    c.async_request_refresh = AsyncMock()
    return c


def test_rutas_usan_doorlock_y_no_el_resource_id():
    rutas = _coordinador([])._rutas_apertura()
    assert rutas[0] == ("DoorLock", "0", 2)
    assert all(r[0] != RECURSO["resourceId"] for r in rutas)
    assert ("Video", "1", 1) in rutas


async def test_para_en_la_primera_ruta_aceptada():
    c = _coordinador([{"meta": {"code": 400, "message": "bad"}}, {"meta": {"code": 200}}])
    await c.async_abrir()
    assert c.client._request_json.call_count == 2
    ruta = c.client._request_json.call_args_list[1].args[1]
    assert ruta == "/v3/iot-feature/action/BG0000000/DoorLock/0/DoorLockMgr/RemoteUnlockReq"
    cuerpo = c.client._request_json.call_args_list[1].kwargs["json_body"]["unLockInfo"]
    assert cuerpo["lockNo"] == 1 and cuerpo["bindCode"] == "bind"
    assert c.cerrojo_abierto is True


async def test_rechazo_total_lanza_error():
    n = len(_coordinador([])._rutas_apertura())
    c = _coordinador([{"meta": {"code": 400, "message": "bad"}}] * n)
    with pytest.raises(PyEzvizError, match="meta 400"):
        await c.async_abrir()
    assert c.cerrojo_abierto is False
