"""Covers PredictionPageReader.place_bet against faked Playwright objects.

The money path: everything after the outcome click is single-attempt, because a
bet that may already be submitted must never be retried. These tests pin that
ordering and the failure modes, which mypy cannot check and the monitor's tests
skip entirely (they fake the whole reader).
"""

import asyncio
import re
from types import SimpleNamespace
from typing import Any

import pytest
from playwright.async_api import Error as PlaywrightError

from app.bot import prediction_page
from app.bot.prediction_page import PredictionPageReader


@pytest.fixture(autouse=True)
def _instant_clock(monkeypatch: Any) -> None:
    """The confirm gate polls against a real 25s deadline with 0.5s naps. Fake
    the clock so these tests prove the logic instead of sitting through it."""
    ticks = iter(range(0, 10_000_000, 3))
    monkeypatch.setattr(prediction_page.time, "monotonic", lambda: next(ticks))

    async def no_nap(_seconds: float) -> None:
        return None

    monkeypatch.setattr(prediction_page.asyncio, "sleep", no_nap)


class FakeLocator:
    def __init__(self, page: "FakePage", name: str) -> None:
        self._page = page
        self._name = name

    # Locator chaining used by the reader: .filter(...).first
    def filter(self, **_: Any) -> "FakeLocator":
        return self

    @property
    def first(self) -> "FakeLocator":
        return self

    async def wait_for(self, state: str = "visible", timeout: float = 0) -> None:
        if self._name == "final" and self._page.open_final_after is not None:
            if self._page.attempts > self._page.open_final_after:
                return
        if self._name == "overlay":
            if state == "detached" and self._page.dialog_open:
                raise PlaywrightError("overlay still attached")
            return
        if self._name in self._page.never_visible:
            raise PlaywrightError(f"{self._name} not visible")

    async def click(self, timeout: float = 0, position: Any = None) -> None:
        # Playwright's click auto-waits for actionability and throws if the
        # element never becomes visible/enabled — the fake mirrors that.
        if (
            self._name == "final"
            and self._page.turnstile_checkbox
            and not self._page.turnstile_ticked
        ):
            raise PlaywrightError("final not enabled (turnstile pending)")
        if self._name in self._page.never_visible and not (
            self._name == "final"
            and self._page.open_final_after is not None
            and self._page.attempts > self._page.open_final_after
        ):
            raise PlaywrightError(f"{self._name} not visible")
        if self._name in self._page.never_enabled:
            raise PlaywrightError(f"{self._name} not enabled")
        if self._name in ("turnstile", "turnstile_widget"):
            self._page.turnstile_ticked = True
            self._page.events.append("tick:turnstile")
            return
        if self._name == "primary":
            self._page.dialog_open = True  # the confirmation modal opens here
        self._page.events.append(f"click:{self._name}")

    async def count(self) -> int:
        if self._name == "no_match":
            return 0
        if self._name in ("turnstile", "turnstile_widget"):
            return 1 if self._page.turnstile_checkbox else 0
        if self._name in self._page.never_visible:
            return 0
        return self._page.outcome_matches if self._name == "outcome" else 1

    async def evaluate(self, expression: str) -> dict[str, Any]:
        return {"tag": "BUTTON", "disabled": False}

    async def fill(self, value: str, timeout: float = 0) -> None:
        self._page.events.append(f"fill:{value}")
        # React sometimes drops fill()'s single input event; the fake models a
        # controlled input that keeps its default when that happens.
        if not self._page.fill_is_ignored:
            self._page.field_value = value

    async def input_value(self, timeout: float = 0) -> str:
        return self._page.field_value

    async def press_sequentially(self, value: str, delay: float = 0) -> None:
        self._page.events.append(f"type:{value}")
        if not self._page.typing_is_ignored:
            self._page.field_value = value

    async def press(self, key: str) -> None:
        self._page.events.append(f"press:{key}")

    async def is_enabled(self) -> bool:
        return self._name not in self._page.never_enabled


class FakeFrame:
    """Stands in for the cross-origin Turnstile iframe.

    Modelled on the live failure: the frame is in page.frames but NOT reachable
    as an `iframe[src*=...]` element from the main frame, and its checkbox sits
    behind a shadow root unless the test says otherwise.
    """

    def __init__(self, page: "FakePage") -> None:
        self._page = page

    @property
    def url(self) -> str:
        # Turnstile puts the widget's state in its own URL: "new" is a fresh
        # challenge, "failure_retry" is one that already errored.
        state = "failure_retry" if self._page.turnstile_failed else "new"
        return (
            "https://challenges.cloudflare.com/cdn-cgi/challenge-platform/h/g/"
            f"turnstile/f/av0/rch/3sp5q/0x4AAA/dark/fbE/{state}/normal?lang=auto"
        )

    def locator(self, selector: str) -> "FakeLocator":
        return FakeLocator(self._page, "turnstile" if self._page.checkbox_reachable else "no_match")

    async def frame_element(self) -> "FakeLocator":
        return FakeLocator(self._page, "turnstile_widget")


class FakePage:
    """Just enough Page surface for place_bet."""

    def __init__(
        self,
        never_visible: set[str] | None = None,
        never_enabled: set[str] | None = None,
        summary_appears: bool = True,
    ) -> None:
        self.never_visible = never_visible or set()
        self.never_enabled = never_enabled or set()
        self.summary_appears = summary_appears
        self.events: list[str] = []
        self.dialog_open = False
        self.open_final_after: int | None = None
        # Managed Turnstile: the checkbox is showing and the confirm button
        # stays disabled until something ticks it.
        self.turnstile_checkbox = False
        self.turnstile_ticked = False
        # The challenge errored outright: clicking does nothing, only a
        # turnstile.reset() gives a widget that can still be solved.
        self.turnstile_failed = False
        # Live evidence says the checkbox is behind a shadow root, so the widget
        # element itself is what gets clicked. Tests flip this to cover both.
        self.checkbox_reachable = False
        self.main_frame = SimpleNamespace(url="https://www.forg1.com/predicciones")
        # The site's default stake is min_bet; that is what a dropped fill()
        # leaves in the field, and what the bot would then confirm.
        self.field_value = "500"
        self.fill_is_ignored = False
        self.typing_is_ignored = False
        self.attempts = 0
        self.url = "https://www.forg1.com/predicciones"
        # How many outcome cards contain the side's label ("OT" is inside "OT 2").
        self.outcome_matches = 1

    def is_closed(self) -> bool:
        return False

    async def reload(self, wait_until: str = "") -> None:
        self.attempts += 1
        self.events.append("reload")

    async def goto(self, url: str, wait_until: str = "") -> None:
        self.url = url
        self.events.append("goto")

    @property
    def frames(self) -> list[Any]:
        # The Turnstile frame only exists while the challenge is showing.
        return [self.main_frame] + ([FakeFrame(self)] if self.turnstile_checkbox else [])

    async def screenshot(self, path: str = "", timeout: float = 0) -> None:
        return None

    def locator(self, selector: str) -> FakeLocator:
        if "dialog-overlay" in selector:
            return FakeLocator(self, "overlay")
        if "challenges.cloudflare.com" in selector:
            # The bug: the widget is nested, so a main-frame iframe selector
            # matches nothing. Going back to page.locator turns the tests red.
            return FakeLocator(self, "no_match")
        return FakeLocator(self, "outcome" if "outcome" in selector else "amount")

    def get_by_role(self, role: str, name: re.Pattern[str] | None = None) -> FakeLocator:
        # "confirmar jugada" is the first step; "^confirmar$" the Cloudflare-gated one.
        assert name is not None
        return FakeLocator(self, "primary" if "jugada" in name.pattern else "final")

    @property
    def keyboard(self) -> "FakeKeyboard":
        return FakeKeyboard(self)

    async def evaluate(self, expression: str) -> None:
        if "turnstile" in expression:
            self.events.append("turnstile:reset")
            self.turnstile_failed = False  # a reset re-renders a fresh challenge

    async def wait_for_function(self, expression: str, timeout: float = 0) -> None:
        if not self.summary_appears:
            raise PlaywrightError("summary never appeared")
        self.events.append("summary")


class FakeKeyboard:
    def __init__(self, page: "FakePage") -> None:
        self._page = page

    async def press(self, key: str) -> None:
        self._page.events.append(f"key:{key}")
        self._page.dialog_open = False


def _reader(page: FakePage) -> PredictionPageReader:
    return PredictionPageReader(page, "https://www.forg1.com/predicciones")  # type: ignore[arg-type]


def test_happy_path_places_the_bet_in_order() -> None:
    page = FakePage()
    assert asyncio.run(_reader(page).place_bet("TeamA", 350)) is True
    assert page.events == [
        "reload",  # DOM is stale after HTTP-only reads
        "click:outcome",
        "fill:350",
        "press:Tab",
        "click:primary",
        "click:final",
        "summary",
    ]


def test_outcome_is_clicked_exactly_once() -> None:
    # The single-attempt guarantee: one outcome click, one primary, one final.
    page = FakePage()
    asyncio.run(_reader(page).place_bet("TeamA", 350))
    for step in ("click:outcome", "click:primary", "click:final"):
        assert page.events.count(step) == 1


def test_missing_outcome_card_retries_the_reload_then_raises() -> None:
    # Retrying here is safe — nothing has been submitted yet.
    page = FakePage(never_visible={"outcome"})
    with pytest.raises(PlaywrightError, match="never became visible"):
        asyncio.run(_reader(page).place_bet("TeamA", 350))
    assert page.events.count("reload") == 3
    assert "click:outcome" not in page.events


def test_label_matching_two_cards_is_refused_before_any_click() -> None:
    # has_text is a substring match: "OT" also matches the "OT 2 o +" card, and
    # .first would stake on whichever renders first.
    page = FakePage()
    page.outcome_matches = 2
    with pytest.raises(RuntimeError, match="refusing to guess"):
        asyncio.run(_reader(page).place_bet("OT", 350))
    assert not any(e.startswith(("click:", "fill:")) for e in page.events)


def test_unclickable_confirm_is_never_retried() -> None:
    # The site's bet history showed bets landing that the logs called failures:
    # Confirmar reads `disabled` BECAUSE the bet already went through. Retrying
    # would stake it twice. One attempt, then report uncertainty.
    page = FakePage(never_visible={"final"})
    assert asyncio.run(_reader(page).place_bet("TeamA", 350)) is False
    assert page.events.count("click:primary") == 1, "must NOT retry — may double the bet"
    assert page.events.count("click:outcome") == 1


def test_missing_post_bet_summary_is_never_retried() -> None:
    # The final click LANDED. The bet may exist on the site even though no
    # summary rendered, so retrying could double it. One attempt, then stop.
    page = FakePage(summary_appears=False)
    assert asyncio.run(_reader(page).place_bet("TeamA", 350)) is False
    assert page.events.count("click:final") == 1
    assert page.events.count("click:primary") == 1


def test_disabled_final_button_is_not_reported_as_placed() -> None:
    # Playwright's click() raises when the element never becomes enabled; the
    # bet did not land, so this must be False and never claim success.
    page = FakePage(never_enabled={"final"})
    assert asyncio.run(_reader(page).place_bet("TeamA", 350)) is False


def test_failed_confirmation_closes_the_dialog() -> None:
    # The leak: a failed bet left the Radix modal open, and its overlay then
    # swallowed every later click — the header balance refresh died with
    # "dialog-overlay intercepts pointer events" minutes afterwards.
    page = FakePage(summary_appears=False)
    assert asyncio.run(_reader(page).place_bet("TeamA", 350)) is False
    assert "key:Escape" in page.events
    assert page.dialog_open is False, "the page must be usable after a failed bet"


def test_missing_summary_also_closes_the_dialog() -> None:
    page = FakePage(summary_appears=False)
    assert asyncio.run(_reader(page).place_bet("TeamA", 350)) is False
    assert page.dialog_open is False


def test_successful_bet_leaves_no_dialog_behind() -> None:
    page = FakePage()
    assert asyncio.run(_reader(page).place_bet("TeamA", 350)) is True
    # The site closes it itself on success; just prove we never wedge the page.
    assert page.events.count("click:final") == 1


def test_managed_turnstile_checkbox_is_ticked_and_unblocks_the_bet() -> None:
    # Cloudflare's MANAGED mode renders a checkbox in a cross-origin iframe and
    # leaves Confirmar disabled until it is clicked. Nothing was clicking it, so
    # the gate never opened and ~25% of bets were lost at random.
    page = FakePage()
    page.turnstile_checkbox = True
    page.checkbox_reachable = True

    assert asyncio.run(_reader(page).place_bet("TeamA", 350)) is True
    assert "tick:turnstile" in page.events
    assert page.events.index("tick:turnstile") < page.events.index("click:final")


def test_failed_turnstile_is_reset_before_clicking_it() -> None:
    # Live on the VPS 2026-08-12: the widget URL said "failure_retry", three
    # clicks landed on it anyway and the token stayed EMPTY. A failed challenge
    # is not waiting on a human — it has to be reset before it can be solved.
    page = FakePage()
    page.turnstile_checkbox = True
    page.checkbox_reachable = True
    page.turnstile_failed = True

    assert asyncio.run(_reader(page).place_bet("TeamA", 350)) is True
    assert page.events.count("turnstile:reset") == 1
    assert page.events.index("turnstile:reset") < page.events.index("tick:turnstile")


def test_healthy_turnstile_is_never_reset() -> None:
    # Resetting a widget that is about to solve itself throws away a good token.
    page = FakePage()
    page.turnstile_checkbox = True
    page.checkbox_reachable = True

    assert asyncio.run(_reader(page).place_bet("TeamA", 350)) is True
    assert "turnstile:reset" not in page.events


def test_no_turnstile_widget_means_no_extra_clicking() -> None:
    # Non-interactive mode solves itself; don't poke at a widget that isn't there.
    page = FakePage()
    assert asyncio.run(_reader(page).place_bet("TeamA", 350)) is True
    assert "tick:turnstile" not in page.events


def test_amount_is_read_back_after_filling() -> None:
    page = FakePage()
    asyncio.run(_reader(page).place_bet("TeamA", 350))
    assert page.field_value == "350"


def test_dropped_fill_is_retyped_rather_than_betting_the_default() -> None:
    # React missed fill()'s input event, so the field still holds the site's
    # default of 500. Retyping key-by-key fires the events React cannot miss.
    page = FakePage()
    page.fill_is_ignored = True

    assert asyncio.run(_reader(page).place_bet("TeamA", 350)) is True
    assert "type:350" in page.events
    assert page.field_value == "350"
    assert page.events.index("type:350") < page.events.index("click:primary")


def test_never_confirms_a_bet_whose_amount_would_not_take() -> None:
    # Neither fill nor typing landed. Placing 500 when the strategy said 350 is
    # worse than placing nothing, so it must refuse and never reach confirm.
    # This is the bug that put a real 500-point bet on the site while the log
    # recorded 6,331.
    page = FakePage()
    page.fill_is_ignored = True
    page.typing_is_ignored = True

    assert asyncio.run(_reader(page).place_bet("TeamA", 350)) is False
    assert "click:primary" not in page.events
    assert "click:final" not in page.events
    assert page.events.count("click:outcome") == 1, "no retry loop on the money path"


def test_widget_is_clicked_when_the_checkbox_cannot_be_reached() -> None:
    # Live evidence (2026-08-12): the Turnstile frame was present, the token
    # stayed EMPTY, no checkbox was reachable (shadow DOM), AND the frame was
    # nested past a main-frame `iframe[src*=...]` selector — so the widget was
    # only findable through page.frames. Nothing was clicked for 27s and
    # CONFIRMAR stayed disabled. Clicking the frame element is what a person
    # does and needs no shadow-DOM piercing.
    page = FakePage()
    page.turnstile_checkbox = True

    assert asyncio.run(_reader(page).place_bet("TeamA", 350)) is True
    assert "tick:turnstile" in page.events


def test_turnstile_clicking_is_capped() -> None:
    # Poking a challenge widget every 500ms for 25s is useless and looks like
    # abuse, so the click budget is small and per-bet.
    page = FakePage(never_visible={"final"})
    page.turnstile_checkbox = True
    asyncio.run(_reader(page).place_bet("TeamA", 350))
    assert page.events.count("tick:turnstile") <= prediction_page._MAX_TURNSTILE_CLICKS


def test_drifted_tab_is_navigated_back_not_reloaded() -> None:
    # 2026-08-16: the tab sat idle overnight, came back parked off /predicciones
    # and stayed there for 2h20m. Every recovery path calls page.reload(), which
    # reloads the WRONG url forever — "Balance button missing" x61, and place_bet
    # would have burned its three reloads on a page with no outcome cards.
    page = FakePage()
    page.url = "https://www.forg1.com/"

    asyncio.run(_reader(page)._ensure_live_page())

    assert page.events == ["goto"], "a drifted tab must be navigated back, never reloaded"
    assert page.url == "https://www.forg1.com/predicciones"


def test_query_string_is_not_treated_as_drift() -> None:
    # The site appends its own query strings; comparing full URLs would
    # renavigate the tab on every poll.
    page = FakePage()
    page.url = "https://www.forg1.com/predicciones?ref=kick"

    asyncio.run(_reader(page)._ensure_live_page())

    assert page.events == []
