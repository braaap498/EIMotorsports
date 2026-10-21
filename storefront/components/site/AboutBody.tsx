import type { ReactNode } from "react";

// Turns the About page's plain text into headings, paragraphs and bullets.
//   "## Something"  -> a heading
//   "- Something"   -> a bullet (a run of them makes one list)
//   a blank line    -> starts a new paragraph
// Everything is drawn as plain text, so nothing typed in the editor can inject HTML.
export default function AboutBody({ text }: { text: string }) {
  const out: ReactNode[] = [];
  let para: string[] = [];
  let bullets: string[] = [];
  let n = 0;

  const flushPara = () => {
    if (para.length) out.push(<p key={n++} className="whitespace-pre-line text-gray-700">{para.join("\n")}</p>);
    para = [];
  };
  const flushBullets = () => {
    if (bullets.length) {
      out.push(
        <ul key={n++} className="list-disc space-y-1 pl-6 text-gray-700">
          {bullets.map((b, i) => (
            <li key={i}>{b}</li>
          ))}
        </ul>
      );
    }
    bullets = [];
  };

  for (const raw of text.split(/\r?\n/)) {
    const line = raw.trimEnd();
    if (!line.trim()) {
      flushPara();
      flushBullets();
    } else if (line.startsWith("## ")) {
      flushPara();
      flushBullets();
      out.push(<h2 key={n++} className="pt-2 text-xl font-bold text-gray-900">{line.slice(3).trim()}</h2>);
    } else if (line.startsWith("- ")) {
      flushPara();
      bullets.push(line.slice(2).trim());
    } else {
      flushBullets();
      para.push(line);
    }
  }
  flushPara();
  flushBullets();

  return <div className="space-y-4 leading-relaxed">{out}</div>;
}
