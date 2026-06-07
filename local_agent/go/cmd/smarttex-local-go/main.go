package main

import (
	"archive/zip"
	"bufio"
	"bytes"
	"context"
	"crypto/rand"
	"crypto/sha256"
	"encoding/base64"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"io"
	"mime/multipart"
	"net"
	"net/http"
	"net/textproto"
	"net/url"
	"os"
	"os/exec"
	"os/signal"
	"path/filepath"
	"runtime"
	"strconv"
	"strings"
	"sync"
	"syscall"
	"time"

	"github.com/gorilla/websocket"
)

const toolVersion = "0.3.1-local-preview-root"

const maxUploadedCompileLogBytes = 1536 * 1024

const localPreviewBridgeScript = `
<script>
(() => {
  if (window.__smarttexPreviewBridgeInstalled) return;
  window.__smarttexPreviewBridgeInstalled = true;
  const PREVIEW_PROJECT_ID = "__SMARTTEX_PREVIEW_PROJECT_ID__";
  const PREVIEW_ROOT_URI = "__SMARTTEX_PREVIEW_ROOT_URI__";
  const HIGHLIGHT_CLASS = "smarttex-preview-sync-highlight";
  const ANNOTATE_BUTTON_ID = "smarttex-preview-annotate-button";
  const CONTEXT_MENU_ID = "smarttex-preview-context-menu";
  const NativeWebSocket = window.WebSocket;
  function parentOrigin() {
    try {
      if (document.referrer) return new URL(document.referrer).origin;
    } catch (_) {}
    return "*";
  }
  const PARENT_ORIGIN = parentOrigin();

  window.WebSocket = function(url, protocols) {
    try {
      const resolved = new URL(String(url || "/"), window.location.href);
      if (resolved.pathname === "/" || resolved.pathname === "") {
        resolved.pathname = "/ws/typst-preview/";
      }
      if (resolved.pathname === "/ws/typst-preview/" && !resolved.searchParams.has("project_id")) {
        resolved.searchParams.set("project_id", PREVIEW_PROJECT_ID);
      }
      const secret = new URL(window.location.href).searchParams.get("secret");
      const theme = new URL(window.location.href).searchParams.get("theme")
        || new URL(window.location.href).searchParams.get("preview_theme")
        || new URL(window.location.href).searchParams.get("invert_colors");
      if (secret && !resolved.searchParams.has("secret")) resolved.searchParams.set("secret", secret);
      if (theme && !resolved.searchParams.has("theme")) resolved.searchParams.set("theme", theme);
      url = resolved.toString();
    } catch (_) {}
    return protocols !== undefined ? new NativeWebSocket(url, protocols) : new NativeWebSocket(url);
  };
  window.WebSocket.prototype = NativeWebSocket.prototype;
  Object.setPrototypeOf(window.WebSocket, NativeWebSocket);

  function normalize(text) {
    return String(text || "")
      .replace(/\s+/g, " ")
      .replace(/[“”«»"]/g, '"')
      .replace(/[’']/g, "'")
      .trim()
      .toLowerCase();
  }

  function ensureStyle() {
    if (document.getElementById("smarttex-preview-sync-style")) return;
    const style = document.createElement("style");
    style.id = "smarttex-preview-sync-style";
    style.textContent =
      "." + HIGHLIGHT_CLASS + "{outline:2px solid rgba(59,130,246,.9)!important;outline-offset:4px!important;border-radius:6px!important;}" +
      "#" + ANNOTATE_BUTTON_ID + "{position:fixed;z-index:2147483647;display:none;align-items:center;gap:7px;padding:8px 11px;border:1px solid rgba(34,197,94,.45);border-radius:999px;background:linear-gradient(135deg,rgba(20,184,166,.96),rgba(34,197,94,.96));color:#04130a;box-shadow:0 12px 32px rgba(0,0,0,.26),0 0 0 1px rgba(255,255,255,.14) inset;font:700 13px/1.1 ui-sans-serif,system-ui,-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;letter-spacing:.01em;cursor:pointer;user-select:none;backdrop-filter:blur(10px);}" +
      "#" + ANNOTATE_BUTTON_ID + ":hover{filter:brightness(1.04);transform:translateY(-1px);}" +
      "#" + ANNOTATE_BUTTON_ID + " svg{width:15px;height:15px;flex:0 0 auto;}" +
      "#" + CONTEXT_MENU_ID + "{position:fixed;z-index:2147483647;display:none;min-width:188px;padding:7px;border:1px solid rgba(148,163,184,.22);border-radius:13px;background:linear-gradient(145deg,rgba(31,41,55,.98),rgba(15,23,42,.98));color:#e5e7eb;box-shadow:0 18px 48px rgba(0,0,0,.38);font:700 13px/1.2 ui-sans-serif,system-ui,-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;backdrop-filter:blur(12px);}" +
      "#" + CONTEXT_MENU_ID + " button{display:flex;align-items:center;gap:9px;width:100%;padding:10px 11px;border:0;border-radius:10px;background:transparent;color:inherit;font:inherit;text-align:left;cursor:pointer;}" +
      "#" + CONTEXT_MENU_ID + " button:hover{background:rgba(34,197,94,.18);color:#bbf7d0;}" +
      "#" + CONTEXT_MENU_ID + " svg{width:16px;height:16px;color:#22c55e;}";
    document.head.appendChild(style);
  }

  function findTextElement(targets) {
    const wanted = targets.map(item => ({...item, norm: normalize(item.value)})).filter(item => item.norm.length >= 3);
    if (!wanted.length) return null;
    const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT, {
      acceptNode(node) {
        return normalize(node.nodeValue || "").length >= 3 ? NodeFilter.FILTER_ACCEPT : NodeFilter.FILTER_REJECT;
      }
    });
    let best = null;
    let node;
    while ((node = walker.nextNode())) {
      const hay = normalize(node.nodeValue || "");
      let score = 0;
      for (const target of wanted) {
        if (hay === target.norm) score = Math.max(score, target.weight + 60);
        else if (hay.includes(target.norm)) score = Math.max(score, target.weight + 25);
        else if (target.norm.includes(hay) && hay.length >= 8) score = Math.max(score, target.weight + 10);
      }
      if (!score) continue;
      const el = node.parentElement?.closest("svg text, h1, h2, h3, h4, h5, h6, p, div, span, li, td, th");
      if (el && (!best || score > best.score)) best = {el, score};
    }
    return best?.el || null;
  }

  let highlightTimer = null;
  function revealElement(el) {
    if (!el) return false;
    ensureStyle();
    el.scrollIntoView({block: "center", inline: "nearest", behavior: "smooth"});
    document.querySelectorAll("." + HIGHLIGHT_CLASS).forEach(node => node.classList.remove(HIGHLIGHT_CLASS));
    el.classList.add(HIGHLIGHT_CLASS);
    clearTimeout(highlightTimer);
    highlightTimer = setTimeout(() => el.classList.remove(HIGHLIGHT_CLASS), 1600);
    return true;
  }

  function bestClickableText(event) {
    const path = event.composedPath ? event.composedPath() : [];
    for (const item of path) {
      if (!(item instanceof Element)) continue;
      const text = normalize(item.innerText || item.textContent || "");
      if (text.length >= 3) return text.slice(0, 220);
    }
    return "";
  }

  function isVisualOnlyTarget(event) {
    const path = event.composedPath ? event.composedPath() : [];
    for (const item of path) {
      if (!(item instanceof Element)) continue;
      const tag = String(item.tagName || "").toLowerCase();
      const text = normalize(item.innerText || item.textContent || "");
      if (text.length >= 3) return false;
      if (["img", "image", "canvas", "figure", "picture"].includes(tag)) return true;
      if (["path", "rect", "circle", "ellipse", "line", "polyline", "polygon"].includes(tag)) return true;
      if (item.closest?.("img, image, canvas, figure, picture")) return true;
    }
    return false;
  }

  function isTextNavigationTarget(event) {
    const path = event.composedPath ? event.composedPath() : [];
    for (const item of path) {
      if (!(item instanceof Element)) continue;
      const tag = String(item.tagName || "").toLowerCase();
      if (["h1", "h2", "h3", "h4", "h5", "h6", "p", "li", "span", "code", "text"].includes(tag)) return true;
      if (tag === "div") {
        const text = normalize(item.textContent || "");
        if (text.length >= 3 && text.length <= 240) return true;
      }
    }
    return false;
  }

  function parseSourceLocation(value) {
    const raw = String(value || "").trim();
    if (!raw) return null;
    const direct = raw.match(/([^\s]+\.typ):(\d+)(?::(\d+))?/);
    if (direct) return {filename: direct[1].replace(/^file:\/\//, ""), line: Number(direct[2] || 1), column: Number(direct[3] || 1)};
    return null;
  }

  function extractSourceLocation(event) {
    const path = event.composedPath ? event.composedPath() : [];
    for (const item of path) {
      if (!(item instanceof Element)) continue;
      for (const name of item.getAttributeNames ? item.getAttributeNames() : []) {
        const loc = parseSourceLocation(item.getAttribute(name));
        if (loc) return loc;
      }
      for (const value of Object.values(item.dataset || {})) {
        const loc = parseSourceLocation(value);
        if (loc) return loc;
      }
    }
    return null;
  }

  function nearestHeadingFromNode(node) {
    let current = node?.nodeType === Node.ELEMENT_NODE ? node : node?.parentElement;
    while (current && current !== document.body) {
      let probe = current;
      while (probe?.previousElementSibling) {
        probe = probe.previousElementSibling;
        if (/^(H[1-6]|text)$/i.test(probe.tagName || "")) {
          const text = normalize(probe.textContent || "");
          if (text.length >= 3) return text.slice(0, 220);
        }
      }
      current = current.parentElement;
    }
    return "";
  }

  let lastSelectionPayload = null;
  let annotationRequestSeq = 0;
  const pendingAnnotationRequests = new Map();
  function ensureAnnotateButton() {
    ensureStyle();
    let button = document.getElementById(ANNOTATE_BUTTON_ID);
    if (button) return button;
    button = document.createElement("button");
    button.id = ANNOTATE_BUTTON_ID;
    button.type = "button";
    button.innerHTML = "<svg viewBox=\"0 0 24 24\" fill=\"none\" aria-hidden=\"true\"><path d=\"M7 8h10M7 12h7M20 11.5c0 4.14-3.58 7.5-8 7.5a8.7 8.7 0 0 1-3.33-.66L4 20l1.2-4.15A7.18 7.18 0 0 1 4 11.5C4 7.36 7.58 4 12 4s8 3.36 8 7.5Z\" stroke=\"currentColor\" stroke-width=\"2\" stroke-linecap=\"round\" stroke-linejoin=\"round\"/></svg><span>Помітка</span>";
    button.addEventListener("mousedown", event => event.preventDefault());
    button.addEventListener("click", event => {
      event.preventDefault();
      event.stopPropagation();
      if (!lastSelectionPayload?.text) return;
      sendAnnotationRequest(lastSelectionPayload);
    });
    document.body.appendChild(button);
    return button;
  }

  function hideAnnotateButton() {
    const button = document.getElementById(ANNOTATE_BUTTON_ID);
    if (button) button.style.display = "none";
  }

  function hideContextMenu() {
    const menu = document.getElementById(CONTEXT_MENU_ID);
    if (menu) menu.style.display = "none";
  }

  function sendAnnotationRequest(payload) {
    if (!payload?.text) return;
    hideAnnotateButton();
    hideContextMenu();
    if (window.parent === window) {
      window.alert("Помітки з превʼю створюються з редактора SmartTeX. Відкрийте це превʼю всередині редактора.");
      return;
    }
    const requestId = "preview-annotation-" + Date.now() + "-" + (++annotationRequestSeq);
    const timer = setTimeout(() => {
      if (!pendingAnnotationRequests.has(requestId)) return;
      pendingAnnotationRequests.delete(requestId);
      window.alert("Редактор не відповів на запит помітки. Переконайтесь, що превʼю відкрите всередині SmartTeX і перезавантажте preview.");
    }, 1800);
    pendingAnnotationRequests.set(requestId, timer);
    window.parent.postMessage({type: "smarttex-preview-annotation-request", requestId, payload}, PARENT_ORIGIN);
  }

  function updateSelectionAnnotationButton() {
    const selection = window.getSelection?.();
    const selectedText = String(selection?.toString?.() || "").replace(/\s+/g, " ").trim();
    if (!selection || selection.rangeCount === 0 || selectedText.length < 3) {
      lastSelectionPayload = null;
      hideAnnotateButton();
      return;
    }
    const range = selection.getRangeAt(0);
    const rects = Array.from(range.getClientRects ? range.getClientRects() : []).filter(rect => rect.width > 1 && rect.height > 1);
    const rect = rects[0] || range.getBoundingClientRect?.();
    if (!rect || !rect.width || !rect.height) {
      hideAnnotateButton();
      return;
    }
    const button = ensureAnnotateButton();
    const buttonWidth = 98;
    const left = Math.max(10, Math.min(rect.left + rect.width / 2 - buttonWidth / 2, window.innerWidth - buttonWidth - 10));
    const top = Math.max(10, rect.top - 44);
    lastSelectionPayload = {
      text: selectedText.slice(0, 2000),
      heading: nearestHeadingFromNode(range.commonAncestorContainer),
      rect: {left: rect.left, top: rect.top, right: rect.right, bottom: rect.bottom, width: rect.width, height: rect.height},
    };
    button.style.left = Math.round(left) + "px";
    button.style.top = Math.round(top) + "px";
    button.style.display = "inline-flex";
  }

  function payloadFromContextEvent(event) {
    const selection = window.getSelection?.();
    const selectedText = String(selection?.toString?.() || "").replace(/\s+/g, " ").trim();
    if (selection && selection.rangeCount > 0 && selectedText.length >= 3) {
      const range = selection.getRangeAt(0);
      const rect = range.getBoundingClientRect?.();
      return {
        text: selectedText.slice(0, 2000),
        heading: nearestHeadingFromNode(range.commonAncestorContainer),
        rect: rect ? {left: rect.left, top: rect.top, right: rect.right, bottom: rect.bottom, width: rect.width, height: rect.height} : {left: event.clientX, top: event.clientY, right: event.clientX, bottom: event.clientY, width: 1, height: 1},
      };
    }
    const text = bestClickableText(event);
    if (text.length < 3) return null;
    return {
      text,
      heading: "",
      rect: {left: event.clientX, top: event.clientY, right: event.clientX, bottom: event.clientY, width: 1, height: 1},
    };
  }

  function ensureContextMenu() {
    ensureStyle();
    let menu = document.getElementById(CONTEXT_MENU_ID);
    if (menu) return menu;
    menu = document.createElement("div");
    menu.id = CONTEXT_MENU_ID;
    menu.innerHTML = "<button type=\"button\" data-action=\"annotate\"><svg viewBox=\"0 0 24 24\" fill=\"none\" aria-hidden=\"true\"><path d=\"M7 8h10M7 12h7M20 11.5c0 4.14-3.58 7.5-8 7.5a8.7 8.7 0 0 1-3.33-.66L4 20l1.2-4.15A7.18 7.18 0 0 1 4 11.5C4 7.36 7.58 4 12 4s8 3.36 8 7.5Z\" stroke=\"currentColor\" stroke-width=\"2\" stroke-linecap=\"round\" stroke-linejoin=\"round\"/></svg><span>Додати помітку</span></button>";
    menu.addEventListener("mousedown", event => event.preventDefault());
    menu.addEventListener("click", event => {
      event.preventDefault();
      event.stopPropagation();
      if (event.target?.closest?.("[data-action='annotate']")) sendAnnotationRequest(lastSelectionPayload);
    });
    document.body.appendChild(menu);
    return menu;
  }

  function showContextMenu(event, payload) {
    lastSelectionPayload = payload;
    const menu = ensureContextMenu();
    const width = 204;
    const height = 54;
    const left = Math.max(8, Math.min(event.clientX, window.innerWidth - width - 8));
    const top = Math.max(8, Math.min(event.clientY, window.innerHeight - height - 8));
    menu.style.left = Math.round(left) + "px";
    menu.style.top = Math.round(top) + "px";
    menu.style.display = "block";
  }

  window.addEventListener("message", event => {
    if (PARENT_ORIGIN !== "*" && event.origin !== PARENT_ORIGIN && event.origin !== window.location.origin) return;
    const data = event.data || {};
    if (data?.type === "smarttex-preview-annotation-response") {
      const requestId = String(data.requestId || "");
      const timer = pendingAnnotationRequests.get(requestId);
      if (timer) clearTimeout(timer);
      pendingAnnotationRequests.delete(requestId);
      if (data.status === "failed" && data.message) window.alert(String(data.message));
      return;
    }
    function primaryScroller() {
      const root = document.scrollingElement || document.documentElement || document.body;
      let best = root;
      let bestDelta = Math.max(0, root.scrollHeight - root.clientHeight);
      for (const el of Array.from(document.querySelectorAll("body *"))) {
        const deltaY = Math.max(0, el.scrollHeight - el.clientHeight);
        const deltaX = Math.max(0, el.scrollWidth - el.clientWidth);
        if ((deltaY > bestDelta || deltaX > 200) && el.clientHeight > 120) {
          best = el;
          bestDelta = deltaY;
        }
      }
      return best;
    }
    if (data?.type === "smarttex-preview-capture-scroll") {
      const scroller = primaryScroller();
      window.parent.postMessage({
        type: "smarttex-preview-scroll-state",
        key: data.key || "",
        x: scroller.scrollLeft || window.scrollX || 0,
        y: scroller.scrollTop || window.scrollY || 0,
      }, PARENT_ORIGIN);
      return;
    }
    if (data?.type === "smarttex-preview-restore-scroll") {
      const x = Number(data.x || 0);
      const y = Number(data.y || 0);
      const apply = () => {
        const scroller = primaryScroller();
        scroller.scrollLeft = x;
        scroller.scrollTop = y;
        window.scrollTo(x, y);
      };
      requestAnimationFrame(apply);
      setTimeout(apply, 80);
      return;
    }
    if (data?.type !== "smarttex-preview-reveal") return;
    const payload = data.payload || {};
    revealElement(findTextElement([
      {value: payload.heading, weight: 100},
      {value: payload.lineText, weight: 70},
      {value: payload.excerpt, weight: 40},
    ]));
  });

  document.addEventListener("click", event => {
    if (event.target?.closest?.("#" + ANNOTATE_BUTTON_ID)) return;
    hideContextMenu();
    if (isVisualOnlyTarget(event) || !isTextNavigationTarget(event)) return;
    const location = extractSourceLocation(event);
    const text = bestClickableText(event);
    if (!location && !text) return;
    window.parent.postMessage({type: "smarttex-preview-click", payload: {text, location}}, PARENT_ORIGIN);
  }, false);

  document.addEventListener("contextmenu", event => {
    const payload = payloadFromContextEvent(event);
    if (!payload) return;
    event.preventDefault();
    event.stopPropagation();
    showContextMenu(event, payload);
  }, true);

  document.addEventListener("mouseup", () => setTimeout(updateSelectionAnnotationButton, 0), true);
  document.addEventListener("keyup", event => {
    if (event.key === "Escape") {
      hideAnnotateButton();
      hideContextMenu();
    }
    else setTimeout(updateSelectionAnnotationButton, 0);
  }, true);
  document.addEventListener("selectionchange", () => setTimeout(updateSelectionAnnotationButton, 80));

  window.parent.postMessage({type: "smarttex-preview-ready", rootUri: PREVIEW_ROOT_URI}, PARENT_ORIGIN);
})();
</script>
`

type projectMeta struct {
	ID           int    `json:"id"`
	Title        string `json:"title"`
	MarkupType   string `json:"markup_type"`
	MainFileName string `json:"main_file_name"`
}

type compileResult struct {
	Status     string
	PDF        []byte
	Log        string
	ReturnCode int
}

type config struct {
	Server    string
	Token     string
	ProjectID int
	JobID     int
	Workspace string
	TypstBin  string
	Timeout   time.Duration
	Pull      bool
}

type serveConfig struct {
	Context     context.Context
	Server      string
	Auth        *tokenSource
	Workspace   string
	TypstBin    string
	TinymistBin string
	Timeout     time.Duration
	Listen      string
	Secret      string
}

type localConfig struct {
	Server           string `json:"server"`
	AccessToken      string `json:"access_token"`
	RefreshToken     string `json:"refresh_token"`
	ClientID         string `json:"client_id"`
	RedirectURI      string `json:"redirect_uri"`
	BridgeSecret     string `json:"bridge_secret"`
	TypstBin         string `json:"typst_bin"`
	TinymistBin      string `json:"tinymist_bin"`
	ToolchainChannel string `json:"toolchain_channel"`
	ExpiresAt        int64  `json:"expires_at"`
}

type tokenSource struct {
	server        string
	explicitToken string
	mu            sync.Mutex
}

type previewSession struct {
	ProjectID    int
	Root         string
	Port         int
	ControlPort  int
	InvertColors string
	Process      *os.Process
	StartedAt    time.Time
}

type previewReadyWatcher struct {
	dataPort     int
	controlPort  int
	dataReady    chan struct{}
	controlReady chan struct{}
	dataOnce     sync.Once
	controlOnce  sync.Once
	mu           sync.Mutex
	lines        []string
}

type lspProcess struct {
	cmd     *exec.Cmd
	stdin   io.WriteCloser
	stdout  *bufio.Reader
	writeMu sync.Mutex
}

var previewSessionsMu sync.Mutex
var previewSessions = map[int]*previewSession{}
var workspaceLocksMu sync.Mutex
var workspaceLocks = map[string]*sync.Mutex{}

type claimedJob struct {
	ID        int            `json:"id"`
	ProjectID int            `json:"project_id"`
	MainFile  string         `json:"main_file"`
	Request   map[string]any `json:"request"`
}

type updateManifest struct {
	Version         string                `json:"version"`
	Channel         string                `json:"channel"`
	Assets          []updateAsset         `json:"assets"`
	VSCodeExtension *vscodeExtensionAsset `json:"vscode_extension"`
	Toolchains      []toolchainAsset      `json:"toolchains"`
}

type updateAsset struct {
	OS     string `json:"os"`
	Arch   string `json:"arch"`
	URL    string `json:"url"`
	SHA256 string `json:"sha256"`
}

type vscodeExtensionAsset struct {
	ID        string `json:"id"`
	Name      string `json:"name"`
	Publisher string `json:"publisher"`
	Version   string `json:"version"`
	URL       string `json:"url"`
	SHA256    string `json:"sha256"`
}

type toolchainAsset struct {
	Tool       string `json:"tool"`
	Version    string `json:"version"`
	OS         string `json:"os"`
	Arch       string `json:"arch"`
	URL        string `json:"url"`
	SHA256     string `json:"sha256"`
	Executable string `json:"executable"`
}

type workspaceState struct {
	ProjectID          int               `json:"project_id"`
	Server             string            `json:"server"`
	WorkspaceID        string            `json:"workspace_id"`
	BaseVersionNumber  int               `json:"base_version_number"`
	LastSyncUnixMillis int64             `json:"last_sync_unix_millis"`
	Files              map[string]string `json:"files"`
}

type workspaceFileSnapshot struct {
	Path     string
	Hash     string
	IsText   bool
	Content  string
	RawBytes []byte
}

func main() {
	if err := run(os.Args[1:]); err != nil {
		fmt.Fprintf(os.Stderr, "smarttex-local-go: %v\n", err)
		os.Exit(1)
	}
}

func run(args []string) error {
	if len(args) == 0 || args[0] == "-h" || args[0] == "--help" {
		printRootHelp()
		return nil
	}
	switch args[0] {
	case "login":
		return runLogin(args[1:])
	case "projects":
		return runProjects(args[1:])
	case "compile":
		return runCompile(args[1:])
	case "workspace":
		return runWorkspace(args[1:])
	case "annotations":
		return runAnnotations(args[1:])
	case "proposals":
		return runProposals(args[1:])
	case "versions":
		return runVersions(args[1:])
	case "pdf-embed":
		return runPdfEmbed(args[1:])
	case "serve":
		return runServe(args[1:])
	case "doctor":
		return runDoctor(args[1:])
	case "update":
		return runUpdate(args[1:])
	case "toolchain":
		return runToolchain(args[1:])
	case "version":
		fmt.Println(toolVersion)
		return nil
	default:
		return fmt.Errorf("unknown command %q", args[0])
	}
}

func printRootHelp() {
	fmt.Println(`SmartTeX local runtime

Usage:
  smarttex-local-go login [--server URL] [--serve]
  smarttex-local-go projects [--server URL] [--token TOKEN]
  smarttex-local-go compile --project ID [--server URL] [--token TOKEN]
  smarttex-local-go workspace open|pull|sync|watch|status|release --project ID [--server URL] [--token TOKEN]
  smarttex-local-go annotations list|add|update --project ID [--server URL] [--token TOKEN]
  smarttex-local-go proposals status|diff|edit-line|accept|discard --project ID [--server URL] [--token TOKEN]
  smarttex-local-go versions list|detail|rollback --project ID [--server URL] [--token TOKEN]
  smarttex-local-go pdf-embed list|set --project ID [--server URL] [--token TOKEN]
  smarttex-local-go serve [--server URL] [--token TOKEN] [--secret SECRET] [--tinymist-bin PATH]
  smarttex-local-go doctor [--server URL] [--token TOKEN]
  smarttex-local-go update [--server URL] [--install-path PATH]
  smarttex-local-go toolchain status|install [--server URL] [--channel stable]
  smarttex-local-go version

Environment:
  SMARTTEX_SERVER defaults to https://smart-tex.pp.ua
  SMARTTEX_TOKEN  overrides the saved OAuth token when set
  SMARTTEX_LOCAL_SECRET optionally overrides the saved localhost bridge secret
  TINYMIST_BIN points local preview to a host tinymist binary`)
}

func baseFlags(name string) (*flag.FlagSet, *string, *string) {
	fs := flag.NewFlagSet(name, flag.ExitOnError)
	server := fs.String("server", defaultServer(), "SmartTeX server URL")
	token := fs.String("token", os.Getenv("SMARTTEX_TOKEN"), "OAuth access token override")
	return fs, server, token
}

func runLogin(args []string) error {
	fs := flag.NewFlagSet("login", flag.ExitOnError)
	server := fs.String("server", defaultServer(), "SmartTeX server URL")
	listen := fs.String("callback-listen", "127.0.0.1:0", "OAuth callback listen address")
	startServe := fs.Bool("serve", false, "start the local bridge immediately after login")
	bridgeListen := fs.String("bridge-listen", envOr("SMARTTEX_LOCAL_LISTEN", "127.0.0.1:8765"), "local bridge listen address used with --serve")
	if err := fs.Parse(args); err != nil {
		return err
	}
	state := randomURLSafe(24)
	redirectURI, code, err := receiveOAuthCode(*listen, state)
	if err != nil {
		return err
	}
	clientID, verifier, err := registerOAuthClientAndOpenBrowser(*server, redirectURI, state)
	if err != nil {
		return err
	}
	authCode := <-code
	token, refreshToken, expiresAt, err := exchangeOAuthCode(*server, clientID, redirectURI, verifier, authCode)
	if err != nil {
		return err
	}
	existingCfg, _ := loadLocalConfig()
	cfg := localConfig{
		Server:           *server,
		AccessToken:      token,
		RefreshToken:     refreshToken,
		ClientID:         clientID,
		RedirectURI:      redirectURI,
		BridgeSecret:     bridgeSecretFromConfig(existingCfg),
		TypstBin:         existingCfg.TypstBin,
		TinymistBin:      existingCfg.TinymistBin,
		ToolchainChannel: existingCfg.ToolchainChannel,
		ExpiresAt:        expiresAt,
	}
	if err := saveLocalConfig(cfg); err != nil {
		return err
	}
	fmt.Println("Login complete. Token saved to", configPath())
	fmt.Println("Local bridge is ready to use with:")
	fmt.Printf("  Agent URL: http://%s\n", *bridgeListen)
	fmt.Printf("  Bridge secret: %s\n", cfg.BridgeSecret)
	if *startServe {
		fmt.Println("Starting local bridge...")
		return runServe([]string{"--server", *server, "--listen", *bridgeListen})
	}
	return nil
}

func runProjects(args []string) error {
	fs, server, token := baseFlags("projects")
	if err := fs.Parse(args); err != nil {
		return err
	}
	resolvedToken, err := resolveToken(*server, *token)
	if err != nil {
		return err
	}
	raw, err := apiRequest("GET", *server, "/api/projects/", resolvedToken, nil, "")
	if err != nil {
		return err
	}
	var items []projectMeta
	if err := json.Unmarshal(raw, &items); err != nil {
		var wrapped struct {
			Projects []projectMeta `json:"projects"`
		}
		if err2 := json.Unmarshal(raw, &wrapped); err2 != nil {
			return fmt.Errorf("server returned unexpected projects payload: %w", err)
		}
		items = wrapped.Projects
	}
	if len(items) == 0 {
		fmt.Println("No projects available for this token/server.")
		return nil
	}
	for _, item := range items {
		fmt.Printf("%d\t%s\t%s\t%s\n", item.ID, item.MarkupType, item.MainFileName, item.Title)
	}
	return nil
}

func runCompile(args []string) error {
	fs, server, token := baseFlags("compile")
	projectID := fs.Int("project", 0, "SmartTeX project id")
	workspace := fs.String("workspace", envOr("SMARTTEX_LOCAL_WORKSPACE", "~/.smarttex-local"), "local workspace root")
	typstBin := fs.String("typst-bin", defaultTypstBin(), "typst executable")
	timeout := fs.Int("timeout", 60, "compile timeout seconds")
	noPull := fs.Bool("no-pull", false, "reuse existing local workspace")
	if err := fs.Parse(args); err != nil {
		return err
	}
	cfg := config{
		Server:    *server,
		ProjectID: *projectID,
		Workspace: *workspace,
		TypstBin:  *typstBin,
		Timeout:   time.Duration(*timeout) * time.Second,
		Pull:      !*noPull,
	}
	if cfg.ProjectID <= 0 {
		return errors.New("--project is required")
	}
	resolvedToken, err := resolveToken(cfg.Server, *token)
	if err != nil {
		return err
	}
	cfg.Token = resolvedToken

	root, err := workspaceRoot(cfg.Workspace, cfg.ProjectID)
	if err != nil {
		return err
	}
	meta, err := loadProject(cfg, root)
	if err != nil {
		return err
	}
	if strings.ToLower(meta.MarkupType) != "typst" {
		return errors.New("local compile currently supports Typst projects only")
	}
	mainFile := meta.MainFileName
	if strings.TrimSpace(mainFile) == "" {
		mainFile = "main.typ"
	}
	if _, err := os.Stat(filepath.Join(root, filepath.FromSlash(mainFile))); err != nil {
		return fmt.Errorf("main file not found in workspace: %s", mainFile)
	}

	result := compileTypst(root, mainFile, cfg.TypstBin, cfg.Timeout)
	response, err := uploadCompileResult(cfg, result)
	if err != nil {
		return err
	}
	var pretty bytes.Buffer
	if err := json.Indent(&pretty, response, "", "  "); err == nil {
		fmt.Println(pretty.String())
	} else {
		fmt.Println(string(response))
	}
	if result.Status != "success" {
		os.Exit(2)
	}
	return nil
}

func runWorkspace(args []string) error {
	if len(args) == 0 {
		return errors.New("workspace subcommand is required: open, sync, or watch")
	}
	switch args[0] {
	case "open":
		return runWorkspaceOpen(args[1:])
	case "sync":
		return runWorkspaceSync(args[1:], false)
	case "watch":
		return runWorkspaceWatch(args[1:])
	case "status":
		return runWorkspaceStatus(args[1:])
	case "pull":
		return runWorkspacePull(args[1:])
	case "release", "close":
		return runWorkspaceRelease(args[1:])
	default:
		return fmt.Errorf("unknown workspace subcommand %q", args[0])
	}
}

func runAnnotations(args []string) error {
	if len(args) == 0 {
		return errors.New("annotations subcommand is required: list, add, or update")
	}
	switch args[0] {
	case "list":
		return runAnnotationsList(args[1:])
	case "add":
		return runAnnotationsAdd(args[1:])
	case "update":
		return runAnnotationsUpdate(args[1:])
	default:
		return fmt.Errorf("unknown annotations subcommand %q", args[0])
	}
}

func runProposals(args []string) error {
	if len(args) == 0 {
		return errors.New("proposals subcommand is required: status, diff, accept, or discard")
	}
	switch args[0] {
	case "status":
		return runProposalStatus(args[1:])
	case "diff":
		return runProposalDiff(args[1:])
	case "edit-line":
		return runProposalEditLine(args[1:])
	case "accept":
		return runProposalAccept(args[1:])
	case "discard":
		return runProposalDiscard(args[1:])
	default:
		return fmt.Errorf("unknown proposals subcommand %q", args[0])
	}
}

func runVersions(args []string) error {
	if len(args) == 0 {
		return errors.New("versions subcommand is required: list, detail, or rollback")
	}
	switch args[0] {
	case "list":
		return runVersionsList(args[1:])
	case "detail", "show":
		return runVersionDetail(args[1:])
	case "rollback":
		return runVersionRollback(args[1:])
	default:
		return fmt.Errorf("unknown versions subcommand %q", args[0])
	}
}

func runPdfEmbed(args []string) error {
	if len(args) == 0 {
		return errors.New("pdf-embed subcommand is required: list or set")
	}
	switch args[0] {
	case "list":
		return runPdfEmbedList(args[1:])
	case "set":
		return runPdfEmbedSet(args[1:])
	default:
		return fmt.Errorf("unknown pdf-embed subcommand %q", args[0])
	}
}

func pdfEmbedFlags(name string) (*flag.FlagSet, *string, *string, *int) {
	fs, server, token := baseFlags("pdf-embed-" + name)
	projectID := fs.Int("project", 0, "SmartTeX project id")
	return fs, server, token, projectID
}

func runPdfEmbedList(args []string) error {
	fs, server, token, projectID := pdfEmbedFlags("list")
	if err := fs.Parse(args); err != nil {
		return err
	}
	if *projectID <= 0 {
		return errors.New("--project is required")
	}
	resolvedToken, err := resolveToken(*server, *token)
	if err != nil {
		return err
	}
	raw, err := apiRequest("GET", *server, fmt.Sprintf("/api/projects/%d/pdf-embed/", *projectID), resolvedToken, nil, "")
	if err != nil {
		return err
	}
	fmt.Println(string(prettyJSON(raw)))
	return nil
}

func runPdfEmbedSet(args []string) error {
	fs, server, token, projectID := pdfEmbedFlags("set")
	filePath := fs.String("file", "", "project PDF file path")
	enabled := fs.Bool("enabled", true, "whether PDF embed is enabled")
	if err := fs.Parse(args); err != nil {
		return err
	}
	if *projectID <= 0 {
		return errors.New("--project is required")
	}
	pdfPath := strings.TrimSpace(*filePath)
	if pdfPath == "" {
		return errors.New("--file is required")
	}
	if !strings.HasSuffix(strings.ToLower(pdfPath), ".pdf") {
		return errors.New("--file must point to a PDF")
	}
	resolvedToken, err := resolveToken(*server, *token)
	if err != nil {
		return err
	}
	body := map[string]any{"file": pdfPath, "enabled": *enabled}
	raw, err := apiJSON("POST", *server, fmt.Sprintf("/api/projects/%d/pdf-embed/", *projectID), resolvedToken, body)
	if err != nil {
		return err
	}
	fmt.Println(string(prettyJSON(raw)))
	return nil
}

func versionFlags(name string) (*flag.FlagSet, *string, *string, *int) {
	fs, server, token := baseFlags("versions-" + name)
	projectID := fs.Int("project", 0, "SmartTeX project id")
	return fs, server, token, projectID
}

func runVersionsList(args []string) error {
	fs, server, token, projectID := versionFlags("list")
	limit := fs.Int("limit", 40, "maximum number of versions")
	beforeID := fs.Int("before-id", 0, "load versions before this version id")
	fileName := fs.String("file", "", "filter by project file path")
	if err := fs.Parse(args); err != nil {
		return err
	}
	if *projectID <= 0 {
		return errors.New("--project is required")
	}
	resolvedToken, err := resolveToken(*server, *token)
	if err != nil {
		return err
	}
	query := url.Values{}
	query.Set("limit", strconv.Itoa(*limit))
	if *beforeID > 0 {
		query.Set("before_id", strconv.Itoa(*beforeID))
	}
	if strings.TrimSpace(*fileName) != "" {
		query.Set("file", strings.TrimSpace(*fileName))
	}
	raw, err := apiRequest("GET", *server, fmt.Sprintf("/api/projects/%d/versions/?%s", *projectID, query.Encode()), resolvedToken, nil, "")
	if err != nil {
		return err
	}
	fmt.Println(string(prettyJSON(raw)))
	return nil
}

func runVersionDetail(args []string) error {
	fs, server, token, projectID := versionFlags("detail")
	versionID := fs.Int("id", 0, "project version id")
	if err := fs.Parse(args); err != nil {
		return err
	}
	if *projectID <= 0 {
		return errors.New("--project is required")
	}
	if *versionID <= 0 {
		return errors.New("--id is required")
	}
	resolvedToken, err := resolveToken(*server, *token)
	if err != nil {
		return err
	}
	raw, err := apiRequest("GET", *server, fmt.Sprintf("/api/projects/%d/versions/%d/", *projectID, *versionID), resolvedToken, nil, "")
	if err != nil {
		return err
	}
	fmt.Println(string(prettyJSON(raw)))
	return nil
}

func runVersionRollback(args []string) error {
	fs, server, token, projectID := versionFlags("rollback")
	versionID := fs.Int("id", 0, "project version id")
	summary := fs.String("summary", "", "rollback change summary")
	if err := fs.Parse(args); err != nil {
		return err
	}
	if *projectID <= 0 {
		return errors.New("--project is required")
	}
	if *versionID <= 0 {
		return errors.New("--id is required")
	}
	resolvedToken, err := resolveToken(*server, *token)
	if err != nil {
		return err
	}
	body := map[string]any{
		"change_source": "api",
	}
	if strings.TrimSpace(*summary) != "" {
		body["change_summary"] = strings.TrimSpace(*summary)
		body["summary"] = strings.TrimSpace(*summary)
	}
	raw, err := apiJSON("POST", *server, fmt.Sprintf("/api/projects/%d/versions/%d/rollback/", *projectID, *versionID), resolvedToken, body)
	if err != nil {
		return err
	}
	fmt.Println(string(prettyJSON(raw)))
	return nil
}

func proposalFlags(name string) (*flag.FlagSet, *string, *string, *int) {
	fs, server, token := baseFlags("proposals-" + name)
	projectID := fs.Int("project", 0, "SmartTeX project id")
	return fs, server, token, projectID
}

func runProposalStatus(args []string) error {
	fs, server, token, projectID := proposalFlags("status")
	if err := fs.Parse(args); err != nil {
		return err
	}
	if *projectID <= 0 {
		return errors.New("--project is required")
	}
	resolvedToken, err := resolveToken(*server, *token)
	if err != nil {
		return err
	}
	raw, err := apiRequest("GET", *server, fmt.Sprintf("/api/projects/%d/change-proposals/status/", *projectID), resolvedToken, nil, "")
	if err != nil {
		return err
	}
	fmt.Println(string(prettyJSON(raw)))
	return nil
}

func runProposalDiff(args []string) error {
	fs, server, token, projectID := proposalFlags("diff")
	if err := fs.Parse(args); err != nil {
		return err
	}
	if *projectID <= 0 {
		return errors.New("--project is required")
	}
	resolvedToken, err := resolveToken(*server, *token)
	if err != nil {
		return err
	}
	raw, err := apiRequest("GET", *server, fmt.Sprintf("/api/projects/%d/change-proposals/diff/", *projectID), resolvedToken, nil, "")
	if err != nil {
		return err
	}
	fmt.Println(string(prettyJSON(raw)))
	return nil
}

func runProposalEditLine(args []string) error {
	fs, server, token, projectID := proposalFlags("edit-line")
	fileName := fs.String("file", "", "proposal file path")
	lineNumber := fs.Int("line", 0, "1-based line number in the proposal new file")
	expectedText := fs.String("expected-text", "", "expected current line text")
	newText := fs.String("new-text", "", "replacement line text")
	expectedTextProvided := cliFlagProvided(args, "expected-text")
	if err := fs.Parse(args); err != nil {
		return err
	}
	if *projectID <= 0 {
		return errors.New("--project is required")
	}
	if strings.TrimSpace(*fileName) == "" {
		return errors.New("--file is required")
	}
	if *lineNumber <= 0 {
		return errors.New("--line is required")
	}
	resolvedToken, err := resolveToken(*server, *token)
	if err != nil {
		return err
	}
	body := map[string]any{
		"file_name":   strings.TrimSpace(*fileName),
		"line_number": *lineNumber,
		"new_text":    *newText,
	}
	if expectedTextProvided {
		body["expected_text"] = *expectedText
	}
	raw, err := apiJSON("POST", *server, fmt.Sprintf("/api/projects/%d/change-proposals/manual-edit/", *projectID), resolvedToken, body)
	if err != nil {
		return err
	}
	fmt.Println(string(prettyJSON(raw)))
	return nil
}

func cliFlagProvided(args []string, name string) bool {
	long := "--" + name
	for _, arg := range args {
		if arg == long || strings.HasPrefix(arg, long+"=") {
			return true
		}
	}
	return false
}

func runProposalAccept(args []string) error {
	fs, server, token, projectID := proposalFlags("accept")
	acceptCompileErrors := fs.Bool("accept-compile-errors", false, "accept a failed-compile proposal by explicit user override")
	if err := fs.Parse(args); err != nil {
		return err
	}
	if *projectID <= 0 {
		return errors.New("--project is required")
	}
	resolvedToken, err := resolveToken(*server, *token)
	if err != nil {
		return err
	}
	body := map[string]any{}
	if *acceptCompileErrors {
		body["accept_compile_errors"] = true
	}
	raw, err := apiJSON("POST", *server, fmt.Sprintf("/api/projects/%d/change-proposals/accept/", *projectID), resolvedToken, body)
	if err != nil {
		return err
	}
	fmt.Println(string(prettyJSON(raw)))
	return nil
}

func runProposalDiscard(args []string) error {
	fs, server, token, projectID := proposalFlags("discard")
	if err := fs.Parse(args); err != nil {
		return err
	}
	if *projectID <= 0 {
		return errors.New("--project is required")
	}
	resolvedToken, err := resolveToken(*server, *token)
	if err != nil {
		return err
	}
	raw, err := apiJSON("POST", *server, fmt.Sprintf("/api/projects/%d/change-proposals/discard/", *projectID), resolvedToken, map[string]any{})
	if err != nil {
		return err
	}
	fmt.Println(string(prettyJSON(raw)))
	return nil
}

func annotationFlags(name string) (*flag.FlagSet, *string, *string, *int) {
	fs, server, token := baseFlags("annotations-" + name)
	projectID := fs.Int("project", 0, "SmartTeX project id")
	return fs, server, token, projectID
}

func runAnnotationsList(args []string) error {
	fs, server, token, projectID := annotationFlags("list")
	status := fs.String("status", "", "filter by annotation status")
	fileName := fs.String("file", "", "filter by project file path")
	human := fs.Bool("human", false, "print a compact table instead of JSON")
	if err := fs.Parse(args); err != nil {
		return err
	}
	if *projectID <= 0 {
		return errors.New("--project is required")
	}
	resolvedToken, err := resolveToken(*server, *token)
	if err != nil {
		return err
	}
	query := url.Values{}
	if strings.TrimSpace(*status) != "" {
		query.Set("status", strings.TrimSpace(*status))
	}
	if strings.TrimSpace(*fileName) != "" {
		query.Set("file_name", strings.TrimSpace(*fileName))
	}
	path := fmt.Sprintf("/api/projects/%d/annotations/", *projectID)
	if encoded := query.Encode(); encoded != "" {
		path += "?" + encoded
	}
	raw, err := apiRequest("GET", *server, path, resolvedToken, nil, "")
	if err != nil {
		return err
	}
	if *human {
		return printAnnotationsHuman(raw)
	}
	fmt.Println(string(prettyJSON(raw)))
	return nil
}

func runAnnotationsAdd(args []string) error {
	fs, server, token, projectID := annotationFlags("add")
	fileName := fs.String("file", "", "project file path")
	instruction := fs.String("text", "", "annotation instruction")
	lineStart := fs.Int("line", 0, "line number")
	lineEnd := fs.Int("line-end", 0, "optional end line")
	selectedText := fs.String("selected-text", "", "selected text fragment")
	status := fs.String("status", "", "initial status")
	if err := fs.Parse(args); err != nil {
		return err
	}
	if *projectID <= 0 {
		return errors.New("--project is required")
	}
	if strings.TrimSpace(*fileName) == "" {
		return errors.New("--file is required")
	}
	if strings.TrimSpace(*instruction) == "" {
		return errors.New("--text is required")
	}
	resolvedToken, err := resolveToken(*server, *token)
	if err != nil {
		return err
	}
	body := map[string]any{
		"file_name":      strings.TrimSpace(*fileName),
		"instruction":    strings.TrimSpace(*instruction),
		"selected_text":  *selectedText,
		"change_source":  "api",
		"change_summary": "Created from VS Code",
	}
	if *lineStart > 0 {
		body["line_start"] = *lineStart
	}
	if *lineEnd > 0 {
		body["line_end"] = *lineEnd
	}
	if strings.TrimSpace(*status) != "" {
		body["status"] = strings.TrimSpace(*status)
	}
	raw, err := apiJSON("POST", *server, fmt.Sprintf("/api/projects/%d/annotations/", *projectID), resolvedToken, body)
	if err != nil {
		return err
	}
	fmt.Println(string(prettyJSON(raw)))
	return nil
}

func runAnnotationsUpdate(args []string) error {
	fs, server, token, projectID := annotationFlags("update")
	annotationID := fs.Int("id", 0, "annotation id")
	status := fs.String("status", "", "new annotation status")
	instruction := fs.String("text", "", "new annotation instruction")
	if err := fs.Parse(args); err != nil {
		return err
	}
	if *projectID <= 0 {
		return errors.New("--project is required")
	}
	if *annotationID <= 0 {
		return errors.New("--id is required")
	}
	body := map[string]any{
		"change_source":  "api",
		"change_summary": "Updated from VS Code",
	}
	if strings.TrimSpace(*status) != "" {
		body["status"] = strings.TrimSpace(*status)
	}
	if strings.TrimSpace(*instruction) != "" {
		body["instruction"] = strings.TrimSpace(*instruction)
	}
	if len(body) <= 2 {
		return errors.New("nothing to update; pass --status or --text")
	}
	resolvedToken, err := resolveToken(*server, *token)
	if err != nil {
		return err
	}
	raw, err := apiJSON("PATCH", *server, fmt.Sprintf("/api/projects/%d/annotations/%d/", *projectID, *annotationID), resolvedToken, body)
	if err != nil {
		return err
	}
	fmt.Println(string(prettyJSON(raw)))
	return nil
}

func printAnnotationsHuman(raw []byte) error {
	var payload struct {
		Annotations []map[string]any `json:"annotations"`
	}
	if err := json.Unmarshal(raw, &payload); err != nil {
		return err
	}
	if len(payload.Annotations) == 0 {
		fmt.Println("No annotations.")
		return nil
	}
	for _, item := range payload.Annotations {
		id := int(numberFromMap(item, "id"))
		lineStart := int(numberFromMap(item, "line_start"))
		lineEnd := int(numberFromMap(item, "line_end"))
		fileName := fmt.Sprint(item["file_name"])
		status := fmt.Sprint(item["status"])
		instruction := strings.ReplaceAll(fmt.Sprint(item["instruction"]), "\n", " ")
		lineLabel := strconv.Itoa(lineStart)
		if lineEnd > 0 && lineEnd != lineStart {
			lineLabel = fmt.Sprintf("%d-%d", lineStart, lineEnd)
		}
		fmt.Printf("#%d\t%s\t%s:%s\t%s\n", id, status, fileName, lineLabel, instruction)
	}
	return nil
}

func workspaceFlags(name string) (*flag.FlagSet, *string, *string, *int, *string, *string) {
	fs, server, token := baseFlags("workspace-" + name)
	projectID := fs.Int("project", 0, "SmartTeX project id")
	workspace := fs.String("workspace", envOr("SMARTTEX_LOCAL_WORKSPACE", "~/.smarttex-local"), "local workspace root")
	agentID := fs.String("agent-id", agentID(), "local workspace agent id")
	return fs, server, token, projectID, workspace, agentID
}

func runWorkspaceOpen(args []string) error {
	fs, server, token, projectID, workspace, agentID := workspaceFlags("open")
	openCode := fs.Bool("code", false, "open the workspace in VS Code after pulling")
	force := fs.Bool("force", false, "overwrite local workspace even if it has unsynced changes")
	if err := fs.Parse(args); err != nil {
		return err
	}
	cfg, root, err := workspaceConfig(*server, *token, *projectID, *workspace)
	if err != nil {
		return err
	}
	previous, _ := loadWorkspaceState(root)
	if !*force {
		if dirty, err := localWorkspaceChangeCount(root); err != nil {
			return err
		} else if dirty > 0 {
			return fmt.Errorf("local workspace has %d unsynced change(s); run `workspace sync` first or retry open with --force", dirty)
		}
	}
	meta, err := loadProject(cfg, root)
	if err != nil {
		return err
	}
	state, err := claimWorkspaceLease(cfg, root, *agentID, previous.WorkspaceID)
	if err != nil {
		return err
	}
	if err := saveWorkspaceState(root, state); err != nil {
		return err
	}
	fmt.Printf("SmartTeX workspace ready: %s\n", root)
	fmt.Printf("Project: %s (#%d)\n", meta.Title, meta.ID)
	fmt.Printf("Workspace lease: %s, server version: %d\n", state.WorkspaceID, state.BaseVersionNumber)
	if *openCode {
		if err := openVSCodeWorkspace(root); err != nil {
			fmt.Fprintln(os.Stderr, err)
			fmt.Printf("Open this folder manually in VS Code: %s\n", root)
		}
	}
	return nil
}

func runWorkspaceSync(args []string, quiet bool) error {
	fs, server, token, projectID, workspace, agentID := workspaceFlags("sync")
	force := fs.Bool("force", false, "sync even if server version advanced")
	if err := fs.Parse(args); err != nil {
		return err
	}
	cfg, root, err := workspaceConfig(*server, *token, *projectID, *workspace)
	if err != nil {
		return err
	}
	state, err := loadWorkspaceState(root)
	if err != nil || state.WorkspaceID == "" {
		state, err = claimWorkspaceLease(cfg, root, *agentID, "")
		if err != nil {
			return err
		}
	}
	result, err := syncWorkspaceOnce(cfg, root, state, *agentID, *force)
	if err != nil {
		return err
	}
	if !quiet {
		fmt.Println(result)
	}
	return nil
}

func runWorkspaceWatch(args []string) error {
	fs, server, token, projectID, workspace, agentID := workspaceFlags("watch")
	interval := fs.Duration("interval", 2*time.Second, "polling interval")
	if err := fs.Parse(args); err != nil {
		return err
	}
	cfg, root, err := workspaceConfig(*server, *token, *projectID, *workspace)
	if err != nil {
		return err
	}
	state, err := loadWorkspaceState(root)
	if err != nil || state.WorkspaceID == "" {
		state, err = claimWorkspaceLease(cfg, root, *agentID, "")
		if err != nil {
			return err
		}
		if err := saveWorkspaceState(root, state); err != nil {
			return err
		}
	}
	fmt.Printf("Watching SmartTeX workspace %s. Press Ctrl+C to stop.\n", root)
	ticker := time.NewTicker(maxDuration(*interval, 500*time.Millisecond))
	defer ticker.Stop()
	for {
		select {
		case <-ticker.C:
			msg, err := syncWorkspaceOnce(cfg, root, state, *agentID, false)
			if err != nil {
				fmt.Fprintln(os.Stderr, "workspace sync:", err)
				continue
			}
			if nextState, err := loadWorkspaceState(root); err == nil {
				state = nextState
			}
			if msg != "" {
				fmt.Println(msg)
			}
		}
	}
}

func runWorkspaceStatus(args []string) error {
	fs, server, token, projectID, workspace, _ := workspaceFlags("status")
	jsonOutput := fs.Bool("json", false, "print workspace status as JSON")
	if err := fs.Parse(args); err != nil {
		return err
	}
	cfg, root, err := workspaceConfig(*server, *token, *projectID, *workspace)
	if err != nil {
		return err
	}
	state, _ := loadWorkspaceState(root)
	dirty, err := localWorkspaceChangeCount(root)
	if err != nil {
		return err
	}
	raw, err := apiRequest("GET", cfg.Server, fmt.Sprintf("/api/projects/%d/local-workspace/", cfg.ProjectID), cfg.Token, nil, "")
	if err != nil {
		return err
	}
	var remote map[string]any
	if err := json.Unmarshal(raw, &remote); err != nil {
		return err
	}
	latestVersion := int(numberFromMap(remote, "latest_version_number"))
	leaseActive := boolFromMap(remote, "active")
	if *jsonOutput {
		payload := map[string]any{
			"workspace":                root,
			"workspace_id":             firstNonEmpty(state.WorkspaceID, ""),
			"project_id":               cfg.ProjectID,
			"local_unsynced_changes":   dirty,
			"local_base_version":       state.BaseVersionNumber,
			"server_latest_version":    latestVersion,
			"server_lease_active":      leaseActive,
			"server_workspace_id":      stringFromMap(remote, "workspace_id"),
			"server_workspace_agent":   stringFromMap(remote, "agent_id"),
			"server_workspace_expires": stringFromMap(remote, "expires_at"),
		}
		encoded, err := json.MarshalIndent(payload, "", "  ")
		if err != nil {
			return err
		}
		fmt.Println(string(encoded))
		return nil
	}
	fmt.Printf("Workspace: %s\n", root)
	fmt.Printf("Workspace ID: %s\n", firstNonEmpty(state.WorkspaceID, "(not opened)"))
	fmt.Printf("Local unsynced changes: %d\n", dirty)
	fmt.Printf("Local base version: %d\n", state.BaseVersionNumber)
	fmt.Printf("Server latest version: %d\n", latestVersion)
	fmt.Printf("Server lease active: %t\n", leaseActive)
	return nil
}

func runWorkspacePull(args []string) error {
	fs, server, token, projectID, workspace, agentID := workspaceFlags("pull")
	force := fs.Bool("force", false, "overwrite local workspace even if it has unsynced changes")
	if err := fs.Parse(args); err != nil {
		return err
	}
	cfg, root, err := workspaceConfig(*server, *token, *projectID, *workspace)
	if err != nil {
		return err
	}
	previous, _ := loadWorkspaceState(root)
	if !*force {
		if dirty, err := localWorkspaceChangeCount(root); err != nil {
			return err
		} else if dirty > 0 {
			return fmt.Errorf("local workspace has %d unsynced change(s); run `workspace sync` first or retry pull with --force", dirty)
		}
	}
	meta, err := loadProject(cfg, root)
	if err != nil {
		return err
	}
	state, err := claimWorkspaceLease(cfg, root, *agentID, previous.WorkspaceID)
	if err != nil {
		return err
	}
	if err := saveWorkspaceState(root, state); err != nil {
		return err
	}
	fmt.Printf("Pulled SmartTeX project %s (#%d) into %s\n", meta.Title, meta.ID, root)
	fmt.Printf("Workspace lease: %s, server version: %d\n", state.WorkspaceID, state.BaseVersionNumber)
	return nil
}

func runWorkspaceRelease(args []string) error {
	fs, server, token, projectID, workspace, _ := workspaceFlags("release")
	if err := fs.Parse(args); err != nil {
		return err
	}
	cfg, root, err := workspaceConfig(*server, *token, *projectID, *workspace)
	if err != nil {
		return err
	}
	state, _ := loadWorkspaceState(root)
	body := map[string]any{"workspace_id": state.WorkspaceID}
	if _, err := apiJSON("DELETE", cfg.Server, fmt.Sprintf("/api/projects/%d/local-workspace/", cfg.ProjectID), cfg.Token, body); err != nil {
		return err
	}
	if state.WorkspaceID != "" {
		state.WorkspaceID = ""
		_ = saveWorkspaceState(root, state)
	}
	fmt.Printf("Released SmartTeX workspace lease for project %d\n", cfg.ProjectID)
	return nil
}

func runDoctor(args []string) error {
	fs, server, token := baseFlags("doctor")
	typstBin := fs.String("typst-bin", defaultTypstBin(), "typst executable")
	tinymistBin := fs.String("tinymist-bin", defaultTinymistBin(), "tinymist executable")
	if err := fs.Parse(args); err != nil {
		return err
	}
	hadError := false
	fmt.Println("SmartTeX local runtime doctor")
	if path, err := exec.LookPath(*typstBin); err == nil {
		fmt.Println("OK   typst:", path)
	} else {
		fmt.Println("FAIL typst: not found. Set --typst-bin or TYPST_BINARY.")
		hadError = true
	}
	if path, err := exec.LookPath(*tinymistBin); err == nil {
		fmt.Println("OK   tinymist:", path)
	} else {
		fmt.Println("WARN tinymist: not found. Local preview and LSP will be unavailable.")
	}
	resolvedToken, tokenErr := resolveToken(*server, *token)
	if tokenErr != nil {
		fmt.Println("FAIL auth:", tokenErr)
		hadError = true
	} else if _, err := apiRequest("GET", *server, "/api/projects/", resolvedToken, nil, ""); err != nil {
		fmt.Println("FAIL server auth:", err)
		hadError = true
	} else {
		fmt.Println("OK   server auth:", *server)
	}
	if secret, err := resolveBridgeSecret(""); err == nil && strings.TrimSpace(secret) != "" {
		fmt.Println("OK   bridge secret is configured")
	} else {
		fmt.Println("WARN bridge secret is not configured yet. Run `smarttex-local login` or `smarttex-local serve` once.")
	}
	if hadError {
		return errors.New("doctor found blocking issues")
	}
	return nil
}

func runUpdate(args []string) error {
	fs := flag.NewFlagSet("update", flag.ExitOnError)
	server := fs.String("server", defaultServer(), "SmartTeX server URL")
	installPath := fs.String("install-path", "", "binary path to replace; defaults to current executable")
	channel := fs.String("channel", envOr("SMARTTEX_LOCAL_UPDATE_CHANNEL", "stable"), "release channel")
	force := fs.Bool("force", false, "install even when the manifest version matches the current binary")
	skipExtension := fs.Bool("skip-vscode-extension", false, "do not update the SmartTeX VS Code extension")
	if err := fs.Parse(args); err != nil {
		return err
	}
	target := strings.TrimSpace(*installPath)
	if target == "" {
		current, err := os.Executable()
		if err != nil {
			return err
		}
		target = current
	}
	absTarget, err := filepath.Abs(target)
	if err != nil {
		return err
	}
	manifest, asset, err := fetchUpdateManifest(*server, *channel)
	if err != nil {
		return err
	}
	if asset == nil {
		return fmt.Errorf("no SmartTeX local agent binary is published for %s/%s on channel %s", runtime.GOOS, runtime.GOARCH, *channel)
	}
	updatedBinary := false
	if !*force && manifest.Version == toolVersion {
		fmt.Printf("SmartTeX local agent is up to date (%s).\n", toolVersion)
	} else {
		raw, err := downloadUpdateAsset(*server, asset.URL)
		if err != nil {
			return err
		}
		if asset.SHA256 != "" {
			sum := sha256.Sum256(raw)
			actual := fmt.Sprintf("%x", sum[:])
			if !strings.EqualFold(actual, asset.SHA256) {
				return fmt.Errorf("downloaded binary checksum mismatch: got %s, expected %s", actual, asset.SHA256)
			}
		}
		if err := installBinary(absTarget, raw); err != nil {
			return err
		}
		updatedBinary = true
		fmt.Printf("Updated SmartTeX local agent %s -> %s at %s\n", toolVersion, manifest.Version, absTarget)
	}
	if !*skipExtension {
		if err := installVSCodeExtensionFromManifest(*server, manifest.VSCodeExtension); err != nil {
			fmt.Fprintf(os.Stderr, "WARN SmartTeX VS Code extension update skipped: %v\n", err)
		}
	}
	if !updatedBinary && *skipExtension {
		fmt.Println("No updates installed.")
	}
	return nil
}

func runToolchain(args []string) error {
	if len(args) == 0 || args[0] == "-h" || args[0] == "--help" {
		fmt.Println(`Usage:
  smarttex-local-go toolchain status
  smarttex-local-go toolchain install [--server URL] [--channel stable]`)
		return nil
	}
	switch args[0] {
	case "status":
		return runToolchainStatus(args[1:])
	case "install", "update":
		return runToolchainInstall(args[1:])
	default:
		return fmt.Errorf("unknown toolchain command %q", args[0])
	}
}

func runToolchainStatus(args []string) error {
	fs := flag.NewFlagSet("toolchain status", flag.ExitOnError)
	typstBin := fs.String("typst-bin", defaultTypstBin(), "typst executable")
	tinymistBin := fs.String("tinymist-bin", defaultTinymistBin(), "tinymist executable")
	if err := fs.Parse(args); err != nil {
		return err
	}
	cfg, _ := loadLocalConfig()
	fmt.Println("SmartTeX local toolchain")
	fmt.Printf("  channel: %s\n", firstNonEmpty(cfg.ToolchainChannel, "stable"))
	fmt.Printf("  typst: %s\n", *typstBin)
	fmt.Printf("    version: %s\n", firstNonEmpty(binaryVersion(*typstBin), "unavailable"))
	fmt.Printf("  tinymist: %s\n", *tinymistBin)
	fmt.Printf("    version: %s\n", firstNonEmpty(binaryVersion(*tinymistBin), "unavailable"))
	return nil
}

func runToolchainInstall(args []string) error {
	fs := flag.NewFlagSet("toolchain install", flag.ExitOnError)
	server := fs.String("server", defaultServer(), "SmartTeX server URL")
	channel := fs.String("channel", envOr("SMARTTEX_LOCAL_UPDATE_CHANNEL", "stable"), "toolchain channel")
	toolsRaw := fs.String("tools", "typst,tinymist", "comma-separated tools to install")
	if err := fs.Parse(args); err != nil {
		return err
	}
	manifest, err := fetchManifest(*server, *channel)
	if err != nil {
		return err
	}
	wanted := map[string]bool{}
	for _, item := range strings.Split(*toolsRaw, ",") {
		name := strings.TrimSpace(strings.ToLower(item))
		if name != "" {
			wanted[name] = true
		}
	}
	if !wanted["typst"] && !wanted["tinymist"] {
		return errors.New("--tools must include typst and/or tinymist")
	}
	cfg, _ := loadLocalConfig()
	cfg.Server = firstNonEmpty(cfg.Server, *server)
	cfg.ToolchainChannel = *channel
	installed := 0
	for tool := range wanted {
		asset := selectToolchainAsset(manifest, tool)
		if asset == nil {
			return fmt.Errorf("no %s toolchain asset is published for %s/%s on channel %s", tool, runtime.GOOS, runtime.GOARCH, *channel)
		}
		path, err := installToolchainAsset(*server, *channel, *asset)
		if err != nil {
			return err
		}
		if tool == "typst" {
			cfg.TypstBin = path
		}
		if tool == "tinymist" {
			cfg.TinymistBin = path
		}
		fmt.Printf("Installed %s %s -> %s\n", tool, firstNonEmpty(asset.Version, "unknown"), path)
		installed++
	}
	if installed > 0 {
		if err := saveLocalConfig(cfg); err != nil {
			return err
		}
	}
	return nil
}

func runServe(args []string) error {
	fs, server, token := baseFlags("serve")
	workspace := fs.String("workspace", envOr("SMARTTEX_LOCAL_WORKSPACE", "~/.smarttex-local"), "local workspace root")
	typstBin := fs.String("typst-bin", defaultTypstBin(), "typst executable")
	tinymistBin := fs.String("tinymist-bin", defaultTinymistBin(), "tinymist executable for local preview")
	timeout := fs.Int("timeout", 60, "compile timeout seconds")
	listen := fs.String("listen", envOr("SMARTTEX_LOCAL_LISTEN", "127.0.0.1:8765"), "local HTTP listen address")
	secret := fs.String("secret", os.Getenv("SMARTTEX_LOCAL_SECRET"), "shared secret required from the web UI")
	if err := fs.Parse(args); err != nil {
		return err
	}
	auth := &tokenSource{server: *server, explicitToken: strings.TrimSpace(*token)}
	if _, err := auth.Token(); err != nil {
		return err
	}
	resolvedSecret, err := resolveBridgeSecret(*secret)
	if err != nil {
		return err
	}
	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()
	cfg := serveConfig{
		Context:     ctx,
		Server:      *server,
		Auth:        auth,
		Workspace:   *workspace,
		TypstBin:    *typstBin,
		TinymistBin: *tinymistBin,
		Timeout:     time.Duration(*timeout) * time.Second,
		Listen:      *listen,
		Secret:      resolvedSecret,
	}
	mux := http.NewServeMux()
	mux.HandleFunc("/v1/health", cfg.withCORS(cfg.handleHealth))
	mux.HandleFunc("/v1/compile", cfg.withCORS(cfg.handleCompile))
	mux.HandleFunc("/v1/preview/refresh", cfg.withCORS(cfg.handlePreviewRefresh))
	mux.HandleFunc("/v1/preview", cfg.withCORS(cfg.handlePreview))
	mux.HandleFunc("/v1/preview/", cfg.withCORS(cfg.handlePreview))
	mux.HandleFunc("/v1/lsp", cfg.handleLSP)
	mux.HandleFunc("/ws/typst-preview/", cfg.handlePreviewDataWebSocket)
	mux.HandleFunc("/ws/typst-preview/control/", cfg.handlePreviewControlWebSocket)
	go cfg.sendHeartbeats()
	go cfg.pollLocalCompileJobs()
	fmt.Printf("SmartTeX local agent listening on http://%s\n", cfg.Listen)
	fmt.Println("Open the SmartTeX editor, click Local, then paste:")
	fmt.Printf("  Agent URL: http://%s\n", cfg.Listen)
	fmt.Printf("  Bridge secret: %s\n", cfg.Secret)
	serverHTTP := &http.Server{Addr: cfg.Listen, Handler: mux}
	go func() {
		<-ctx.Done()
		shutdownCtx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
		defer cancel()
		_ = serverHTTP.Shutdown(shutdownCtx)
	}()
	err = serverHTTP.ListenAndServe()
	if errors.Is(err, http.ErrServerClosed) {
		return nil
	}
	return err
}

func (cfg serveConfig) withCORS(next http.HandlerFunc) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Access-Control-Allow-Origin", "*")
		w.Header().Set("Access-Control-Allow-Methods", "GET,POST,OPTIONS")
		w.Header().Set("Access-Control-Allow-Headers", "Content-Type,X-SmartTeX-Local-Secret")
		w.Header().Set("Access-Control-Max-Age", "600")
		if r.Method == http.MethodOptions {
			w.WriteHeader(http.StatusNoContent)
			return
		}
		next(w, r)
	}
}

func (cfg serveConfig) authorize(r *http.Request) bool {
	return r.Header.Get("X-SmartTeX-Local-Secret") == cfg.Secret
}

func (cfg serveConfig) authorizePreviewRequest(r *http.Request) bool {
	if cfg.authorize(r) || r.URL.Query().Get("secret") == cfg.Secret {
		return true
	}
	ref := strings.TrimSpace(r.Header.Get("Referer"))
	if ref == "" {
		return false
	}
	parsed, err := url.Parse(ref)
	if err != nil {
		return false
	}
	return parsed.Query().Get("secret") == cfg.Secret
}

func (cfg serveConfig) handleHealth(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodGet {
		http.Error(w, "method not allowed", http.StatusMethodNotAllowed)
		return
	}
	if !cfg.authorize(r) {
		http.Error(w, "unauthorized", http.StatusUnauthorized)
		return
	}
	writeJSON(w, map[string]any{
		"ok":                 true,
		"tool":               "smarttex-local-go",
		"tool_version":       toolVersion,
		"server":             cfg.Server,
		"capabilities":       localCapabilities(),
		"typst_version":      binaryVersion(cfg.TypstBin),
		"tinymist_version":   binaryVersion(cfg.TinymistBin),
		"tinymist_available": binaryAvailable(cfg.TinymistBin),
	})
}

func (cfg serveConfig) handleCompile(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		http.Error(w, "method not allowed", http.StatusMethodNotAllowed)
		return
	}
	if !cfg.authorize(r) {
		http.Error(w, "unauthorized", http.StatusUnauthorized)
		return
	}
	var payload struct {
		ProjectID int `json:"project_id"`
	}
	if err := json.NewDecoder(r.Body).Decode(&payload); err != nil {
		http.Error(w, "invalid JSON body", http.StatusBadRequest)
		return
	}
	if payload.ProjectID <= 0 {
		http.Error(w, "project_id is required", http.StatusBadRequest)
		return
	}
	root, err := workspaceRootFor(cfg.Workspace, payload.ProjectID, "compile")
	if err != nil {
		http.Error(w, err.Error(), http.StatusInternalServerError)
		return
	}
	compileCfg := config{
		Server:    cfg.Server,
		ProjectID: payload.ProjectID,
		Workspace: cfg.Workspace,
		TypstBin:  cfg.TypstBin,
		Timeout:   cfg.Timeout,
		Pull:      true,
	}
	compileCfg.Token, err = cfg.authToken()
	if err != nil {
		http.Error(w, err.Error(), http.StatusBadGateway)
		return
	}
	meta, err := loadProject(compileCfg, root)
	if err != nil {
		http.Error(w, err.Error(), http.StatusBadGateway)
		return
	}
	if strings.ToLower(meta.MarkupType) != "typst" {
		http.Error(w, "local compile currently supports Typst projects only", http.StatusBadRequest)
		return
	}
	result := compileTypst(root, meta.MainFileName, cfg.TypstBin, cfg.Timeout)
	response, err := uploadCompileResult(compileCfg, result)
	if err != nil {
		http.Error(w, err.Error(), http.StatusBadGateway)
		return
	}
	w.Header().Set("Content-Type", "application/json; charset=utf-8")
	w.WriteHeader(http.StatusOK)
	_, _ = w.Write(response)
}

func (cfg serveConfig) pollLocalCompileJobs() {
	ticker := time.NewTicker(2 * time.Second)
	defer ticker.Stop()
	for {
		if cfg.Context != nil {
			select {
			case <-cfg.Context.Done():
				return
			default:
			}
		}
		if err := cfg.claimAndRunOneJob(); err != nil {
			fmt.Fprintln(os.Stderr, "local runtime poll:", err)
			if !cfg.sleepOrDone(5 * time.Second) {
				return
			}
			continue
		}
		if cfg.Context != nil {
			select {
			case <-cfg.Context.Done():
				return
			case <-ticker.C:
			}
		} else {
			<-ticker.C
		}
	}
}

func (cfg serveConfig) sendHeartbeats() {
	ticker := time.NewTicker(10 * time.Second)
	defer ticker.Stop()
	for {
		if cfg.Context != nil {
			select {
			case <-cfg.Context.Done():
				return
			default:
			}
		}
		if err := cfg.sendHeartbeat(); err != nil {
			fmt.Fprintln(os.Stderr, "local runtime heartbeat:", err)
		}
		if cfg.Context != nil {
			select {
			case <-cfg.Context.Done():
				return
			case <-ticker.C:
			}
		} else {
			<-ticker.C
		}
	}
}

func (cfg serveConfig) sleepOrDone(delay time.Duration) bool {
	if cfg.Context == nil {
		time.Sleep(delay)
		return true
	}
	timer := time.NewTimer(delay)
	defer timer.Stop()
	select {
	case <-cfg.Context.Done():
		return false
	case <-timer.C:
		return true
	}
}

func (cfg serveConfig) sendHeartbeat() error {
	payload := map[string]any{
		"agent_id":      agentID(),
		"agent_version": toolVersion,
		"capabilities":  localCapabilities(),
	}
	rawBody, _ := json.Marshal(payload)
	token, err := cfg.authToken()
	if err != nil {
		return err
	}
	_, err = apiRequest("POST", cfg.Server, "/api/local-runtime/heartbeat/", token, bytes.NewReader(rawBody), "application/json")
	return err
}

func (cfg serveConfig) claimAndRunOneJob() error {
	payload := map[string]any{
		"agent_id":      agentID(),
		"agent_version": toolVersion,
		"capabilities":  localCapabilities(),
	}
	rawBody, _ := json.Marshal(payload)
	token, err := cfg.authToken()
	if err != nil {
		return err
	}
	raw, err := apiRequest("POST", cfg.Server, "/api/local-runtime/jobs/claim/", token, bytes.NewReader(rawBody), "application/json")
	if err != nil {
		return err
	}
	var claim struct {
		Job *claimedJob `json:"job"`
	}
	if err := json.Unmarshal(raw, &claim); err != nil {
		return fmt.Errorf("server returned invalid local job payload: %w", err)
	}
	if claim.Job == nil || claim.Job.ID <= 0 || claim.Job.ProjectID <= 0 {
		return nil
	}
	fmt.Printf("Running local compile job #%d for project %d\n", claim.Job.ID, claim.Job.ProjectID)
	compileCfg := config{
		Server:    cfg.Server,
		Token:     token,
		ProjectID: claim.Job.ProjectID,
		JobID:     claim.Job.ID,
		Workspace: cfg.Workspace,
		TypstBin:  cfg.TypstBin,
		Timeout:   cfg.Timeout,
		Pull:      true,
	}
	root, err := workspaceRootFor(cfg.Workspace, claim.Job.ProjectID, "compile")
	if err != nil {
		return err
	}
	meta, err := loadProject(compileCfg, root)
	if err != nil {
		result := compileResult{Status: "error", Log: "SmartTeX local job failed before compile:\n" + err.Error(), ReturnCode: 1}
		_, uploadErr := uploadCompileResult(compileCfg, result)
		if uploadErr != nil {
			return errors.Join(err, uploadErr)
		}
		return nil
	}
	if strings.ToLower(meta.MarkupType) != "typst" {
		result := compileResult{Status: "error", Log: "Local runtime currently supports Typst projects only", ReturnCode: 1}
		_, err := uploadCompileResult(compileCfg, result)
		return err
	}
	mainFile := meta.MainFileName
	if strings.TrimSpace(mainFile) == "" {
		mainFile = "main.typ"
	}
	result := compileTypst(root, mainFile, cfg.TypstBin, cfg.Timeout)
	_, err = uploadCompileResult(compileCfg, result)
	return err
}

func (cfg serveConfig) handlePreview(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodGet {
		http.Error(w, "method not allowed", http.StatusMethodNotAllowed)
		return
	}
	if !cfg.authorizePreviewRequest(r) {
		http.Error(w, "unauthorized", http.StatusUnauthorized)
		return
	}
	projectID := 0
	_, _ = fmt.Sscanf(r.URL.Query().Get("project_id"), "%d", &projectID)
	if projectID <= 0 {
		projectID = previewProjectIDFromReferer(r)
	}
	if projectID <= 0 {
		http.Error(w, "project_id is required", http.StatusBadRequest)
		return
	}
	session, err := cfg.ensurePreview(projectID, previewInvertColorsFromRequest(r))
	if err != nil {
		http.Error(w, err.Error(), http.StatusBadGateway)
		return
	}
	upstreamPath := strings.TrimPrefix(r.URL.Path, "/v1/preview")
	if upstreamPath == "" {
		upstreamPath = "/"
	}
	upstreamURL := fmt.Sprintf("http://127.0.0.1:%d%s", session.Port, upstreamPath)
	if r.URL.RawQuery != "" {
		upstreamURL += "?" + r.URL.RawQuery
	}
	req, err := http.NewRequest(http.MethodGet, upstreamURL, nil)
	if err != nil {
		http.Error(w, err.Error(), http.StatusInternalServerError)
		return
	}
	req.Header.Set("Accept", r.Header.Get("Accept"))
	req.Header.Set("User-Agent", r.Header.Get("User-Agent"))
	resp, err := (&http.Client{Timeout: 30 * time.Second}).Do(req)
	if err != nil {
		http.Error(w, err.Error(), http.StatusBadGateway)
		return
	}
	defer resp.Body.Close()
	body, _ := io.ReadAll(resp.Body)
	contentType := resp.Header.Get("Content-Type")
	if contentType == "" {
		contentType = "text/html; charset=utf-8"
	}
	if upstreamPath == "/" && strings.HasPrefix(strings.ToLower(contentType), "text/html") {
		body = injectLocalPreviewBridge(body, projectID, session.Root)
	}
	for _, header := range []string{"Cache-Control", "ETag", "Last-Modified"} {
		if value := resp.Header.Get(header); value != "" {
			w.Header().Set(header, value)
		}
	}
	w.Header().Set("Content-Type", contentType)
	w.WriteHeader(resp.StatusCode)
	_, _ = w.Write(body)
}

func (cfg serveConfig) handlePreviewRefresh(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		http.Error(w, "method not allowed", http.StatusMethodNotAllowed)
		return
	}
	if !cfg.authorizePreviewRequest(r) {
		http.Error(w, "unauthorized", http.StatusUnauthorized)
		return
	}
	projectID := 0
	_, _ = fmt.Sscanf(r.URL.Query().Get("project_id"), "%d", &projectID)
	if projectID <= 0 {
		http.Error(w, "project_id is required", http.StatusBadRequest)
		return
	}
	session, restarted, err := cfg.restartPreview(projectID, previewInvertColorsFromRequest(r))
	if err != nil {
		http.Error(w, err.Error(), http.StatusBadGateway)
		return
	}
	writeJSON(w, map[string]any{
		"ok":           true,
		"project_id":   projectID,
		"restarted":    restarted,
		"root":         session.Root,
		"root_uri":     fileURI(session.Root),
		"started_at":   session.StartedAt,
		"invertColors": session.InvertColors,
	})
}

func (cfg serveConfig) handlePreviewDataWebSocket(w http.ResponseWriter, r *http.Request) {
	cfg.handlePreviewWebSocket(w, r, false)
}

func (cfg serveConfig) handlePreviewControlWebSocket(w http.ResponseWriter, r *http.Request) {
	cfg.handlePreviewWebSocket(w, r, true)
}

func (cfg serveConfig) handlePreviewWebSocket(w http.ResponseWriter, r *http.Request, control bool) {
	if !cfg.authorizePreviewRequest(r) {
		http.Error(w, "unauthorized", http.StatusUnauthorized)
		return
	}
	projectID := 0
	_, _ = fmt.Sscanf(r.URL.Query().Get("project_id"), "%d", &projectID)
	if projectID <= 0 {
		projectID = previewProjectIDFromReferer(r)
	}
	if projectID <= 0 {
		http.Error(w, "project_id is required", http.StatusBadRequest)
		return
	}
	session, err := cfg.ensurePreview(projectID, previewInvertColorsFromRequest(r))
	if err != nil {
		http.Error(w, err.Error(), http.StatusBadGateway)
		return
	}
	upgrader := websocket.Upgrader{CheckOrigin: cfg.allowWebSocketOrigin}
	browser, err := upgrader.Upgrade(w, r, nil)
	if err != nil {
		return
	}
	defer browser.Close()
	upstreamPort := session.Port
	origin := fmt.Sprintf("http://127.0.0.1:%d", session.Port)
	if control {
		upstreamPort = session.ControlPort
		origin = "vscode-webview://smarttex"
	}
	dialer := websocket.Dialer{}
	upstream, _, err := dialer.Dial(
		fmt.Sprintf("ws://127.0.0.1:%d/", upstreamPort),
		http.Header{"Origin": []string{origin}},
	)
	if err != nil {
		return
	}
	defer upstream.Close()
	proxyWebSockets(browser, upstream)
}

func (cfg serveConfig) handleLSP(w http.ResponseWriter, r *http.Request) {
	if !cfg.authorize(r) && r.URL.Query().Get("secret") != cfg.Secret {
		http.Error(w, "unauthorized", http.StatusUnauthorized)
		return
	}
	projectID := 0
	_, _ = fmt.Sscanf(r.URL.Query().Get("project_id"), "%d", &projectID)
	if projectID <= 0 {
		http.Error(w, "project_id is required", http.StatusBadRequest)
		return
	}
	root, err := workspaceRootFor(cfg.Workspace, projectID, "lsp")
	if err != nil {
		http.Error(w, err.Error(), http.StatusInternalServerError)
		return
	}
	token, err := cfg.authToken()
	if err != nil {
		http.Error(w, err.Error(), http.StatusBadGateway)
		return
	}
	meta, err := loadProject(config{
		Server: cfg.Server, Token: token, ProjectID: projectID,
		Workspace: cfg.Workspace, TypstBin: cfg.TypstBin, Timeout: cfg.Timeout, Pull: true,
	}, root)
	if err != nil {
		http.Error(w, err.Error(), http.StatusBadGateway)
		return
	}
	if strings.ToLower(meta.MarkupType) != "typst" {
		http.Error(w, "local LSP currently supports Typst projects only", http.StatusBadRequest)
		return
	}
	lsp, legend, err := startTinymistLSP(cfg.TinymistBin, root, projectID)
	if err != nil {
		http.Error(w, err.Error(), http.StatusBadGateway)
		return
	}
	defer lsp.close()

	upgrader := websocket.Upgrader{CheckOrigin: cfg.allowWebSocketOrigin}
	ws, err := upgrader.Upgrade(w, r, nil)
	if err != nil {
		return
	}
	defer ws.Close()

	if err := ws.WriteJSON(map[string]any{
		"type":                   "tinymist_connected",
		"project_id":             projectID,
		"root_uri":               fileURI(root),
		"semantic_tokens_legend": legend,
	}); err != nil {
		return
	}

	done := make(chan struct{}, 2)
	go func() {
		defer func() { done <- struct{}{} }()
		for {
			var msg map[string]any
			if err := ws.ReadJSON(&msg); err != nil {
				return
			}
			if err := lsp.write(msg); err != nil {
				return
			}
		}
	}()
	go func() {
		defer func() { done <- struct{}{} }()
		for {
			msg, err := lsp.read()
			if err != nil {
				return
			}
			if err := ws.WriteJSON(msg); err != nil {
				return
			}
		}
	}()
	<-done
}

func (cfg serveConfig) allowWebSocketOrigin(r *http.Request) bool {
	origin := strings.TrimSpace(r.Header.Get("Origin"))
	if origin == "" {
		return true
	}
	originURL, err := url.Parse(origin)
	if err != nil {
		return false
	}
	if originURL.Scheme == "vscode-webview" {
		return true
	}
	if isLoopbackHost(originURL.Hostname()) {
		return true
	}
	serverURL, err := url.Parse(apiURL(cfg.Server, "/"))
	if err == nil && strings.EqualFold(originURL.Scheme, serverURL.Scheme) && strings.EqualFold(originURL.Host, serverURL.Host) {
		return true
	}
	return false
}

func (cfg serveConfig) ensurePreview(projectID int, invertColors string) (*previewSession, error) {
	previewSessionsMu.Lock()
	defer previewSessionsMu.Unlock()

	root, pullPreview, err := cfg.previewRoot(projectID)
	if err != nil {
		return nil, err
	}
	if session := previewSessions[projectID]; session != nil {
		if processAlive(session.Process) {
			if strings.TrimSpace(invertColors) == "" {
				invertColors = session.InvertColors
			} else {
				invertColors = normalizePreviewInvertColors(invertColors)
			}
			if session.InvertColors == invertColors && session.Root == root {
				return session, nil
			}
			_ = session.Process.Kill()
		}
		delete(previewSessions, projectID)
	}
	invertColors = normalizePreviewInvertColors(invertColors)
	token, err := cfg.authToken()
	if err != nil {
		return nil, err
	}
	compileCfg := config{
		Server: cfg.Server, Token: token, ProjectID: projectID,
		Workspace: cfg.Workspace, TypstBin: cfg.TypstBin, Timeout: cfg.Timeout, Pull: pullPreview,
	}
	meta, err := loadProject(compileCfg, root)
	if err != nil {
		return nil, err
	}
	mainFile := meta.MainFileName
	if strings.TrimSpace(mainFile) == "" {
		mainFile = "main.typ"
	}
	port, err := reserveLocalPort()
	if err != nil {
		return nil, err
	}
	controlPort, err := reserveLocalPort()
	if err != nil {
		return nil, err
	}
	cmd := exec.Command(
		cfg.TinymistBin,
		"preview",
		filepath.Join(root, filepath.FromSlash(mainFile)),
		"--root="+root,
		"--partial-rendering=true",
		fmt.Sprintf("--data-plane-host=127.0.0.1:%d", port),
		fmt.Sprintf("--control-plane-host=127.0.0.1:%d", controlPort),
		fmt.Sprintf("--invert-colors=%s", invertColors),
		"--no-open",
	)
	cmd.Dir = root
	cmd.Stdout = io.Discard
	stderr, closeStderr := previewLogWriter(root)
	readyWatcher := newPreviewReadyWatcher(port, controlPort)
	cmd.Stderr = io.MultiWriter(stderr, readyWatcher)
	if err := cmd.Start(); err != nil {
		closeStderr()
		return nil, err
	}
	if err := readyWatcher.wait(20 * time.Second); err != nil {
		_ = cmd.Process.Kill()
		closeStderr()
		return nil, err
	}
	session := &previewSession{
		ProjectID:    projectID,
		Root:         root,
		Port:         port,
		ControlPort:  controlPort,
		InvertColors: invertColors,
		Process:      cmd.Process,
		StartedAt:    time.Now(),
	}
	previewSessions[projectID] = session
	go func() {
		defer closeStderr()
		_ = cmd.Wait()
		previewSessionsMu.Lock()
		if previewSessions[projectID] == session {
			delete(previewSessions, projectID)
		}
		previewSessionsMu.Unlock()
	}()
	return session, nil
}

// restartPreview rebuilds the preview process when its content is backed by a
// pulled server snapshot. When the preview is backed by the live local
// workspace, tinymist already watches the files on disk and streams incremental
// updates over the data WebSocket, so killing and relaunching it would only
// blank the preview and drop the connection. The returned bool reports whether
// the process was actually restarted, so the client knows whether it must
// reconnect (reload the iframe) or can keep its live session untouched.
func (cfg serveConfig) restartPreview(projectID int, invertColors string) (*previewSession, bool, error) {
	_, pull, err := cfg.previewRoot(projectID)
	if err != nil {
		return nil, false, err
	}
	if !pull {
		session, err := cfg.ensurePreview(projectID, invertColors)
		return session, false, err
	}
	previewSessionsMu.Lock()
	if session := previewSessions[projectID]; session != nil {
		if strings.TrimSpace(invertColors) == "" {
			invertColors = session.InvertColors
		}
		_ = session.Process.Kill()
		delete(previewSessions, projectID)
	}
	previewSessionsMu.Unlock()
	session, err := cfg.ensurePreview(projectID, invertColors)
	return session, true, err
}

func (cfg serveConfig) previewRoot(projectID int) (root string, pull bool, err error) {
	workspaceRoot, err := workspaceRootFor(cfg.Workspace, projectID, "workspace")
	if err != nil {
		return "", false, err
	}
	if state, stateErr := loadWorkspaceState(workspaceRoot); stateErr == nil && state.ProjectID == projectID && state.WorkspaceID != "" {
		return workspaceRoot, false, nil
	}
	previewRoot, err := workspaceRootFor(cfg.Workspace, projectID, "preview")
	if err != nil {
		return "", false, err
	}
	return previewRoot, true, nil
}

func writeJSON(w http.ResponseWriter, payload any) {
	w.Header().Set("Content-Type", "application/json; charset=utf-8")
	_ = json.NewEncoder(w).Encode(payload)
}

func startTinymistLSP(tinymistBin, root string, projectID int) (*lspProcess, map[string]any, error) {
	cmd := exec.Command(tinymistBin, "lsp")
	cmd.Dir = root
	stdin, err := cmd.StdinPipe()
	if err != nil {
		return nil, nil, err
	}
	stdout, err := cmd.StdoutPipe()
	if err != nil {
		return nil, nil, err
	}
	cmd.Stderr = os.Stderr
	if err := cmd.Start(); err != nil {
		return nil, nil, err
	}
	lsp := &lspProcess{cmd: cmd, stdin: stdin, stdout: bufio.NewReader(stdout)}
	legend, err := lsp.handshake(root, projectID)
	if err != nil {
		lsp.close()
		return nil, nil, err
	}
	return lsp, legend, nil
}

func (l *lspProcess) handshake(root string, projectID int) (map[string]any, error) {
	rootURI := fileURI(root)
	initID := 1
	if err := l.write(map[string]any{
		"jsonrpc": "2.0",
		"id":      initID,
		"method":  "initialize",
		"params": map[string]any{
			"processId": os.Getpid(),
			"rootUri":   rootURI,
			"capabilities": map[string]any{
				"textDocument": map[string]any{
					"synchronization": map[string]any{"didSave": true},
					"completion": map[string]any{
						"completionItem": map[string]any{"snippetSupport": true, "labelDetailsSupport": true},
						"contextSupport": true,
					},
					"signatureHelp": map[string]any{"signatureInformation": map[string]any{
						"documentationFormat":    []string{"markdown", "plaintext"},
						"parameterInformation":   map[string]any{"labelOffsetSupport": true},
						"activeParameterSupport": true,
					}},
					"hover":              map[string]any{"contentFormat": []string{"markdown", "plaintext"}},
					"definition":         map[string]any{},
					"references":         map[string]any{},
					"documentSymbol":     map[string]any{},
					"documentLink":       map[string]any{},
					"rename":             map[string]any{"prepareSupport": false},
					"publishDiagnostics": map[string]any{"relatedInformation": true},
					"formatting":         map[string]any{},
					"foldingRange":       map[string]any{"lineFoldingOnly": false},
					"semanticTokens": map[string]any{
						"formats":                 []string{"relative"},
						"requests":                map[string]any{"full": true},
						"multilineTokenSupport":   false,
						"overlappingTokenSupport": false,
						"tokenTypes": []string{
							"namespace", "type", "class", "enum", "interface", "struct", "typeParameter", "parameter",
							"variable", "property", "enumMember", "event", "function", "method", "macro", "keyword",
							"modifier", "comment", "string", "number", "regexp", "operator", "decorator",
						},
						"tokenModifiers": []string{
							"declaration", "definition", "readonly", "static", "deprecated", "abstract", "async",
							"modification", "documentation", "defaultLibrary",
						},
					},
				},
				"workspace": map[string]any{"workspaceFolders": true, "symbol": map[string]any{}},
			},
			"workspaceFolders":      []map[string]any{{"uri": rootURI, "name": fmt.Sprintf("project-%d", projectID)}},
			"initializationOptions": map[string]any{"settings": map[string]any{}},
		},
	}); err != nil {
		return nil, err
	}
	deadline := time.Now().Add(30 * time.Second)
	for time.Now().Before(deadline) {
		msg, err := l.read()
		if err != nil {
			return nil, err
		}
		if intFromAny(msg["id"]) != initID {
			continue
		}
		if rawErr, ok := msg["error"].(map[string]any); ok {
			return nil, fmt.Errorf("tinymist initialize failed: %v", rawErr["message"])
		}
		legend := semanticLegendFromInitializeResult(msg["result"])
		_ = l.write(map[string]any{"jsonrpc": "2.0", "method": "initialized", "params": map[string]any{}})
		_ = l.write(map[string]any{
			"jsonrpc": "2.0",
			"method":  "workspace/didChangeConfiguration",
			"params":  map[string]any{"settings": map[string]any{}},
		})
		return legend, nil
	}
	return nil, errors.New("tinymist initialize timed out")
}

func (l *lspProcess) write(msg map[string]any) error {
	raw, err := json.Marshal(msg)
	if err != nil {
		return err
	}
	l.writeMu.Lock()
	defer l.writeMu.Unlock()
	_, err = fmt.Fprintf(l.stdin, "Content-Length: %d\r\n\r\n%s", len(raw), raw)
	return err
}

func (l *lspProcess) read() (map[string]any, error) {
	contentLength := -1
	for {
		line, err := l.stdout.ReadString('\n')
		if err != nil {
			return nil, err
		}
		line = strings.TrimRight(line, "\r\n")
		if line == "" {
			break
		}
		parts := strings.SplitN(line, ":", 2)
		if len(parts) == 2 && strings.EqualFold(strings.TrimSpace(parts[0]), "Content-Length") {
			contentLength, _ = strconv.Atoi(strings.TrimSpace(parts[1]))
		}
	}
	if contentLength <= 0 {
		return nil, errors.New("tinymist sent message without Content-Length")
	}
	raw := make([]byte, contentLength)
	if _, err := io.ReadFull(l.stdout, raw); err != nil {
		return nil, err
	}
	var msg map[string]any
	if err := json.Unmarshal(raw, &msg); err != nil {
		return nil, err
	}
	return msg, nil
}

func (l *lspProcess) close() {
	if l == nil {
		return
	}
	if l.stdin != nil {
		_ = l.stdin.Close()
	}
	if l.cmd != nil && l.cmd.Process != nil {
		_ = l.cmd.Process.Kill()
		_, _ = l.cmd.Process.Wait()
	}
}

func semanticLegendFromInitializeResult(raw any) map[string]any {
	result, _ := raw.(map[string]any)
	caps, _ := result["capabilities"].(map[string]any)
	provider, _ := caps["semanticTokensProvider"].(map[string]any)
	legend, _ := provider["legend"].(map[string]any)
	tokenTypes, _ := legend["tokenTypes"].([]any)
	tokenModifiers, _ := legend["tokenModifiers"].([]any)
	return map[string]any{
		"tokenTypes":     tokenTypes,
		"tokenModifiers": tokenModifiers,
	}
}

func intFromAny(value any) int {
	switch v := value.(type) {
	case int:
		return v
	case float64:
		return int(v)
	case json.Number:
		i, _ := v.Int64()
		return int(i)
	default:
		return 0
	}
}

func fileURI(path string) string {
	abs, err := filepath.Abs(path)
	if err != nil {
		abs = path
	}
	return (&url.URL{Scheme: "file", Path: filepath.ToSlash(abs)}).String()
}

func previewProjectIDFromReferer(r *http.Request) int {
	ref := strings.TrimSpace(r.Header.Get("Referer"))
	if ref == "" {
		return 0
	}
	parsed, err := url.Parse(ref)
	if err != nil {
		return 0
	}
	value := parsed.Query().Get("project_id")
	if value == "" {
		return 0
	}
	projectID, _ := strconv.Atoi(value)
	return projectID
}

func previewInvertColorsFromRequest(r *http.Request) string {
	for _, key := range []string{"theme", "preview_theme", "invert_colors"} {
		if value := r.URL.Query().Get(key); strings.TrimSpace(value) != "" {
			return normalizePreviewInvertColors(value)
		}
	}
	ref := strings.TrimSpace(r.Header.Get("Referer"))
	if ref == "" {
		return ""
	}
	parsed, err := url.Parse(ref)
	if err != nil {
		return ""
	}
	for _, key := range []string{"theme", "preview_theme", "invert_colors"} {
		if value := parsed.Query().Get(key); strings.TrimSpace(value) != "" {
			return normalizePreviewInvertColors(value)
		}
	}
	return ""
}

func normalizePreviewInvertColors(value string) string {
	switch strings.ToLower(strings.TrimSpace(value)) {
	case "always", "dark":
		return "always"
	case "never", "light":
		return "never"
	default:
		return "auto"
	}
}

func injectLocalPreviewBridge(body []byte, projectID int, root string) []byte {
	html := string(body)
	bridge := strings.ReplaceAll(localPreviewBridgeScript, "__SMARTTEX_PREVIEW_PROJECT_ID__", strconv.Itoa(projectID))
	bridge = strings.ReplaceAll(bridge, "__SMARTTEX_PREVIEW_ROOT_URI__", fileURI(root))
	lower := strings.ToLower(html)
	marker := "</body>"
	idx := strings.LastIndex(lower, marker)
	if idx == -1 {
		return []byte(html + bridge)
	}
	return []byte(html[:idx] + bridge + html[idx:])
}

func proxyWebSockets(left, right *websocket.Conn) {
	done := make(chan struct{}, 2)
	copyConn := func(dst, src *websocket.Conn) {
		defer func() { done <- struct{}{} }()
		for {
			messageType, payload, err := src.ReadMessage()
			if err != nil {
				return
			}
			if err := dst.WriteMessage(messageType, payload); err != nil {
				return
			}
		}
	}
	go copyConn(right, left)
	go copyConn(left, right)
	<-done
}

func loadProject(cfg config, root string) (projectMeta, error) {
	raw, err := apiRequest("GET", cfg.Server, fmt.Sprintf("/api/projects/%d/", cfg.ProjectID), cfg.Token, nil, "")
	if err != nil {
		return projectMeta{}, err
	}
	var meta projectMeta
	if err := json.Unmarshal(raw, &meta); err != nil {
		return projectMeta{}, fmt.Errorf("server returned invalid project metadata: %w", err)
	}
	if cfg.Pull {
		unlock := lockWorkspace(root)
		defer unlock()
		archive, err := apiRequest("GET", cfg.Server, fmt.Sprintf("/api/projects/%d/download-zip/?compile_support=1", cfg.ProjectID), cfg.Token, nil, "")
		if err != nil {
			return projectMeta{}, err
		}
		if err := removeAllRetry(root); err != nil {
			return projectMeta{}, err
		}
		if err := os.MkdirAll(root, 0o755); err != nil {
			return projectMeta{}, err
		}
		if err := safeExtractZip(archive, root); err != nil {
			return projectMeta{}, err
		}
	} else if _, err := os.Stat(root); err != nil {
		return projectMeta{}, fmt.Errorf("workspace does not exist: %s. Run without --no-pull first", root)
	}
	return meta, nil
}

func workspaceConfig(server, token string, projectID int, workspace string) (config, string, error) {
	cfg := config{
		Server:    server,
		ProjectID: projectID,
		Workspace: workspace,
		Pull:      true,
	}
	if cfg.ProjectID <= 0 {
		return config{}, "", errors.New("--project is required")
	}
	resolvedToken, err := resolveToken(cfg.Server, token)
	if err != nil {
		return config{}, "", err
	}
	cfg.Token = resolvedToken
	root, err := workspaceRootFor(cfg.Workspace, cfg.ProjectID, "workspace")
	if err != nil {
		return config{}, "", err
	}
	return cfg, root, nil
}

func claimWorkspaceLease(cfg config, root, agentID, existingWorkspaceID string) (workspaceState, error) {
	state, _ := loadWorkspaceState(root)
	workspaceID := strings.TrimSpace(existingWorkspaceID)
	if workspaceID == "" {
		workspaceID = strings.TrimSpace(state.WorkspaceID)
	}
	if workspaceID == "" {
		workspaceID = randomURLSafe(18)
	}
	body := map[string]any{
		"workspace_id":        workspaceID,
		"agent_id":            agentID,
		"base_version_number": state.BaseVersionNumber,
		"ttl_seconds":         180,
	}
	raw, err := apiJSON("POST", cfg.Server, fmt.Sprintf("/api/projects/%d/local-workspace/", cfg.ProjectID), cfg.Token, body)
	if err != nil {
		return workspaceState{}, err
	}
	var payload struct {
		WorkspaceID         string `json:"workspace_id"`
		LatestVersionNumber int    `json:"latest_version_number"`
		BaseVersionNumber   int    `json:"base_version_number"`
	}
	if err := json.Unmarshal(raw, &payload); err != nil {
		return workspaceState{}, err
	}
	files, err := scanWorkspaceFiles(root)
	if err != nil {
		return workspaceState{}, err
	}
	hashes := make(map[string]string, len(files))
	for path, item := range files {
		hashes[path] = item.Hash
	}
	if payload.WorkspaceID != "" {
		workspaceID = payload.WorkspaceID
	}
	version := payload.LatestVersionNumber
	if version == 0 {
		version = payload.BaseVersionNumber
	}
	return workspaceState{
		ProjectID:          cfg.ProjectID,
		Server:             cfg.Server,
		WorkspaceID:        workspaceID,
		BaseVersionNumber:  version,
		LastSyncUnixMillis: time.Now().UnixMilli(),
		Files:              hashes,
	}, nil
}

func syncWorkspaceOnce(cfg config, root string, state workspaceState, agentID string, force bool) (string, error) {
	return syncWorkspaceOnceRetry(cfg, root, state, agentID, force, true)
}

func syncWorkspaceOnceRetry(cfg config, root string, state workspaceState, agentID string, force bool, retryInactiveLease bool) (string, error) {
	if state.WorkspaceID == "" {
		return "", errors.New("workspace is not opened yet; run `smarttex-local workspace open --project ID` first")
	}
	current, err := scanWorkspaceFiles(root)
	if err != nil {
		return "", err
	}
	changes := workspaceChanges(state, current)
	body := map[string]any{
		"workspace_id":        state.WorkspaceID,
		"base_version_number": state.BaseVersionNumber,
		"changes":             changes,
		"ttl_seconds":         180,
		"summary":             fmt.Sprintf("Synced local workspace from %s", agentID),
		"force":               force,
	}
	raw, err := apiJSON("POST", cfg.Server, fmt.Sprintf("/api/projects/%d/local-workspace/sync/", cfg.ProjectID), cfg.Token, body)
	if err != nil {
		if retryInactiveLease && isLocalWorkspaceNotActiveError(err) {
			claimed, claimErr := claimWorkspaceLease(cfg, root, agentID, state.WorkspaceID)
			if claimErr != nil {
				return "", err
			}
			retryState := state
			retryState.WorkspaceID = claimed.WorkspaceID
			if retryState.BaseVersionNumber == 0 {
				retryState.BaseVersionNumber = claimed.BaseVersionNumber
			}
			return syncWorkspaceOnceRetry(cfg, root, retryState, agentID, force, false)
		}
		return "", err
	}
	var payload struct {
		LatestVersionNumber int      `json:"latest_version_number"`
		ChangedPaths        []string `json:"changed_paths"`
	}
	if err := json.Unmarshal(raw, &payload); err != nil {
		return "", err
	}
	next := workspaceState{
		ProjectID:          cfg.ProjectID,
		Server:             cfg.Server,
		WorkspaceID:        state.WorkspaceID,
		BaseVersionNumber:  payload.LatestVersionNumber,
		LastSyncUnixMillis: time.Now().UnixMilli(),
		Files:              map[string]string{},
	}
	for path, item := range current {
		next.Files[path] = item.Hash
	}
	if err := saveWorkspaceState(root, next); err != nil {
		return "", err
	}
	if len(payload.ChangedPaths) == 0 {
		return "", nil
	}
	return fmt.Sprintf("Synced %d file(s): %s", len(payload.ChangedPaths), strings.Join(payload.ChangedPaths, ", ")), nil
}

func isLocalWorkspaceNotActiveError(err error) bool {
	if err == nil {
		return false
	}
	return strings.Contains(err.Error(), "LOCAL_WORKSPACE_NOT_ACTIVE")
}

func workspaceChanges(state workspaceState, current map[string]workspaceFileSnapshot) []map[string]any {
	prev := state.Files
	if prev == nil {
		prev = map[string]string{}
	}
	changes := []map[string]any{}
	for path, item := range current {
		if prev[path] == item.Hash {
			continue
		}
		change := map[string]any{
			"path":    path,
			"action":  "upsert",
			"is_text": item.IsText,
		}
		if item.IsText {
			change["content"] = item.Content
		} else {
			change["content_base64"] = base64.StdEncoding.EncodeToString(item.RawBytes)
		}
		changes = append(changes, change)
	}
	for path := range prev {
		if shouldSkipWorkspacePath(path, false) {
			continue
		}
		if _, ok := current[path]; !ok {
			changes = append(changes, map[string]any{"path": path, "action": "delete"})
		}
	}
	return changes
}

func localWorkspaceChangeCount(root string) (int, error) {
	if _, err := os.Stat(root); os.IsNotExist(err) {
		return 0, nil
	} else if err != nil {
		return 0, err
	}
	current, err := scanWorkspaceFiles(root)
	if err != nil {
		return 0, err
	}
	state, err := loadWorkspaceState(root)
	if err != nil || state.Files == nil {
		return len(current), nil
	}
	return len(workspaceChanges(state, current)), nil
}

func workspaceStatePath(root string) string {
	return filepath.Join(root, ".smarttex", "local_workspace_state.json")
}

func loadWorkspaceState(root string) (workspaceState, error) {
	raw, err := os.ReadFile(workspaceStatePath(root))
	if err != nil {
		return workspaceState{}, err
	}
	var state workspaceState
	if err := json.Unmarshal(raw, &state); err != nil {
		return workspaceState{}, err
	}
	if state.Files == nil {
		state.Files = map[string]string{}
	}
	return state, nil
}

func saveWorkspaceState(root string, state workspaceState) error {
	path := workspaceStatePath(root)
	if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
		return err
	}
	raw, err := json.MarshalIndent(state, "", "  ")
	if err != nil {
		return err
	}
	return os.WriteFile(path, raw, 0o600)
}

func scanWorkspaceFiles(root string) (map[string]workspaceFileSnapshot, error) {
	items := map[string]workspaceFileSnapshot{}
	err := filepath.WalkDir(root, func(path string, entry os.DirEntry, err error) error {
		if err != nil {
			return err
		}
		if path == root {
			return nil
		}
		rel, err := filepath.Rel(root, path)
		if err != nil {
			return err
		}
		rel = filepath.ToSlash(rel)
		if shouldSkipWorkspacePath(rel, entry.IsDir()) {
			if entry.IsDir() {
				return filepath.SkipDir
			}
			return nil
		}
		if entry.IsDir() {
			return nil
		}
		raw, err := os.ReadFile(path)
		if err != nil {
			return err
		}
		sum := sha256.Sum256(raw)
		isText := isWorkspaceTextFile(rel)
		item := workspaceFileSnapshot{Path: rel, Hash: fmt.Sprintf("%x", sum[:]), IsText: isText, RawBytes: raw}
		if isText {
			item.Content = string(raw)
			item.RawBytes = nil
		}
		items[rel] = item
		return nil
	})
	return items, err
}

func shouldSkipWorkspacePath(rel string, isDir bool) bool {
	parts := strings.Split(rel, "/")
	if len(parts) == 0 {
		return true
	}
	switch parts[0] {
	case ".git", ".smarttex-git", "__MACOSX":
		return true
	case ".smarttex":
		// The server only accepts hidden paths under .smarttex/context/ (see
		// projects/services.py). Everything else under .smarttex/ is local-only
		// state and artifacts — workspace lease state, compile output (main.pdf,
		// main.log), caches, and the tinymist preview log — and must never be
		// synced, or the server rejects the whole push with "hidden files not
		// allowed". Allow-list context only; skip the rest.
		if len(parts) == 1 {
			return false
		}
		if parts[1] == "context" {
			return false
		}
		return true
	}
	if strings.HasPrefix(parts[0], ".") {
		return true
	}
	if isDir {
		return false
	}
	ext := strings.ToLower(filepath.Ext(rel))
	if ext == ".aux" || ext == ".out" || ext == ".toc" || ext == ".fls" || ext == ".fdb_latexmk" || ext == ".xdv" || ext == ".bbl" || ext == ".blg" || ext == ".nav" || ext == ".snm" || ext == ".vrb" || ext == ".log" {
		return true
	}
	return false
}

func isWorkspaceTextFile(path string) bool {
	switch strings.ToLower(filepath.Ext(path)) {
	case ".tex", ".typ", ".sty", ".cls", ".bib", ".txt", ".md", ".csv", ".json", ".yaml", ".yml", ".csl", ".puml":
		return true
	default:
		return false
	}
}

func apiJSON(method, server, path, token string, payload any) ([]byte, error) {
	raw, err := json.Marshal(payload)
	if err != nil {
		return nil, err
	}
	return apiRequest(method, server, path, token, bytes.NewReader(raw), "application/json")
}

func prettyJSON(raw []byte) []byte {
	var out bytes.Buffer
	if err := json.Indent(&out, raw, "", "  "); err != nil {
		return raw
	}
	return out.Bytes()
}

func numberFromMap(values map[string]any, key string) float64 {
	switch value := values[key].(type) {
	case float64:
		return value
	case int:
		return float64(value)
	case json.Number:
		out, _ := value.Float64()
		return out
	default:
		return 0
	}
}

func stringFromMap(values map[string]any, key string) string {
	switch value := values[key].(type) {
	case string:
		return value
	case fmt.Stringer:
		return value.String()
	default:
		return ""
	}
}

func boolFromMap(values map[string]any, key string) bool {
	value, _ := values[key].(bool)
	return value
}

func openVSCodeWorkspace(root string) error {
	if path, err := exec.LookPath("code"); err == nil {
		return exec.Command(path, root).Start()
	}
	if runtime.GOOS == "darwin" {
		if err := exec.Command("open", "-a", "Visual Studio Code", root).Start(); err == nil {
			return nil
		}
		if err := exec.Command("open", root).Start(); err == nil {
			return nil
		}
	}
	return errors.New("VS Code CLI `code` is not available in PATH")
}

func lockWorkspace(root string) func() {
	key, err := filepath.Abs(root)
	if err != nil {
		key = root
	}
	workspaceLocksMu.Lock()
	lock := workspaceLocks[key]
	if lock == nil {
		lock = &sync.Mutex{}
		workspaceLocks[key] = lock
	}
	workspaceLocksMu.Unlock()
	lock.Lock()
	return lock.Unlock
}

func removeAllRetry(path string) error {
	var lastErr error
	for attempt := 0; attempt < 5; attempt++ {
		if err := os.RemoveAll(path); err != nil {
			lastErr = err
			time.Sleep(time.Duration(attempt+1) * 90 * time.Millisecond)
			continue
		}
		if _, err := os.Stat(path); os.IsNotExist(err) {
			return nil
		} else if err != nil {
			return err
		}
		lastErr = fmt.Errorf("remove %s: directory still exists", path)
		time.Sleep(time.Duration(attempt+1) * 90 * time.Millisecond)
	}
	return lastErr
}

func newPreviewReadyWatcher(dataPort, controlPort int) *previewReadyWatcher {
	return &previewReadyWatcher{
		dataPort:     dataPort,
		controlPort:  controlPort,
		dataReady:    make(chan struct{}),
		controlReady: make(chan struct{}),
	}
}

func (watcher *previewReadyWatcher) Write(p []byte) (int, error) {
	text := string(p)
	for _, line := range strings.Split(text, "\n") {
		line = strings.TrimSpace(line)
		if line == "" {
			continue
		}
		watcher.mu.Lock()
		watcher.lines = append(watcher.lines, line)
		if len(watcher.lines) > 40 {
			watcher.lines = watcher.lines[len(watcher.lines)-40:]
		}
		watcher.mu.Unlock()
		watcher.markReadyFromLine(line)
	}
	return len(p), nil
}

func (watcher *previewReadyWatcher) markReadyFromLine(line string) {
	const marker = "listening on http://127.0.0.1:"
	idx := strings.Index(line, marker)
	if idx < 0 {
		return
	}
	rest := line[idx+len(marker):]
	end := 0
	for end < len(rest) && rest[end] >= '0' && rest[end] <= '9' {
		end++
	}
	if end == 0 {
		return
	}
	port, err := strconv.Atoi(rest[:end])
	if err != nil {
		return
	}
	if port == watcher.dataPort {
		watcher.dataOnce.Do(func() { close(watcher.dataReady) })
	}
	if port == watcher.controlPort {
		watcher.controlOnce.Do(func() { close(watcher.controlReady) })
	}
}

func (watcher *previewReadyWatcher) wait(timeout time.Duration) error {
	timer := time.NewTimer(timeout)
	defer timer.Stop()
	dataReady := false
	controlReady := false
	for !dataReady || !controlReady {
		select {
		case <-watcher.dataReady:
			dataReady = true
		case <-watcher.controlReady:
			controlReady = true
		case <-timer.C:
			return fmt.Errorf("tinymist preview did not become ready (data=%t control=%t). %s", dataReady, controlReady, watcher.tail())
		}
	}
	return nil
}

func (watcher *previewReadyWatcher) tail() string {
	watcher.mu.Lock()
	defer watcher.mu.Unlock()
	if len(watcher.lines) == 0 {
		return ""
	}
	return "stderr: " + strings.Join(watcher.lines, " | ")
}

func previewLogWriter(root string) (io.Writer, func()) {
	dir := filepath.Join(root, ".smarttex")
	if err := os.MkdirAll(dir, 0o755); err != nil {
		return os.Stderr, func() {}
	}
	path := filepath.Join(dir, "tinymist-preview.log")
	file, err := os.OpenFile(path, os.O_CREATE|os.O_WRONLY|os.O_TRUNC, 0o644)
	if err != nil {
		return os.Stderr, func() {}
	}
	return file, func() { _ = file.Close() }
}

func compileTypst(root, mainFile, typstBin string, timeout time.Duration) compileResult {
	outDir := filepath.Join(root, ".smarttex")
	_ = os.MkdirAll(outDir, 0o755)
	pdfPath := filepath.Join(outDir, "main.pdf")
	_ = os.Remove(pdfPath)

	ctxTimeout := timeout
	started := time.Now()
	cmd := exec.Command(typstBin, "compile", "--root", ".", filepath.FromSlash(mainFile), filepath.ToSlash(filepath.Join(".smarttex", "main.pdf")))
	cmd.Dir = root
	var stdout, stderr bytes.Buffer
	cmd.Stdout = &stdout
	cmd.Stderr = &stderr

	done := make(chan error, 1)
	go func() { done <- cmd.Run() }()
	var err error
	timedOut := false
	select {
	case err = <-done:
	case <-time.After(ctxTimeout):
		timedOut = true
		if cmd.Process != nil {
			_ = cmd.Process.Kill()
		}
		err = fmt.Errorf("compile timed out after %s", timeout)
	}

	returnCode := 0
	if err != nil {
		returnCode = 1
		var exitErr *exec.ExitError
		if errors.As(err, &exitErr) {
			returnCode = exitErr.ExitCode()
		}
		if timedOut {
			returnCode = 124
		}
	}
	pdf, _ := os.ReadFile(pdfPath)
	status := "error"
	if returnCode == 0 && len(pdf) > 0 {
		status = "success"
	}
	log := strings.Join([]string{
		"=== SmartTeX local compile debug ===",
		"tool=smarttex-local-go " + toolVersion,
		"workspace=" + root,
		"cmd=" + strings.Join(cmd.Args, " "),
		fmt.Sprintf("elapsed_ms=%d", time.Since(started).Milliseconds()),
		"=== compiler stdout ===",
		stdout.String(),
		"=== compiler stderr ===",
		stderr.String(),
		"=== compiler result ===",
		fmt.Sprintf("returncode=%d", returnCode),
		fmt.Sprintf("pdf_exists=%t", len(pdf) > 0),
		fmt.Sprintf("pdf_size=%d", len(pdf)),
	}, "\n")
	return compileResult{Status: status, PDF: pdf, Log: limitCompileLog(log, maxUploadedCompileLogBytes), ReturnCode: returnCode}
}

func limitCompileLog(log string, maxBytes int) string {
	if maxBytes <= 0 || len([]byte(log)) <= maxBytes {
		return log
	}
	raw := []byte(log)
	marker := []byte(fmt.Sprintf("\n\n=== SmartTeX local compile log truncated to %d bytes ===\n", maxBytes))
	if len(marker)+256 >= maxBytes {
		return string(raw[:maxBytes])
	}
	headBytes := (maxBytes - len(marker)) / 2
	tailBytes := maxBytes - len(marker) - headBytes
	head := strings.TrimRight(string(raw[:headBytes]), "\x00")
	tail := strings.TrimLeft(string(raw[len(raw)-tailBytes:]), "\x00")
	return head + string(marker) + tail
}

func uploadCompileResult(cfg config, result compileResult) ([]byte, error) {
	var body bytes.Buffer
	writer := multipart.NewWriter(&body)
	fields := map[string]string{
		"status":       result.Status,
		"tool":         "smarttex-local-go",
		"tool_version": toolVersion,
		"compiler":     cfg.TypstBin,
		"returncode":   fmt.Sprintf("%d", result.ReturnCode),
		"diagnostics":  "[]",
	}
	if cfg.JobID > 0 {
		fields["job_id"] = fmt.Sprintf("%d", cfg.JobID)
	}
	for key, value := range fields {
		if err := writer.WriteField(key, value); err != nil {
			return nil, err
		}
	}
	if err := writeMultipartFile(writer, "log", "main.log", "text/plain; charset=utf-8", []byte(result.Log)); err != nil {
		return nil, err
	}
	if len(result.PDF) > 0 {
		if err := writeMultipartFile(writer, "pdf", "main.pdf", "application/pdf", result.PDF); err != nil {
			return nil, err
		}
	}
	if err := writer.Close(); err != nil {
		return nil, err
	}
	return apiRequest(
		"POST",
		cfg.Server,
		fmt.Sprintf("/api/projects/%d/compile/local-result/", cfg.ProjectID),
		cfg.Token,
		&body,
		writer.FormDataContentType(),
	)
}

func writeMultipartFile(writer *multipart.Writer, field, filename, contentType string, data []byte) error {
	header := make(textproto.MIMEHeader)
	header.Set("Content-Disposition", fmt.Sprintf(`form-data; name="%s"; filename="%s"`, field, filename))
	header.Set("Content-Type", contentType)
	part, err := writer.CreatePart(header)
	if err != nil {
		return err
	}
	_, err = part.Write(data)
	return err
}

func apiRequest(method, server, path, token string, body io.Reader, contentType string) ([]byte, error) {
	client := &http.Client{Timeout: 120 * time.Second}
	req, err := http.NewRequest(method, apiURL(server, path), body)
	if err != nil {
		return nil, err
	}
	req.Header.Set("Accept", "application/json")
	if strings.TrimSpace(token) != "" {
		req.Header.Set("Authorization", "Bearer "+token)
	}
	if contentType != "" {
		req.Header.Set("Content-Type", contentType)
	}
	resp, err := client.Do(req)
	if err != nil {
		return nil, fmt.Errorf("cannot reach SmartTeX server: %w", err)
	}
	defer resp.Body.Close()
	raw, _ := io.ReadAll(resp.Body)
	if resp.StatusCode < 200 || resp.StatusCode >= 300 {
		return nil, fmt.Errorf("HTTP %d %s: %s", resp.StatusCode, path, string(raw))
	}
	return raw, nil
}

func apiURL(server, path string) string {
	root := strings.TrimRight(strings.TrimSpace(server), "/")
	if root == "" {
		root = defaultServer()
	}
	if !strings.HasPrefix(root, "http://") && !strings.HasPrefix(root, "https://") {
		root = "https://" + root
	}
	return root + path
}

func fetchUpdateManifest(server, channel string) (updateManifest, *updateAsset, error) {
	manifest, err := fetchManifest(server, channel)
	if err != nil {
		return updateManifest{}, nil, err
	}
	for i := range manifest.Assets {
		asset := &manifest.Assets[i]
		if asset.OS == runtime.GOOS && asset.Arch == runtime.GOARCH {
			return manifest, asset, nil
		}
	}
	return manifest, nil, nil
}

func fetchManifest(server, channel string) (updateManifest, error) {
	manifestPath := fmt.Sprintf("/static/local-agent/%s/manifest.json", url.PathEscape(strings.TrimSpace(channel)))
	raw, err := apiRequest("GET", server, manifestPath, "", nil, "")
	if err != nil {
		return updateManifest{}, err
	}
	var manifest updateManifest
	if err := json.Unmarshal(raw, &manifest); err != nil {
		return updateManifest{}, fmt.Errorf("server returned invalid local agent manifest: %w", err)
	}
	return manifest, nil
}

func installVSCodeExtensionFromManifest(server string, asset *vscodeExtensionAsset) error {
	if asset == nil || strings.TrimSpace(asset.URL) == "" {
		return nil
	}
	codePath, err := findVSCodeCLI()
	if err != nil {
		return fmt.Errorf("%w; VSIX: %s", err, absoluteAssetURL(server, asset.URL))
	}
	raw, err := downloadUpdateAsset(server, asset.URL)
	if err != nil {
		return err
	}
	if asset.SHA256 != "" {
		sum := sha256.Sum256(raw)
		actual := fmt.Sprintf("%x", sum[:])
		if !strings.EqualFold(actual, asset.SHA256) {
			return fmt.Errorf("downloaded VS Code extension checksum mismatch: got %s, expected %s", actual, asset.SHA256)
		}
	}
	tmp, err := os.CreateTemp("", "smarttex-vscode-*.vsix")
	if err != nil {
		return err
	}
	tmpPath := tmp.Name()
	defer func() { _ = os.Remove(tmpPath) }()
	if _, err := tmp.Write(raw); err != nil {
		_ = tmp.Close()
		return err
	}
	if err := tmp.Close(); err != nil {
		return err
	}
	cmd := exec.Command(codePath, "--install-extension", tmpPath, "--force")
	cmd.Stdout = os.Stdout
	cmd.Stderr = os.Stderr
	if err := cmd.Run(); err != nil {
		return fmt.Errorf("code --install-extension failed: %w", err)
	}
	fmt.Printf("Installed SmartTeX VS Code extension %s.\n", firstNonEmpty(asset.Version, "latest"))
	return nil
}

func findVSCodeCLI() (string, error) {
	if path, err := exec.LookPath("code"); err == nil {
		return path, nil
	}
	candidates := []string{}
	switch runtime.GOOS {
	case "darwin":
		candidates = append(candidates, "/Applications/Visual Studio Code.app/Contents/Resources/app/bin/code")
	case "windows":
		if localAppData := os.Getenv("LOCALAPPDATA"); localAppData != "" {
			candidates = append(candidates, filepath.Join(localAppData, "Programs", "Microsoft VS Code", "bin", "code.cmd"))
		}
		if programFiles := os.Getenv("ProgramFiles"); programFiles != "" {
			candidates = append(candidates, filepath.Join(programFiles, "Microsoft VS Code", "bin", "code.cmd"))
		}
		if programFilesX86 := os.Getenv("ProgramFiles(x86)"); programFilesX86 != "" {
			candidates = append(candidates, filepath.Join(programFilesX86, "Microsoft VS Code", "bin", "code.cmd"))
		}
	}
	for _, candidate := range candidates {
		if info, err := os.Stat(candidate); err == nil && !info.IsDir() {
			return candidate, nil
		}
	}
	return "", errors.New("VS Code CLI `code` was not found")
}

func absoluteAssetURL(server, assetURL string) string {
	target := strings.TrimSpace(assetURL)
	if strings.HasPrefix(target, "http://") || strings.HasPrefix(target, "https://") {
		return target
	}
	return apiURL(server, target)
}

func selectToolchainAsset(manifest updateManifest, tool string) *toolchainAsset {
	tool = strings.ToLower(strings.TrimSpace(tool))
	for i := range manifest.Toolchains {
		asset := &manifest.Toolchains[i]
		if strings.EqualFold(asset.Tool, tool) && asset.OS == runtime.GOOS && asset.Arch == runtime.GOARCH {
			return asset
		}
	}
	return nil
}

func downloadUpdateAsset(server, assetURL string) ([]byte, error) {
	target := strings.TrimSpace(assetURL)
	if target == "" {
		return nil, errors.New("update asset URL is empty")
	}
	if strings.HasPrefix(target, "http://") || strings.HasPrefix(target, "https://") {
		req, err := http.NewRequest("GET", target, nil)
		if err != nil {
			return nil, err
		}
		return downloadHTTP(req)
	}
	req, err := http.NewRequest("GET", apiURL(server, target), nil)
	if err != nil {
		return nil, err
	}
	return downloadHTTP(req)
}

func downloadHTTP(req *http.Request) ([]byte, error) {
	client := &http.Client{Timeout: 5 * time.Minute}
	resp, err := client.Do(req)
	if err != nil {
		return nil, fmt.Errorf("cannot download update asset: %w", err)
	}
	defer resp.Body.Close()
	raw, err := io.ReadAll(io.LimitReader(resp.Body, 200*1024*1024))
	if err != nil {
		return nil, err
	}
	if resp.StatusCode < 200 || resp.StatusCode >= 300 {
		return nil, fmt.Errorf("HTTP %d update asset: %s", resp.StatusCode, string(raw))
	}
	return raw, nil
}

func installBinary(path string, raw []byte) error {
	if len(raw) == 0 {
		return errors.New("downloaded binary is empty")
	}
	dir := filepath.Dir(path)
	if err := os.MkdirAll(dir, 0o755); err != nil {
		return err
	}
	tmp, err := os.CreateTemp(dir, ".smarttex-local-update-*")
	if err != nil {
		return err
	}
	tmpPath := tmp.Name()
	defer func() { _ = os.Remove(tmpPath) }()
	if _, err := tmp.Write(raw); err != nil {
		_ = tmp.Close()
		return err
	}
	if err := tmp.Chmod(0o755); err != nil {
		_ = tmp.Close()
		return err
	}
	if err := tmp.Close(); err != nil {
		return err
	}
	return os.Rename(tmpPath, path)
}

func installToolchainAsset(server, channel string, asset toolchainAsset) (string, error) {
	raw, err := downloadUpdateAsset(server, asset.URL)
	if err != nil {
		return "", err
	}
	if asset.SHA256 != "" {
		sum := sha256.Sum256(raw)
		actual := fmt.Sprintf("%x", sum[:])
		if !strings.EqualFold(actual, asset.SHA256) {
			return "", fmt.Errorf("%s checksum mismatch: got %s, expected %s", asset.Tool, actual, asset.SHA256)
		}
	}
	tool := strings.ToLower(strings.TrimSpace(asset.Tool))
	version := sanitizePathSegment(firstNonEmpty(asset.Version, "unknown"))
	executable := strings.TrimSpace(asset.Executable)
	if executable == "" {
		executable = tool
		if runtime.GOOS == "windows" {
			executable += ".exe"
		}
	}
	root, err := localDataDir()
	if err != nil {
		return "", err
	}
	target := filepath.Join(root, "toolchains", sanitizePathSegment(channel), tool, version, executable)
	if err := installBinary(target, raw); err != nil {
		return "", err
	}
	return target, nil
}

func safeExtractZip(raw []byte, destination string) error {
	reader, err := zip.NewReader(bytes.NewReader(raw), int64(len(raw)))
	if err != nil {
		return err
	}
	root, err := filepath.Abs(destination)
	if err != nil {
		return err
	}
	for _, file := range reader.File {
		name := filepath.Clean(filepath.FromSlash(file.Name))
		if name == "." || strings.HasPrefix(name, ".."+string(os.PathSeparator)) || filepath.IsAbs(name) {
			return fmt.Errorf("unsafe path in project ZIP: %s", file.Name)
		}
		target := filepath.Join(root, name)
		rel, err := filepath.Rel(root, target)
		if err != nil || strings.HasPrefix(rel, ".."+string(os.PathSeparator)) || rel == ".." {
			return fmt.Errorf("unsafe path in project ZIP: %s", file.Name)
		}
		if file.FileInfo().IsDir() {
			if err := os.MkdirAll(target, 0o755); err != nil {
				return err
			}
			continue
		}
		if err := os.MkdirAll(filepath.Dir(target), 0o755); err != nil {
			return err
		}
		src, err := file.Open()
		if err != nil {
			return err
		}
		dst, err := os.OpenFile(target, os.O_WRONLY|os.O_CREATE|os.O_TRUNC, file.Mode())
		if err != nil {
			_ = src.Close()
			return err
		}
		_, copyErr := io.Copy(dst, src)
		closeErr := errors.Join(src.Close(), dst.Close())
		if copyErr != nil {
			return copyErr
		}
		if closeErr != nil {
			return closeErr
		}
	}
	return nil
}

func workspaceRoot(base string, projectID int) (string, error) {
	return workspaceRootFor(base, projectID, "")
}

func workspaceRootFor(base string, projectID int, purpose string) (string, error) {
	if strings.HasPrefix(base, "~/") {
		home, err := os.UserHomeDir()
		if err != nil {
			return "", err
		}
		base = filepath.Join(home, strings.TrimPrefix(base, "~/"))
	}
	abs, err := filepath.Abs(base)
	if err != nil {
		return "", err
	}
	suffix := strings.TrimSpace(purpose)
	if suffix != "" {
		suffix = strings.Map(func(r rune) rune {
			if (r >= 'a' && r <= 'z') || (r >= 'A' && r <= 'Z') || (r >= '0' && r <= '9') || r == '-' || r == '_' {
				return r
			}
			return '-'
		}, suffix)
		return filepath.Join(abs, fmt.Sprintf("project-%d-%s", projectID, suffix)), nil
	}
	return filepath.Join(abs, fmt.Sprintf("project-%d", projectID)), nil
}

func envOr(key, fallback string) string {
	if value := os.Getenv(key); value != "" {
		return value
	}
	return fallback
}

func maxDuration(value, minimum time.Duration) time.Duration {
	if value < minimum {
		return minimum
	}
	return value
}

func binaryAvailable(name string) bool {
	if strings.TrimSpace(name) == "" {
		return false
	}
	_, err := exec.LookPath(name)
	return err == nil
}

func binaryVersion(name string) string {
	if strings.TrimSpace(name) == "" {
		return ""
	}
	ctx, cancel := context.WithTimeout(context.Background(), 2*time.Second)
	defer cancel()
	out, err := exec.CommandContext(ctx, name, "--version").CombinedOutput()
	if err != nil {
		return ""
	}
	text := strings.TrimSpace(string(out))
	if idx := strings.IndexAny(text, "\r\n"); idx >= 0 {
		text = strings.TrimSpace(text[:idx])
	}
	return text
}

func localCapabilities() []string {
	return []string{"compile", "typst-preview", "tinymist-lsp"}
}

func agentID() string {
	host, _ := os.Hostname()
	host = strings.TrimSpace(host)
	if host == "" {
		host = "unknown-host"
	}
	return fmt.Sprintf("smarttex-local-go:%s:%d", host, os.Getpid())
}

func isLoopbackHost(host string) bool {
	normalized := strings.Trim(strings.ToLower(host), "[]")
	if normalized == "localhost" || normalized == "" {
		return true
	}
	ip := net.ParseIP(normalized)
	return ip != nil && ip.IsLoopback()
}

func defaultServer() string {
	if value := os.Getenv("SMARTTEX_SERVER"); value != "" {
		return value
	}
	if cfg, err := loadLocalConfig(); err == nil && cfg.Server != "" {
		return cfg.Server
	}
	return "https://smart-tex.pp.ua"
}

func defaultTypstBin() string {
	if value := os.Getenv("TYPST_BINARY"); value != "" {
		return value
	}
	if cfg, err := loadLocalConfig(); err == nil && strings.TrimSpace(cfg.TypstBin) != "" {
		return cfg.TypstBin
	}
	return "typst"
}

func defaultTinymistBin() string {
	if value := os.Getenv("TINYMIST_BIN"); value != "" {
		return value
	}
	if cfg, err := loadLocalConfig(); err == nil && strings.TrimSpace(cfg.TinymistBin) != "" {
		return cfg.TinymistBin
	}
	return "tinymist"
}

func firstNonEmpty(values ...string) string {
	for _, value := range values {
		if strings.TrimSpace(value) != "" {
			return strings.TrimSpace(value)
		}
	}
	return ""
}

func sanitizePathSegment(value string) string {
	value = strings.TrimSpace(value)
	if value == "" {
		return "default"
	}
	return strings.Map(func(r rune) rune {
		if (r >= 'a' && r <= 'z') || (r >= 'A' && r <= 'Z') || (r >= '0' && r <= '9') || r == '.' || r == '-' || r == '_' {
			return r
		}
		return '-'
	}, value)
}

func localDataDir() (string, error) {
	if value := os.Getenv("SMARTTEX_LOCAL_HOME"); strings.TrimSpace(value) != "" {
		return expandHome(value)
	}
	home, err := os.UserHomeDir()
	if err != nil {
		return "", err
	}
	return filepath.Join(home, ".smarttex-local"), nil
}

func expandHome(path string) (string, error) {
	if strings.HasPrefix(path, "~/") {
		home, err := os.UserHomeDir()
		if err != nil {
			return "", err
		}
		return filepath.Join(home, strings.TrimPrefix(path, "~/")), nil
	}
	return path, nil
}

func tokenFresh(expiresAt int64) bool {
	return expiresAt == 0 || expiresAt > time.Now().Add(60*time.Second).Unix()
}

func resolveToken(server, explicitToken string) (string, error) {
	source := &tokenSource{server: server, explicitToken: strings.TrimSpace(explicitToken)}
	return source.Token()
}

func (source *tokenSource) Token() (string, error) {
	if strings.TrimSpace(source.explicitToken) != "" {
		return strings.TrimSpace(source.explicitToken), nil
	}
	source.mu.Lock()
	defer source.mu.Unlock()
	cfg, err := loadLocalConfig()
	if err != nil {
		return "", errors.New("no OAuth login found. Run `smarttex-local-go login` or pass --token/SMARTTEX_TOKEN")
	}
	if strings.TrimSpace(cfg.AccessToken) != "" && tokenFresh(cfg.ExpiresAt) {
		return cfg.AccessToken, nil
	}
	if strings.TrimSpace(cfg.RefreshToken) == "" || strings.TrimSpace(cfg.ClientID) == "" {
		return "", errors.New("saved OAuth token expired and no refresh token is available. Run `smarttex-local-go login` again")
	}
	server := strings.TrimSpace(source.server)
	if server == "" {
		server = cfg.Server
	}
	accessToken, refreshToken, expiresAt, err := refreshOAuthToken(server, cfg.ClientID, cfg.RefreshToken)
	if err != nil {
		return "", fmt.Errorf("saved OAuth token expired and refresh failed: %w. Run `smarttex-local-go login` again", err)
	}
	cfg.Server = server
	cfg.AccessToken = accessToken
	cfg.RefreshToken = refreshToken
	cfg.ExpiresAt = expiresAt
	if err := saveLocalConfig(cfg); err != nil {
		return "", err
	}
	return cfg.AccessToken, nil
}

func (cfg serveConfig) authToken() (string, error) {
	if cfg.Auth == nil {
		return "", errors.New("local agent auth is not configured")
	}
	return cfg.Auth.Token()
}

func bridgeSecretFromConfig(cfg localConfig) string {
	if value := strings.TrimSpace(os.Getenv("SMARTTEX_LOCAL_SECRET")); value != "" {
		return value
	}
	if value := strings.TrimSpace(cfg.BridgeSecret); value != "" {
		return value
	}
	return randomURLSafe(32)
}

func resolveBridgeSecret(explicitSecret string) (string, error) {
	if value := strings.TrimSpace(explicitSecret); value != "" {
		return value, nil
	}
	if value := strings.TrimSpace(os.Getenv("SMARTTEX_LOCAL_SECRET")); value != "" {
		return value, nil
	}
	cfg, err := loadLocalConfig()
	if err != nil {
		return "", errors.New("no local bridge secret is configured. Run `smarttex-local-go login` first or pass --secret/SMARTTEX_LOCAL_SECRET")
	}
	secret := bridgeSecretFromConfig(cfg)
	if strings.TrimSpace(cfg.BridgeSecret) == "" {
		cfg.BridgeSecret = secret
		if err := saveLocalConfig(cfg); err != nil {
			return "", err
		}
	}
	return secret, nil
}

func configPath() string {
	if value := os.Getenv("SMARTTEX_LOCAL_CONFIG"); value != "" {
		return value
	}
	home, err := os.UserHomeDir()
	if err != nil {
		return ".smarttex-local-config.json"
	}
	return filepath.Join(home, ".smarttex-local", "config.json")
}

func loadLocalConfig() (localConfig, error) {
	raw, err := os.ReadFile(configPath())
	if err != nil {
		return localConfig{}, err
	}
	var cfg localConfig
	if err := json.Unmarshal(raw, &cfg); err != nil {
		return localConfig{}, err
	}
	return cfg, nil
}

func saveLocalConfig(cfg localConfig) error {
	path := configPath()
	if err := os.MkdirAll(filepath.Dir(path), 0o700); err != nil {
		return err
	}
	raw, err := json.MarshalIndent(cfg, "", "  ")
	if err != nil {
		return err
	}
	return os.WriteFile(path, raw, 0o600)
}

func receiveOAuthCode(listen, expectedState string) (string, <-chan string, error) {
	ln, err := net.Listen("tcp", listen)
	if err != nil {
		return "", nil, err
	}
	addr := ln.Addr().String()
	if strings.HasPrefix(addr, "127.0.0.1:") {
		addr = "localhost:" + strings.TrimPrefix(addr, "127.0.0.1:")
	}
	redirectURI := "http://" + addr + "/oauth/callback"
	codeCh := make(chan string, 1)
	mux := http.NewServeMux()
	serverHTTP := &http.Server{Handler: mux}
	mux.HandleFunc("/oauth/callback", func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Query().Get("state") != expectedState {
			http.Error(w, "OAuth callback state mismatch", http.StatusBadRequest)
			return
		}
		code := r.URL.Query().Get("code")
		if code == "" {
			http.Error(w, "OAuth callback did not include code", http.StatusBadRequest)
			return
		}
		fmt.Fprintln(w, "SmartTeX Local login complete. You can close this tab.")
		codeCh <- code
		go func() {
			time.Sleep(300 * time.Millisecond)
			_ = serverHTTP.Close()
		}()
	})
	go func() { _ = serverHTTP.Serve(ln) }()
	return redirectURI, codeCh, nil
}

func registerOAuthClientAndOpenBrowser(server, redirectURI, state string) (clientID, verifier string, err error) {
	body := map[string]any{
		"client_name":                "SmartTeX Local Agent",
		"redirect_uris":              []string{redirectURI},
		"grant_types":                []string{"authorization_code", "refresh_token"},
		"response_types":             []string{"code"},
		"token_endpoint_auth_method": "none",
		"scope":                      "smarttex:read smarttex:write",
	}
	rawBody, _ := json.Marshal(body)
	raw, err := apiRequest("POST", server, "/oauth/register/", "", bytes.NewReader(rawBody), "application/json")
	if err != nil {
		return "", "", err
	}
	var reg struct {
		ClientID string `json:"client_id"`
	}
	if err := json.Unmarshal(raw, &reg); err != nil {
		return "", "", err
	}
	verifier = randomURLSafe(48)
	challenge := pkceChallenge(verifier)
	authURL := apiURL(server, "/oauth/authorize/") + "?" + url.Values{
		"response_type":         {"code"},
		"client_id":             {reg.ClientID},
		"redirect_uri":          {redirectURI},
		"scope":                 {"smarttex:read smarttex:write"},
		"code_challenge":        {challenge},
		"code_challenge_method": {"S256"},
		"state":                 {state},
	}.Encode()
	fmt.Println("Opening browser for SmartTeX login:")
	fmt.Println(authURL)
	if err := openBrowser(authURL); err != nil {
		fmt.Println("Could not open browser automatically:", err)
	}
	return reg.ClientID, verifier, nil
}

func exchangeOAuthCode(server, clientID, redirectURI, verifier, code string) (token string, refreshToken string, expiresAt int64, err error) {
	form := url.Values{
		"grant_type":    {"authorization_code"},
		"code":          {code},
		"client_id":     {clientID},
		"redirect_uri":  {redirectURI},
		"code_verifier": {verifier},
	}
	raw, err := apiRequest("POST", server, "/oauth/token/", "", strings.NewReader(form.Encode()), "application/x-www-form-urlencoded")
	if err != nil {
		return "", "", 0, err
	}
	var payload struct {
		AccessToken  string `json:"access_token"`
		RefreshToken string `json:"refresh_token"`
		ExpiresIn    int64  `json:"expires_in"`
	}
	if err := json.Unmarshal(raw, &payload); err != nil {
		return "", "", 0, err
	}
	return payload.AccessToken, payload.RefreshToken, time.Now().Unix() + payload.ExpiresIn, nil
}

func refreshOAuthToken(server, clientID, refreshToken string) (token string, replacementRefreshToken string, expiresAt int64, err error) {
	form := url.Values{
		"grant_type":    {"refresh_token"},
		"client_id":     {clientID},
		"refresh_token": {refreshToken},
	}
	raw, err := apiRequest("POST", server, "/oauth/token/", "", strings.NewReader(form.Encode()), "application/x-www-form-urlencoded")
	if err != nil {
		return "", "", 0, err
	}
	var payload struct {
		AccessToken  string `json:"access_token"`
		RefreshToken string `json:"refresh_token"`
		ExpiresIn    int64  `json:"expires_in"`
	}
	if err := json.Unmarshal(raw, &payload); err != nil {
		return "", "", 0, err
	}
	if strings.TrimSpace(payload.AccessToken) == "" || strings.TrimSpace(payload.RefreshToken) == "" {
		return "", "", 0, errors.New("OAuth refresh response did not include access_token and refresh_token")
	}
	return payload.AccessToken, payload.RefreshToken, time.Now().Unix() + payload.ExpiresIn, nil
}

func randomURLSafe(n int) string {
	buf := make([]byte, n)
	_, _ = rand.Read(buf)
	return base64.RawURLEncoding.EncodeToString(buf)
}

func pkceChallenge(verifier string) string {
	sum := sha256.Sum256([]byte(verifier))
	return base64.RawURLEncoding.EncodeToString(sum[:])
}

func openBrowser(target string) error {
	var cmd *exec.Cmd
	switch runtime.GOOS {
	case "darwin":
		cmd = exec.Command("open", target)
	case "windows":
		cmd = exec.Command("rundll32", "url.dll,FileProtocolHandler", target)
	default:
		cmd = exec.Command("xdg-open", target)
	}
	return cmd.Start()
}

func reserveLocalPort() (int, error) {
	ln, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		return 0, err
	}
	defer ln.Close()
	return ln.Addr().(*net.TCPAddr).Port, nil
}

func waitForPort(port int, timeout time.Duration) error {
	deadline := time.Now().Add(timeout)
	for time.Now().Before(deadline) {
		conn, err := net.DialTimeout("tcp", fmt.Sprintf("127.0.0.1:%d", port), 250*time.Millisecond)
		if err == nil {
			_ = conn.Close()
			return nil
		}
		time.Sleep(150 * time.Millisecond)
	}
	return fmt.Errorf("port %d did not become ready", port)
}

func processAlive(process *os.Process) bool {
	if process == nil {
		return false
	}
	return process.Signal(syscall.Signal(0)) == nil
}
