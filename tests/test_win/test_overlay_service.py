"""Tests for the Windows overlay service (thin wrapper over OverlayWindow).

Mirrors ``myvoice/services/overlay_service.py``'s ``OverlayService``
surface. Uses the real (headless) ``OverlayWindow`` -- no mocking of Qt --
via the shared ``qapp`` fixture.
"""
from __future__ import annotations

from myvoice_win.services.overlay_service import OverlayService


def test_no_window_constructed_until_first_use(qapp):
    svc = OverlayService()
    assert svc._window is None


def test_calls_before_construction_are_safe_no_ops(qapp):
    svc = OverlayService()
    # None of these should raise even though no window exists yet.
    svc.hide()
    svc.set_level(0.5)
    svc.set_preview("hello")
    svc.destroy()
    assert svc._window is None


def test_show_listening_lazily_constructs_and_shows_window(qapp):
    svc = OverlayService()
    svc.show_listening()
    assert svc._window is not None
    assert svc._window.isVisible() is True


def test_show_processing_lazily_constructs_and_shows_window(qapp):
    svc = OverlayService()
    svc.show_processing()
    assert svc._window is not None
    assert svc._window.isVisible() is True


def test_set_level_and_set_preview_delegate_to_existing_window(qapp):
    svc = OverlayService()
    svc.show_listening()
    svc.set_level(0.9)
    svc.set_preview("שלום עולם")
    assert svc._window._level > 0.0
    assert svc._window._preview_text == "שלום עולם"


def test_hide_delegates_to_window_and_clears_its_state(qapp):
    svc = OverlayService()
    svc.show_listening()
    svc.set_preview("hello")
    svc.hide()
    assert svc._window.isVisible() is False
    assert svc._window._preview_text == ""


def test_destroy_closes_window_and_resets_for_lazy_recreation(qapp):
    svc = OverlayService()
    svc.show_listening()
    first_window = svc._window
    svc.destroy()
    assert svc._window is None
    assert first_window.isVisible() is False

    svc.show_listening()
    assert svc._window is not None
    assert svc._window is not first_window
