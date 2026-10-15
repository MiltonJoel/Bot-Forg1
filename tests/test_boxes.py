"""Parsing of the /cajas cards. Fixture markup is trimmed from the real page
(fetched 2026-08-18) — the attribute order and button order are what the
parser keys off, so keep them as the site renders them."""

import asyncio

from app.services import boxes_service
from app.services.boxes_service import BoxOpener, next_check_delay, parse_boxes


class _FakeResponse:
    def __init__(self, page_html: str) -> None:
        self.status = 200
        self.headers: dict[str, str] = {}
        self._html = page_html

    async def text(self) -> str:
        return self._html


class _FakeContext:
    """Only what _tick touches: context.request.get()."""

    def __init__(self, page_html: str) -> None:
        self.request = self
        self._html = page_html

    async def get(self, url: str) -> _FakeResponse:
        return _FakeResponse(self._html)


async def _noop(_result: object = None) -> None:
    return None


_CARD = """<article class="forg1-card-in group relative">
 <button type="button" aria-label="Ver los premios de {name}" class="x">
   <span>6</span> drops<span>Ver premios</span></button>
 {timer}
 <button data-slot="button" data-variant="secondary" class="y disabled:pointer-events-none disabled:opacity-50"{disabled}>{cta}</button>
</article>"""
_TIMER = '<span class="font-mono tabular-nums">{t}</span>'


def _page(*cards: str) -> str:
    return "<main>" + "".join(cards) + "</main>"


def _cooling(name: str, timer: str) -> str:
    return _CARD.format(
        name=name, timer=_TIMER.format(t=timer), disabled=' disabled=""', cta="En cooldown"
    )


def test_cooling_boxes_are_not_ready_and_carry_their_countdown() -> None:
    boxes = parse_boxes(
        _page(_cooling("Caja Bronce", "23:25:39"), _cooling("Caja Plata", "01:02:03"))
    )
    assert [b.name for b in boxes] == ["Caja Bronce", "Caja Plata"]
    assert not any(b.ready for b in boxes)
    assert boxes[1].seconds_left == 3723


def test_level_locked_box_is_not_ready_and_has_no_timer() -> None:
    locked = _CARD.format(
        name="Caja Oro", timer="", disabled=' disabled=""', cta="Nivel 7 para abrir"
    )
    (box,) = parse_boxes(_page(locked))
    assert not box.ready and box.seconds_left is None


def test_an_enabled_cta_is_a_ready_box() -> None:
    # The one state never observed live (both boxes were cooling down), so it
    # is pinned here: readiness must come from the missing `disabled`, not from
    # the label, which nobody has seen.
    ready = _CARD.format(name="Caja Bronce", timer="", disabled="", cta="Abrir caja")
    (box,) = parse_boxes(_page(ready))
    assert box.ready


def test_ver_premios_button_alone_never_reads_as_ready() -> None:
    # Regression guard: that button is always enabled, so a parser taking the
    # FIRST button would open nothing and claim every box was ready.
    (box,) = parse_boxes(_page(_cooling("Caja Bronce", "10:00:00")))
    assert not box.ready
    assert box.cta == "En cooldown"


def test_delay_follows_the_soonest_countdown() -> None:
    boxes = parse_boxes(_page(_cooling("A", "05:00:00"), _cooling("B", "00:30:00")))
    assert next_check_delay(boxes) == 30 * 60 + 15


def test_delay_is_clamped_when_a_countdown_is_about_to_expire() -> None:
    assert next_check_delay(parse_boxes(_page(_cooling("A", "00:00:01")))) == 60.0


def test_delay_falls_back_when_nothing_is_counting_down() -> None:
    locked = _CARD.format(
        name="Caja Oro", timer="", disabled=' disabled=""', cta="Nivel 7 para abrir"
    )
    assert next_check_delay(parse_boxes(_page(locked))) == 6 * 3600


def test_a_ready_box_saves_the_dom_and_gets_opened(tmp_path, monkeypatch) -> None:
    """The enabled state has never been observed live, so the tick that finally
    sees one must leave the markup on disk — that dump is the whole record."""
    ready = _CARD.format(name="Caja Bronce", timer="", disabled="", cta="Abrir caja")
    monkeypatch.setattr(boxes_service, "_DUMP_DIR", tmp_path)
    opener = BoxOpener(_FakeContext(_page(ready)), "https://www.forg1.com/cajas", None)
    opened = []
    monkeypatch.setattr(opener, "_open", lambda box: _noop(opened.append(box.name)))

    asyncio.run(opener._tick())

    (dump,) = list(tmp_path.glob("cajas_ready_*.html"))
    assert "Abrir caja" in dump.read_text()
    assert opened == ["Caja Bronce"]


def test_a_cooling_tick_writes_nothing(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(boxes_service, "_DUMP_DIR", tmp_path)
    opener = BoxOpener(_FakeContext(_page(_cooling("Caja Bronce", "05:00:00"))), "u", None)

    asyncio.run(opener._tick())

    assert list(tmp_path.iterdir()) == []


def test_tailwind_disabled_variant_classes_do_not_read_as_disabled() -> None:
    # The live CTA's class list always carries `disabled:opacity-50`; matching
    # the word anywhere in the tag kept every "Abrir caja" box closed for days.
    ready = _CARD.format(name="Caja Bronce", timer="", disabled="", cta="Abrir caja")
    assert "disabled:" in ready
    (box,) = parse_boxes(_page(ready))
    assert box.ready


def test_card_served_without_its_cta_is_skipped() -> None:
    # 2026-09-07: the server sent cards with only "Ver premios", which then
    # read as an enabled CTA on every box.
    lone = """<article class="c"><button type="button"
      aria-label="Ver los premios de Caja Bronce" class="x">Ver premios</button></article>"""
    assert parse_boxes(_page(lone)) == []
