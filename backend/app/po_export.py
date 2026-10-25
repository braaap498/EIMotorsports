"""
Purchase order files: a PDF laid out like the QuickBooks purchase order (company block top left,
Date / P.O. No. box top right, Vendor and Ship To boxes, then Item / Description / Qty / Cost / Amount
with the Total at the bottom), and an Excel file with the same information.

Needs two packages in requirements.txt:   reportlab    openpyxl

build_pdf(po) and build_xlsx(po) both take the same dict (see _example in the tests) and return bytes:
    po_number, date (a date), vendor_name, vendor_address, ship_to, note,
    lines: [{item, description, quantity, rate}  (rate is printed under the heading "Cost")]
"""
import io
from datetime import date
from decimal import ROUND_HALF_UP, Decimal

from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.worksheet.properties import PageSetupProperties
from reportlab.lib.utils import simpleSplit
from reportlab.pdfgen import canvas

# The business's own address, printed top left. The Ship To box defaults to the warehouse (it can be
# changed on each purchase order when it is created).
COMPANY_LINES = ["E & I Motorsports LLC", "7236 Valley Rd.", "Honor, MI 49640"]
DEFAULT_SHIP_TO = "E & I Motorsports LLC\n5572 Betsie River Rd\nInterlochen, MI 49643"


def _money(v) -> Decimal:
    return Decimal(str(v)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def line_amount(line: dict) -> Decimal:
    return _money(_money(line["rate"]) * int(line["quantity"]))


def po_total(lines: list[dict]) -> Decimal:
    return sum((line_amount(l) for l in lines), Decimal("0.00"))


def _date_text(d) -> str:
    d = d if isinstance(d, date) else date.today()
    return f"{d.month:02d}/{d.day:02d}/{d.year % 100:02d}"


# --------------------------------------------------------------------------
# PDF
# --------------------------------------------------------------------------
_S = 72 / 70  # layout below was measured on a 70-dpi picture of the QuickBooks PO; this makes it points


def build_pdf(po: dict) -> bytes:
    buf = io.BytesIO()
    W, H = 612, 792
    c = canvas.Canvas(buf, pagesize=(W, H))
    c.setTitle(f"Purchase Order {po['po_number']}")

    def X(v):
        return v * _S

    def Y(v):  # distance from the TOP of the page, in layout units
        return H - v * _S

    def box(x1, y1, x2, y2, fill=None):
        if fill is not None:
            c.setFillGray(fill)
            c.rect(X(x1), Y(y2), X(x2) - X(x1), Y(y1) - Y(y2), stroke=0, fill=1)
            c.setFillGray(0)
        c.rect(X(x1), Y(y2), X(x2) - X(x1), Y(y1) - Y(y2), stroke=1, fill=0)

    def hline(x1, x2, y):
        c.line(X(x1), Y(y), X(x2), Y(y))

    def vline(x, y1, y2):
        c.line(X(x), Y(y1), X(x), Y(y2))

    c.setLineWidth(0.8)

    # ---- column layout of the item table
    TX1, TX2 = 36, 560
    COLS = [36, 113, 323, 402, 481, 560]
    BODY_BOTTOM, TOTAL_BOTTOM = 695, 727
    lines = po["lines"]
    total = po_total(lines)

    def page_one_header():
        c.setFont("Times-Roman", 11.5)
        for n, t in enumerate(COMPANY_LINES):
            c.drawString(X(38), Y(48 + (0 if n == 0 else 28 + (n - 1) * 16)), t)
        c.setFont("Helvetica-Bold", 19.5)
        c.drawString(X(404), Y(57), "Purchase Order")
        # date / PO number box
        box(428, 64, 560, 108)
        hline(428, 560, 88)
        vline(493, 64, 108)
        c.setFont("Helvetica", 9.5)
        c.drawCentredString(X(460), Y(80), "Date")
        c.drawCentredString(X(526), Y(80), "P.O. No.")
        c.setFont("Times-Roman", 9.5)
        c.drawCentredString(X(460), Y(101), _date_text(po.get("date")))
        c.drawCentredString(X(526), Y(101), str(po["po_number"]))
        # vendor and ship to
        for x1, x2, title, body in (
            (61, 301, "Vendor", f"{po['vendor_name']}\n{po.get('vendor_address') or ''}"),
            (319, 559, "Ship To", po.get("ship_to") or ""),
        ):
            box(x1, 126, x2, 231)
            hline(x1, x2, 147)
            c.setFont("Helvetica", 9.5)
            c.drawString(X(x1 + 12), Y(140), title)
            c.setFont("Times-Roman", 9)
            yy = 160
            for raw in body.splitlines():
                for part in simpleSplit(raw, "Times-Roman", 9, X(x2 - x1 - 12)) or [""]:
                    if yy > 226:
                        break
                    c.drawString(X(x1 + 3), Y(yy), part)
                    yy += 11.5
        return 300

    def table_frame(top, last):
        head_bottom = top + 24
        box(TX1, top, TX2, head_bottom, fill=0.9)
        c.setFont("Helvetica", 9.5)
        for label, x1, x2 in zip(("Item", "Description", "Qty", "Cost", "Amount"), COLS, COLS[1:]):
            c.drawCentredString(X((x1 + x2) / 2), Y(top + 15), label)
        for x in COLS[1:-1]:
            vline(x, top, BODY_BOTTOM)
        vline(TX1, top, BODY_BOTTOM)
        vline(TX2, top, BODY_BOTTOM)
        hline(TX1, TX2, BODY_BOTTOM)
        if last:
            box(TX1, BODY_BOTTOM, TX2, TOTAL_BOTTOM)
            vline(367, BODY_BOTTOM, TOTAL_BOTTOM)
            c.setFont("Helvetica-Bold", 14)
            c.drawString(X(380), Y(718), "Total")
            c.setFont("Times-Roman", 9.5)
            c.drawRightString(X(551), Y(718), f"${total:,.2f}")
        return head_bottom

    LH = 11.5  # row line height

    # Work out which rows go on which page first, so the Total is only drawn on the last page.
    desc_w = X(COLS[2] - COLS[1] - 8)

    def wrap(line):
        return simpleSplit(str(line["description"]), "Times-Roman", 9, desc_w) or [""]

    pages: list[list[dict]] = [[]]
    first_top = 300 + 24
    cont_top = 70 + 24
    used = 0.0
    limit = BODY_BOTTOM - first_top - 8
    for line in lines:
        wrapped = wrap(line)
        h = max(len(wrapped), 1) * LH + 3
        if used + h > limit and pages[-1]:
            pages.append([])
            used = 0.0
            limit = BODY_BOTTOM - cont_top - 8
        pages[-1].append((line, wrapped))
        used += h

    for n, rows in enumerate(pages):
        last = n == len(pages) - 1
        if n == 0:
            top = page_one_header()
        else:
            top = 70
            c.setFont("Helvetica", 10)
            c.drawString(X(36), Y(52), f"Purchase Order {po['po_number']} (continued, page {n + 1})")
        y = table_frame(top, last) + 14
        for line, wrapped in rows:
            c.setFont("Times-Roman", 9)
            c.drawString(X(COLS[0] + 3), Y(y), str(line.get("item") or ""))
            for k, part in enumerate(wrapped):
                c.drawString(X(COLS[1] + 4), Y(y + k * LH), part)
            c.drawRightString(X(COLS[3] - 4), Y(y), str(int(line["quantity"])))
            c.drawRightString(X(COLS[4] - 4), Y(y), f"{_money(line['rate']):,.2f}")
            c.drawRightString(X(COLS[5] - 4), Y(y), f"{line_amount(line):,.2f}")
            y += max(len(wrapped), 1) * LH + 3
        if last and po.get("note"):
            c.setFont("Times-Italic", 9)
            c.drawString(X(COLS[1] + 4), Y(y + 6), f"Note: {po['note']}"[:110])
        c.showPage()
    c.save()
    return buf.getvalue()


# --------------------------------------------------------------------------
# Excel: laid out like the PDF - a letter-size page with boxes around the date / PO number, the Vendor and
# Ship To addresses, every table cell and the Total. Prints on one page width (as many pages tall as needed).
# --------------------------------------------------------------------------
def _box(ws, ref: str, style: str = "thin") -> None:
    """Draw an outline around a block of cells (keeping any borders the cells already have)."""
    side = Side(style=style, color="000000")
    rows = list(ws[ref])
    for r, row in enumerate(rows):
        for c, cell in enumerate(row):
            old = cell.border
            cell.border = Border(
                left=side if c == 0 else old.left,
                right=side if c == len(row) - 1 else old.right,
                top=side if r == 0 else old.top,
                bottom=side if r == len(rows) - 1 else old.bottom,
            )


def _wrapped_lines(text_value: str, chars: int) -> int:
    return sum(max(1, -(-len(part) // chars)) for part in (text_value or "").splitlines() or [""])


def build_xlsx(po: dict) -> bytes:
    wb = Workbook()
    ws = wb.active
    ws.title = f"PO {po['po_number']}"
    wb.properties.title = f"Purchase Order {po['po_number']}"

    SZ = 12  # body text size (one size up from the old sheet)
    grey = PatternFill("solid", fgColor="E6E6E6")
    thin = Side(style="thin", color="000000")
    grid = Border(left=thin, right=thin, top=thin, bottom=thin)
    top_wrap = Alignment(wrap_text=True, vertical="top")
    centre = Alignment(horizontal="center", vertical="center")

    # Columns A-E: Item | Description | Qty | Cost | Amount  (about 7.3 inches wide, so it fits a letter page)
    for col, width in zip("ABCDE", (20, 52, 8, 13, 15)):
        ws.column_dimensions[col].width = width
    ws.sheet_view.showGridLines = False

    def put(ref, value, bold=False, size=SZ, align=None, fill=None, italic=False, fmt=None):
        cell = ws[ref]
        cell.value = value
        cell.font = Font(bold=bold, size=size, italic=italic)
        if align:
            cell.alignment = align
        if fill:
            cell.fill = fill
        if fmt:
            cell.number_format = fmt
        return cell

    # ---- top: company block (left) and the title with the Date / P.O. No. box (right)
    for n, t in enumerate(COMPANY_LINES):
        put(f"A{1 + n}", t, bold=(n == 0), size=SZ + (1 if n == 0 else 0))
    ws.merge_cells("C1:E2")
    put("C1", "Purchase Order", bold=True, size=20, align=Alignment(horizontal="right", vertical="center"))
    put("D3", "Date", bold=True, align=centre, fill=grey)
    put("E3", "P.O. No.", bold=True, align=centre, fill=grey)
    put("D4", _date_text(po.get("date")), align=centre)
    put("E4", po["po_number"], align=centre)
    for ref in ("D3:E3", "D4:E4", "D3:E4"):
        _box(ws, ref)
    ws.row_dimensions[4].height = 20

    # ---- Vendor box (A:B) and Ship To box (C:E), same height
    vendor = [po["vendor_name"]] + [l.strip() for l in (po.get("vendor_address") or "").splitlines() if l.strip()]
    ship = [l.strip() for l in (po.get("ship_to") or "").splitlines() if l.strip()]
    need = max(_wrapped_lines("\n".join(vendor), 70), _wrapped_lines("\n".join(ship), 34), 5)
    top, first, last = 6, 7, 7 + need - 1
    ws.merge_cells(f"A{top}:B{top}")
    ws.merge_cells(f"C{top}:E{top}")
    put(f"A{top}", "Vendor", bold=True, fill=grey, align=Alignment(vertical="center", indent=1))
    put(f"C{top}", "Ship To", bold=True, fill=grey, align=Alignment(vertical="center", indent=1))
    ws.merge_cells(f"A{first}:B{last}")
    ws.merge_cells(f"C{first}:E{last}")
    put(f"A{first}", "\n".join(vendor), align=Alignment(wrap_text=True, vertical="top", indent=1))
    put(f"C{first}", "\n".join(ship), align=Alignment(wrap_text=True, vertical="top", indent=1))
    for r in range(first, last + 1):
        ws.row_dimensions[r].height = 17
    for ref in (f"A{top}:B{top}", f"C{top}:E{top}", f"A{top}:B{last}", f"C{top}:E{last}"):
        _box(ws, ref)

    # ---- item table: every cell boxed
    head = last + 2
    for col, label in zip("ABCDE", ("Item", "Description", "Qty", "Cost", "Amount")):
        put(f"{col}{head}", label, bold=True, align=centre, fill=grey).border = grid
    ws.row_dimensions[head].height = 20
    r = head + 1
    for line in po["lines"]:
        put(f"A{r}", line.get("item") or "", align=top_wrap)
        put(f"B{r}", line["description"], align=top_wrap)
        put(f"C{r}", int(line["quantity"]), align=Alignment(horizontal="right", vertical="top"))
        put(f"D{r}", float(_money(line["rate"])), align=Alignment(horizontal="right", vertical="top"), fmt="#,##0.00")
        put(f"E{r}", float(line_amount(line)), align=Alignment(horizontal="right", vertical="top"), fmt="#,##0.00")
        for col in "ABCDE":
            ws[f"{col}{r}"].border = grid
        ws.row_dimensions[r].height = 17 * max(_wrapped_lines(str(line["description"]), 46), _wrapped_lines(str(line.get("item") or ""), 18))
        r += 1
    last_row = r - 1

    # ---- total, boxed, under the Amount column
    r += 0
    ws.merge_cells(f"C{r}:D{r}")
    put(f"C{r}", "Total", bold=True, size=SZ + 2, align=Alignment(horizontal="right", vertical="center", indent=1))
    put(f"E{r}", float(po_total(po["lines"])), bold=True, size=SZ + 2, align=Alignment(horizontal="right", vertical="center"), fmt='"$"#,##0.00')
    ws.row_dimensions[r].height = 22
    _box(ws, f"C{r}:E{r}", "medium")
    last_row = r

    # ---- note, in its own box
    if po.get("note"):
        r += 2
        ws.merge_cells(f"A{r}:E{r}")
        put(f"A{r}", "Note", bold=True, fill=grey, align=Alignment(vertical="center", indent=1))
        ws.merge_cells(f"A{r + 1}:E{r + 1}")
        put(f"A{r + 1}", str(po["note"]), italic=True, align=Alignment(wrap_text=True, vertical="top", indent=1))
        ws.row_dimensions[r + 1].height = 17 * _wrapped_lines(str(po["note"]), 95)
        _box(ws, f"A{r}:E{r}")
        _box(ws, f"A{r}:E{r + 1}")
        last_row = r + 1

    # ---- page setup: US Letter, portrait, one page wide, repeat the table heading on every page
    ws.page_setup.orientation = "portrait"
    ws.page_setup.paperSize = ws.PAPERSIZE_LETTER
    ws.page_setup.fitToWidth = 1
    ws.page_setup.fitToHeight = 0
    ws.sheet_properties.pageSetUpPr = PageSetupProperties(fitToPage=True)
    ws.page_margins.left = ws.page_margins.right = 0.5
    ws.page_margins.top = 0.6
    ws.page_margins.bottom = 0.7
    ws.print_options.horizontalCentered = True
    ws.print_area = f"A1:E{last_row}"
    ws.print_title_rows = f"{head}:{head}"
    ws.oddFooter.center.text = f"Purchase Order {po['po_number']}  -  Page &P of &N"

    out = io.BytesIO()
    wb.save(out)
    return out.getvalue()
