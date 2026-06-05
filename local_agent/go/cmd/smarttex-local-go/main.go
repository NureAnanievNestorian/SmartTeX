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

const localPreviewBridgeScript = `
<script>
(() => {
  if (window.__smarttexPreviewBridgeInstalled) return;
  window.__smarttexPreviewBridgeInstalled = true;
  const PREVIEW_PROJECT_ID = "__SMARTTEX_PREVIEW_PROJECT_ID__";
  const PREVIEW_ROOT_URI = "__SMARTTEX_PREVIEW_ROOT_URI__";
  const HIGHLIGHT_CLASS = "smarttex-preview-sync-highlight";
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
    style.textContent = "." + HIGHLIGHT_CLASS + "{outline:2px solid rgba(59,130,246,.9)!important;outline-offset:4px!important;border-radius:6px!important;}";
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

  window.addEventListener("message", event => {
    if (PARENT_ORIGIN !== "*" && event.origin !== PARENT_ORIGIN && event.origin !== window.location.origin) return;
    const data = event.data || {};
    if (data?.type !== "smarttex-preview-reveal") return;
    const payload = data.payload || {};
    revealElement(findTextElement([
      {value: payload.heading, weight: 100},
      {value: payload.lineText, weight: 70},
      {value: payload.excerpt, weight: 40},
    ]));
  });

  document.addEventListener("click", event => {
    if (isVisualOnlyTarget(event) || !isTextNavigationTarget(event)) return;
    const location = extractSourceLocation(event);
    const text = bestClickableText(event);
    if (!location && !text) return;
    window.parent.postMessage({type: "smarttex-preview-click", payload: {text, location}}, PARENT_ORIGIN);
  }, true);

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
	Version    string           `json:"version"`
	Channel    string           `json:"channel"`
	Assets     []updateAsset    `json:"assets"`
	Toolchains []toolchainAsset `json:"toolchains"`
}

type updateAsset struct {
	OS     string `json:"os"`
	Arch   string `json:"arch"`
	URL    string `json:"url"`
	SHA256 string `json:"sha256"`
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
  smarttex-local-go serve [--server URL] [--token TOKEN] [--secret SECRET] [--tinymist-bin PATH]
  smarttex-local-go doctor [--server URL] [--token TOKEN]
  smarttex-local-go update [--server URL] [--install-path PATH]
  smarttex-local-go toolchain status|install [--server URL] [--channel stable]
  smarttex-local-go version

Environment:
  SMARTTEX_SERVER defaults to http://localhost:8000
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
	if !*force && manifest.Version == toolVersion {
		fmt.Printf("SmartTeX local agent is up to date (%s).\n", toolVersion)
		return nil
	}
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
	fmt.Printf("Updated SmartTeX local agent %s -> %s at %s\n", toolVersion, manifest.Version, absTarget)
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

	if session := previewSessions[projectID]; session != nil {
		if processAlive(session.Process) {
			if strings.TrimSpace(invertColors) == "" {
				return session, nil
			}
			invertColors = normalizePreviewInvertColors(invertColors)
			if session.InvertColors == invertColors {
				return session, nil
			}
			_ = session.Process.Kill()
		}
		delete(previewSessions, projectID)
	}
	invertColors = normalizePreviewInvertColors(invertColors)
	root, err := workspaceRootFor(cfg.Workspace, projectID, "preview")
	if err != nil {
		return nil, err
	}
	token, err := cfg.authToken()
	if err != nil {
		return nil, err
	}
	compileCfg := config{
		Server: cfg.Server, Token: token, ProjectID: projectID,
		Workspace: cfg.Workspace, TypstBin: cfg.TypstBin, Timeout: cfg.Timeout, Pull: true,
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
	cmd.Stderr = stderr
	if err := cmd.Start(); err != nil {
		closeStderr()
		return nil, err
	}
	if err := waitForPort(port, 15*time.Second); err != nil {
		_ = cmd.Process.Kill()
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
	return compileResult{Status: status, PDF: pdf, Log: log, ReturnCode: returnCode}
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
		root = "http://localhost:8000"
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
	return "http://localhost:8000"
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
