"""Check real noVNC imports under synthetic WebCodecs failures without login."""

import json
from pathlib import Path
import sys
from urllib.parse import urlparse

from playwright.sync_api import expect, sync_playwright

ORIGIN = "https://raven.test"
OLD_PREFIX = "/static/novnc/"
NEW_PREFIX = "/static/novnc-1.7.0-raven1/"
BASELINE_OBSERVE_MS = 2600
IMPORT_TIMEOUT_MS = 5000
VIEWER_TIMEOUT_MS = 10000
MODES = ("normal", "missing_api", "rejected_support", "hanging_flush", "rejected_flush", "late_support", "successful_decode")
SOURCE = Path("/source")

STUB = """
mode => {
  window.decoderProbe = {supportCalls: 0, created: 0, closed: 0, flushCalls: 0};
  if (mode === 'normal' || mode === 'loader_timeout') return;
  class Decoder {
    static isConfigSupported() {
      window.decoderProbe.supportCalls++;
      if (mode === 'rejected_support') return Promise.reject(new Error('fixture support failure'));
      if (mode === 'late_support') return new Promise(resolve => setTimeout(() => resolve({supported: true}), 3000));
      return Promise.resolve({supported: true});
    }
    constructor(options) { window.decoderProbe.created++; this.state = 'unconfigured'; this.output = options.output; }
    configure() { this.state = 'configured'; }
    decode() { if (mode === 'successful_decode') this.output({close() {}}); }
    flush() {
      window.decoderProbe.flushCalls++;
      if (mode === 'hanging_flush') return new Promise(() => {});
      if (mode === 'rejected_flush') return Promise.reject(new Error('fixture flush failure'));
      return Promise.resolve();
    }
    close() { window.decoderProbe.closed++; this.state = 'closed'; }
  }
  if (mode === 'missing_api') delete Decoder.isConfigSupported;
  Object.defineProperty(window, 'VideoDecoder', {value: Decoder, configurable: true});
  if (!window.EncodedVideoChunk) window.EncodedVideoChunk = class EncodedVideoChunk {};
}
"""

options = json.load(sys.stdin)
output = Path(options["output"])
output.mkdir(parents=True, exist_ok=True)
vendor = Path(options["vendor"])
results = []
if options.get("loader_timeout_only"):
    modes = ("loader_timeout",)
else:
    modes = MODES if options["baseline"] else (*MODES, "loader_timeout")

with sync_playwright() as playwright:
    browser = playwright.chromium.launch(chromium_sandbox=True)
    for mode in modes:
        context = browser.new_context(viewport={"width": 390, "height": 844}, is_mobile=True, has_touch=True)
        page = context.new_page()
        if mode == "loader_timeout":
            page.clock.install()
        page.add_init_script(f"({STUB})({json.dumps(mode)});")
        errors = []
        api_requests = []
        vendor_requests = []
        app_requests = []
        pending_routes = []
        page.on("pageerror", lambda error: errors.append(str(error)))

        def serve(route):
            request = route.request
            url = urlparse(request.url)
            assert url.netloc == "raven.test", "The fixture must not contact an external host"
            path = url.path
            if path == "/":
                route.fulfill(content_type="text/html", body=options["html"])
            elif path in ("/static/app.js", "/static/style.css"):
                if path == "/static/app.js":
                    app_requests.append(request.url)
                route.fulfill(content_type="text/javascript" if path.endswith(".js") else "text/css",
                              body=(SOURCE / "web/static" / Path(path).name).read_text())
            elif path.startswith((OLD_PREFIX, NEW_PREFIX)):
                vendor_requests.append(path)
                if mode == "loader_timeout" and path == NEW_PREFIX + "core/rfb.js":
                    pending_routes.append(route)
                    return
                prefix = NEW_PREFIX if path.startswith(NEW_PREFIX) else OLD_PREFIX
                relative = path[len(prefix):]
                target = vendor / relative
                if not options["baseline"] and relative == "core/util/browser.js":
                    target = SOURCE / "web/novnc/browser.js"
                assert target.is_file(), f"Missing module fixture: {relative}"
                route.fulfill(content_type="text/javascript", body=target.read_text())
            elif path == "/api/sessions/":
                api_requests.append(path)
                route.fulfill(json={"sessions": [{"id": "41", "name": "fixture-desktop", "state": "running",
                                                  "wm": "openbox", "geometry": "800x600", "command": "xterm",
                                                  "generation": "a" * 64}]})
            elif path == "/favicon.ico":
                route.fulfill(status=204)
            else:
                raise AssertionError(f"Unexpected fixture request: {path}")

        page.route("**/*", serve)
        page.goto(ORIGIN, wait_until="commit")
        page.locator("#loading-status").wait_for(state="attached")
        if mode == "loader_timeout":
            expect(page.locator("#session-count")).to_have_text("1")
            assert len(api_requests) == 1 and not vendor_requests
            with page.expect_request(ORIGIN + NEW_PREFIX + "core/rfb.js"):
                page.locator(".session-open").click()
            expect(page.locator("#connection-status")).to_have_text("연결 중…")
            assert len(pending_routes) == 1
            page.clock.fast_forward(VIEWER_TIMEOUT_MS + 1)
            expect(page.locator("#connection-status")).to_have_text("연결 실패")
            expect(page.locator("#reconnect-viewer")).to_be_visible()
            expect(page.locator("#reconnect-viewer")).to_be_enabled()
            expect(page.locator("#viewer-notice")).to_have_text("화면 준비 시간이 초과되었습니다. 다시 연결해 주세요.")
            assert len(api_requests) == 1
            page.screenshot(path=str(output / "fixed-loader-timeout.png"))
            page.locator("#close-viewer").click()
            expect(page.locator("#workspace")).to_be_visible()
            expect(page.locator("#viewer")).not_to_be_visible()
            expect(page.locator("#refresh-sessions")).to_be_enabled()
            expect(page.locator("#session-count")).to_have_text("1")
            assert len(api_requests) == 2
            assert not errors, errors
            record = {"mode": mode, "api_requests": len(api_requests), "backend_open_requests": 0,
                      "pending_module_requests": len(pending_routes), "timeout_ms": VIEWER_TIMEOUT_MS,
                      "retry_visible": True, "returned_to_list": True, "errors": errors}
        elif options["baseline"]:
            if mode in ("normal", "rejected_flush", "successful_decode"):
                expect(page.locator("#session-count")).to_have_text("1", timeout=IMPORT_TIMEOUT_MS)
            else:
                page.wait_for_timeout(BASELINE_OBSERVE_MS)
            record = {"mode": mode, "api_requests": len(api_requests),
                      "loading": page.locator("#loading-status").inner_text(), "errors": errors,
                      "decoder": page.evaluate("window.decoderProbe")}
            if mode in ("missing_api", "rejected_support", "hanging_flush", "late_support"):
                assert record["api_requests"] == 0, record
                assert record["loading"] == "세션을 불러오는 중…", record
            if mode == "hanging_flush":
                assert record["decoder"]["created"] == 1 and record["decoder"]["closed"] == 0
            page.screenshot(path=str(output / f"baseline-{mode}.png"))
        else:
            expect(page.locator("#session-count")).to_have_text("1", timeout=IMPORT_TIMEOUT_MS)
            assert len(api_requests) == 1 and not vendor_requests
            assert len(app_requests) == 1 and urlparse(app_requests[0]).query == "v=4"
            assert page.evaluate("window.decoderProbe.supportCalls === 0 && window.decoderProbe.created === 0")
            imported = page.evaluate("""async ({prefix, timeout}) => {
              const started = performance.now();
              return await Promise.race([
                import(prefix + 'core/rfb.js').then(async module => ({
                  resolved: typeof module.default === 'function',
                  support: (await import(prefix + 'core/util/browser.js')).supportsWebCodecsH264Decode,
                  elapsed: performance.now() - started,
                })),
                new Promise(resolve => setTimeout(() => resolve({resolved: false, timedOut: true}), timeout)),
              ]);
            }""", {"prefix": NEW_PREFIX, "timeout": IMPORT_TIMEOUT_MS})
            assert imported["resolved"], {"mode": mode, **imported}
            assert vendor_requests and all(path.startswith(NEW_PREFIX) for path in vendor_requests)
            if mode == "late_support":
                page.wait_for_timeout(1500)
            decoder = page.evaluate("window.decoderProbe")
            if mode not in ("normal", "successful_decode"):
                assert imported["support"] is False, {"mode": mode, **imported}
                assert decoder["closed"] == decoder["created"], {"mode": mode, **decoder}
            if mode == "successful_decode":
                assert imported["support"] is True, imported
                assert decoder["created"] == decoder["closed"] == 1, decoder
            if mode == "late_support":
                assert decoder["created"] == 0, decoder
            if mode in ("hanging_flush", "rejected_flush"):
                assert decoder["created"] == decoder["closed"] == 1, decoder
            assert not errors, errors
            record = {"mode": mode, "api_requests": len(api_requests), "import": imported,
                      "decoder": decoder, "errors": errors}
            page.screenshot(path=str(output / f"fixed-{mode}.png"))
        results.append(record)
        print(json.dumps(record, ensure_ascii=False), flush=True)
        context.close()
    browser.close()

report = {"baseline": options["baseline"], "fixture": "synthetic WebCodecs API states", "results": results}
(output / "module-loading.json").write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n")
