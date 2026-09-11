import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.patches as patches
from matplotlib.patches import FancyArrowPatch

INK = "#1c2318"
SUB = "#55604c"
ACCENT = "#a5691f"
ACCENT2 = "#3f7a4e"
LINE = "#cfd3c2"
BOX_FILL = "#f7f8f2"
BOX2_FILL = "#ece0c9"
BOX3_FILL = "#e2ede4"

FIG_W, FIG_H = 9.6, 15.6
fig, ax = plt.subplots(figsize=(FIG_W, FIG_H), dpi=200)
ax.set_xlim(0, FIG_W)
ax.set_ylim(0, FIG_H)
ax.axis("off")
fig.patch.set_facecolor("white")

LANE_LB_X, LANE_LB_W = 0.5, 4.05
LANE_MRA_X, LANE_MRA_W = 5.05, 4.05

TITLE_LINE_H = 0.42   # space reserved for the title row inside a box
BODY_LINE_H = 0.30    # space per body line
PAD_TOP = 0.18
PAD_BOTTOM = 0.20

def box_height(n_lines):
    return PAD_TOP + TITLE_LINE_H + n_lines * BODY_LINE_H + PAD_BOTTOM

def lane_header(x, w, title, sub, fill):
    y = FIG_H - 0.75
    rect = patches.FancyBboxPatch((x, y), w, 0.6, boxstyle="round,pad=0.02,rounding_size=0.06",
                                   linewidth=1.3, edgecolor=LINE, facecolor=fill, zorder=2)
    ax.add_patch(rect)
    ax.text(x + w / 2, y + 0.40, title, ha="center", va="center", fontsize=12.5, fontweight="bold",
            color=INK, family="Georgia")
    ax.text(x + w / 2, y + 0.14, sub, ha="center", va="center", fontsize=8.3, color=SUB)
    return y

def step_box(lane_x, lane_w, top_y, num, title, lines, fill=BOX_FILL, num_color=ACCENT):
    """Draws a box with its TOP at top_y, sized to fit its content. Returns the y of its bottom edge."""
    h = box_height(len(lines))
    y = top_y - h
    rect = patches.FancyBboxPatch((lane_x, y), lane_w, h, boxstyle="round,pad=0.02,rounding_size=0.07",
                                   linewidth=1.3, edgecolor=LINE, facecolor=fill, zorder=2)
    ax.add_patch(rect)
    badge_cy = y + h - PAD_TOP - TITLE_LINE_H / 2
    badge = patches.Circle((lane_x + 0.32, badge_cy), 0.185, facecolor=num_color, edgecolor="none", zorder=3)
    ax.add_patch(badge)
    ax.text(lane_x + 0.32, badge_cy, str(num), ha="center", va="center", fontsize=10.5,
            fontweight="bold", color="white", zorder=4)
    ax.text(lane_x + 0.66, badge_cy, title, ha="left", va="center", fontsize=10.2,
            fontweight="bold", color=INK, family="Georgia", zorder=4)
    first_line_y = y + h - PAD_TOP - TITLE_LINE_H
    for i, line in enumerate(lines):
        ax.text(lane_x + 0.32, first_line_y - i * BODY_LINE_H, line, ha="left", va="top",
                fontsize=8.3, color=SUB, zorder=4)
    return y, y + h  # (bottom, top)

def cross_arrow(y1, y2, label, direction="right", color=ACCENT, style="-", label_dy=0.16):
    x1 = LANE_LB_X + LANE_LB_W if direction == "right" else LANE_MRA_X
    x2 = LANE_MRA_X if direction == "right" else LANE_LB_X + LANE_LB_W
    a = FancyArrowPatch((x1, y1), (x2, y2), arrowstyle="-|>", mutation_scale=14,
                         linewidth=1.6, color=color, linestyle=style, zorder=3)
    ax.add_patch(a)
    ax.text((x1 + x2) / 2, (y1 + y2) / 2 + label_dy, label, ha="center",
            va="bottom" if label_dy >= 0 else "top", fontsize=7.4, color=color, fontweight="bold")

def note(cy, lane_x, lane_w, text, color=ACCENT2):
    ax.text(lane_x + lane_w / 2, cy, text, ha="center", va="center", fontsize=7.6,
            color=color, style="italic")

header_top = lane_header(LANE_LB_X, LANE_LB_W, "LedgerBooks", "Company: Second Test Co Ltd", BOX_FILL)
lane_header(LANE_MRA_X, LANE_MRA_W, "MRA_TaxInvoice_System", "Company: MRA Second Co Ltd", BOX2_FILL)

cursor_lb = header_top - 0.25

# ── Step 1 ──
b1_bottom, b1_top = step_box(LANE_LB_X, LANE_LB_W, cursor_lb, 1, "User submits New Invoice form", [
    "Customer: Co2 Customer",
    "Line: Test Service   Qty 1 @ 1,000.00",
    "VAT rate: 15%  (line marked taxable)",
])
cursor_lb = b1_bottom - 0.22

# ── Step 2 ──
b2_bottom, b2_top = step_box(LANE_LB_X, LANE_LB_W, cursor_lb, 2, "LedgerBooks posts the ledger entry", [
    "Invoice INV-0001  (company-scoped numbering)",
    "Journal Entry #60 — 2026-08-14:",
    "  Dr 1200 Accounts Receivable    1,150.00",
    "  Cr 4000 Sales Revenue              1,000.00",
    "  Cr 2100 VAT Payable                     150.00",
])
cursor_lb = b2_bottom - 0.10
note(cursor_lb, LANE_LB_X, LANE_LB_W, "committed — exists in LedgerBooks' own books,\nindependent of what happens in step 3")
cursor_lb = cursor_lb - 0.42

# ── Step 3 ──
b3_bottom, b3_top = step_box(LANE_LB_X, LANE_LB_W, cursor_lb, 3, "Bridge call fired", [
    "POST http://localhost:5059/api/v1/fiscalize",
    "X-Api-Key: adc1bad8...c9de9d21",
    "Company 2's own key/URL — resolved from the",
    "invoice's own company_id, not the caller",
])

# ── Step 4 (MRA side), aligned to sit beside step 3 ──
cursor_mra = b3_top
b4_bottom, b4_top = step_box(LANE_MRA_X, LANE_MRA_W, cursor_mra, 4, "MRA_TaxInvoice_System receives it", [
    "Customer 'Co2 Customer' matched/auto-created",
    "Document type: STD  ·  VAT code: TC01 (15%)",
    "Subtotal 1,000.00 + VAT 150.00 = 1,150.00 MUR",
])

cross_arrow((b3_bottom + b3_top) / 2, (b4_bottom + b4_top) / 2,
            "customer + line_items\nJSON payload", direction="right", color=ACCENT, label_dy=0.05)

cursor_lb = b3_bottom - 0.30
cursor_mra = b4_bottom - 0.28

# ── Step 5 (MRA side) ──
b5_bottom, b5_top = step_box(LANE_MRA_X, LANE_MRA_W, cursor_mra, 5, "Invoice fiscalised (offline mode)", [
    "Invoice No.: STD-000001",
    "IRN: OFFLINE-E7E868E322DF",
    "Status: OFFLINE — no MRA EBS credentials",
    "configured yet for this company (Section 4.7)",
])

# ── Step 6 (LedgerBooks side) ──
b6_bottom, b6_top = step_box(LANE_LB_X, LANE_LB_W, cursor_lb, 6, "Result saved back onto the invoice", [
    "INV-0001.mra_invoice_number = STD-000001",
    "INV-0001.mra_irn = OFFLINE-E7E868E322DF",
    "INV-0001.mra_status = OFFLINE",
])

cross_arrow(b5_top - 0.25, b6_top - 0.25,
            "{ invoice_number, irn, status }\nreturned in the response", direction="left", color=SUB, style="--", label_dy=0.12)

cursor_lb = b6_bottom - 0.22

# ── Step 7 ──
b7_bottom, b7_top = step_box(LANE_LB_X, LANE_LB_W, cursor_lb, 7, "Visible on the invoice detail page", [
    '"MRA: OFFLINE · STD-000001 · IRN OFFLINE-E7E868E322DF"',
    "shown directly under the invoice header",
])

bottom_of_lanes = min(b7_bottom, b5_bottom) - 0.35

# ── Verification banner ──
verify_h = 1.85
verify_y = bottom_of_lanes - verify_h
verify = patches.FancyBboxPatch((0.5, verify_y), LANE_MRA_X + LANE_MRA_W - 0.5, verify_h,
                                 boxstyle="round,pad=0.03,rounding_size=0.08",
                                 linewidth=1.4, edgecolor=ACCENT2, facecolor=BOX3_FILL, zorder=2)
ax.add_patch(verify)
cx = (0.5 + LANE_MRA_X + LANE_MRA_W) / 2
ax.text(cx, verify_y + verify_h - 0.32, "Verified independently on both sides", ha="center", va="center",
        fontsize=11, fontweight="bold", color=ACCENT2, family="Georgia")
ax.text(cx, verify_y + verify_h - 0.72,
        "LedgerBooks (Company 2, \"Second Test Co Ltd\"): Invoice INV-0001 shows MRA STD-000001 / OFFLINE-E7E868E322DF",
        ha="center", va="center", fontsize=8.4, color=SUB)
ax.text(cx, verify_y + verify_h - 1.06,
        "MRA_TaxInvoice_System (\"MRA Second Co Ltd\"): STD-000001 present in its own invoice list, total 1,150.00 MUR",
        ha="center", va="center", fontsize=8.4, color=SUB)
ax.text(cx, verify_y + verify_h - 1.40,
        "Confirmed absent from Company 1 / the original MRA company on both sides — no cross-tenant bleed",
        ha="center", va="center", fontsize=8.4, color=SUB)

ax.text(cx, verify_y - 0.35,
        "Fiscalisation is a best-effort follow-up: step 2's journal entry commits before step 3 is even attempted,\n"
        "so the invoice is fully posted and correct in LedgerBooks regardless of what happens on the MRA side.",
        ha="center", va="top", fontsize=8, color=SUB, style="italic")

ax.set_ylim(max(0, verify_y - 1.1), FIG_H)

plt.tight_layout()
plt.savefig("example_transaction_diagram.png", facecolor="white", bbox_inches="tight")
print("saved")
