import DOMPurify from "dompurify";

/**
 * Attributes the rendered markdown needs that are not in DOMPurify's defaults.
 * A delegated click handler reads these to copy a code block or a math source,
 * so stripping them silently breaks the copy buttons.
 */
const ALLOWED_DATA_ATTRIBUTES = [
  "data-code",
  "data-code-id",
  "data-math-source",
];

/** Tags that can execute code, load a remote document, or restyle the page. */
const FORBIDDEN_TAGS = ["script", "style", "iframe", "object", "embed"];

/**
 * Strip anything executable from HTML built out of model output.
 *
 * Model replies reach the dashboard through `{@html}`, so a reply carrying
 * `<img src=x onerror=...>` — written by the model, or echoed from a tool
 * result or pasted page — would run in the dashboard's origin, which can reach
 * every control endpoint on the node and read the whole event log.
 *
 * KaTeX output survives the default profile: it is spans, classes, inline
 * styles and MathML.
 */
export function sanitizeMarkdownHtml(html: string): string {
  if (!DOMPurify.isSupported) {
    // Without a DOM, DOMPurify.sanitize is an identity function, which would
    // pass model output through untouched. The dashboard renders markdown only
    // in the browser, so reaching this means a caller changed that; escape the
    // input rather than trust it.
    return escapeHtml(html);
  }
  return DOMPurify.sanitize(html, {
    ADD_ATTR: ALLOWED_DATA_ATTRIBUTES,
    FORBID_TAGS: FORBIDDEN_TAGS,
    ALLOW_UNKNOWN_PROTOCOLS: false,
  });
}

/**
 * Escape text that is about to be interpolated into an HTML string.
 *
 * The LaTeX preprocessor wraps captured content in markup before markdown runs,
 * which puts raw model output inside an HTML string. Sanitizing the final
 * document already blocks execution; escaping here keeps the captured text from
 * changing the surrounding markup in the first place.
 */
export function escapeHtml(text: string): string {
  return text
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}
