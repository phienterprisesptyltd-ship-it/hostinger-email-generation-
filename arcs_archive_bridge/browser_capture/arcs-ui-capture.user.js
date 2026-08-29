// ==UserScript==
// @name         ARCS UI-assisted capture (read-only)
// @namespace    arcs.archive.bridge
// @version      1.0.0
// @description  Save a provenance-stamped JSON bundle of your own ChatGPT conversations, using the session your browser already has. Reads nothing else.
// @match        https://chatgpt.com/*
// @match        https://chat.openai.com/*
// @grant        none
// @run-at       document-idle
// ==/UserScript==

/*
 * WHAT THIS SCRIPT DOES, AND WHAT IT REFUSES TO DO
 * ------------------------------------------------
 * Does:
 *   - runs inside your normal, already-signed-in browser session;
 *   - issues GET requests only, to the same origin you are already on;
 *   - lets the browser attach your session the way it does for any page
 *     request, so the archive never has to hold a credential;
 *   - falls back to reading the rendered conversation out of the DOM when the
 *     backend JSON is not available to an unprivileged page script;
 *   - writes one JSON file to your Downloads folder and stops.
 *
 * Refuses:
 *   - it never reads the page's cookie jar. The cookie accessor does not appear
 *     anywhere in this file, and the ARCS test suite asserts that it does not.
 *   - it never requests, reads or stores an access token. It does NOT call
 *     /api/auth/session. That is why backend JSON may be unavailable: getting
 *     it would mean handling the token, and a capture that needs a token is
 *     not a capture this archive wants.
 *   - it never sends anything anywhere. There is no upload, no telemetry, no
 *     third-party request. The only output is a file on your own disk.
 *   - it never writes to ChatGPT: no POST, PUT, PATCH or DELETE, no title
 *     edits, no deletions, no archiving. ChatGPT is treated as read-only.
 *
 * The long-term path is the OpenAI Enterprise Compliance API, which returns the
 * same material with better provenance and no browser involvement at all. The
 * archive's `compliance_api` adapter already reads that format; this script
 * exists so the work can start before that access is arranged.
 */

(function () {
  "use strict";

  const SCRIPT_VERSION = "1.0.0";
  const BUNDLE_VERSION = 1;
  const REQUEST_DELAY_MS = 900;      // be a polite guest on someone else's server
  const MAX_CONVERSATIONS = 200;

  // Keys that must never appear in a saved bundle, whatever the source.
  const FORBIDDEN_KEYS = new Set([
    "cookie", "cookies", "authorization", "access_token", "id_token",
    "refresh_token", "session_token", "sessiontoken", "accesstoken",
    "bearer", "auth_token", "api_key", "apikey", "client_secret",
  ]);

  const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

  /** Recursively drop anything credential-shaped before it can be written. */
  function scrub(value, dropped) {
    if (Array.isArray(value)) {
      return value.map((item) => scrub(item, dropped));
    }
    if (value && typeof value === "object") {
      const out = {};
      for (const [key, child] of Object.entries(value)) {
        if (FORBIDDEN_KEYS.has(String(key).toLowerCase())) {
          dropped.push(key);
          continue;
        }
        out[key] = scrub(child, dropped);
      }
      return out;
    }
    return value;
  }

  /** GET, same-origin, with the browser's own session. No headers of ours. */
  async function getJson(path) {
    const response = await fetch(path, {
      method: "GET",
      credentials: "same-origin",
      redirect: "follow",
    });
    if (!response.ok) {
      throw new Error("GET " + path + " -> " + response.status);
    }
    return response.json();
  }

  function currentConversationId() {
    const match = location.pathname.match(/\/c\/([0-9a-zA-Z-]+)/);
    return match ? match[1] : null;
  }

  /**
   * Read the rendered conversation out of the page.
   * Lower fidelity than the backend JSON - markdown has already been rendered
   * and metadata is gone - so the bundle records that, and the archive marks
   * these captures `fidelity: dom_rendered` rather than pretending otherwise.
   */
  function scrapeDom() {
    const nodes = document.querySelectorAll("[data-message-author-role]");
    const messages = [];
    nodes.forEach((node, index) => {
      const text = (node.innerText || "").trim();
      if (!text) return;
      messages.push({
        order: index + 1,
        role: node.getAttribute("data-message-author-role") || "unknown",
        message_id: node.getAttribute("data-message-id") || null,
        text: text,
        captured_from: "dom",
      });
    });
    return messages;
  }

  function titleFromDom() {
    const heading = document.querySelector("h1, [data-testid='conversation-title']");
    if (heading && heading.innerText.trim()) return heading.innerText.trim();
    return (document.title || "").replace(/\s*[|-]\s*ChatGPT\s*$/i, "").trim() || null;
  }

  async function captureOne(conversationId, warnings, dropped) {
    const entry = {
      conversation_id: conversationId,
      title: titleFromDom(),
      url: location.origin + "/c/" + conversationId,
      captured_at: new Date().toISOString(),
      capture_method: "dom_scrape",
      payload: null,
      messages: [],
    };
    try {
      // Preferred: the provider's own JSON for this conversation. This will
      // fail with 401 in most sessions precisely because we refuse to read the
      // access token; that is the intended trade-off, not a bug.
      const payload = await getJson("/backend-api/conversation/" + conversationId);
      entry.payload = scrub(payload, dropped);
      entry.capture_method = "backend_json";
      entry.title = entry.title || payload.title || null;
    } catch (error) {
      warnings.push(
        "backend JSON unavailable for " + conversationId + " (" + error.message +
        "); fell back to reading the rendered page"
      );
      entry.messages = scrapeDom();
      entry.notes = "DOM capture: markdown already rendered, message metadata not available.";
    }
    return entry;
  }

  async function listConversationIds(limit) {
    // Optional: the conversation list endpoint, when the page is allowed it.
    const ids = [];
    try {
      const page = await getJson("/backend-api/conversations?offset=0&limit=" + limit +
                                 "&order=updated");
      for (const item of page.items || []) {
        if (item && item.id) ids.push(item.id);
      }
    } catch (error) {
      /* Expected without a token. The caller falls back to the open tab. */
    }
    return ids;
  }

  function download(bundle) {
    const text = JSON.stringify(bundle, null, 2) + "\n";
    const blob = new Blob([text], { type: "application/json" });
    const url = URL.createObjectURL(blob);
    const link = document.createElement("a");
    link.href = url;
    link.download = "arcs-capture-" + new Date().toISOString().replace(/[:.]/g, "-") + ".json";
    document.body.appendChild(link);
    link.click();
    link.remove();
    setTimeout(() => URL.revokeObjectURL(url), 2000);
    return text.length;
  }

  async function run() {
    const warnings = [];
    const dropped = [];
    const conversations = [];

    const here = currentConversationId();
    let ids = await listConversationIds(MAX_CONVERSATIONS);
    if (ids.length === 0 && here) {
      ids = [here];
      warnings.push("conversation list unavailable; captured only the open conversation");
    }
    if (ids.length === 0) {
      alert("ARCS capture: open a conversation first, then run the script again.");
      return;
    }
    if (!confirm(
      "ARCS capture\n\n" + ids.length + " conversation(s) will be read and saved to a " +
      "file on this computer.\n\nNothing is uploaded. No cookie or token is read. " +
      "Nothing in ChatGPT is changed.\n\nContinue?"
    )) {
      return;
    }

    for (const id of ids) {
      conversations.push(await captureOne(id, warnings, dropped));
      if (ids.length > 1) await sleep(REQUEST_DELAY_MS);
    }

    if (dropped.length) {
      warnings.push("removed " + dropped.length + " credential-shaped field(s) before saving");
    }

    const bundle = {
      arcs_capture_version: BUNDLE_VERSION,
      capture_method: "ui_assisted_readonly",
      script_version: SCRIPT_VERSION,
      source_system: "chatgpt",
      captured_at: new Date().toISOString(),
      origin: location.origin,
      note:
        "Captured in a normally signed-in browser. GET requests only; no cookie, " +
        "token or Authorization header was read, copied or stored. ChatGPT was not " +
        "modified.",
      warnings: warnings,
      conversations: conversations,
    };

    const size = download(bundle);
    alert(
      "ARCS capture saved: " + conversations.length + " conversation(s), " +
      Math.round(size / 1024) + " KB.\n\nIngest it with:\n  arcs ingest <the downloaded file>"
    );
  }

  // Expose a manual trigger rather than firing on page load: capture is
  // something the researcher does deliberately, not something that happens.
  window.arcsCapture = run;
  console.info(
    "%cARCS capture ready%c - run arcsCapture() in this console to save a bundle.",
    "font-weight:bold", "font-weight:normal"
  );
})();
