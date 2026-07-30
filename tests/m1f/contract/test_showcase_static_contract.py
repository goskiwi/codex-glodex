"""Static-page and loopback-server contract coverage for M1f."""

from __future__ import annotations

from pathlib import Path

import pytest

import scripts.serve_m1f_showcase as server

pytestmark = pytest.mark.contract

PROJECT_ROOT = Path(__file__).parents[3]
SHOWCASE_ROOT = PROJECT_ROOT / "showcase"


class _FakeServer:
    def __init__(self) -> None:
        self.closed = False
        self.served = False

    def serve_forever(self) -> None:
        self.served = True

    def server_close(self) -> None:
        self.closed = True


@pytest.mark.spec("GLO-M1F-P0-001", "GLO-M1F-P0-002", "GLO-M1F-NFR-003")
def test_static_page_is_local_only_and_contains_the_required_showcase_regions() -> None:
    html = (SHOWCASE_ROOT / "index.html").read_text(encoding="utf-8")
    css = (SHOWCASE_ROOT / "styles.css").read_text(encoding="utf-8")
    javascript = (SHOWCASE_ROOT / "app.js").read_text(encoding="utf-8")

    assert "本地录制回放" in html
    assert "非实时 Agent 运行" in html
    assert 'id="event-timeline"' in html
    assert 'id="tool-inventory"' in html
    assert 'id="metric-cards"' in html
    assert "@media (max-width: 720px)" in css
    assert "./assets/m1d-replay.v1.json" in javascript
    assert "./assets/m1e-summary.v1.json" in javascript
    for tool_name in (
        "planner",
        "chat_fallback",
        "web_search",
        "category_insight",
        "item_search",
        "item_picker",
        "price_compare",
        "shipping_calc",
        "shopping_summary",
        "dispatch_tool",
    ):
        assert f'"{tool_name}"' in javascript
    joined = "\n".join((html, css, javascript))
    for forbidden in (
        "http://",
        "https://",
        "<iframe",
        "agent-runs",
        "benchmark_summary",
        "WebSocket",
        "eval(",
        "@import",
    ):
        assert forbidden not in joined


@pytest.mark.spec("GLO-M1F-P0-001", "GLO-M1F-NFR-001", "GLO-M1F-NFR-003")
def test_server_factory_is_fixed_to_loopback_and_never_opens_a_socket_in_tests(
    capsys: pytest.CaptureFixture[str],
) -> None:
    received: list[tuple[tuple[str, int], type[server.ShowcaseRequestHandler]]] = []
    fake = _FakeServer()

    def factory(
        address: tuple[str, int],
        handler: type[server.ShowcaseRequestHandler],
    ) -> _FakeServer:
        received.append((address, handler))
        return fake

    exit_code = server.main(("--port", "9123"), server_factory=factory)

    assert exit_code == 0
    assert received == [(("127.0.0.1", 9123), server.ShowcaseRequestHandler)]
    assert fake.served is True
    assert fake.closed is True
    assert capsys.readouterr().out == "Showcase: http://127.0.0.1:9123/\n"


@pytest.mark.spec("GLO-M1F-P0-001", "GLO-M1F-NFR-001")
def test_server_rejects_ports_outside_the_safe_tcp_range() -> None:
    with pytest.raises(ValueError, match="between 1 and 65535"):
        server.build_server(0)
    with pytest.raises(ValueError, match="between 1 and 65535"):
        server.build_server(65536)
