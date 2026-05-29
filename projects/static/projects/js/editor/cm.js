import {
  EditorView, keymap,
  lineNumbers, highlightActiveLineGutter, highlightSpecialChars,
  drawSelection, dropCursor, rectangularSelection, crosshairCursor,
  highlightActiveLine,
} from "https://esm.sh/@codemirror/view@6";
import { EditorState, Compartment } from "https://esm.sh/@codemirror/state@6";
import {
  defaultKeymap, historyKeymap, history, indentWithTab,
} from "https://esm.sh/@codemirror/commands@6";
import {
  StreamLanguage, syntaxHighlighting, defaultHighlightStyle, HighlightStyle,
  bracketMatching, foldGutter, foldKeymap, indentOnInput,
} from "https://esm.sh/@codemirror/language@6";
import {
  autocompletion, closeBrackets, closeBracketsKeymap, completionKeymap,
} from "https://esm.sh/@codemirror/autocomplete@6";
import { highlightSelectionMatches, searchKeymap } from "https://esm.sh/@codemirror/search@6";
import { tags } from "https://esm.sh/@lezer/highlight@1";
import { stex } from "https://esm.sh/@codemirror/legacy-modes@6/mode/stex";
import * as state from "./state.js";

const { s } = state;

// ── basicSetup equivalent ────────────────────────────────────────────────────

const basicSetup = [
  lineNumbers(),
  highlightActiveLineGutter(),
  highlightSpecialChars(),
  history(),
  foldGutter(),
  drawSelection(),
  dropCursor(),
  rectangularSelection(),
  crosshairCursor(),
  highlightActiveLine(),
  highlightSelectionMatches(),
  EditorState.allowMultipleSelections.of(false),
  indentOnInput(),
  syntaxHighlighting(defaultHighlightStyle, { fallback: true }),
  bracketMatching(),
  closeBrackets(),
  autocompletion(),
  keymap.of([
    ...closeBracketsKeymap,
    ...defaultKeymap,
    ...searchKeymap,
    ...historyKeymap,
    ...foldKeymap,
    ...completionKeymap,
  ]),
];

// ── Theme ────────────────────────────────────────────────────────────────────

const darkTheme = EditorView.theme({
  "&": { height: "100%", background: "#1e1e1e", color: "#d4d4d4" },
  ".cm-scroller": {
    fontFamily: '"JetBrains Mono", "Fira Code", ui-monospace, Menlo, Consolas, monospace',
    fontSize: "14px",
    lineHeight: "22px",
    overflow: "auto",
  },
  ".cm-content": { padding: "12px 0" },
  ".cm-line": { padding: "0 16px" },
  ".cm-gutters": {
    background: "#1e1e1e",
    borderRight: "1px solid rgba(62,62,66,.6)",
    color: "#858585",
    minWidth: "52px",
  },
  ".cm-lineNumbers .cm-gutterElement": {
    padding: "0 8px 0 0",
    textAlign: "right",
    minWidth: "44px",
  },
  ".cm-cursor, .cm-dropCursor": { borderLeftColor: "#d4d4d4 !important" },
  ".cm-selectionBackground": { background: "rgba(38,79,120,.7) !important" },
  "&.cm-focused .cm-selectionBackground": { background: "rgba(38,79,120,.7) !important" },
  ".cm-activeLine": { background: "rgba(255,255,255,.04)" },
  ".cm-activeLineGutter": { background: "rgba(255,255,255,.04)", color: "#c6c6c6" },
  ".cm-matchingBracket": {
    background: "rgba(95,126,151,.25)",
    outline: "1px solid rgba(95,126,151,.5)",
    borderRadius: "2px",
  },
  ".cm-tooltip": { background: "#252526", border: "1px solid #3e3e42", color: "#d4d4d4" },
  ".cm-tooltip-autocomplete > ul > li[aria-selected]": { background: "#094771" },
  ".cm-panels": { background: "#252526", borderTop: "1px solid #3e3e42" },
  ".cm-panels input, .cm-panels button": {
    background: "#3c3c3c", color: "#d4d4d4", border: "1px solid #5a5a5a",
  },
  ".cm-searchMatch": { background: "rgba(234,92,0,.22)", borderRadius: "2px" },
  ".cm-searchMatch.cm-searchMatch-selected": { background: "rgba(234,92,0,.55)" },
  ".cm-selectionMatch": { background: "rgba(255,255,255,.07)" },
  ".cm-foldPlaceholder": { background: "#3c3c3c", border: "1px solid #5a5a5a", color: "#858585" },
}, { dark: true });

// ── Highlight style (VS Code Dark+) ─────────────────────────────────────────

const vscodeHighlight = HighlightStyle.define([
  { tag: tags.keyword,                          color: "#569cd6" },
  { tag: tags.atom,                             color: "#569cd6" },
  { tag: tags.comment,                          color: "#6a9955", fontStyle: "italic" },
  { tag: tags.string,                           color: "#ce9178" },
  { tag: tags.number,                           color: "#b5cea8" },
  { tag: tags.bool,                             color: "#569cd6" },
  { tag: tags.bracket,                          color: "#d4d4d4" },
  { tag: tags.punctuation,                      color: "#d4d4d4" },
  { tag: tags.operator,                         color: "#d4d4d4" },
  { tag: tags.variableName,                     color: "#9cdcfe" },
  { tag: tags.typeName,                         color: "#4ec9b0" },
  { tag: tags.className,                        color: "#4ec9b0" },
  { tag: tags.definition(tags.variableName),    color: "#9cdcfe" },
  { tag: tags.propertyName,                     color: "#9cdcfe" },
  { tag: tags.special(tags.string),             color: "#ce9178" },
  { tag: tags.heading,                          color: "#569cd6", fontWeight: "bold" },
  { tag: tags.meta,                             color: "#569cd6" },
  { tag: tags.tagName,                          color: "#569cd6" },
  { tag: tags.attributeName,                    color: "#9cdcfe" },
  { tag: tags.link,                             color: "#ce9178" },
]);

// ── Language modes ───────────────────────────────────────────────────────────

const typstParser = {
  startState: () => ({ inBlockComment: false }),
  token(stream, state) {
    if (state.inBlockComment) {
      if (stream.match("*/")) { state.inBlockComment = false; return "comment"; }
      stream.next();
      return "comment";
    }
    if (stream.match("//")) { stream.skipToEnd(); return "comment"; }
    if (stream.match("/*")) { state.inBlockComment = true; return "comment"; }
    if (stream.sol() && stream.match(/^=+ .+/)) { stream.skipToEnd(); return "heading"; }
    if (stream.match(/^#[a-zA-Z][a-zA-Z0-9._-]*/)) return "keyword";
    if (stream.match(/^\$[^$\n]*\$/)) return "string";
    if (stream.match(/^"(?:[^"\\]|\\.)*"/)) return "string";
    if (stream.match(/^`[^`]*`/)) return "string";
    if (stream.match(/^\d+(\.\d+)?(em|rem|pt|cm|in|%|fr|mm|deg)?/)) return "number";
    if (stream.match(/^[{}[\]()]/)) return "bracket";
    stream.next();
    return null;
  },
};

export const langCompartment = new Compartment();

export function getLanguageExt(filename) {
  const ext = String(filename || "").split(".").pop().toLowerCase();
  if (["tex", "cls", "sty", "bib"].includes(ext)) return StreamLanguage.define(stex);
  if (ext === "typ") return StreamLanguage.define(typstParser);
  return [];
}

// ── Editor instance ──────────────────────────────────────────────────────────

let _settingContent = false;
let _onInputCallback = () => {};
let _onSelectionCallback = () => {};

export let view = null;

// Per-tab EditorState cache — preserves independent undo/redo history per tab
const _tabStates = new Map();

function makeExtensions(filename) {
  return [
    basicSetup,
    darkTheme,
    syntaxHighlighting(vscodeHighlight),
    langCompartment.of(filename ? getLanguageExt(filename) : []),
    keymap.of([
      indentWithTab,
      { key: "Mod-s",     run: () => { _onInputCallback("save");    return true; } },
      { key: "Mod-Enter", run: () => { _onInputCallback("compile"); return true; } },
    ]),
    EditorView.updateListener.of(update => {
      if (update.selectionSet || update.docChanged) _onSelectionCallback();
      if (update.docChanged && !_settingContent) _onInputCallback("change");
    }),
  ];
}

export function saveTabState(name) {
  if (view && name) _tabStates.set(name, view.state);
}

export function hasTabState(name) {
  return _tabStates.has(name);
}

// Restores cached state if available, otherwise creates fresh state with content.
// Returns true when cached state was restored (no content fetch needed).
// Pass cacheOnly=true to update the cached state without touching the active editor view.
export function activateTab(name, content, filename, forceFresh = false, cacheOnly = false) {
  if (!view) return false;
  const saved = forceFresh ? null : _tabStates.get(name);
  if (saved && !forceFresh) {
    if (!cacheOnly) view.setState(saved);
    return true;
  }
  _settingContent = true;
  const nextState = EditorState.create({ doc: content || "", extensions: makeExtensions(filename) });
  _tabStates.set(name, nextState);
  if (!cacheOnly) {
    view.setState(nextState);
  }
  _settingContent = false;
  return false;
}

export function dropTabState(name) {
  _tabStates.delete(name);
}

export function initCodeMirror(parent, onInput, onSelection) {
  _onInputCallback = onInput;
  _onSelectionCallback = onSelection;

  view = new EditorView({
    parent,
    state: EditorState.create({
      doc: "",
      extensions: makeExtensions(null),
    }),
  });
  return view;
}

export function getContent() {
  return view ? view.state.doc.toString() : "";
}

export function setContent(text) {
  if (!view) return;
  _settingContent = true;
  view.dispatch({ changes: { from: 0, to: view.state.doc.length, insert: text } });
  if (s.activeTabName) _tabStates.set(s.activeTabName, view.state);
  _settingContent = false;
}


export function jumpToLine(n, column = 1) {
  if (!view) return;
  if (!s.selectedFile?.is_text || s.selectedFile?.is_dir) return;
  const doc     = view.state.doc;
  const lineNum = Math.max(1, Math.min(n, doc.lines));
  const line    = doc.line(lineNum);
  const pos     = Math.min(line.from + Math.max(0, Number(column || 1) - 1), line.to);
  view.dispatch({ selection: { anchor: pos }, scrollIntoView: true });
  view.focus();
}

export function switchLanguage(filename) {
  if (!view) return;
  view.dispatch({ effects: langCompartment.reconfigure(getLanguageExt(filename)) });
}

export function focusEditor() { view?.focus(); }
export function refreshLayout() { view?.requestMeasure(); }
