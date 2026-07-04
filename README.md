# forg1-monitor

Monitor en Python para la página de predicciones de Forg1. El proyecto está diseñado como un sistema de solo lectura:

- detecta predicciones activas,
- calcula una sugerencia de apuesta,
- envía alertas a Telegram,
- y muestra alertas de Kick en modo informativo.

No hace clics para apostar ni automatiza la colocación de apuestas.

## Estado del proyecto

La base del proyecto ya está armada y organizada por módulos:

- `app/bot/`: navegador, login persistente y lectura de predicciones.
- `app/services/`: monitoreo, notificaciones y verificación de Kick.
- `app/strategies/`: lógica de decisión de apuestas.
- `app/models/`: modelos de dominio.
- `tests/`: pruebas unitarias de la estrategia.

## Requisitos

- Python 3.13, según `pyproject.toml`.
- Un bot de Telegram.
- `playwright` instalado y los navegadores descargados.

## Archivos que faltan o que no se versionan

Estos no están en el repositorio porque son de entorno local o se generan en runtime:

- `.env`: credenciales y configuración local.
- `.auth/forg1_state.json`: sesión persistente del navegador.
- `.data/seen_predictions.json`: estado para no repetir avisos.
- `logs/`: archivos de log.

Ya existe un ejemplo de configuración en [`.env.example`](./.env.example).

## Configuración

1. Copia [`.env.example`](./.env.example) a `.env`.
2. Completa estos valores:
   - `TELEGRAM_BOT_TOKEN`
   - `TELEGRAM_CHAT_ID`
3. Opcionalmente ajusta:
   - `HEADLESS`
   - `FORG1_URL`
   - `KICK_CHANNEL`
   - umbrales de estrategia

## Instalación

```bash
uv sync
uv run playwright install
```

Si no usas `uv`, instala dependencias equivalentes con tu gestor preferido.

## Verificación

Ejecuta las pruebas unitarias:

```bash
pytest -q
```

Con el ajuste de compatibilidad aplicado, las pruebas deberían correr en Python 3.10+ para esta base de código. Si quieres mantener el requisito estricto de Python 3.13, puedes volver a fijarlo en el entorno de ejecución.

## Ejecución

```bash
python main.py
```

La primera vez puede pedir login manual en el navegador controlado por Playwright y luego guardar la sesión en `.auth/forg1_state.json`. Después, el bot vuelve a levantarse con un solo comando y reutiliza esa sesión sin depender de Chrome abierto en paralelo.

## Estructura

```text
app/
  bot/           browser, login, lector de predicciones
  config/        settings
  models/        modelos de dominio
  services/      monitoreo, Telegram, Kick
  strategies/    lógica de decisión
  utils/         helpers
tests/           pruebas
```

## Notas

- Los selectores de la página de predicciones están marcados como provisionales y deben validarse contra el DOM real.
- `TelegramNotifier` es un canal de alertas, no un controlador del bot.
- `KickLiveChecker` solo informa estado en vivo/offline.
