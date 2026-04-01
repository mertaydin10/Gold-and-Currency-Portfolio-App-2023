"""Tkinter arayüz: varlık özeti, portföy kaydı, tarihsel kur, grafik, kâr/zarar."""

from __future__ import annotations

import tkinter as tk
from tkinter import messagebox, ttk
from datetime import date, datetime

import database as db
from collect_api import KNOWN_ASSET_LABELS, fetch_rates
from portfolio_service import (
    current_totals_latest_save,
    default_asset_codes,
    merge_with_defaults,
)
from valuation import (
    breakdown_for_snapshot_view,
    compute_lot_pnl,
    interpolated_daily_series,
    ordered_asset_codes,
)


def _fmt_money(x: float) -> str:
    return f"{x:,.2f} TL".replace(",", "X").replace(".", ",").replace("X", ".")


def _fmt_qty(x: float) -> str:
    if abs(x - round(x)) < 1e-6:
        return str(int(round(x)))
    return f"{x:.4f}".rstrip("0").rstrip(".")


def _label_for_code(code: str) -> str:
    return KNOWN_ASSET_LABELS.get(code, code)


def _fmt_short_date(d: date) -> str:
    return d.strftime("%d.%m")


class VarlikApp(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("Altın & Döviz — TL Karşılık")
        self.minsize(960, 640)
        self._qty_vars: dict[str, tk.StringVar] = {}
        self._snapshot_var = tk.StringVar()
        self._chart_asset = tk.StringVar()
        self._status = tk.StringVar(value="Hazır.")
        self._eff_date = tk.StringVar(value=date.today().isoformat())

        db.init_db()

        self._build()
        self.after(100, self._bootstrap)

    def _build(self) -> None:
        top = ttk.Frame(self, padding=8)
        top.pack(fill=tk.X)
        ttk.Button(top, text="Kurları yenile (CollectAPI)", command=self._on_refresh_rates).pack(
            side=tk.LEFT, padx=(0, 8)
        )
        ttk.Label(top, textvariable=self._status).pack(side=tk.LEFT)

        nb = ttk.Notebook(self)
        nb.pack(fill=tk.BOTH, expand=True, padx=8, pady=(0, 8))

        self.tab_summary = ttk.Frame(nb, padding=8)
        self.tab_portfolio = ttk.Frame(nb, padding=8)
        self.tab_chart = ttk.Frame(nb, padding=8)
        self.tab_pnl = ttk.Frame(nb, padding=8)
        nb.add(self.tab_summary, text="Özet")
        nb.add(self.tab_portfolio, text="Portföy kaydı")
        nb.add(self.tab_chart, text="Grafik")
        nb.add(self.tab_pnl, text="Kâr / Zarar")

        self._build_summary()
        self._build_portfolio()
        self._build_chart()
        self._build_pnl()

    def _build_summary(self) -> None:
        f = ttk.Frame(self.tab_summary)
        f.pack(fill=tk.X)
        ttk.Label(f, text="Kur anlık görüntü:").pack(side=tk.LEFT)
        self._cb_snapshot = ttk.Combobox(
            f, textvariable=self._snapshot_var, width=48, state="readonly"
        )
        self._cb_snapshot.pack(side=tk.LEFT, padx=8)
        self._cb_snapshot.bind("<<ComboboxSelected>>", lambda e: self._render_summary_table())

        self._lbl_total = ttk.Label(self.tab_summary, text="", font=("", 14, "bold"))
        self._lbl_total.pack(anchor=tk.W, pady=(8, 4))

        cols = ("code", "name", "buy", "sell", "qty", "tl")
        self.tree_sum = ttk.Treeview(self.tab_summary, columns=cols, show="headings", height=18)
        self.tree_sum.heading("code", text="Kod")
        self.tree_sum.heading("name", text="Varlık")
        self.tree_sum.heading("buy", text="Alış")
        self.tree_sum.heading("sell", text="Satış")
        self.tree_sum.heading("qty", text="Miktar")
        self.tree_sum.heading("tl", text="TL (satış)")
        self.tree_sum.column("code", width=90)
        self.tree_sum.column("name", width=160)
        self.tree_sum.column("buy", width=90)
        self.tree_sum.column("sell", width=90)
        self.tree_sum.column("qty", width=100)
        self.tree_sum.column("tl", width=140)
        sy = ttk.Scrollbar(self.tab_summary, orient=tk.VERTICAL, command=self.tree_sum.yview)
        self.tree_sum.configure(yscrollcommand=sy.set)
        self.tree_sum.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        sy.pack(side=tk.RIGHT, fill=tk.Y)

    def _build_portfolio(self) -> None:
        g = ttk.LabelFrame(self.tab_portfolio, text="Yeni kayıt", padding=8)
        g.pack(fill=tk.X)
        ttk.Label(g, text="Geçerlilik tarihi (YYYY-MM-DD):").grid(row=0, column=0, sticky=tk.W)
        ttk.Entry(g, textvariable=self._eff_date, width=14).grid(row=0, column=1, sticky=tk.W)

        codes = default_asset_codes()

        grid = ttk.Frame(self.tab_portfolio, padding=(0, 8))
        grid.pack(fill=tk.BOTH, expand=True)
        for i, code in enumerate(codes):
            ttk.Label(grid, text=f"{_label_for_code(code)} ({code})").grid(
                row=i, column=0, sticky=tk.W, pady=2
            )
            var = tk.StringVar(value="0")
            self._qty_vars[code] = var
            ttk.Entry(grid, textvariable=var, width=18).grid(row=i, column=1, sticky=tk.W, padx=8)

        ttk.Button(
            self.tab_portfolio,
            text="Miktarları kaydet (geçmiş silinmez)",
            command=self._on_save_portfolio,
        ).pack(anchor=tk.W, pady=8)

        ttk.Label(
            self.tab_portfolio,
            text="Her kayıt tüm satırları içerir; eksik bıraktığınız kodlar önceki kayıttaki değerleri korur.",
            wraplength=700,
        ).pack(anchor=tk.W)

    def _build_chart(self) -> None:
        row = ttk.Frame(self.tab_chart)
        row.pack(fill=tk.X)
        ttk.Label(row, text="Varlık:").pack(side=tk.LEFT)
        self._cb_chart = ttk.Combobox(
            row, textvariable=self._chart_asset, width=20, state="readonly"
        )
        self._cb_chart.pack(side=tk.LEFT, padx=8)
        ttk.Button(row, text="Grafiği çiz", command=self._on_draw_chart).pack(side=tk.LEFT)
        self._chart_info = ttk.Label(self.tab_chart, text="")
        self._chart_info.pack(anchor=tk.W, pady=(8, 4))
        self._chart_canvas = tk.Canvas(self.tab_chart, bg="white", height=380, highlightthickness=1)
        self._chart_canvas.pack(fill=tk.BOTH, expand=True, pady=4)

    def _build_pnl(self) -> None:
        cols = ("asset", "qty", "entry", "days", "entry_tl", "cur_tl", "pnl", "pct")
        self.tree_pnl = ttk.Treeview(self.tab_pnl, columns=cols, show="headings", height=20)
        heads = [
            ("asset", "Varlık / lot"),
            ("qty", "Miktar"),
            ("entry", "Giriş tarihi"),
            ("days", "Gün"),
            ("entry_tl", "Giriş TL"),
            ("cur_tl", "Güncel TL"),
            ("pnl", "K/Z TL"),
            ("pct", "K/Z %"),
        ]
        for c, t in heads:
            self.tree_pnl.heading(c, text=t)
        self.tree_pnl.column("asset", width=200)
        self.tree_pnl.column("qty", width=70)
        self.tree_pnl.column("entry", width=90)
        self.tree_pnl.column("days", width=50)
        for c in ("entry_tl", "cur_tl", "pnl", "pct"):
            self.tree_pnl.column(c, width=100)
        sy = ttk.Scrollbar(self.tab_pnl, orient=tk.VERTICAL, command=self.tree_pnl.yview)
        self.tree_pnl.configure(yscrollcommand=sy.set)
        self.tree_pnl.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        sy.pack(side=tk.RIGHT, fill=tk.Y)

        self._lbl_pnl_total = ttk.Label(self.tab_pnl, text="", font=("", 12, "bold"))
        self._lbl_pnl_total.pack(anchor=tk.W, pady=6)
        ttk.Button(self.tab_pnl, text="Listeyi yenile", command=self._render_pnl).pack(anchor=tk.W)

    def _bootstrap(self) -> None:
        self._load_last_into_form()
        self._on_refresh_rates(silent_fail=True)
        self._render_pnl()

    def _snapshot_choices(self) -> list[tuple[int, str]]:
        rows = db.list_price_snapshot_times()
        out: list[tuple[int, str]] = []
        for sid, iso in rows:
            try:
                dt = datetime.fromisoformat(iso)
                label = dt.strftime("%Y-%m-%d %H:%M:%S")
            except ValueError:
                label = iso
            out.append((sid, f"{label}  (id={sid})"))
        return out

    def _sync_snapshot_combo(self) -> None:
        ch = self._snapshot_choices()
        labels = [x[1] for x in ch]
        self._cb_snapshot["values"] = labels
        self._sid_by_label = {lbl: sid for sid, lbl in ch}
        if labels:
            self._snapshot_var.set(labels[0])
        self._render_summary_table()

        assets = ordered_asset_codes()
        self._cb_chart["values"] = assets
        if assets and not self._chart_asset.get():
            self._chart_asset.set(assets[0])

    def _selected_snapshot_id(self) -> int | None:
        lbl = self._snapshot_var.get()
        return self._sid_by_label.get(lbl) if hasattr(self, "_sid_by_label") else None

    def _render_summary_table(self) -> None:
        for x in self.tree_sum.get_children():
            self.tree_sum.delete(x)
        sid = self._selected_snapshot_id()
        if sid is None:
            self._lbl_total.config(text="Henüz kayıtlı kur yok. «Kurları yenile» ile veri alın.")
            return
        pos, per, total = breakdown_for_snapshot_view(sid)
        self._lbl_total.config(text=f"Toplam varlığım: {_fmt_money(total)}")
        rates = db.get_rates_for_snapshot(sid)
        all_codes = sorted(rates.keys())
        if not all_codes:
            self._lbl_total.config(text="Seçili tarihte kur verisi bulunamadı.")
            return
        for code in all_codes:
            qty = pos.get(code, 0.0)
            rate = rates.get(code, {})
            sell = float(rate.get("selling", 0.0) or 0.0)
            buy = float(rate.get("buying", sell) or sell)
            tl = per.get(code, qty * sell)
            name = rates.get(code, {}).get("label") or _label_for_code(code)
            self.tree_sum.insert(
                "",
                tk.END,
                values=(
                    code,
                    name,
                    f"{buy:.4f}",
                    f"{sell:.4f}",
                    _fmt_qty(qty),
                    _fmt_money(tl),
                ),
            )

    def _on_refresh_rates(self, silent_fail: bool = False) -> None:
        try:
            res = fetch_rates()
            db.insert_price_snapshot(res.fetched_at, res.rates)
            self._status.set(f"Son çekim: {res.fetched_at.strftime('%Y-%m-%d %H:%M:%S')}")
        except Exception as e:
            self._status.set("Kur çekilemedi.")
            if not silent_fail:
                messagebox.showerror("CollectAPI", str(e))
        self._sync_snapshot_combo()
        self._render_pnl()

    def _load_last_into_form(self) -> None:
        cur = current_totals_latest_save()
        merged = merge_with_defaults(cur)
        for code, var in self._qty_vars.items():
            var.set(_fmt_qty(merged.get(code, 0.0)))

    def _on_save_portfolio(self) -> None:
        try:
            ed = date.fromisoformat(self._eff_date.get().strip())
        except ValueError:
            messagebox.showerror("Tarih", "Geçerlilik tarihini YYYY-MM-DD olarak girin.")
            return
        raw: dict[str, float] = {}
        for code, var in self._qty_vars.items():
            s = var.get().strip().replace(",", ".")
            if not s:
                s = "0"
            try:
                raw[code] = float(s)
            except ValueError:
                messagebox.showerror("Miktar", f"{code} için sayısal değer girin.")
                return
        lines = [(c, q) for c, q in merge_with_defaults(raw).items()]
        db.insert_portfolio_save(ed, lines)
        messagebox.showinfo("Kayıt", "Portföy satırı kaydedildi.")
        self._render_summary_table()
        self._render_pnl()

    def _on_draw_chart(self) -> None:
        code = self._chart_asset.get().strip()
        if not code:
            return
        days, vals = interpolated_daily_series(code)
        if not days:
            self._chart_canvas.delete("all")
            self._chart_info.config(text="Grafik için yeterli kur geçmişi yok.")
            return
        self._draw_series(days, vals, f"{_label_for_code(code)} ({code})")

    def _draw_series(self, days: list[date], vals: list[float], title: str) -> None:
        c = self._chart_canvas
        c.delete("all")
        c.update_idletasks()
        w = max(640, c.winfo_width())
        h = max(320, c.winfo_height())
        pad_l, pad_r, pad_t, pad_b = 60, 20, 24, 48
        plot_w = max(1, w - pad_l - pad_r)
        plot_h = max(1, h - pad_t - pad_b)

        vmin = min(vals)
        vmax = max(vals)
        if abs(vmax - vmin) < 1e-9:
            vmax = vmin + 1.0

        n = len(vals)
        def x_of(i: int) -> float:
            return pad_l + (i / max(1, n - 1)) * plot_w

        def y_of(v: float) -> float:
            return pad_t + (1 - (v - vmin) / (vmax - vmin)) * plot_h

        # axes
        c.create_line(pad_l, pad_t, pad_l, pad_t + plot_h, fill="#666")
        c.create_line(pad_l, pad_t + plot_h, pad_l + plot_w, pad_t + plot_h, fill="#666")

        # y labels (min/max)
        c.create_text(8, y_of(vmax), anchor="w", text=_fmt_money(vmax), fill="#333")
        c.create_text(8, y_of(vmin), anchor="w", text=_fmt_money(vmin), fill="#333")

        # x labels (start/end)
        c.create_text(x_of(0), h - 16, text=_fmt_short_date(days[0]), fill="#333")
        c.create_text(x_of(n - 1), h - 16, text=_fmt_short_date(days[-1]), fill="#333")

        pts: list[float] = []
        for i, v in enumerate(vals):
            pts.extend([x_of(i), y_of(v)])
        if len(pts) >= 4:
            c.create_line(*pts, fill="#1a5fb4", width=2, smooth=False)

        self._chart_info.config(
            text=f"{title} — günlük interpolasyonlu TL seri ({days[0].isoformat()} -> {days[-1].isoformat()})"
        )

    def _render_pnl(self) -> None:
        for x in self.tree_pnl.get_children():
            self.tree_pnl.delete(x)
        rows = compute_lot_pnl()
        total_pnl = 0.0
        total_entry = 0.0
        for r in rows:
            label = f"{_label_for_code(r.asset_code)} ({r.asset_code})"
            self.tree_pnl.insert(
                "",
                tk.END,
                values=(
                    label,
                    _fmt_qty(r.amount),
                    r.entry_date.isoformat(),
                    r.days_held,
                    _fmt_money(r.entry_tl),
                    _fmt_money(r.current_tl),
                    _fmt_money(r.pnl_tl),
                    f"{r.pnl_pct:.2f}%",
                ),
            )
            total_pnl += r.pnl_tl
            total_entry += r.entry_tl
        pct_all = (total_pnl / total_entry * 100.0) if total_entry > 1e-9 else 0.0
        self._lbl_pnl_total.config(
            text=f"Toplam K/Z: {_fmt_money(total_pnl)}  (girişe göre %{pct_all:.2f})"
        )


def main() -> None:
    app = VarlikApp()
    app.mainloop()


if __name__ == "__main__":
    main()
