# EZVIZ DL05 para Home Assistant

Integracion no oficial para el cerrojo **EZVIZ DL05**, derivada de la logica de
[`ha-ezviz-dl03-pro`](https://github.com/iwanstudio/ha-ezviz-dl03-pro) pero
reescrita para que el mapa de datos del modelo viva en un solo archivo
(`const.py`) y se pueda ajustar sin tocar el resto del codigo.

Estado: **verificada contra un CS-DL05-R101-WBCP-GR real.** 19 pruebas pasando
sobre el JSON que devuelve ese cerrojo. Queda **un** dato por confirmar antes de
usarla en produccion: el significado de los codigos `dlDoor` y `dlLock` (ver
"Codigos de estado" abajo).

### Lo que se confirmo con un DL05 real

| Dato | Ruta | Valor visto |
|---|---|---|
| Bateria | `STATUS.optionals.multiPower[0].Remaining` | 85 |
| Puerta | `STATUS.optionals.dlDoor` | `4` (codigo, no booleano) |
| Pestillo | `STATUS.optionals.dlLock` | `3` (codigo, no booleano) |
| Wi-Fi | `WIFI.signal` / `WIFI.ssid` / `WIFI.address` | 100 |
| Ruta de apertura | `resourceInfos[0]` | `localIndex` = **`0`**, no `1` |
| Intentos fallidos | `FEATURE_INFO...TryErrLock` | **no existe** en el DL05 |
| Categoria | `deviceInfos.deviceCategory` | `DoorLock` |

Dos hallazgos que cambiaron el diseno:

1. **`dlDoor` y `dlLock` no son booleanos.** El DL03 usaba `1` = abierto; el
   DL05 devuelve `4` y `3`. Leerlos como booleanos daria "cerrado" siempre, en
   un sensor de seguridad.
2. **Los eventos traen datos estructurados.** El campo `customerInfo` es JSON en
   base64: `{"user":"Ana","openDoorWay":"fingerprint"}`, y `alarmType` es
   numerico (`17029` = apertura con huella). La integracion usa eso en vez de
   buscar palabras en el texto, asi que funciona sea cual sea el idioma de la
   cuenta. El texto quedo solo como respaldo para tipos sin mapear.

La categoria `DoorLock` no esta en la lista de categorias soportadas de
`pyezvizapi`, y por eso la integracion oficial de EZVIZ no maneja este cerrojo.

## Como funciona

EZVIZ no expone un API publico para cerrojos. Todo esto va contra el API privado
de la app movil, a traves de la libreria `pyezvizapi` (la misma que usa la
integracion oficial de EZVIZ en Home Assistant). Hay dos fuentes de datos:

| Fuente | Llamada | Cada | Da |
|---|---|---|---|
| Estado completo | `get_device_infos(serial)` | 30 s | bateria, puerta, Wi-Fi, intentos fallidos |
| Registro de eventos | `get_alarminfo(serial)` | 3 s | quien abrio, timbre, hora |

El estado del pestillo se lee del API **si tu DL05 lo publica**. Si no lo
publica, se deduce del texto del registro de eventos y se asume cerrado despues
de unos segundos. La entidad `binary_sensor` del pestillo lleva un atributo
`fuente` que dice cual de los dos casos aplica en tu equipo.

---

## Paso 1: averiguar que publica TU cerrojo

Esto es lo unico que no se puede adivinar. Las claves que usa el DL03 Pro
(`dlDoor`, `multiPower`) no estan documentadas en ninguna parte: su autor las
encontro mirando el JSON crudo. Hay que hacer lo mismo con el DL05.

```bash
pip install pyezvizapi
python3 descubrir_dl05.py --usuario tu@correo.com --serial TU_SERIAL --region us
```

El script pide la contrasena por teclado, se conecta, y deja en
`volcado_dl05/<fecha>/` un JSON por cada llamada del API. En pantalla resume las
tres cosas que importan:

- **`STATUS.optionals`** — aqui viven puerta, bateria y (con suerte) el pestillo.
- **`resourceInfos`** — el `resourceId` y `localIndex` que necesita la apertura remota.
- **Ultimos eventos** — los textos exactos, en el idioma de tu cuenta.

Los archivos salen censurados (correo, serial, IP, MAC, tokens). Aun asi,
revisalos antes de pegarlos en un issue publico.

Si el login falla con credenciales buenas, casi siempre es la region. El codigo
corto se expande a `apii<codigo>.ezvizlife.com`; para Costa Rica suele ser `us`,
y si no, prueba `eu` o `sgp`.

**Antes de correrlo, abre y cierra el cerrojo un par de veces** con huella,
codigo y app, para que el registro tenga un evento de cada tipo.

## Paso 2: decodificar los codigos de estado

`dlDoor=4` y `dlLock=3` son codigos que EZVIZ no documenta en ningun lado. Lo
unico que se sabe es que esos son los valores **en reposo** (puerta cerrada y
con llave). Falta ver que valores toman en los demas estados:

```bash
python3 vigilar_estados.py --usuario tu@correo.com --serial TU_SERIAL
```

Consulta cada 2 segundos y solo escribe cuando algo cambia. Con el corriendo,
abri con huella sin abrir la puerta, despues abri la puerta, dejala abierta un
rato, cerrala, y espera a que eche llave sola. Ctrl+C imprime la tabla.

Los codigos que salgan se ponen en `CODIGOS_PUERTA` y `CODIGOS_CERROJO` de
`const.py`:

```python
CODIGOS_PUERTA: Final[dict[str, bool]] = {
    "4": False,   # cerrada
    "?": True,    # abierta  <-- lo que te diga vigilar_estados.py
}
```

**Mientras un codigo no este en el mapa, la entidad queda no disponible y se
escribe un aviso en el registro con el valor exacto.** Es a proposito: en un
sensor de puerta, admitir que no se sabe es mejor que inventar un "cerrada".

## Paso 3: ajustar el resto del mapa de datos

Abre `custom_components/ezviz_dl05/const.py` y compara lo que viste en el volcado
con las tuplas `RUTAS_*`. Cada tupla es una lista de caminos alternativos y gana
el primero que exista, asi que **agregar** un camino es seguro; no hace falta
borrar los del DL03.

Ejemplo: si tu volcado muestra `STATUS.optionals.doorState` en vez de `dlDoor`:

```python
RUTAS_PUERTA: Final = (
    ("STATUS", "optionals", "dlDoor"),
    ("STATUS", "optionals", "doorStatus"),
    ("STATUS", "optionals", "doorState"),   # <-- el tuyo
)
```

Lo mismo con `PALABRAS_ABIERTO` / `PALABRAS_CERRADO` / `PALABRAS_TIMBRE` si tus
eventos usan otras frases. Son raices en minusculas, comparadas por subcadena.

## Paso 4: instalar

### Con HACS (recomendado)

[![Abrir en HACS](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=JOSDEAD&repository=ha-ezviz-dl05&category=integration)

1. En HACS, menu de tres puntos -> **Repositorios personalizados**.
2. Agrega `https://github.com/JOSDEAD/ha-ezviz-dl05` con la categoria **Integracion**.
3. Busca **EZVIZ DL05 Lock**, descargala y reinicia Home Assistant.

### Manual

Copia la carpeta `custom_components/ezviz_dl05/` dentro de la carpeta
`config/custom_components/` de tu Home Assistant y reinicia.

Despues, en ambos casos:
**Ajustes -> Dispositivos y servicios -> Anadir integracion -> EZVIZ DL05**.

Pide correo, contrasena, serial y region. A diferencia de la integracion del
DL03, esta **valida las credenciales y comprueba que el serial exista** antes de
crear la entrada; si el serial no aparece, escribe en el registro la lista de
seriales disponibles en la cuenta.

## Paso 5: verificar sin abrir codigo

En la pagina del dispositivo hay un boton **Descargar diagnostico**. Ese JSON es
el mismo volcado del paso 1, pero tomado desde Home Assistant ya en marcha. Sirve
para comprobar que las rutas que pusiste en `const.py` estan encontrando datos.

---

## Pruebas

Hay 19 pruebas que arrancan un Home Assistant real en memoria con el cerrojo
simulado. No tocan la nube de EZVIZ ni necesitan credenciales.

```bash
python3.12 -m venv venv && source venv/bin/activate
pip install -r requirements-test.txt
pytest -q
```

Cubren el alta (credenciales malas, 2FA, serial inexistente, duplicado), la
creacion de las entidades con sus valores, la deteccion de eventos en espanol,
el retorno automatico del pestillo a cerrado, la precedencia del estado del API
sobre el deducido, la apertura remota y la descarga limpia de la integracion.

**Por que importan para vos:** `tests/conftest.py` tiene el JSON simulado del
cerrojo en `DATOS_CERROJO`. Cuando tengas el volcado real del paso 1, pega esa
forma ahi y corre `pytest`. Las pruebas que fallen te dicen exactamente que
entidades no encuentran su dato, sin tener que reiniciar Home Assistant ni
adivinar mirando el registro.

---

## Entidades

| Entidad | Tipo | Notas |
|---|---|---|
| Puerta | `binary_sensor` (door) | No disponible si tu DL05 no publica el dato |
| Pestillo | `binary_sensor` (lock) | Atributo `fuente`: `api` o `eventos` |
| Timbre | `binary_sensor` (sound) | Activo 7 s tras el toque |
| Bateria | `sensor` (%) | |
| Ultimo evento | `sensor` | Texto crudo del registro |
| Ultimo usuario | `sensor` | Quien abrio, del `customerInfo` |
| Metodo de apertura | `sensor` | huella, codigo, tarjeta, app... |
| Señal Wi-Fi, Red, IP | `sensor` | Diagnostico; los dos ultimos desactivados por defecto |
| Intentos fallidos | `sensor` | Solo DL03 Pro; en el DL05 queda vacio |
| Cerradura | `lock` | **Desactivada por defecto**, ver abajo |

## Apertura remota

Esta apagada a proposito. Se activa en **Opciones** de la integracion, junto con
el numero de cerrojo (`2` = puerta, `1` = porton; asi los numera `pyezvizapi`).

Dos advertencias reales:

1. Una entidad `lock` en Home Assistant significa que cualquier automatizacion,
   escena o error de script puede abrir la puerta de la casa. Si solo queres
   monitoreo, dejala apagada.
2. Muchos DL05 cierran solos por motor y la nube **rechaza** el comando de
   cierre. En ese caso `lock.lock` va a dar error y solo `lock.unlock` sirve.
   Es limitacion del cerrojo, no de la integracion.

## Limitaciones conocidas

- **Es la nube, no local.** Sin internet no hay estado ni control. El DL05 no
  habla Matter ni Zigbee, asi que no hay forma local.
- **Sondeo, no push.** El retraso tipico es de unos segundos, no instantaneo.
- **2FA no soportado.** Si la cuenta EZVIZ pide codigo de verificacion, el
  login falla. Lo habitual es crear una cuenta EZVIZ aparte, compartirle el
  cerrojo desde la app, y usar esa cuenta aqui sin 2FA. Ademas evita que Home
  Assistant tenga las credenciales de tu cuenta principal.
- **API privado.** EZVIZ puede cambiarlo o cerrarlo sin aviso.

## Creditos

La logica de deducir el estado del pestillo desde el registro de eventos, y el
sondeo rapido de `get_alarminfo`, vienen de `ha-ezviz-dl03-pro` de
[iwanstudio](https://github.com/iwanstudio/ha-ezviz-dl03-pro) (MIT). El acceso
al API lo hace [`pyezvizapi`](https://github.com/RenierM26/pyEzvizApi).

No afiliado con EZVIZ ni con Hangzhou Hikvision. Usalo bajo tu propio riesgo.
