import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.patches as patches
from matplotlib.patches import FancyArrowPatch

INK = "#1c2318"
SUB = "#55604c"
ACCENT = "#a5691f"
LINE = "#cfd3c2"
BOX_FILL = "#f7f8f2"
BOX2_FILL = "#ece0c9"

fig, ax = plt.subplots(figsize=(11, 6.2), dpi=200)
ax.set_xlim(0, 11)
ax.set_ylim(0, 6.2)
ax.axis("off")
fig.patch.set_facecolor("white")

def box(x, y, w, h, title, lines, fill=BOX_FILL, title_color=INK):
    rect = patches.FancyBboxPatch(
        (x, y), w, h, boxstyle="round,pad=0.02,rounding_size=0.08",
        linewidth=1.3, edgecolor=LINE, facecolor=fill,
    )
    ax.add_patch(rect)
    ax.text(x + w / 2, y + h - 0.32, title, ha="center", va="top",
            fontsize=12.5, fontweight="bold", color=title_color, family="Georgia")
    for i, line in enumerate(lines):
        ax.text(x + w / 2, y + h - 0.68 - i * 0.32, line, ha="center", va="top",
                fontsize=9.3, color=SUB, family="DejaVu Sans")
    return rect

def arrow(x1, y1, x2, y2, label, color=ACCENT, style="-", dy_label=0.22, curve=0.0):
    a = FancyArrowPatch((x1, y1), (x2, y2), arrowstyle="-|>", mutation_scale=16,
                         linewidth=1.6, color=color, linestyle=style,
                         connectionstyle=f"arc3,rad={curve}")
    ax.add_patch(a)
    mx, my = (x1 + x2) / 2, (y1 + y2) / 2
    ax.text(mx, my + dy_label, label, ha="center", va="bottom", fontsize=8.6,
            color=color, fontweight="bold", family="DejaVu Sans")

# Boxes
box(0.3, 2.3, 3.1, 3.4, "LedgerBooks", [
    "Full double-entry accounting",
    "Multi-company / multi-tenant",
    "AR: Invoices, Estimates,",
    "Payments, Credit Memos",
    "AP: Vendors, Bills, POs",
    "Inventory, COGS, Banking",
], fill=BOX_FILL)

box(3.95, 2.3, 3.1, 3.4, "MRA_TaxInvoice_System", [
    "MRA compliance engine",
    "Invoice hash chain",
    "VAT calc (TC01–TC06)",
    "POS · Inventory · Analytics",
    "Multi-company / currency",
    "Offline retry queue",
], fill=BOX2_FILL)

box(7.6, 2.3, 3.1, 3.4, "MRA", [
    "Mauritius Revenue",
    "Authority",
    "",
    "Invoice Fiscalisation",
    "Platform (sandbox / live)",
], fill=BOX_FILL)

# Forward arrows
arrow(3.4, 4.55, 3.95, 4.55, "POST /api/v1/fiscalize\n(X-Api-Key header)", curve=0.0, dy_label=0.15)
arrow(7.05, 4.55, 7.6, 4.55, "sign + encrypt +\ntransmit invoice", curve=0.0, dy_label=0.15)

# Return arrows (dashed, curving below)
arrow(7.6, 3.1, 7.05, 3.1, "IRN + QR code", color=SUB, style="--", curve=-0.25, dy_label=-0.42)
arrow(3.95, 3.1, 3.4, 3.1, "invoice_number, IRN, status", color=SUB, style="--", curve=-0.25, dy_label=-0.42)

# Trigger label from LedgerBooks
ax.annotate("Invoice or Credit Memo\nposted to the ledger",
            xy=(1.85, 2.3), xytext=(1.85, 1.15),
            ha="center", fontsize=9, color=ACCENT, fontweight="bold",
            arrowprops=dict(arrowstyle="-|>", color=ACCENT, lw=1.4))

ax.text(5.5, 0.35, "Fiscalisation is a best-effort follow-up — the ledger entry always posts first and is never rolled back if MRA is unreachable.",
        ha="center", fontsize=8.6, color=SUB, style="italic")

plt.tight_layout()
plt.savefig("architecture_diagram.png", facecolor="white", bbox_inches="tight")
print("saved")
