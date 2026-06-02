import {
  EditorView, keymap, Decoration,
  lineNumbers, highlightActiveLineGutter, highlightSpecialChars,
  drawSelection, dropCursor, rectangularSelection, crosshairCursor,
  highlightActiveLine, hoverTooltip, gutter, GutterMarker,
} from "https://esm.sh/@codemirror/view@6";
import { EditorState, Compartment, StateEffect, StateField, RangeSetBuilder } from "https://esm.sh/@codemirror/state@6";
import {
  defaultKeymap, historyKeymap, history, indentWithTab, undo, redo,
} from "https://esm.sh/@codemirror/commands@6";
import {
  StreamLanguage, syntaxHighlighting, defaultHighlightStyle, HighlightStyle,
  bracketMatching, foldGutter, foldKeymap, indentOnInput, foldService,
} from "https://esm.sh/@codemirror/language@6";
import {
  autocompletion, closeBrackets, closeBracketsKeymap, completionKeymap,
  snippetCompletion, startCompletion,
} from "https://esm.sh/@codemirror/autocomplete@6";
import { highlightSelectionMatches, searchKeymap } from "https://esm.sh/@codemirror/search@6";
import { lintGutter, lintKeymap, setDiagnostics } from "https://esm.sh/@codemirror/lint@6";
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
  lintGutter(),
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
  keymap.of([
    ...closeBracketsKeymap,
    ...defaultKeymap,
    ...searchKeymap,
    ...historyKeymap,
    ...foldKeymap,
    ...lintKeymap,
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
  ".cm-annotation-gutter": {
    width: "20px",
    minWidth: "20px",
    borderRight: "1px solid rgba(62,62,66,.24)",
  },
  ".cm-annotation-gutter .cm-gutterElement": {
    padding: "0",
    width: "20px",
  },
  ".cm-annotation-marker": {
    width: "16px",
    minWidth: "16px",
    height: "16px",
    margin: "3px auto 0",
    borderRadius: "999px",
    border: "0",
    display: "grid",
    placeItems: "center",
    background: "transparent",
    color: "#b6bec8",
    cursor: "pointer",
    padding: "0",
    opacity: ".92",
  },
  ".cm-annotation-marker.in_progress": {
    color: "#7dd3fc",
  },
  ".cm-annotation-marker.done": {
    color: "#86efac",
  },
  ".cm-annotation-marker:hover": { color: "#f2f5f8", background: "rgba(255,255,255,.06)" },
  ".cm-annotation-marker svg": { width: "14px", height: "14px", display: "block" },
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
    if (stream.match(/^@[A-Za-z0-9:_-]+/)) return "link";
    if (stream.match(/^<[A-Za-z0-9:_-]+>/)) return "tag";
    if (stream.match(/^#[a-zA-Z][a-zA-Z0-9._-]*/)) return "keyword";
    if (stream.match(/^[A-Za-z_][A-Za-z0-9_-]*(?=\s*:)/)) return "property";
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
export const wrapCompartment = new Compartment();

const TYPOGRAPHIC_QUOTES = /["'`]/;
const typstKeywordOptions = [
  { label: "#let", type: "keyword", detail: "binding", info: "Bind a value or function in Typst." },
  { label: "#set", type: "keyword", detail: "style rule", info: "Set styling defaults for following content." },
  { label: "#show", type: "keyword", detail: "transform rule", info: "Transform matching elements before rendering." },
  { label: "#import", type: "keyword", detail: "module", info: "Import definitions from another Typst file or package." },
  { label: "#include", type: "keyword", detail: "content", info: "Include another Typst file into the document." },
  { label: "#if", type: "keyword", detail: "control flow", info: "Conditional Typst expression." },
  { label: "#for", type: "keyword", detail: "control flow", info: "Loop over Typst content or collections." },
  { label: "#context", type: "keyword", detail: "layout context", info: "Read contextual layout information." },
  { label: "#here", type: "variable", detail: "location", info: "Current document location." },
  snippetCompletion("#let ${name} = ${value}", { label: "#let …", type: "keyword", detail: "snippet", boost: 90 }),
  snippetCompletion("#show ${selector}: it => ${body}", { label: "#show …", type: "keyword", detail: "snippet", boost: 85 }),
  snippetCompletion("#set ${rule}(${value})", { label: "#set …", type: "keyword", detail: "snippet", boost: 80 }),
  snippetCompletion("#import \"${path}.typ\": ${member}", { label: "#import …", type: "keyword", detail: "snippet", boost: 75 }),
  snippetCompletion("#figure(\n\t${body},\n\tcaption: [${caption}],\n) <${label}>", { label: "figure", type: "function", detail: "snippet", boost: 70 }),
  snippetCompletion("#table(\n\tcolumns: (${columns}),\n\t${body},\n) <${label}>", { label: "table", type: "function", detail: "snippet", boost: 68 }),
  snippetCompletion("= ${title}\n${}", { label: "heading 1", type: "text", detail: "snippet", boost: 66 }),
  snippetCompletion("== ${title}\n${}", { label: "heading 2", type: "text", detail: "snippet", boost: 64 }),
];

function uniqueSorted(values) {
  return [...new Set(values.filter(Boolean))].sort((a, b) => a.localeCompare(b));
}

function collectTypstSymbols(docText) {
  const imports = uniqueSorted([...docText.matchAll(/#import\s+"([^"]+)"/g)].map(match => match[1]));
  const labels = uniqueSorted([...docText.matchAll(/<([A-Za-z0-9:_-]+)>/g)].map(match => match[1]));
  const refs = uniqueSorted([...docText.matchAll(/@([A-Za-z0-9:_-]+)/g)].map(match => match[1]));
  const bindings = uniqueSorted([...docText.matchAll(/#let\s+([A-Za-z_][A-Za-z0-9_-]*)/g)].map(match => match[1]));
  const functions = uniqueSorted(
    [...docText.matchAll(/#let\s+([A-Za-z_][A-Za-z0-9_-]*)\s*\(/g)].map(match => match[1])
  );
  const headings = uniqueSorted(docText
    .split("\n")
    .map(line => line.match(/^\s*(=+)\s+(.+?)\s*$/))
    .filter(Boolean)
    .map(match => match[2]));
  return { imports, labels, refs, headings, bindings, functions };
}

function buildTypstCompletions(docText) {
  const symbols = collectTypstSymbols(docText);
  const dynamic = [
    ...symbols.functions.map(name => ({ label: name, type: "function", detail: "local function", boost: 82 })),
    ...symbols.bindings
      .filter(name => !symbols.functions.includes(name))
      .map(name => ({ label: name, type: "variable", detail: "local binding", boost: 74 })),
    ...symbols.labels.map(label => ({ label: `@${label}`, type: "variable", detail: "reference", boost: 88 })),
    ...symbols.labels.map(label => ({ label: `<${label}>`, type: "property", detail: "label", boost: 60 })),
    ...symbols.imports.map(path => ({ label: path, type: "namespace", detail: "import path", boost: 56 })),
    ...symbols.headings.map(title => ({ label: title, type: "text", detail: "heading", boost: 40 })),
    ...symbols.refs.filter(ref => !symbols.labels.includes(ref)).map(ref => ({ label: `@${ref}`, type: "variable", detail: "reference", boost: 54 })),
  ];
  return [...typstKeywordOptions, ...dynamic];
}

function findTypstTokenAt(docText, pos) {
  const before = docText.slice(0, pos);
  const refMatch = before.match(/@([A-Za-z0-9:_-]+)$/);
  if (refMatch) {
    return {
      kind: "ref",
      name: refMatch[1],
      from: pos - refMatch[0].length,
      to: pos,
    };
  }

  const labelStart = before.lastIndexOf("<");
  if (labelStart >= 0) {
    const after = docText.slice(labelStart);
    const labelMatch = after.match(/^<([A-Za-z0-9:_-]+)>/);
    if (labelMatch) {
      const from = labelStart;
      const to = labelStart + labelMatch[0].length;
      if (pos >= from && pos <= to) {
        return {
          kind: "label",
          name: labelMatch[1],
          from,
          to,
        };
      }
    }
  }
  return null;
}

function findTypstLabelDefinition(docText, labelName) {
  const escaped = String(labelName || "").replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  const re = new RegExp(`<(${escaped})>`, "g");
  const match = re.exec(docText);
  if (!match) return null;
  return {
    name: labelName,
    from: match.index,
    to: match.index + match[0].length,
  };
}

function lineNumberAt(doc, pos) {
  return doc.lineAt(pos).number;
}

function typstRefTooltip(view, pos) {
  const docText = view.state.doc.toString();
  const token = findTypstTokenAt(docText, pos);
  if (!token) return null;
  const definition = findTypstLabelDefinition(docText, token.name);
  const dom = document.createElement("div");
  dom.className = "cm-typst-tooltip";
  if (token.kind === "ref") {
    dom.textContent = definition
      ? `Reference @${token.name} -> line ${lineNumberAt(view.state.doc, definition.from)}`
      : `Reference @${token.name} (label not found in this file)`;
  } else {
    dom.textContent = `Label <${token.name}> on line ${lineNumberAt(view.state.doc, token.from)}`;
  }
  return {
    pos: token.from,
    end: token.to,
    above: true,
    create() {
      return { dom };
    },
  };
}

function jumpToTypstDefinition(targetPos = null) {
  if (!view || !s.selectedFile?.is_text || s.selectedFile?.is_dir) return false;
  const pos = typeof targetPos === "number" ? targetPos : view.state.selection.main.head;
  const docText = view.state.doc.toString();
  const token = findTypstTokenAt(docText, pos);
  if (!token) return false;
  const destination = token.kind === "label" ? token : findTypstLabelDefinition(docText, token.name);
  if (!destination) return false;
  view.dispatch({
    selection: { anchor: destination.from, head: destination.to },
    scrollIntoView: true,
  });
  view.focus();
  return true;
}

function typstClickHandler(event) {
  if (!view || (!event.metaKey && !event.ctrlKey)) return false;
  const pos = view.posAtCoords({ x: event.clientX, y: event.clientY });
  if (typeof pos !== "number") return false;
  if (!findTypstTokenAt(view.state.doc.toString(), pos)) return false;
  if (!jumpToTypstDefinition(pos)) return false;
  event.preventDefault();
  return true;
}

// LSP providers — set by tinymist.js when connected, cleared on disconnect
let _lspCompletionFn = null;
let _lspHoverFn = null;
let _lspDefinitionFn = null;
let _lspMetaClickFn = null;
let _lspFoldFn = null;
let _editorContextMenuFn = null;
let _annotationMarkerClickFn = null;

export function setLspCompletionProvider(fn)  { _lspCompletionFn = fn; }
export function setLspHoverProvider(fn)        { _lspHoverFn = fn; }
export function setLspDefinitionProvider(fn)   { _lspDefinitionFn = fn; }
export function setLspMetaClickProvider(fn)    { _lspMetaClickFn = fn; }
export function setLspFoldProvider(fn)         { _lspFoldFn = fn; }
export function setEditorContextMenuProvider(fn) { _editorContextMenuFn = fn; }
export function setAnnotationMarkerClickProvider(fn) { _annotationMarkerClickFn = fn; }
export function clearLspProviders() {
  _lspCompletionFn = null;
  _lspHoverFn = null;
  _lspDefinitionFn = null;
  _lspMetaClickFn = null;
  _lspFoldFn = null;
  _clearSemanticTokens();
}

const setAnnotationMarkersEffect = StateEffect.define();

class AnnotationGutterMarker extends GutterMarker {
  constructor(info) {
    super();
    this.info = info;
  }
  toDOM() {
    const el = document.createElement("button");
    el.type = "button";
    el.className = `cm-annotation-marker ${this.info.status || "open"}`;
    el.title = this.info.title || "Помітка";
    el.setAttribute("aria-label", this.info.title || "Помітка");
    el.innerHTML = `
      <svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.4" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
        <path d="M3 3.75h10v6.5H7.25L4 12.75v-2.5H3z"></path>
      </svg>
    `;
    if (this.info.count > 1) {
      el.dataset.count = String(this.info.count);
      el.title = `${this.info.count} помітки`;
    }
    const info = this.info;
    el.addEventListener("click", (event) => {
      event.preventDefault();
      event.stopPropagation();
      if (_annotationMarkerClickFn) _annotationMarkerClickFn(info, event);
    });
    return el;
  }
}

const annotationMarkerField = StateField.define({
  create() {
    return new Map();
  },
  update(value, tr) {
    for (const effect of tr.effects) {
      if (effect.is(setAnnotationMarkersEffect)) {
        const next = new Map();
        for (const item of effect.value || []) {
          next.set(Number(item.line), item);
        }
        return next;
      }
    }
    return value;
  },
});

const annotationGutter = gutter({
  class: "cm-annotation-gutter",
  lineMarker(view, line) {
    const markers = view.state.field(annotationMarkerField, false);
    const lineNumber = view.state.doc.lineAt(line.from).number;
    const info = markers?.get(lineNumber);
    return info ? new AnnotationGutterMarker(info) : null;
  },
  lineMarkerChange(update) {
    return update.transactions.some(t => t.effects.some(e => e.is(setAnnotationMarkersEffect)));
  },
});

// ── Semantic tokens ──────────────────────────────────────────────────────────

const _setSemanticTokensEffect = StateEffect.define();

const _semanticTokensField = StateField.define({
  create: () => Decoration.none,
  update(value, tr) {
    for (const e of tr.effects) {
      if (e.is(_setSemanticTokensEffect)) return e.value;
    }
    return value.map(tr.changes);
  },
  provide: f => EditorView.decorations.from(f),
});

function _decodeSemanticTokens(data, legend, doc) {
  const builder = new RangeSetBuilder();
  let lineNum = 0;
  let charNum = 0;
  for (let i = 0; i + 4 < data.length; i += 5) {
    const deltaLine = data[i];
    const deltaChar = data[i + 1];
    const length    = data[i + 2];
    const tokenType = data[i + 3];
    if (deltaLine > 0) { lineNum += deltaLine; charNum = deltaChar; }
    else { charNum += deltaChar; }
    const typeName = legend.tokenTypes?.[tokenType];
    if (!typeName || lineNum + 1 > doc.lines) continue;
    const docLine = doc.line(lineNum + 1);
    const from = docLine.from + charNum;
    const to   = Math.min(from + length, docLine.to);
    if (from >= doc.length || to <= from) continue;
    builder.add(from, to, Decoration.mark({ class: `cm-st-${typeName}` }));
  }
  return builder.finish();
}

export function applySemanticTokens(tokenData, legend) {
  if (!view || !Array.isArray(tokenData)) return;
  try {
    const decos = _decodeSemanticTokens(tokenData, legend, view.state.doc);
    view.dispatch({ effects: _setSemanticTokensEffect.of(decos) });
  } catch (_) {}
}

export function clearSemanticTokens() {
  if (view) {
    try {
      view.dispatch({ effects: _setSemanticTokensEffect.of(Decoration.none) });
    } catch (_) {}
  }
}

function _clearSemanticTokens() {
  clearSemanticTokens();
}

// ── Text edits (LSP formatting) ──────────────────────────────────────────────

export function applyTextEdits(edits) {
  if (!view || !edits?.length) return;
  const doc = view.state.doc;
  const sorted = [...edits].sort((a, b) => {
    const la = a.range.start.line, lb = b.range.start.line;
    if (la !== lb) return lb - la;
    return b.range.start.character - a.range.start.character;
  });
  const changes = sorted.map(edit => {
    const sl = Math.min(edit.range.start.line + 1, doc.lines);
    const el = Math.min(edit.range.end.line + 1, doc.lines);
    const startLine = doc.line(sl);
    const endLine   = doc.line(el);
    const from = Math.min(startLine.from + (edit.range.start.character || 0), startLine.to);
    const to   = Math.min(endLine.from   + (edit.range.end.character   || 0), endLine.to);
    return { from, to, insert: String(edit.newText ?? "") };
  });
  view.dispatch({ changes });
}

async function typstCompletionSource(context) {
  const word = context.matchBefore(/[#@<]?[A-Za-z0-9:_./-]*$/);
  const prevChar = context.pos > 0 ? context.state.sliceDoc(context.pos - 1, context.pos) : "";
  const explicitTrigger = prevChar === "#" || prevChar === "@" || prevChar === "<";
  if ((!word || word.from === word.to) && !context.explicit && !explicitTrigger) return null;
  if (TYPOGRAPHIC_QUOTES.test(prevChar)) return null;

  const from = word ? word.from : context.pos;
  const typed = context.state.sliceDoc(from, context.pos);
  const trigger = /^[#@<]/.test(typed) ? typed[0] : "";
  const localOptions = buildTypstCompletions(context.state.doc.toString());

  if (_lspCompletionFn) {
    try {
      const lspResult = await Promise.race([
        _lspCompletionFn(context, {
          from,
          typed,
          trigger,
        }),
        new Promise(resolve => setTimeout(() => resolve(null), 400)),
      ]);
      if (lspResult?.options?.length > 0) {
        return {
          from: typeof lspResult.from === "number" ? lspResult.from : from,
          options: lspResult.options,
          validFor: lspResult.validFor || /^[#@<]?[A-Za-z0-9:_./-]*$/,
          filter: lspResult.filter,
        };
      }
    } catch (_) {}
  }

  return { from, options: localOptions, validFor: /^[#@<]?[A-Za-z0-9:_./-]*$/ };
}

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
let _lineWrappingEnabled = true;

function makeExtensions(filename) {
  const isTypst = String(filename || "").toLowerCase().endsWith(".typ");
  return [
    annotationMarkerField,
    annotationGutter,
    basicSetup,
    darkTheme,
    syntaxHighlighting(vscodeHighlight),
    langCompartment.of(filename ? getLanguageExt(filename) : []),
    wrapCompartment.of(_lineWrappingEnabled ? EditorView.lineWrapping : []),
    autocompletion(isTypst ? { override: [typstCompletionSource], activateOnTyping: true } : {}),
    EditorView.domEventHandlers({
      contextmenu(event) {
        if (!_editorContextMenuFn) return false;
        return _editorContextMenuFn(event, view) === true;
      },
    }),
    ...(isTypst ? [
      foldService.of((state, lineStart, lineEnd) => _lspFoldFn ? _lspFoldFn(state, lineStart, lineEnd) : null),
      EditorView.updateListener.of(update => {
        if (!update.docChanged || !update.view.hasFocus) return;
        let shouldTrigger = false;
        update.changes.iterChanges((_fromA, _toA, _fromB, _toB, inserted) => {
          if (shouldTrigger || !inserted.length) return;
          const text = inserted.toString();
          if (text.includes("#") || text.includes("@") || text.includes("<")) {
            shouldTrigger = true;
          }
        });
        if (shouldTrigger) {
          queueMicrotask(() => startCompletion(update.view));
        }
      }),
      _semanticTokensField,
      hoverTooltip((view, pos) => _lspHoverFn ? _lspHoverFn(view, pos) : typstRefTooltip(view, pos)),
      keymap.of([
        {
          key: "F12",
          run: (v) => {
            const pos = v.state.selection.main.head;
            if (_lspDefinitionFn) {
              _lspDefinitionFn(v, pos).catch(() => null).then(handled => {
                if (!handled) jumpToTypstDefinition(pos);
              });
              return true;
            }
            return jumpToTypstDefinition(pos);
          },
        },
        { key: "Ctrl-Space", run: v => { startCompletion(v); return true; } },
        { key: "Mod-Space",  run: v => { startCompletion(v); return true; } },
      ]),
      EditorView.domEventHandlers({
        mousedown(event) {
          if (!event.metaKey && !event.ctrlKey) return false;
          const pos = view?.posAtCoords({ x: event.clientX, y: event.clientY });
          if (typeof pos !== "number") return false;
          event.preventDefault();
          if (_lspMetaClickFn) {
            _lspMetaClickFn(view, pos).catch(() => null);
            return true;
          }
          if (_lspDefinitionFn) {
            _lspDefinitionFn(view, pos).catch(() => null).then(handled => {
              if (!handled) jumpToTypstDefinition(pos);
            });
            return true;
          }
          if (!jumpToTypstDefinition(pos)) return typstClickHandler(event);
          return true;
        },
      }),
    ] : []),
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
    if (!cacheOnly) {
      _settingContent = true;
      view.setState(saved);
      _settingContent = false;
    }
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

export function getTabStateContent(name) {
  const st = _tabStates.get(name);
  return st ? st.doc.toString() : null;
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

export function getSelectionSnapshot(fromState = null) {
  const source = fromState || view?.state;
  if (!source?.selection) return null;
  return {
    ranges: source.selection.ranges.map(range => ({
      anchor: range.anchor,
      head: range.head,
    })),
    mainIndex: source.selection.mainIndex,
  };
}

export function getActiveSelectionDetails() {
  if (!view?.state) return null;
  const doc = view.state.doc;
  const range = view.state.selection.main;
  const from = Math.min(range.anchor, range.head);
  const to = Math.max(range.anchor, range.head);
  const startLine = doc.lineAt(from).number;
  const endPos = Math.max(from, to > from ? to - 1 : to);
  const endLine = doc.lineAt(endPos).number;
  return {
    from,
    to,
    empty: from === to,
    selectedText: doc.sliceString(from, to),
    lineStart: startLine,
    lineEnd: endLine,
  };
}

export function getSelectionScreenRect() {
  if (!view?.state) return null;
  const range = view.state.selection.main;
  const from = Math.min(range.anchor, range.head);
  const to = Math.max(range.anchor, range.head);
  const start = view.coordsAtPos(from);
  const endPos = Math.max(from, to > from ? to - 1 : to);
  const end = view.coordsAtPos(endPos);
  if (!start && !end) return null;
  const first = start || end;
  const last = end || start;
  return {
    left: Math.min(first.left, last.left),
    right: Math.max(first.right, last.right),
    top: Math.min(first.top, last.top),
    bottom: Math.max(first.bottom, last.bottom),
  };
}

export function setCursorFromClientPoint(x, y) {
  if (!view?.state) return false;
  const pos = view.posAtCoords({ x: Number(x), y: Number(y) });
  if (typeof pos !== "number") return false;
  view.dispatch({
    selection: { anchor: pos, head: pos },
    scrollIntoView: true,
  });
  return true;
}

export function setAnnotationMarkers(items = []) {
  if (!view) return;
  const effects = [];
  if (!view.state.field(annotationMarkerField, false)) {
    effects.push(StateEffect.appendConfig.of([annotationMarkerField, annotationGutter]));
  }
  effects.push(setAnnotationMarkersEffect.of(items));
  view.dispatch({
    effects,
  });
}

export function setSelectionSnapshot(snapshot) {
  if (!view || !snapshot?.ranges?.length) return;
  try {
    const docLen = view.state.doc.length;
    const ranges = snapshot.ranges.map(range => ({
      anchor: Math.max(0, Math.min(Number(range.anchor) || 0, docLen)),
      head: Math.max(0, Math.min(Number(range.head) || 0, docLen)),
    }));
    view.dispatch({
      selection: {
        ranges,
        mainIndex: Math.max(0, Math.min(Number(snapshot.mainIndex) || 0, ranges.length - 1)),
      },
    });
  } catch (_) {}
}

function resolveDiagnosticRange(doc, lineNumber, column = 1) {
  const lineNum = Math.max(1, Math.min(Number(lineNumber) || 1, doc.lines));
  const line = doc.line(lineNum);
  const anchor = Math.min(line.from + Math.max(0, Number(column || 1) - 1), line.to);
  let to = Math.min(anchor + 1, line.to);
  if (to <= anchor) to = line.to > anchor ? line.to : anchor;
  return { from: anchor, to };
}

// Per-file diagnostic stores — both sources are merged before display
const _compileDiagsPerFile = new Map(); // filename -> raw diag[]
const _lspDiagsPerFile = new Map();     // filename -> raw diag[]

function _toCmDiag(item, source) {
  const range = resolveDiagnosticRange(view.state.doc, item.line, item.column || 1);
  return {
    from: range.from,
    to: range.to,
    severity: item.severity === "warning" ? "warning" : "error",
    message: String(item.message || "Typst diagnostic"),
    source,
  };
}

function _applyDiagnostics(forFilename) {
  if (!view) return;
  const current = String(s.activeTabName || s.selectedFile?.name || "");
  if (forFilename && current !== forFilename) return;
  const compile = (_compileDiagsPerFile.get(current) || []).map(d => _toCmDiag(d, "compile"));
  const lsp     = (_lspDiagsPerFile.get(current) || []).map(d => _toCmDiag(d, "lsp"));
  view.dispatch(setDiagnostics(view.state, [...compile, ...lsp]));
}

export function setEditorDiagnostics(filename, diagnostics = []) {
  if (!view) return;
  const target = String(filename || "");
  const current = String(s.activeTabName || s.selectedFile?.name || "");
  if (target && current === target) {
    _compileDiagsPerFile.set(target, diagnostics.filter(d => String(d?.file || "") === target));
  } else {
    _compileDiagsPerFile.set(target, []);
  }
  _applyDiagnostics(target);
}

export function setLspDiagnostics(filename, diagnostics = []) {
  _lspDiagsPerFile.set(String(filename || ""), diagnostics);
  _applyDiagnostics(String(filename || ""));
}

export function setContent(text) {
  if (!view) return;
  _settingContent = true;
  view.dispatch({ changes: { from: 0, to: view.state.doc.length, insert: text } });
  if (s.activeTabName) _tabStates.set(s.activeTabName, view.state);
  _settingContent = false;
}

export function replaceContentPreservingViewport(text, tabName = s.activeTabName) {
  if (!view) return;
  const nextText = String(text || "");
  const currentText = view.state.doc.toString();
  if (currentText === nextText) return;

  const prevSelection = getSelectionSnapshot();
  const prevScrollTop = view.scrollDOM?.scrollTop;

  _settingContent = true;
  view.dispatch({
    changes: { from: 0, to: view.state.doc.length, insert: nextText },
  });
  _settingContent = false;

  if (prevSelection?.ranges?.length) {
    setSelectionSnapshot(prevSelection);
  }
  if (typeof prevScrollTop === "number" && view.scrollDOM) {
    requestAnimationFrame(() => {
      if (view?.scrollDOM) view.scrollDOM.scrollTop = prevScrollTop;
    });
  }
  if (tabName) _tabStates.set(tabName, view.state);
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

export function setLineWrapping(enabled) {
  _lineWrappingEnabled = Boolean(enabled);
  if (!view) return;
  view.dispatch({
    effects: wrapCompartment.reconfigure(_lineWrappingEnabled ? EditorView.lineWrapping : []),
  });
}

export function isLineWrappingEnabled() {
  return _lineWrappingEnabled;
}

export function focusEditor() { view?.focus(); }
export function refreshLayout() { view?.requestMeasure(); }
export function runUndo() { return view ? undo(view) : false; }
export function runRedo() { return view ? redo(view) : false; }

export function insertAtCursor(text) {
  if (!view) return;
  const sel = view.state.selection.main;
  view.dispatch({
    changes: { from: sel.from, to: sel.to, insert: text },
    selection: { anchor: sel.from + text.length },
  });
  view.focus();
}
