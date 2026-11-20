/* Reads a CSV / tab / semicolon / pipe separated file into rows of cells.
   Handles quoted cells (with commas, line breaks and "" inside), CRLF or LF line ends, and a byte order mark.
   Blank lines are dropped. The separator is guessed from the first line. */

const CANDIDATES = [",", "\t", ";", "|"];

function sniff(text: string): string {
  const counts = CANDIDATES.map(() => 0);
  let inQuotes = false;
  for (let i = 0; i < text.length; i++) {
    const c = text[i];
    if (c === '"') inQuotes = !inQuotes;
    else if (!inQuotes && (c === "\n" || c === "\r")) break;
    else if (!inQuotes) {
      const k = CANDIDATES.indexOf(c);
      if (k >= 0) counts[k]++;
    }
  }
  const best = counts.indexOf(Math.max(...counts));
  return counts[best] > 0 ? CANDIDATES[best] : ",";
}

export function parseDelimited(input: string): { delimiter: string; rows: string[][] } {
  const text = input.replace(/^﻿/, "");
  const delimiter = sniff(text);
  const rows: string[][] = [];
  let row: string[] = [];
  let cell = "";
  let inQuotes = false;

  const endRow = () => {
    row.push(cell);
    cell = "";
    if (row.length > 1 || row[0].trim() !== "") rows.push(row);
    row = [];
  };

  for (let i = 0; i < text.length; i++) {
    const c = text[i];
    if (inQuotes) {
      if (c === '"') {
        if (text[i + 1] === '"') {
          cell += '"';
          i++;
        } else {
          inQuotes = false;
        }
      } else {
        cell += c;
      }
    } else if (c === '"' && cell === "") {
      inQuotes = true;
    } else if (c === delimiter) {
      row.push(cell);
      cell = "";
    } else if (c === "\n" || c === "\r") {
      if (c === "\r" && text[i + 1] === "\n") i++;
      endRow();
    } else {
      cell += c;
    }
  }
  if (cell !== "" || row.length > 0) endRow();
  return { delimiter, rows };
}
