"""Step 6 proof: drive the real UI in a browser and check what a visitor would actually see."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from playwright.sync_api import sync_playwright  # noqa: E402

UI = "http://127.0.0.1:5173"
SHOTS = Path(sys.argv[1] if len(sys.argv) > 1 else "/tmp/mcp-ui")
failures: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    print(f"  [{'PASS' if condition else 'FAIL'}] {label}" + (f" — {detail}" if detail else ""))
    if not condition:
        failures.append(label)


def main() -> int:
    SHOTS.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        # Uses the system Chrome, so no browser download.
        browser = p.chromium.launch(channel="chrome")
        page = browser.new_page(viewport={"width": 1200, "height": 900})
        page.goto(UI, wait_until="networkidle")

        print("\n1. idle state")
        check("header names the project", page.locator(".brand").inner_text() == "mcp-router")
        check("header reports 11 servers and 504 tools",
              "11 servers" in page.locator(".counts").inner_text()
              and "504 tools" in page.locator(".counts").inner_text(),
              page.locator(".counts").inner_text())
        check("one status dot per server", page.locator(".dot").count() == 11)
        check("all dots are up", page.locator(".dot.up").count() == 11)
        check("four presets", page.locator(".preset").count() == 4)
        page.screenshot(path=str(SHOTS / "01-idle.png"))

        print("\n2. a preset fills the field, it does not submit")
        page.locator(".preset", has_text="Cross-server chain").click()
        page.wait_for_timeout(200)
        value = page.locator("input[type=text]").input_value()
        check("the field is filled", "newest open PR" in value, value[:60])
        check("nothing ran", page.locator(".row").count() == 0)

        print("\n3. the run streams, and the input locks while it does")
        page.locator("button[type=submit]").click()
        page.wait_for_selector(".row", timeout=15_000)
        check("input disabled during the run", page.locator("input[type=text]").is_disabled())
        check("presets disabled during the run", page.locator(".preset").first.is_disabled())
        page.wait_for_selector(".line >> text=tools selected", timeout=30_000)
        page.screenshot(path=str(SHOTS / "02-routing.png"))

        page.wait_for_selector(".answer", timeout=180_000)
        page.wait_for_timeout(400)

        print("\n4. the finished trace")
        check("the header reports what was sent",
              "of 504 tools sent" in page.locator(".sent").inner_text(),
              page.locator(".sent").inner_text())
        check("a widen was rendered as an event",
              page.locator(".line", has_text="Wrong toolbox").count() >= 1)
        calls = page.locator(".line .server").count()
        check("tool calls rendered", calls >= 2, f"{calls} calls")
        check("both servers appear in the trace",
              page.locator(".server", has_text="github").count() >= 1
              and page.locator(".server", has_text="slack").count() >= 1)
        check("an answer rendered", page.locator(".answer p").count() >= 1)
        check("input re-enabled after the run",
              not page.locator("input[type=text]").is_disabled())
        page.screenshot(path=str(SHOTS / "03-answer.png"), full_page=True)

        print("\n5. arguments expand on click")
        first_args = page.locator(".sub.clickable").first
        before = first_args.inner_text()
        first_args.click()
        page.wait_for_timeout(200)
        check("clicking an argument line reveals the full value",
              page.locator(".open").count() >= 1, f"was: {before[:40]}")
        page.screenshot(path=str(SHOTS / "04-expanded.png"))

        print("\n6. the ambiguous preset asks instead of calling tools")
        page.goto(UI, wait_until="networkidle")
        page.locator(".preset", has_text="Ambiguous").click()
        page.locator("button[type=submit]").click()
        page.wait_for_selector(".line >> text=Needs clarification", timeout=120_000)
        page.wait_for_timeout(300)
        check("no tool call rows", page.locator(".line .server").count() == 0)
        check("a question is shown", page.locator(".row .say").count() >= 1)
        page.screenshot(path=str(SHOTS / "05-clarify.png"))

        browser.close()

    print(f"\nscreenshots in {SHOTS}")
    if failures:
        print(f"FAILED {len(failures)}: " + "; ".join(failures))
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
