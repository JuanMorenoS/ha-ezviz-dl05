"""Coordinador de datos del cerrojo EZVIZ DL05."""

from __future__ import annotations

import base64
import binascii
import json
import logging
import time
from datetime import timedelta
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from pyezvizapi import EzvizClient
from pyezvizapi.exceptions import EzvizAuthTokenExpired, PyEzvizError

from .const import (
    CODIGOS_CERROJO,
    CODIGOS_PUERTA,
    DEFAULT_LOCK_NO,
    DEFAULT_RELOCK_SECONDS,
    DOMAIN,
    METODOS_APERTURA,
    PALABRAS_ABIERTO,
    PALABRAS_CERRADO,
    PALABRAS_TIMBRE,
    RUTAS_CERROJO,
    RUTAS_PUERTA,
    TIPOS_ALARMA,
)

_LOGGER = logging.getLogger(__name__)


def leer_ruta(datos: Any, ruta: tuple) -> Any:
    """Recorre `ruta` dentro de `datos` y devuelve el valor, o None si no existe."""
    actual = datos
    for paso in ruta:
        if isinstance(actual, dict):
            actual = actual.get(paso)
        elif isinstance(actual, (list, tuple)) and isinstance(paso, int):
            actual = actual[paso] if -len(actual) <= paso < len(actual) else None
        else:
            return None
        if actual is None:
            return None
    return actual


def primer_valor(datos: Any, rutas: tuple[tuple, ...]) -> Any:
    """Devuelve el valor de la primera ruta que exista."""
    for ruta in rutas:
        valor = leer_ruta(datos, ruta)
        if valor is not None:
            return valor
    return None


def decodificar_estado(
    valor: Any, mapa: dict[str, bool], etiqueta: str, serial: str
) -> bool | None:
    """Traduce un codigo de estado del cerrojo a booleano.

    Un codigo que no este en el mapa devuelve None (entidad no disponible) y
    deja un aviso con el valor exacto. En un sensor de seguridad es preferible
    admitir que no se sabe antes que inventar un "cerrado".
    """
    if valor is None:
        return None
    clave = str(valor).strip()
    if clave in mapa:
        return mapa[clave]
    _LOGGER.warning(
        "Cerrojo %s: codigo %s=%s desconocido. Corre vigilar_estados.py y "
        "agrega el codigo al mapa correspondiente en const.py",
        serial,
        etiqueta,
        clave,
    )
    return None


def decodificar_customer_info(valor: Any) -> dict[str, Any]:
    """Descodifica el campo customerInfo del evento.

    EZVIZ lo manda como JSON en base64, por ejemplo
    {"user":"Ana","openDoorWay":"fingerprint"}. Es la unica parte del evento
    que no depende del idioma de la cuenta.
    """
    if not isinstance(valor, str) or not valor:
        return {}
    try:
        crudo = base64.b64decode(valor, validate=True)
        datos = json.loads(crudo.decode("utf-8"))
    except (binascii.Error, UnicodeDecodeError, json.JSONDecodeError, ValueError):
        return {}
    return datos if isinstance(datos, dict) else {}


class EzvizDl05Coordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Mantiene el estado del cerrojo combinando dos fuentes.

    - `get_device_infos`: estado completo, lento, cada `scan_interval`.
    - `get_alarminfo`: registro de eventos, ligero, cada `event_interval`.

    El estado del cerrojo se lee del API si el DL05 lo publica; si no, se
    deduce del registro de eventos igual que hace la integracion del DL03.
    """

    def __init__(
        self,
        hass: HomeAssistant,
        client: EzvizClient,
        serial: str,
        *,
        scan_interval: int,
        relock_seconds: int = DEFAULT_RELOCK_SECONDS,
        lock_no: int = DEFAULT_LOCK_NO,
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=f"{DOMAIN} {serial}",
            update_interval=timedelta(seconds=scan_interval),
        )
        self.client = client
        self.serial = serial
        self.lock_no = lock_no
        self.relock_seconds = relock_seconds

        # Estado derivado del registro de eventos.
        self.cerrojo_abierto = False
        self.timbre_sonando = False
        self.ultimo_evento: str | None = None
        self.ultimo_evento_id: str | None = None
        self.ultimo_evento_hora: str | None = None
        self.ultimo_usuario: str | None = None
        self.ultimo_metodo: str | None = None
        self.ultimo_tipo: int | None = None
        self.momento_apertura = 0.0
        # True cuando el propio API publica el estado del cerrojo y no hay que
        # inferirlo. Se detecta solo, en la primera actualizacion.
        self.estado_nativo = False

    # -- Lectura de estado completo -----------------------------------------

    async def _async_update_data(self) -> dict[str, Any]:
        def obtener() -> dict[str, Any]:
            return self.client.get_device_infos(self.serial)

        try:
            datos = await self.hass.async_add_executor_job(obtener)
        except EzvizAuthTokenExpired:
            await self.hass.async_add_executor_job(self.client.login)
            datos = await self.hass.async_add_executor_job(obtener)
        except PyEzvizError as err:
            raise UpdateFailed(f"Error consultando EZVIZ: {err}") from err

        if not datos:
            raise UpdateFailed(
                f"El serial {self.serial} no aparece en la cuenta EZVIZ"
            )

        # Si el firmware publica un codigo de cerrojo que sabemos leer, ese
        # manda y se deja de inferir desde los eventos.
        crudo = primer_valor(datos, RUTAS_CERROJO)
        estado = decodificar_estado(crudo, CODIGOS_CERROJO, "dlLock", self.serial)
        # Se recalcula en cada refresco: si el firmware manda un codigo que no
        # sabemos leer, se vuelve a deducir por eventos en vez de quedarse
        # congelado en el ultimo valor bueno.
        self.estado_nativo = estado is not None
        if estado is not None:
            self.cerrojo_abierto = estado

        return datos

    @property
    def puerta_abierta(self) -> bool | None:
        """Estado de la hoja de la puerta, o None si el codigo es desconocido."""
        return decodificar_estado(
            primer_valor(self.data, RUTAS_PUERTA),
            CODIGOS_PUERTA,
            "dlDoor",
            self.serial,
        )

    # -- Lectura del registro de eventos ------------------------------------

    async def async_revisar_eventos(self) -> None:
        """Lee el ultimo evento y actualiza el estado derivado.

        La llama la tarea de fondo creada en __init__.py.
        """
        def obtener() -> dict[str, Any]:
            return self.client.get_alarminfo(self.serial, limit=1)

        try:
            respuesta = await self.hass.async_add_executor_job(obtener)
        except EzvizAuthTokenExpired:
            # El refresco completo tambien reintenta el login, pero este sondeo
            # corre mas seguido y no debe quedarse atascado esperandolo.
            await self.hass.async_add_executor_job(self.client.login)
            respuesta = await self.hass.async_add_executor_job(obtener)
        alarmas = respuesta.get("alarms") or []
        cambio = False

        if alarmas and isinstance(alarmas[0], dict):
            evento = alarmas[0]
            evento_id = str(evento.get("alarmId") or "")
            if evento_id and evento_id != self.ultimo_evento_id:
                mensaje = str(evento.get("alarmMessage") or "")
                self.ultimo_evento_id = evento_id
                self.ultimo_evento = mensaje
                self.ultimo_evento_hora = evento.get("alarmStartTimeStr") or evento.get(
                    "alarmStartTime"
                )
                cambio = True

                # Quien abrio y con que metodo. Viene en base64 y no depende
                # del idioma, a diferencia de alarmMessage.
                detalle = decodificar_customer_info(evento.get("customerInfo"))
                self.ultimo_usuario = detalle.get("user") or None
                metodo = detalle.get("openDoorWay")
                self.ultimo_metodo = METODOS_APERTURA.get(metodo, metodo) or None

                tipo = evento.get("alarmType")
                self.ultimo_tipo = tipo if isinstance(tipo, int) else None
                clase = TIPOS_ALARMA.get(self.ultimo_tipo) if self.ultimo_tipo else None

                if clase is None:
                    # Tipo desconocido: se cae al texto, que es lo unico que queda.
                    texto = mensaje.lower()
                    if any(p in texto for p in PALABRAS_TIMBRE):
                        clase = "timbre"
                    elif any(p in texto for p in PALABRAS_ABIERTO):
                        clase = "abierto"
                    elif any(p in texto for p in PALABRAS_CERRADO):
                        clase = "cerrado"
                    if self.ultimo_tipo is not None:
                        _LOGGER.debug(
                            "Cerrojo %s: alarmType %s sin mapear, deducido %s "
                            "del texto %r",
                            self.serial,
                            self.ultimo_tipo,
                            clase,
                            mensaje,
                        )

                if clase == "timbre":
                    self.timbre_sonando = True
                elif not self.estado_nativo:
                    if clase == "abierto":
                        self.cerrojo_abierto = True
                        self.momento_apertura = time.time()
                    elif clase == "cerrado":
                        self.cerrojo_abierto = False

        # El DL05 no avisa cuando vuelve a cerrar, asi que tras `relock_seconds`
        # se asume cerrado. Solo aplica al estado inferido.
        if (
            not self.estado_nativo
            and self.cerrojo_abierto
            and self.momento_apertura
            and (time.time() - self.momento_apertura) > self.relock_seconds
        ):
            self.cerrojo_abierto = False
            self.momento_apertura = 0.0
            cambio = True

        if cambio:
            self.async_set_updated_data(self.data)

    def apagar_timbre(self) -> None:
        """Devuelve el timbre a reposo tras la ventana de aviso."""
        if self.timbre_sonando:
            self.timbre_sonando = False
            self.async_set_updated_data(self.data)

    # -- Comandos -----------------------------------------------------------

    def _ruta_recurso(self) -> tuple[str, str, str | None, str | None]:
        """resourceId, localIndex, streamToken y type para el comando remoto."""
        recursos = (self.data or {}).get("resourceInfos") or []
        info = next((r for r in recursos if isinstance(r, dict)), None) or {}
        resource_id = info.get("resourceId") if isinstance(info.get("resourceId"), str) else "Video"
        indice = info.get("localIndex")
        local_index = str(indice) if isinstance(indice, (int, str)) else "1"
        tipo = info.get("type") if isinstance(info.get("type"), str) else None
        return resource_id, local_index, info.get("streamToken"), tipo

    def _bind_code(self) -> str:
        """bindCode de apertura: firma de la sesion + cliente, como el app.

        El app oficial manda ``s`` + ``aud`` del JWT de su sesion. Se usa el
        token de la sesion de Home Assistant; si no se puede leer, se cae al
        bind de terminal de pyezvizapi.
        """
        sesion = str(getattr(self.client, "_token", {}).get("session_id") or "")
        partes = sesion.split(".")
        if len(partes) == 3:
            cuerpo = partes[1] + "=" * (-len(partes[1]) % 4)
            try:
                datos = json.loads(base64.urlsafe_b64decode(cuerpo))
            except (binascii.Error, ValueError, UnicodeDecodeError):
                datos = {}
            firma, cliente = datos.get("s"), datos.get("aud")
            if isinstance(firma, str) and isinstance(cliente, str) and firma and cliente:
                return f"{firma}{cliente}"
        bind_code, _nombre = self.client.get_latest_terminal_bind(terminal_name=None)
        return bind_code

    async def async_abrir(self) -> None:
        """Abre igual que el app EZVIZ: pide un codigo y luego lo usa.

        Flujo capturado del app iOS para el DL05:
        1. PUT .../DoorLock/<i>/DoorLockMgr/QueryRemoteUnlockRandomCode {"value": {}}
           -> data.randomCode (lo genera EZVIZ, un solo uso)
        2. PUT .../DoorLock/<i>/DoorLockMgr/RemoteUnlockReq
           {"value": {"unLockInfo": {"bindCode", "randomCode", "type": "unLinkIPC", "userName"}}}

        pyezvizapi.remote_unlock usa otro cuerpo (sin "value", con lockNo y
        sin randomCode) que el DL05 rechaza con meta 400, y ademas devuelve
        True aunque falle; por eso se llama a los endpoints directamente y se
        revisa meta.code en cada paso.
        """
        recursos = (self.data or {}).get("resourceInfos") or []
        info = next((r for r in recursos if isinstance(r, dict)), None) or {}
        recurso = info.get("resourceIdentifier") or "DoorLock"
        indice = info.get("localIndex")
        indice = str(indice) if indice is not None else "0"
        # El app manda el identificador con el que se inicia sesion (el email),
        # no loginUser.username, que es un nombre interno de la cuenta.
        usuario = str(
            getattr(self.client, "account", None) or getattr(self.client, "_token", {}).get("username") or ""
        )

        base = f"/v3/iot-feature/action/{self.serial}/{recurso}/{indice}/DoorLockMgr"

        def revisar(respuesta: Any, paso: str) -> dict[str, Any]:
            meta = respuesta.get("meta") if isinstance(respuesta, dict) else None
            codigo = meta.get("code") if isinstance(meta, dict) else None
            mensaje = meta.get("message") if isinstance(meta, dict) else None
            _LOGGER.info("Cerrojo %s: %s -> meta %s %s", self.serial, paso, codigo, mensaje)
            if codigo != 200:
                raise PyEzvizError(f"EZVIZ rechazo {paso} (meta {codigo}: {mensaje})")
            return respuesta

        def pedir(ruta: str, cuerpo: dict[str, Any], paso: str) -> dict[str, Any]:
            try:
                respuesta = self.client._request_json(  # noqa: SLF001
                    "PUT", f"{base}/{ruta}", json_body=cuerpo, retry_401=True, max_retries=0
                )
            except PyEzvizError as err:
                causa = err.__cause__
                detalle = getattr(getattr(causa, "response", None), "text", "") or str(causa or err)
                _LOGGER.warning("Cerrojo %s: %s fallo: %s", self.serial, paso, detalle[:500])
                raise PyEzvizError(f"EZVIZ rechazo {paso}: {detalle[:200]}") from err
            return revisar(respuesta, paso)

        def enviar() -> None:
            codigo = pedir("QueryRemoteUnlockRandomCode", {"value": {}}, "la solicitud de codigo de apertura")
            random_code = (codigo.get("data") or {}).get("randomCode")
            if not random_code:
                raise PyEzvizError("EZVIZ no entrego codigo de apertura (randomCode)")
            bind_code = self._bind_code()
            _LOGGER.debug(
                "Cerrojo %s: apertura con bindCode de %d caracteres y userName %s",
                self.serial,
                len(bind_code),
                (usuario[:2] + "***" + usuario[usuario.find("@") :]) if "@" in usuario else "***",
            )
            pedir(
                "RemoteUnlockReq",
                {
                    "value": {
                        "unLockInfo": {
                            "bindCode": bind_code,
                            "randomCode": str(random_code),
                            "type": "unLinkIPC",
                            "userName": usuario,
                        }
                    }
                },
                "la apertura remota",
            )

        await self.hass.async_add_executor_job(enviar)

        if self.estado_nativo:
            # El cerrojo publica su propio estado: se le pregunta a el en vez
            # de suponer que el comando funciono.
            await self.async_request_refresh()
            return
        self.cerrojo_abierto = True
        self.momento_apertura = time.time()
        self.async_set_updated_data(self.data)

    async def async_cerrar(self) -> None:
        """Manda el comando de cierre remoto.

        Muchos DL05 cierran solos por motor y rechazan este comando; en ese caso
        el API devuelve error y se propaga a Home Assistant.
        """
        resource_id, local_index, stream_token, tipo = self._ruta_recurso()
        usuario = str(getattr(self.client, "_token", {}).get("username") or "")

        def enviar() -> bool:
            return self.client.remote_lock(
                self.serial,
                usuario,
                self.lock_no,
                resource_id=resource_id,
                local_index=local_index,
                stream_token=stream_token,
                lock_type=tipo,
            )

        await self.hass.async_add_executor_job(enviar)
        if self.estado_nativo:
            # El cerrojo publica su propio estado: se le pregunta a el en vez
            # de suponer que el comando funciono.
            await self.async_request_refresh()
            return
        self.cerrojo_abierto = False
        self.momento_apertura = 0.0
        self.async_set_updated_data(self.data)
