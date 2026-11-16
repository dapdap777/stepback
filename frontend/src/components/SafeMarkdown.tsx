// Markdown for untrusted, recorded model output.
//
// - react-markdown builds React elements; it never uses innerHTML. Raw HTML in the source is
//   not interpreted (no rehype-raw): it's shown as literal text.
// - Images are never loaded (no tracking pixels, no external requests): they render as text.
// - Links must be http(s) or mailto; others (javascript:, data:, ...) render as plain text.
import type { ReactNode } from "react";
import Markdown, { type Components } from "react-markdown";
import remarkGfm from "remark-gfm";

const SAFE_LINK = /^(https?:|mailto:)/i;

const components: Components = {
  img: ({ alt, src }) => (
    <span className="rounded bg-surface-3 px-1 font-mono text-[0.85em] text-muted" title={typeof src === "string" ? src : undefined}>
      [image{alt ? `: ${alt}` : ""}]
    </span>
  ),
  a: ({ href, children }) =>
    typeof href === "string" && SAFE_LINK.test(href) ? (
      <a href={href} target="_blank" rel="noopener noreferrer nofollow">
        {children}
      </a>
    ) : (
      <span>{children}</span>
    ),
};

export function SafeMarkdown({ children }: { children: string }): ReactNode {
  return (
    <div className="prose-sb">
      <Markdown remarkPlugins={[remarkGfm]} components={components} skipHtml={false}>
        {children}
      </Markdown>
    </div>
  );
}
