// Shelf-label building for a Zebra printer.
//   * Print from the browser: draws the barcode with the bwip-js library and opens a print page
//     sized to the label. Works with the Zebra driver installed in Windows.
//   * Download ZPL: a text file Zebra printers understand natively. No library needed. Send it
//     to the printer (see the note under the buttons on the Barcodes tab).

export type Symbology = "datamatrix" | "qrcode" | "code128";
export type LabelSpec = { code: string; sku: string; title: string; bin: string | null };
export type LabelSize = { id: string; label: string; wIn: number; hIn: number };

export const SIZES: LabelSize[] = [
  { id: "4x2", label: "4 × 2 in", wIn: 4, hIn: 2 },
  { id: "3x2", label: "3 × 2 in", wIn: 3, hIn: 2 },
  { id: "225x125", label: "2.25 × 1.25 in", wIn: 2.25, hIn: 1.25 },
];

const DPI = 203; // standard Zebra desktop printers; a 300 dpi printer just prints the same label a bit smaller

const esc = (s: string) =>
  s.replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c] as string));

/** Draws the barcode and returns it as a PNG data address. Needs: npm install bwip-js */
export async function renderBarcodePng(code: string, sym: Symbology): Promise<string> {
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  const mod: any = await import("bwip-js");
  const bwip = mod.default ?? mod;
  const canvas = document.createElement("canvas");
  const opts =
    sym === "datamatrix"
      ? { bcid: "datamatrix", text: code, scale: 8, padding: 2 }
      : sym === "qrcode"
      ? { bcid: "qrcode", text: code, scale: 8, padding: 2, eclevel: "M" }
      : { bcid: "code128", text: code, scale: 4, height: 12, includetext: false, paddingwidth: 8, paddingheight: 2 };
  bwip.toCanvas(canvas, opts);
  return canvas.toDataURL("image/png");
}

/** The HTML of the print window: one label per page, exact label size, no margins. */
export type Paper = "label" | "letter";

/** What the print window is laid out for: one label per page (label printer) or a grid of labels on a regular 8.5 × 11 in sheet. */
export function buildPrintHtml(
  img: string,
  spec: LabelSpec,
  size: LabelSize,
  sym: Symbology,
  copies: number,
  paper: Paper = "label",
): string {
  const oneD = sym === "code128";
  const label = `
    <div class="lbl ${oneD ? "col" : "row"}">
      <img src="${img}" alt="" />
      <div class="txt">
        <div class="bin">${esc(spec.bin ?? "")}</div>
        <div class="title">${esc(spec.title)}</div>
        <div class="sku">${esc(spec.sku)} &nbsp;·&nbsp; ${esc(spec.code)}</div>
      </div>
    </div>`;
  const imgH = oneD ? size.hIn * 0.34 : size.hIn * 0.72;
  return `<!doctype html><html><head><meta charset="utf-8"><title>Shelf labels</title>
<style>
  ${
    paper === "letter"
      ? "@page { size: 8.5in 11in; margin: 0.25in; }\n  body { display: flex; flex-wrap: wrap; align-content: flex-start; }"
      : `@page { size: ${size.wIn}in ${size.hIn}in; margin: 0; }`
  }
  html, body { margin: 0; padding: 0; }
  .lbl { width: ${size.wIn}in; height: ${size.hIn}in; box-sizing: border-box; padding: 0.07in; overflow: hidden;
         ${
           paper === "letter"
             ? "flex: none; break-inside: avoid; outline: 1px dashed #999; outline-offset: -1px;"
             : "page-break-after: always;"
         } display: flex; gap: 0.1in; font-family: Arial, Helvetica, sans-serif; color: #000; }
  .row { flex-direction: row; align-items: center; }
  .col { flex-direction: column; align-items: stretch; }
  img { image-rendering: pixelated; flex: none; }
  .row img { height: ${imgH}in; width: ${imgH}in; }
  .col img { height: ${imgH}in; width: 100%; object-fit: fill; }
  .txt { min-width: 0; flex: 1; }
  .bin { font-weight: 800; font-size: ${size.hIn * 0.2}in; line-height: 1.05; word-break: break-all; }
  .title { font-size: ${size.hIn * 0.085}in; line-height: 1.12; max-height: ${size.hIn * 0.27}in; overflow: hidden; margin-top: 0.03in; }
  .sku { font-size: ${size.hIn * 0.07}in; margin-top: 0.04in; }
</style></head><body>${Array.from({ length: copies }, () => label).join("")}
<script>window.onload=function(){setTimeout(function(){window.focus();window.print();},300);};</script>
</body></html>`;
}

/** ZPL for the same label. Data Matrix, QR and Code 128 are built into the printer. */
export function buildZpl(spec: LabelSpec, size: LabelSize, sym: Symbology, copies: number): string {
  const W = Math.round(size.wIn * DPI);
  const H = Math.round(size.hIn * DPI);
  const m = Math.round(0.07 * DPI); // margin
  const clean = (s: string) => s.replace(/[\^~\\]/g, " ").replace(/[^\x20-\x7e]/g, "").trim();
  const code = clean(spec.code);

  const len = code.length;
  const dmModules = len <= 3 ? 10 : len <= 6 ? 12 : len <= 10 ? 14 : len <= 16 ? 16 : len <= 22 ? 18 : 22;
  const qrModules = len <= 14 ? 21 : 25;
  const fBin = Math.round(H * 0.2);
  const fTitle = Math.round(H * 0.085);
  const fSmall = Math.round(H * 0.07);

  const out: string[] = ["^XA", "^CI28", `^PW${W}`, `^LL${H}`, "^LH0,0"];
  let textX: number;
  let textY: number;
  let textW: number;

  if (sym === "code128") {
    const bh = Math.round(H * 0.34);
    out.push("^BY2,3," + bh, `^FO${m},${m}^BCN,${bh},N,N,N^FD${code}^FS`);
    textX = m;
    textY = m + bh + Math.round(H * 0.05);
    textW = W - 2 * m;
  } else {
    const modules = sym === "datamatrix" ? dmModules : qrModules;
    const mod = Math.max(2, Math.floor((H - 2 * m) * 0.85 / modules));
    const size2 = mod * modules;
    if (sym === "datamatrix") out.push(`^FO${m},${m}^BXN,${mod},200^FD${code}^FS`);
    else out.push(`^FO${m},${m}^BQN,2,${mod}^FDQA,${code}^FS`);
    textX = m + size2 + Math.round(0.1 * DPI);
    textY = m;
    textW = W - textX - m;
  }

  const bin = clean(spec.bin ?? "");
  let y = textY;
  if (bin) {
    out.push(`^FO${textX},${y}^A0N,${fBin},${fBin}^FB${textW},1,0,L^FD${bin}^FS`);
    y += Math.round(fBin * 1.15);
  }
  out.push(`^FO${textX},${y}^A0N,${fTitle},${fTitle}^FB${textW},3,0,L^FD${clean(spec.title)}^FS`);
  y += Math.round(fTitle * 3.4);
  out.push(`^FO${textX},${y}^A0N,${fSmall},${fSmall}^FB${textW},2,0,L^FD${clean(spec.sku)}  ${code}^FS`);
  out.push(`^PQ${copies}`, "^XZ");
  return out.join("\n") + "\n";
}

export function download(filename: string, content: string) {
  const url = URL.createObjectURL(new Blob([content], { type: "text/plain" }));
  const a = document.createElement("a");
  a.href = url;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}
