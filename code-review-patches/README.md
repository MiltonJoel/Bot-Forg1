# forg1-monitor

Bot que mira las predicciones de [forg1.com](https://www.forg1.com/predicciones),
decide si conviene apostar, **apuesta de verdad** en la página con Chrome
(Playwright) y avisa todo por Telegram. También abre las cajas gratis de forg1 y
deja el stream de Kick reproduciéndose.

> **Antes de tocar nada, tres reglas**
>
> 1. **Este bot gasta puntos reales.** Todo cambio en `place_bet()`
>    (`app/bot/prediction_page.py`) o en `MonitoringService` es un cambio en el
>    camino del dinero: con tests, y probado con `dry_run.py` antes de reiniciar.
> 2. **Los puntos se borran cada 30 días** y el objetivo es el umbral de premio de
>    4.500.000. Eso cambia cómo se elige cada dial (ver `CLAUDE.md`, sección
>    "The objective"). No bajes la agresividad sólo por miedo a perder.
> 3. **El `.env` manda.** Los valores por defecto de `app/config/settings.py` y de
>    `.env.example` son viejos en varios casos. Lo que el bot usa es lo que dice
>    tu `.env`.

---

## 1. Puesta en marcha (primera vez)

Necesitás: Linux, Python 3.13, [uv](https://docs.astral.sh/uv/), Google Chrome
instalado (el bot usa el Chrome real, no Chromium) y un bot de Telegram creado
con [@BotFather](https://t.me/BotFather).

```bash
cd ~/Desktop/bot-forg1
uv sync                                   # instala dependencias en .venv
uv run playwright install                 # drivers de Playwright
cp .env.example .env                      # después editá .env (ver sección 4)
uv run python scripts/fetch_team_ratings.py   # ranking oficial de Valve
```

En `.env` lo único obligatorio es `TELEGRAM_BOT_TOKEN`. `TELEGRAM_CHAT_ID` puede
quedar vacío: mandale cualquier mensaje al bot por Telegram, arrancá el bot, y
el chat id aparece en el log. Pegalo en `.env` para los próximos arranques.

Para el modelo de forma (CounterPredictor, ver sección 5) hace falta además
exportar `.data/counterpredictor.json` una vez.

## 2. Arrancar, parar y reiniciar

```bash
./run.sh            # arranca el bot (usa el Python de .venv a propósito)
Ctrl+C              # lo para limpio y avisa "🛑 Bot forg1 detenido"
```

- **Primer arranque:** se abre Chrome. Si forg1 no tiene sesión, el bot avisa
  por Telegram y espera a que te loguees con Kick en esa ventana. Cuando detecta el login sigue solo y guarda la sesión en
  `.auth/forg1_state.json`; los próximos arranques la restauran.
- **Dónde corre:** sólo en esta máquina (ya no se usa el VPS). Con
  `HEADLESS=false`: Chrome visible es lo que mantiene contenta a Cloudflare, y
  la ventana es donde te volvés a loguear si hace falta.
- **Reiniciar a distancia:** mandale `/reainiciobot` al bot por Telegram
  (también `/reiniciobot` o `/reiniciarbot`). Cierra Chrome y lo vuelve a abrir
  con la sesión guardada. Sólo obedece al chat de `TELEGRAM_CHAT_ID`.
- **No corras dos copias a la vez** en la misma carpeta: comparten el perfil de
  Chrome y el bot se niega a arrancar si otro Chrome lo tiene abierto.

Los logs están en `logs/forg1_monitor.log`.

### Cómo saber si está corriendo

Lo prendés y lo apagás vos, a mano. Mientras esté apagado no ve ni apuesta
ningún mercado, así que acordate de prenderlo antes del stream y de dejar la
terminal abierta (cerrarla o suspender la máquina lo apaga).

- **Por Telegram:** al arrancar manda "✅ Bot forg1 conectado y en marcha" y al
  parar "🛑 Bot forg1 detenido". Si el último que recibiste es el de detenido,
  está apagado.
- **En la máquina:**

  ```bash
  pgrep -af "python main.py"        # si imprime una línea, está corriendo
  tail -f logs/forg1_monitor.log    # lo que está haciendo ahora
  ```

  Con el bot prendido y el stream en vivo, el log muestra "Prediction read"
  cada pocos segundos mientras hay un mercado abierto.

Tip: si lo corrés dentro de `tmux`, cerrar la terminal sin querer no lo apaga.
Arrancarlo antes del stream y dejarlo prendido no cuesta nada: mientras el
stream está offline, el bot espera sin leer forg1.

## 3. Qué significan los avisos de Telegram

| Aviso | Qué pasó | Qué hacer |
|---|---|---|
| 🔔 Nueva predicción … ✅ Apuesta ejecutada | Apostó y forg1 mostró el resumen | Nada |
| 🔔 … ⏭️ Sin apuesta | El precio no tenía ventaja | Nada |
| 🔔 … ❓ No pude confirmar la apuesta | El botón final no respondió (Turnstile, ~1 de cada 4) | Mirá tus jugadas en forg1 **antes** de apostar a mano: muchas veces la apuesta sí entró |
| 🏁 Predicción finalizada | Se resolvió el mercado | Nada |
| 🛑 Freno automático | El balance cayó `BANKROLL_STOP_PCT` desde que arrancó la sesión | Reiniciar el bot lo rehabilita |
| ⚠️ La sesión de forg1 expiró | Se perdió el login | Logueate en la ventana de Chrome del bot |
| 🚫 Cloudflare está bloqueando al bot | No puede leer predicciones | Resolvé el desafío a mano en la ventana de Chrome |
| 🚫 … bloqueando el chequeo de Kick | No sabe si hay stream | Suele pasar solo |
| ⚠️ El bot no puede leer la página … | Varias lecturas seguidas fallaron | Revisá conexión y la ventana de Chrome |
| ⚠️ La ventana de Chrome se cerró sola | Chrome murió | Se relanza solo en 15 s |
| 🎁 Caja abierta | Abrió una caja gratis | Nada |

Cada alerta de problema se manda una sola vez y llega un ✅ cuando se arregla.

Si ves respuestas tipo "something went wrong while processing your request",
son de **OpenClaw**, que usa el mismo token de Telegram. Los dos programas se
pisan los mensajes; la solución de fondo es un token propio para este bot.

## 4. Configuración (`.env`)

Todo se configura en `.env` (lista completa y comentada en `.env.example`). Los
diales que deciden las apuestas:

| Variable | Qué controla |
|---|---|
| `STRATEGY_CROWD_WEIGHT` | Cuánto le cree el bot al pool en partidos de equipos. 1.0 = el pool es un precio justo (casi nunca apuesta). Se mide con `analyze_results.py` |
| `STRATEGY_UNRATED_CROWD_WEIGHT` | Lo mismo para mercados sin equipos (kills, preguntas sueltas) |
| `STRATEGY_KELLY_FRACTION` | Fracción del monto Kelly (1.0 = Kelly completo) |
| `STRATEGY_MAX_PCT` | Tope por apuesta, en % del balance |
| `STRATEGY_MIN_EDGE` | Ventaja mínima esperada para entrar |
| `STRATEGY_RATING_WEIGHT` | Peso del ranking de Valve (sólo si los dos equipos están rankeados) |
| `STRATEGY_FORM_WEIGHT` | Peso del modelo de forma (CounterPredictor, o MapForm si no hay export) |
| `STRATEGY_MIN_FORM_EDGE` | Sólo apuesta si el modelo de forma, solo, ve al menos esta ventaja sobre el pool (0.05 = 5 puntos). Vacío = apagado. Requiere `STRATEGY_FORM_WEIGHT` > 0 |
| `STRATEGY_OT_CROWD_WEIGHT` | Precio del "OT" en mercados de mapa. 1.0 = nunca apuesta a OT |
| `STRATEGY_ALWAYS_BACK` | Equipos a los que apostar siempre, a cualquier precio. Vacío = ninguno |
| `BANKROLL_STOP_PCT` | Freno automático por caída del balance en la sesión |

Por qué cada uno tiene el valor que tiene está en `CLAUDE.md`, con fecha y
medición. **Antes de cambiar un dial**, mirá cuántos días quedan del ciclo de 30
días y medí el cambio (sección 6).

Otros que conviene conocer:

- `KICK_CHECK_INTERVAL_SECONDS`: cada cuánto se fija si el stream está en vivo.
  Mientras Kick confirma que está offline, el bot no lee forg1, así que dejalo
  bajo (30 a 300). El 1800 de `.env.example` puede hacerle perder el primer
  mercado del día.
- `PREDICTION_STABILIZATION_SECONDS`: cuánto espera a que el pool se asiente
  antes de apostar.
- `HEADLESS`: `false` en producción.

## 5. Mantener los datos al día

| Qué | Cuándo | Comando |
|---|---|---|
| Ranking de Valve | Cuando sale uno nuevo (~1 vez por mes) | `uv run python scripts/fetch_team_ratings.py` |
| CounterPredictor | Después de cada `update.py` en `~/Desktop/CounterPredictor` | ver abajo |
| Historial de bo3.gg (MapForm) | Sólo si usás MapForm | `uv run python scripts/fetch_match_history.py` |

**CounterPredictor** es el modelo de forma que usa el bot cuando existe
`.data/counterpredictor.json`. Se exporta con el Python **del sistema** (tiene
pandas y sklearn; el venv del bot no):

```bash
cd ~/Desktop/CounterPredictor && python3 update.py      # opcional: datos nuevos
cd ~/Desktop/bot-forg1
/usr/bin/python3 scripts/export_counterpredictor.py --v1   # modelo v1 (el que está en vivo desde 2026-10-08)
/usr/bin/python3 scripts/export_counterpredictor.py        # modelo v2
```

Tarda varios minutos. El archivo es una foto fija: si no lo volvés a exportar,
los ratings se quedan viejos. Después de exportar, reiniciá el bot para que lo
cargue.

## 6. Medir antes de decidir

```bash
.venv/bin/python scripts/analyze_results.py   # ¿el pool está calibrado? ¿los modelos le ganan al pool?
.venv/bin/python scripts/replay.py            # barrido de diales sobre los mercados registrados
.venv/bin/python scripts/dry_run.py           # lee el mercado abierto en vivo e imprime qué apostaría, SIN apostar
```

- `analyze_results.py` lee `.data/results.jsonl` (cada mercado resuelto, con
  el pool como estaba al decidir) y dice si conviene subir algún peso. Mientras
  el intervalo de confianza cruce el cero, la respuesta es "no".
- `replay.py` no toca el navegador y corre en segundos. Ojo: hoy sólo barre
  `crowd_weight`, `kelly_fraction` y `max_pct`, sin modelo de forma.
- `dry_run.py` necesita Chrome y un mercado abierto. Corrélo después de tocar
  el scraping o la estrategia.

Ojo: el `results.jsonl` de esta máquina llega hasta 2026-09-08. Los mercados
de después quedaron en el del VPS; si todavía tenés acceso, traelo y juntalo
antes de medir.

## 7. Aplicar un cambio

1. `uv run pytest -q` (tiene que dar todo verde).
2. Si tocaste el scraping o la estrategia, `dry_run.py` con un mercado abierto.
3. Si cambiaste diales, editá `.env`.
4. Reiniciá el bot (Ctrl+C y `./run.sh` de nuevo) y mirá que llegue "✅ Bot forg1 conectado".

## 8. Tests y calidad

```bash
uv run pytest -q                                         # todos los tests
uv run pytest tests/test_place_bet_flow.py -q            # un archivo
uv run ruff check . && uv run black . && uv run mypy app # lint, formato, tipos
```

Los tests usan objetos falsos de Playwright: no abren Chrome ni apuestan.

## 9. Cómo está armado

```text
main.py                 arranca todo y relanza Chrome si muere
app/
  bot/
    browser.py          Chrome real con perfil propio (.auth/chrome-profile)
    login.py            login con Kick y restauración de cookies
    prediction_page.py  lee predicciones (del payload RSC, no del DOM) y APUESTA
  services/
    monitoring_service.py  el loop: detecta mercado → espera → decide → apuesta → avisa
    kick_service.py        ¿hay stream? + abre el reproductor de Kick
    notifier.py            Telegram
    telegram_commands.py   /reainiciobot
    boxes_service.py       abre las cajas gratis de /cajas
    counter_predictor.py   modelo de forma (lee el JSON exportado)
    match_history.py, bo3.py  MapForm (respaldo si no hay CounterPredictor)
    map_resolver.py        averigua en bo3.gg qué mapa se juega
    team_ratings.py        ranking de Valve
  strategies/
    contrarian_pari_mutuel.py  la estrategia (pura: no ve el navegador)
  config/settings.py    lee .env
  models/prediction.py  Prediction, Outcome, BetDecision
  utils/                HTTP (IPv4 primero, detección de Cloudflare), retry, logger
scripts/                herramientas de la sección 5 y 6
tests/
```

**El ciclo de una apuesta:** el monitor lee la página por HTTP cada 45 s (5 s
con un mercado abierto). Cuando aparece un mercado nuevo espera
`PREDICTION_STABILIZATION_SECONDS`, lo vuelve a leer, averigua el mapa, y le
pasa todo a la estrategia. La estrategia ve cada opción como lo que paga (en
forg1, el % del pool *es* el precio), mezcla el pool con el ranking de Valve y
el modelo de forma, y calcula el monto. Si apuesta, el bot recarga la página,
clickea la opción, escribe el monto, **verifica que el campo lo tenga**, y
confirma una sola vez. Nunca reintenta después del click: un "fallo" puede ser
una apuesta que ya entró.

## 10. Archivos que no se versionan

- `.env`: tu configuración y el token de Telegram.
- `.auth/`: perfiles de Chrome y la sesión de forg1 (sensible).
- `.data/`: `results.jsonl` (historial para recalibrar), `counterpredictor.json`,
  `team_ratings.json`, `match_history.json`, `seen_predictions.json`.
- `logs/`: logs, capturas de apuestas que no se pudieron confirmar.

Para el detalle de cada decisión y cada medición, leé `CLAUDE.md`.
