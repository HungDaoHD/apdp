"""Price-check service: parse Guardian.xlsx → compare weeks → generate DFI output xlsx."""
from __future__ import annotations

import base64
import io
from collections import defaultdict
from datetime import date, datetime
from typing import Any

import openpyxl
from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side


# ── Helpers ───────────────────────────────────────────────────────────────────

def _parse_price(v: Any) -> float | None:
    if v is None:
        return None
    s = str(v).replace(',', '').strip()
    try:
        return float(s)
    except ValueError:
        return None


def _to_date(v: Any) -> date | None:
    if v is None:
        return None
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    for fmt in ('%Y-%m-%d', '%B %d, %Y', '%d/%m/%Y'):
        try:
            return datetime.strptime(str(v).strip(), fmt).date()
        except ValueError:
            pass
    return None


def _iso_week(d: date) -> int:
    return d.isocalendar()[1]


def _parse_product_code(code: Any) -> tuple[str | None, str | None]:
    """'01-3021691-8809647390015' → ('VN-GD-3021691', '8809647390015')"""
    if not code:
        return None, None
    parts = str(code).split('-')
    if len(parts) >= 3:
        return f"VN-GD-{parts[1]}", parts[2]
    return None, None


def _others_remarks(row: dict) -> str | None:
    if row.get('mua_tang') == 'Yes':
        q1, q2 = row.get('qty1'), row.get('qty2')
        if q1 is not None and q2 is not None:
            return f"Buy {int(q1)} Get {int(q2)}"
    if row.get('mua_gia') == 'Yes':
        q3, g2 = row.get('qty3'), _parse_price(row.get('gia2'))
        if q3 is not None and g2 is not None:
            return f"Buy {int(q3)} for {int(g2):,}"
    return None


def _compute_prices(row: dict) -> tuple[float | None, float | None, float | None]:
    """Returns (price, regular, promo_off)."""
    gia_ban = _parse_price(row.get('gia_ban'))
    gia_goc = _parse_price(row.get('gia_goc'))

    if gia_ban is None:
        return None, None, None

    if row.get('giam') == 'Yes' and gia_goc is not None:
        # Case 1: normal discount
        promo_off = (gia_goc - gia_ban) / gia_goc if gia_goc > 0 else 0
        return gia_ban, gia_goc, promo_off

    if row.get('mua_tang') == 'Yes':
        # Case 3: Buy X Get Y → Regular = Giá bán, Price = Regular × Qty1 / (Qty1+Qty2)
        q1, q2 = row.get('qty1'), row.get('qty2')
        if q1 is not None and q2 is not None and (q1 + q2) > 0:
            price = gia_ban * q1 / (q1 + q2)
            promo_off = (gia_ban - price) / gia_ban if gia_ban > 0 else 0
            return price, gia_ban, promo_off
        return gia_ban, gia_ban, 0

    if row.get('mua_gia') == 'Yes':
        # Case 4: Buy X for Y → Price = Giá2 / Qty3, Regular = Giá bán
        q3, g2 = row.get('qty3'), _parse_price(row.get('gia2'))
        if q3 is not None and q3 > 0 and g2 is not None:
            price = g2 / q3
            promo_off = (gia_ban - price) / gia_ban if gia_ban > 0 else 0
            return price, gia_ban, promo_off
        return gia_ban, gia_ban, 0

    # Case 2: no promo
    return gia_ban, gia_ban, 0


# ── Parse ─────────────────────────────────────────────────────────────────────

def parse_guardian(xlsx_bytes: bytes) -> list[dict]:
    """Parse Guardian.xlsx → list of row dicts (only Bán=Yes rows)."""
    wb = openpyxl.load_workbook(io.BytesIO(xlsx_bytes), data_only=True)
    ws = wb['Q1']
    rows = list(ws.iter_rows(values_only=True))
    wb.close()

    # Row 4 (index 3) = header, data from index 4
    header = rows[3]
    col = {str(h).strip(): i for i, h in enumerate(header) if h}

    def g(row: tuple, name: str) -> Any:
        idx = col.get(name)
        return row[idx] if idx is not None and idx < len(row) else None

    records = []
    for row in rows[4:]:
        if row[0] is None:
            continue

        d = _to_date(g(row, 'Date'))
        if d is None:
            continue

        article, barcode = _parse_product_code(g(row, 'Product code'))
        if not barcode:
            continue

        ban = g(row, 'Sản phẩm có bán không?')

        records.append({
            'date':         d,
            'week':         _iso_week(d),
            'article':      article,
            'barcode':      barcode,
            'description':  g(row, 'Description'),
            'product_name': g(row, 'Product name'),
            'ban':          ban,                       # 'Yes' / 'No' / None
            'gia_ban':      g(row, 'Giá bán')      if ban == 'Yes' else None,
            'gia_goc':      g(row, 'Giá gốc')      if ban == 'Yes' else None,
            'giam':         g(row, 'Sản phẩm có giảm giá không?') if ban == 'Yes' else None,
            'promo_start':  _to_date(g(row, 'Từ ngày'))            if ban == 'Yes' else None,
            'promo_end':    _to_date(g(row, 'Đến ngày'))           if ban == 'Yes' else None,
            'mua_tang':     g(row, 'Có [Mua ... tặng ... không?]') if ban == 'Yes' else None,
            'qty1':         g(row, 'Mua với số lượng ...')         if ban == 'Yes' else None,
            'qty2':         g(row, 'Thì tặng với số lượng ...')    if ban == 'Yes' else None,
            'mua_gia':      g(row, 'Có [Mua ... với giá ... không?]') if ban == 'Yes' else None,
            'qty3':         g(row, 'Mua với số lượng là')          if ban == 'Yes' else None,
            'gia2':         g(row, 'Thì có giá là')                if ban == 'Yes' else None,
        })

    return records


# ── Aggregate ─────────────────────────────────────────────────────────────────

def aggregate_skus(records: list[dict]) -> tuple[list[dict], int, int]:
    """Group by (barcode, week), pick latest-date row per SKU.
    Returns (sku_list, curr_week, prev_week).
    """
    by_bc_week: dict[tuple, list] = defaultdict(list)
    for r in records:
        by_bc_week[(r['barcode'], r['week'])].append(r)

    all_weeks = sorted({w for _, w in by_bc_week})
    curr_week = max(all_weeks)
    prev_week = curr_week - 1

    # Preserve insertion order for barcodes
    seen: dict[str, dict] = {}
    for r in records:
        if r['barcode'] not in seen:
            seen[r['barcode']] = r

    def _best(rows: list[dict]) -> dict | None:
        """Pick latest-date row; prefer Bán=Yes over Bán=No."""
        if not rows:
            return None
        yes_rows = [r for r in rows if r.get('ban') == 'Yes']
        pool = yes_rows if yes_rows else rows
        return max(pool, key=lambda r: r['date'])

    results = []
    for stt, (bc, base) in enumerate(seen.items(), 1):
        curr_rows = by_bc_week.get((bc, curr_week), [])
        prev_rows = by_bc_week.get((bc, prev_week), [])

        curr = _best(curr_rows)
        prev = _best(prev_rows)

        cp, cr, co = _compute_prices(curr) if curr else (None, None, None)
        pp, pr, po = _compute_prices(prev) if prev else (None, None, None)

        remarks = _others_remarks(curr) if curr else (_others_remarks(prev) if prev else None)

        curr_sold = curr is not None and curr.get('ban') == 'Yes'
        prev_sold = prev is not None and prev.get('ban') == 'Yes'

        error: str | None = None
        if not curr_sold and not prev_sold:
            error = f"Not sold W{curr_week} & W{prev_week}"
        elif not curr_sold:
            error = f"Not sold W{curr_week}"
        elif not prev_sold:
            error = f"Not sold W{prev_week}"
        elif cp is not None and pp is not None and round(cp) != round(pp):
            error = f"Price changed: {int(pp):,} → {int(cp):,}"

        results.append({
            'stt':          stt,
            'article':      base['article'],
            'barcode':      base['barcode'],
            'description':  base['description'],
            'product_name': base['product_name'],
            'curr_price':   cp,
            'curr_regular': cr,
            'curr_promo_off': co,
            'promo_start':  curr['promo_start'] if curr else None,
            'promo_end':    curr['promo_end'] if curr else None,
            'remarks':      remarks,
            'prev_price':   pp,
            'prev_regular': pr,
            'prev_promo_off': po,
            'error':        error,
        })

    return results, curr_week, prev_week


# ── Generate xlsx ─────────────────────────────────────────────────────────────

def _thin() -> Border:
    s = Side(style='thin', color='BFBFBF')
    return Border(left=s, right=s, top=s, bottom=s)


def generate_xlsx(skus: list[dict], curr_week: int, prev_week: int) -> bytes:
    wb = Workbook()
    ws = wb.active
    ws.title = "Watsons"

    hdr_fill = PatternFill("solid", fgColor="1F4E79")
    hdr_font = Font(bold=True, color="FFFFFF", size=10)
    ctr      = Alignment(horizontal='center', vertical='center', wrap_text=True)
    border   = _thin()

    headers = [
        'STT', 'Aricle Number (banner)', 'BARCODE',
        'Product Description', 'Product Description Detail',
        f'Price W{curr_week}', f'Regular price W{curr_week}', f'Promo OFF W{curr_week}',
        'Promo Start Date', 'Promo End Date', 'Others Remarks', "Agency's comment",
        f'Price W{prev_week}', f'Regular price W{prev_week}', f'Promo OFF W{prev_week}',
        'ERRORS',
    ]

    for ci, h in enumerate(headers, 1):
        c = ws.cell(row=1, column=ci, value=h)
        c.fill = hdr_fill
        c.font = hdr_font
        c.alignment = ctr
        c.border = border

    alt_fill   = PatternFill("solid", fgColor="DEEAF1")
    err_fill   = PatternFill("solid", fgColor="FCE4D6")
    norm_font  = Font(size=10)
    num_fmt    = '#,##0'
    pct_fmt    = '0.00%'
    date_fmt   = 'D-MMM-YY'

    for ri, sku in enumerate(skus, 2):
        fill = err_fill if sku['error'] else (alt_fill if ri % 2 == 0 else None)
        vals = [
            sku['stt'], sku['article'], sku['barcode'],
            sku['description'], sku['product_name'],
            sku['curr_price'], sku['curr_regular'], sku['curr_promo_off'],
            sku['promo_start'], sku['promo_end'], sku['remarks'], None,
            sku['prev_price'], sku['prev_regular'], sku['prev_promo_off'],
            sku['error'],
        ]
        for ci, val in enumerate(vals, 1):
            c = ws.cell(row=ri, column=ci, value=val)
            c.font = norm_font
            c.border = border
            c.alignment = Alignment(vertical='center')
            if fill:
                c.fill = fill
            h = headers[ci - 1]
            if 'Price' in h or 'Regular' in h:
                c.number_format = num_fmt
            elif 'Promo OFF' in h:
                c.number_format = pct_fmt
            elif 'Date' in h and isinstance(val, date):
                c.number_format = date_fmt

    col_widths = [5, 18, 16, 42, 58, 14, 14, 12, 14, 14, 18, 16, 14, 14, 12, 30]
    for ci, w in enumerate(col_widths, 1):
        ws.column_dimensions[ws.cell(1, ci).column_letter].width = w

    ws.row_dimensions[1].height = 36
    ws.freeze_panes = 'A2'

    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


# ── Entry point ───────────────────────────────────────────────────────────────

def process(xlsx_bytes: bytes) -> dict:
    records = parse_guardian(xlsx_bytes)
    if not records:
        raise ValueError("No valid rows found — check that the file has a 'Q1' sheet with Bán=Yes rows")

    skus, curr_week, prev_week = aggregate_skus(records)
    n_errors = sum(1 for s in skus if s['error'])

    xlsx_out = generate_xlsx(skus, curr_week, prev_week)

    return {
        'filename':     f"DFI_PriceCheck_W{curr_week}_vs_W{prev_week}.xlsx",
        'week_current': f'W{curr_week}',
        'week_prev':    f'W{prev_week}',
        'n_skus':       len(skus),
        'n_errors':     n_errors,
        'base64':       base64.b64encode(xlsx_out).decode('ascii'),
        'errors': [
            {'stt': s['stt'], 'barcode': s['barcode'],
             'description': s['description'], 'error': s['error']}
            for s in skus if s['error']
        ],
    }
