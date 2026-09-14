"""Browser golden path for the TicketPilot workbench, with README screenshots.

Drives the Streamlit workbench the way a demo would - scenario buttons, identity
switching, approval - and asserts the business outcome at every step, so the
suite catches UI/contract drift that the mocked pytest suite cannot. Each step also
saves a screenshot, which is where the images under ``media/ticketpilot/`` come from.

Usage:
    uv run --with playwright python scripts/ticketpilot_ui_e2e.py [URL] [--out DIR] [--headed]
        [--video DIR] [--pace SECONDS]

Defaults to http://localhost:8501 and ``media/ticketpilot``. ``--video`` records the whole
run as a WebM (a backup demo when the live stack misbehaves) and ``--pace`` holds each
finished step on screen for that many seconds so a viewer can read it. Expects the Docker demo
stack (``docker/compose.ticketpilot-demo.yaml``: deterministic reasoner, demo-*-token
identities), so no model call is made. Re-seed before a run if TP-0013 has been refunded
below 200 CNY: ``docker compose -f compose.yaml -f docker/compose.ticketpilot-demo.yaml
run --rm ticketpilot_seed``.

Uses the locally installed Chrome/Edge when Playwright's own Chromium is absent.
Exits 0 when every step passes; writes ``ticketpilot_ui_e2e_failure.png`` otherwise.
"""

import argparse
import re
import sys
import time
from pathlib import Path
from typing import Any

from playwright.sync_api import Browser, Page, expect, sync_playwright

DEFAULT_URL = "http://localhost:8501"
DEFAULT_OUT = Path("media/ticketpilot")
STEP_TIMEOUT_MS = 90_000
SIDEBAR = '[data-testid="stSidebar"]'
MAIN = '[data-testid="stMainBlockContainer"]'

CUSTOMER = "客户 · customer-demo-01"
APPROVER = "审批员 · approver-demo-01"
OTHER_TENANT = "其他租户审批员 · tenant-demo-02"


class StepFailed(AssertionError):
    pass


def log(message: str) -> None:
    print(f"[ui-e2e] {message}", flush=True)


def launch_browser(p, headed: bool) -> Browser:
    errors: list[str] = []
    for channel in (None, "chrome", "msedge"):
        try:
            if channel is None:
                return p.chromium.launch(headless=not headed)
            return p.chromium.launch(channel=channel, headless=not headed)
        except Exception as exc:  # noqa: BLE001 - try the next browser
            errors.append(f"{channel or 'bundled'}: {exc}")
    raise RuntimeError("no Chromium available: " + "; ".join(errors))


def open_workbench(browser: Browser, url: str, video_dir: Path | None = None) -> Page:
    context_options: dict[str, Any] = {"viewport": VIEWPORT}
    if video_dir is not None:
        video_dir.mkdir(parents=True, exist_ok=True)
        context_options["record_video_dir"] = str(video_dir)
        context_options["record_video_size"] = VIEWPORT
    page = browser.new_context(**context_options).new_page()
    page.goto(url, wait_until="domcontentloaded", timeout=60_000)
    heading = page.get_by_text("TicketPilot 售后工单工作台")
    try:
        heading.wait_for(state="visible", timeout=30_000)
    except Exception:
        # Generic mode: the console is an opt-in sidebar toggle.
        page.locator(SIDEBAR).get_by_text("TicketPilot 业务控制台").click()
        heading.wait_for(state="visible", timeout=30_000)
    return page


def main_text(page: Page) -> str:
    return page.locator(MAIN).inner_text()


def wait_for_text(page: Page, text: str, scope: str = MAIN) -> None:
    expect(page.locator(scope).get_by_text(text, exact=False).first).to_be_visible(
        timeout=STEP_TIMEOUT_MS
    )


def click_button(page: Page, label: str, scope: str = MAIN) -> None:
    page.locator(scope).get_by_role("button", name=label, exact=False).first.click()


def field(page: Page, label: str, scope: str = MAIN):
    # While Streamlit swaps in a rerun's elements the old and new widget briefly coexist;
    # wait for the DOM to settle before asserting on a value.
    locator = page.locator(scope).get_by_label(label)
    expect(locator).to_have_count(1, timeout=STEP_TIMEOUT_MS)
    return locator


def choose_identity(page: Page, label: str) -> None:
    box = field(page, "演示身份", SIDEBAR)
    box.click()
    option = page.get_by_role("option", name=label)
    try:
        option.wait_for(state="visible", timeout=5_000)
        option.click()
    except Exception:
        box.fill(label)
        box.press("ArrowDown")
        box.press("Enter")
    expect(page.locator(SIDEBAR).get_by_text("后端已接受该身份")).to_be_visible(
        timeout=STEP_TIMEOUT_MS
    )
    expect(box).to_have_value(label, timeout=STEP_TIMEOUT_MS)


def ensure_docker_tokens(page: Page) -> None:
    source = page.locator(SIDEBAR).get_by_text("Docker 演示令牌")
    if source.count():
        source.first.click()
        page.wait_for_timeout(500)


def action_id(page: Page) -> str:
    match = re.search(r"action ([0-9a-f]{8})", main_text(page))
    if not match:
        raise StepFailed("pending approval card does not show an action id")
    return match.group(1)


def refundable(page: Page) -> str:
    match = re.search(r"当前可退（CNY）\s+([\d,]+\.\d{2})", main_text(page))
    if not match:
        raise StepFailed("order card does not show the refundable amount")
    return match.group(1)


def wait_for_refundable_change(page: Page, previous: str) -> str:
    # "Mock 退款已执行" from an earlier approval stays on screen, so the balance is
    # the only signal that this approval's resume run has finished.
    deadline = time.monotonic() + STEP_TIMEOUT_MS / 1000
    while time.monotonic() < deadline:
        current = refundable(page)
        if current != previous:
            return current
        page.wait_for_timeout(500)
    raise StepFailed(f"refundable amount stayed at {previous} after approval")


def ticket_id(page: Page) -> str:
    page.locator(MAIN).get_by_text("开发者详情").click()
    match = re.search(r"Ticket ([0-9a-f-]{36})", main_text(page))
    if not match:
        raise StepFailed("developer details do not show the ticket id")
    return match.group(1)


def _optimize_png(path: Path) -> None:
    try:
        from PIL import Image
    except ImportError:
        return
    with Image.open(path) as image:
        image.convert("RGB").quantize(colors=256).save(path, optimize=True)


VIEWPORT = {"width": 1440, "height": 960}
MAX_SHOT_HEIGHT = 3200
PACE_SECONDS = 0.0


def shoot(page: Page, out: Path, name: str, full_page: bool = False) -> None:
    out.mkdir(parents=True, exist_ok=True)
    path = out / f"{name}.png"
    if full_page:
        # Streamlit scrolls an inner section, not the document, so Playwright's own
        # full_page capture stops at the viewport; grow the viewport instead.
        content = page.evaluate(
            "() => document.querySelector('[data-testid=\"stMainBlockContainer\"]').scrollHeight"
        )
        page.set_viewport_size(
            {"width": VIEWPORT["width"], "height": min(int(content) + 80, MAX_SHOT_HEIGHT)}
        )
    page.evaluate(
        "() => document.querySelectorAll('[data-testid=\"stMain\"], section')"
        ".forEach(el => el.scrollTo(0, 0))"
    )
    page.wait_for_timeout(400)
    page.screenshot(path=str(path))
    _optimize_png(path)
    if full_page:
        page.set_viewport_size(VIEWPORT)
        page.wait_for_timeout(300)
    log(f"screenshot {path}")
    page.wait_for_timeout(int(PACE_SECONDS * 1000))


def run(page: Page, out: Path) -> None:
    ensure_docker_tokens(page)
    choose_identity(page, CUSTOMER)

    log("1/8 logistics query")
    click_button(page, "查询物流", SIDEBAR)
    expect(field(page, "主题")).to_have_value("查询订单物流")
    click_button(page, "创建并运行")
    wait_for_text(page, "已基于证据回答")
    wait_for_text(page, "政策依据：订单物流与预计送达")
    shoot(page, out, "01-logistics-answered")

    log("2/8 retry same request")
    click_button(page, "重试上一次请求")
    wait_for_text(page, "幂等验证通过")
    shoot(page, out, "02-retry-idempotent")

    log("3/8 vague refund needs input")
    click_button(page, "模糊退款", SIDEBAR)
    expect(field(page, "主题")).to_have_value("申请部分退款")
    click_button(page, "创建并运行")
    wait_for_text(page, "需要客户补充信息")
    if "待审批的退款动作" in main_text(page):
        raise StepFailed("vague refund must not create an approval")
    shoot(page, out, "03-needs-input")

    log("4/8 supply amount -> waiting approval")
    click_button(page, "补充金额：100 元")
    expect(field(page, "追加消息")).to_have_value("100 元", timeout=STEP_TIMEOUT_MS)
    click_button(page, "发送并运行")
    wait_for_text(page, "退款动作等待人工审批")
    wait_for_text(page, "当前身份是客户，不能审批")
    first_action = action_id(page)
    before = refundable(page)
    shoot(page, out, "04-waiting-approval", full_page=True)

    log("5/8 approver approves -> refund executed")
    choose_identity(page, APPROVER)
    wait_for_text(page, "批准并恢复执行")
    shoot(page, out, "05-approver-view")
    click_button(page, "批准并恢复执行")
    after_first = wait_for_refundable_change(page, before)
    wait_for_text(page, "Mock 退款已执行")
    shoot(page, out, "06-refund-executed", full_page=True)

    log("6/8 same amount again is a new action")
    choose_identity(page, CUSTOMER)
    click_button(page, "再次申请相同金额")
    expect(field(page, "追加消息")).to_have_value(
        re.compile("再申请退款 100 元"), timeout=STEP_TIMEOUT_MS
    )
    click_button(page, "发送并运行")
    wait_for_text(page, "退款动作等待人工审批")
    second_action = action_id(page)
    if second_action == first_action:
        raise StepFailed("second 100 CNY request reused the first action id")
    choose_identity(page, APPROVER)
    click_button(page, "批准并恢复执行")
    after_second = wait_for_refundable_change(page, after_first)
    shoot(page, out, "07-second-refund-overview")
    shoot(page, out, "07-second-refund-full", full_page=True)

    log("7/8 cross-tenant lookup is hidden")
    current = ticket_id(page)
    choose_identity(page, OTHER_TENANT)
    page.locator(SIDEBAR).get_by_text("按 Ticket ID 打开").click()
    ticket_field = field(page, "Ticket ID", SIDEBAR)
    ticket_field.fill(current)
    ticket_field.press("Enter")
    click_button(page, "打开工单", SIDEBAR)
    wait_for_text(page, "统一返回 404", SIDEBAR)
    shoot(page, out, "08-cross-tenant-404")

    log("8/8 summary")
    log(
        f"refundable {before} -> {after_first} -> {after_second}; "
        f"actions {first_action} != {second_action}"
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="TicketPilot workbench browser golden path")
    parser.add_argument("url", nargs="?", default=DEFAULT_URL)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--headed", action="store_true")
    parser.add_argument("--video", type=Path, default=None)
    parser.add_argument("--pace", type=float, default=0.0)
    return parser.parse_args()


def main() -> int:
    global PACE_SECONDS
    args = parse_args()
    PACE_SECONDS = max(0.0, args.pace)
    log(f"target {args.url}; screenshots -> {args.out}")
    started = time.monotonic()
    with sync_playwright() as p:
        browser = launch_browser(p, args.headed)
        page = open_workbench(browser, args.url, args.video)
        try:
            run(page, args.out)
        except Exception as exc:  # noqa: BLE001 - report and save evidence
            page.screenshot(path="ticketpilot_ui_e2e_failure.png", full_page=True)
            log(f"FAIL after {time.monotonic() - started:.0f}s: {exc}")
            return 1
        finally:
            video = page.video
            page.context.close()
            if video is not None:
                log(f"video {video.path()}")
            browser.close()
    log(f"PASS in {time.monotonic() - started:.0f}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
