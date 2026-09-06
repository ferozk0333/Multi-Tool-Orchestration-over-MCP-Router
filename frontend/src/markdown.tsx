// A small markdown renderer for answers. The model writes tables, lists and bold, and showing
// the raw asterisks is worse than not formatting at all. No dependency, no HTML injection.

import type { ReactNode } from "react";

const INLINE = /(\*\*[^*]+?\*\*|`[^`]+?`|\*[^*\n]+?\*|\[[^\]]+?\]\([^)]+?\))/g;

// Renders bold, inline code, italics and link text inside one line.
export function inline(text: string, keyBase = ""): ReactNode[] {
  const out: ReactNode[] = [];
  let last = 0;
  let match: RegExpExecArray | null;
  const re = new RegExp(INLINE);
  while ((match = re.exec(text)) !== null) {
    if (match.index > last) out.push(text.slice(last, match.index));
    const tok = match[0];
    const key = `${keyBase}-${match.index}`;
    if (tok.startsWith("**")) out.push(<strong key={key}>{tok.slice(2, -2)}</strong>);
    else if (tok.startsWith("`")) out.push(<code key={key}>{tok.slice(1, -1)}</code>);
    else if (tok.startsWith("[")) out.push(tok.slice(1, tok.indexOf("]")));
    else out.push(<em key={key}>{tok.slice(1, -1)}</em>);
    last = match.index + tok.length;
  }
  if (last < text.length) out.push(text.slice(last));
  return out;
}

function cells(line: string): string[] {
  return line.replace(/^\s*\|/, "").replace(/\|\s*$/, "").split("|").map((c) => c.trim());
}

const isTableRow = (l: string) => /^\s*\|.*\|\s*$/.test(l);
const isDivider = (l: string) => /^\s*\|?[\s:-]*-[\s:|-]*\|?\s*$/.test(l) && l.includes("-");

// Renders a markdown block into elements: headings, tables, lists, quotes, paragraphs.
export function Markdown({ text }: { text: string }) {
  const lines = text.replace(/\r/g, "").split("\n");
  const out: ReactNode[] = [];
  let i = 0;

  while (i < lines.length) {
    const line = lines[i];

    if (!line.trim()) { i++; continue; }

    // Table: a header row, a divider, then body rows.
    if (isTableRow(line) && i + 1 < lines.length && isDivider(lines[i + 1])) {
      const head = cells(line);
      i += 2;
      const body: string[][] = [];
      while (i < lines.length && isTableRow(lines[i])) body.push(cells(lines[i++]));
      out.push(
        <div className="tablewrap" key={`t${i}`}>
          <table>
            <thead>
              <tr>{head.map((c, n) => <th key={n}>{inline(c, `h${n}`)}</th>)}</tr>
            </thead>
            <tbody>
              {body.map((r, n) => (
                <tr key={n}>{r.map((c, m) => <td key={m}>{inline(c, `c${n}-${m}`)}</td>)}</tr>
              ))}
            </tbody>
          </table>
        </div>,
      );
      continue;
    }

    const heading = /^(#{1,4})\s+(.*)$/.exec(line);
    if (heading) {
      out.push(<h3 key={`h${i}`}>{inline(heading[2], `hd${i}`)}</h3>);
      i++;
      continue;
    }

    if (/^\s*>\s?/.test(line)) {
      const quote: string[] = [];
      while (i < lines.length && /^\s*>\s?/.test(lines[i])) {
        quote.push(lines[i++].replace(/^\s*>\s?/, ""));
      }
      out.push(<blockquote key={`q${i}`}>{inline(quote.join(" "), `qq${i}`)}</blockquote>);
      continue;
    }

    const bullet = /^\s*[-*+]\s+/;
    const numbered = /^\s*\d+[.)]\s+/;
    if (bullet.test(line) || numbered.test(line)) {
      const ordered = numbered.test(line);
      const items: string[] = [];
      const re = ordered ? numbered : bullet;
      while (i < lines.length && re.test(lines[i])) items.push(lines[i++].replace(re, ""));
      const li = items.map((t, n) => <li key={n}>{inline(t, `li${n}`)}</li>);
      out.push(ordered ? <ol key={`l${i}`}>{li}</ol> : <ul key={`l${i}`}>{li}</ul>);
      continue;
    }

    // Paragraph: consecutive non-blank lines that start no other block.
    const para: string[] = [];
    while (
      i < lines.length && lines[i].trim() &&
      !isTableRow(lines[i]) && !/^(#{1,4})\s/.test(lines[i]) &&
      !bullet.test(lines[i]) && !numbered.test(lines[i]) && !/^\s*>\s?/.test(lines[i])
    ) {
      para.push(lines[i++]);
    }
    if (para.length) out.push(<p key={`p${i}`}>{inline(para.join(" "), `pp${i}`)}</p>);
  }

  return <>{out}</>;
}
